"""Project-local KiCad library resources and table registration."""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterable
from pathlib import Path

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import atomic_write_bytes
from pcbdraft.core.project import sha256_file
from pcbdraft.domain.ir import Design
from pcbdraft.domain.parts import PartGraph
from pcbdraft.kicad.runtime import kicad_data_directory

SYMBOL_TABLE_NAME = "sym-lib-table"
FOOTPRINT_TABLE_NAME = "fp-lib-table"
PROJECT_SYMBOL_ROOT = Path("libraries") / "symbols"
PROJECT_FOOTPRINT_ROOT = Path("libraries") / "footprints"
_LIBRARY_NAME = re.compile(r"^[A-Za-z0-9_.+-]+$")
_TABLE_LIBRARY = re.compile(
    r'\(lib\s+\(name\s+"((?:[^"\\]|\\.)*)"\).*?'
    r'\(uri\s+"((?:[^"\\]|\\.)*)"\)'
)
_TABLE_NAME = re.compile(r'\(lib\s+\(name\s+"((?:[^"\\]|\\.)*)"\)')


def materialize_project_libraries(
    root: str | Path,
    design: Design,
    graph: PartGraph,
    *,
    symbol_root: str | Path | None = None,
    footprint_root: str | Path | None = None,
    stock_symbol_root: str | Path | None = None,
    stock_footprint_root: str | Path | None = None,
) -> dict[str, str]:
    """Copy only custom libraries referenced by ``design`` and register them.

    Stock libraries remain global dependencies.  A referenced library is copied
    when its source differs from the installed stock library (or stock does not
    provide it); this keeps normal projects small while making project-local
    custom parts such as ``LQEDA:*`` portable.  The returned mapping is suitable
    for a managed-project manifest's ``files`` map.  Native generation remains
    bound to the caller's KiCad library environment; this helper deliberately
    does not mutate process-wide environment variables.
    """

    project_root = Path(root).resolve(strict=True)
    if not project_root.is_dir() or project_root.is_symlink():
        raise ValidationError("project library root is unavailable")
    symbols = _library_root(symbol_root, "symbols")
    footprints = _library_root(footprint_root, "footprints")
    stock_symbols = (
        _library_root(stock_symbol_root, "symbols")
        if stock_symbol_root is not None
        else _stock_library_root("symbols")
    )
    stock_footprints = (
        _library_root(stock_footprint_root, "footprints")
        if stock_footprint_root is not None
        else _stock_library_root("footprints")
    )

    parts = {
        graph.get(component.part_id).id: graph.get(component.part_id)
        for component in design.components
    }
    symbol_names = sorted(
        {_split_library_id(part.symbol, "symbol")[0] for part in parts.values()}
    )
    footprints_by_library: dict[str, set[str]] = {}
    for part in parts.values():
        if part.footprint is None:
            continue
        library, name = _split_library_id(part.footprint, "footprint")
        footprints_by_library.setdefault(library, set()).add(name)

    files: dict[str, str] = {}
    symbol_entries: list[tuple[str, str]] = []
    for library in symbol_names:
        source = _library_file(symbols, library, suffix=".kicad_sym")
        stock = _library_file(
            stock_symbols, library, suffix=".kicad_sym", allow_missing=True
        )
        if _same_regular_file(source, stock):
            continue
        relative = PROJECT_SYMBOL_ROOT / f"{library}.kicad_sym"
        _copy_regular_file(source, project_root / relative, symbols)
        files[f"library:symbol:{library}"] = relative.as_posix()
        symbol_entries.append((library, _project_uri(relative)))

    footprint_entries: list[tuple[str, str]] = []
    for library, names in sorted(footprints_by_library.items()):
        source_dir = _library_directory(footprints, library, suffix=".pretty")
        stock_dir = _library_directory(
            stock_footprints, library, suffix=".pretty", allow_missing=True
        )
        source_files = {
            name: _library_file(source_dir, name, suffix=".kicad_mod")
            for name in sorted(names)
        }
        stock_files = {
            name: _library_file(
                stock_dir, name, suffix=".kicad_mod", allow_missing=True
            )
            for name in sorted(names)
        }
        if all(
            _same_regular_file(source_files[name], stock_files[name]) for name in names
        ):
            continue
        for name, source in source_files.items():
            relative = (
                PROJECT_FOOTPRINT_ROOT / f"{library}.pretty" / f"{name}.kicad_mod"
            )
            _copy_regular_file(source, project_root / relative, source_dir)
            files[f"library:footprint:{library}:{name}"] = relative.as_posix()
        footprint_entries.append(
            (library, _project_uri(PROJECT_FOOTPRINT_ROOT / f"{library}.pretty"))
        )

    table_files = merge_project_library_tables(
        project_root,
        symbol_entries=symbol_entries,
        footprint_entries=footprint_entries,
    )
    files.update(table_files)
    return files


def merge_project_library_tables(
    root: str | Path,
    *,
    symbol_entries: Iterable[tuple[str, str]] = (),
    footprint_entries: Iterable[tuple[str, str]] = (),
) -> dict[str, str]:
    """Append missing project entries without replacing existing table content."""

    project_root = Path(root).resolve(strict=True)
    if not project_root.is_dir() or project_root.is_symlink():
        raise ValidationError("project library root is unavailable")
    files: dict[str, str] = {}
    symbols = tuple(sorted(set(symbol_entries)))
    footprints = tuple(sorted(set(footprint_entries)))
    symbol_path = project_root / SYMBOL_TABLE_NAME
    footprint_path = project_root / FOOTPRINT_TABLE_NAME
    if symbols or symbol_path.exists():
        path = symbol_path
        _merge_table(path, "sym_lib_table", symbols)
        files["symbol_table"] = SYMBOL_TABLE_NAME
    if footprints or footprint_path.exists():
        path = footprint_path
        _merge_table(path, "fp_lib_table", footprints)
        files["footprint_table"] = FOOTPRINT_TABLE_NAME
    return files


def _library_root(value: str | Path | None, kind: str) -> Path:
    candidate = (
        Path(value).expanduser() if value is not None else kicad_data_directory(kind)
    )
    if candidate.is_symlink():
        raise ValidationError(f"KiCad {kind} library directory is unavailable")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"KiCad {kind} library directory is unavailable") from exc
    if resolved.is_symlink() or not resolved.is_dir():
        raise ValidationError(f"KiCad {kind} library directory is unavailable")
    return resolved


def _stock_library_root(kind: str) -> Path:
    # Explicitly omit environment overrides so comparison is against the real
    # installed stock tree when a private overlay is active.
    return _library_root(kicad_data_directory(kind, environment={}), kind)


def _split_library_id(value: str, kind: str) -> tuple[str, str]:
    if not isinstance(value, str) or value.count(":") != 1:
        raise ValidationError(f"{kind} must be a KiCad library id")
    library, name = value.split(":", 1)
    if not _LIBRARY_NAME.fullmatch(library) or not name:
        raise ValidationError(f"{kind} contains an invalid library name")
    name_path = Path(name)
    if name_path.is_absolute() or any(
        part in {"", ".", ".."} for part in name_path.parts
    ):
        raise ValidationError(f"{kind} contains an unsafe library item name")
    return library, name


def _library_file(
    root: Path, name: str, *, suffix: str, allow_missing: bool = False
) -> Path:
    path = root / f"{name}{suffix}"
    if path.is_symlink():
        raise ValidationError(f"KiCad library resource is unavailable: {path}")
    if not path.exists() and allow_missing:
        return path
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"KiCad library resource is unavailable: {path}") from exc
    if not resolved.is_file() or not resolved.is_relative_to(root):
        raise ValidationError(f"KiCad library resource is unavailable: {path}")
    return resolved


def _library_directory(
    root: Path, name: str, *, suffix: str, allow_missing: bool = False
) -> Path:
    path = root / f"{name}{suffix}"
    if path.is_symlink():
        raise ValidationError(f"KiCad library directory is unavailable: {path}")
    if not path.exists() and allow_missing:
        return path
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValidationError(
            f"KiCad library directory is unavailable: {path}"
        ) from exc
    if not resolved.is_dir() or not resolved.is_relative_to(root):
        raise ValidationError(f"KiCad library directory is unavailable: {path}")
    return resolved


def _same_regular_file(first: Path, second: Path) -> bool:
    if (
        not first.is_file()
        or first.is_symlink()
        or not second.is_file()
        or second.is_symlink()
    ):
        return False
    return sha256_file(first, max_bytes=128 * 1024 * 1024) == sha256_file(
        second, max_bytes=128 * 1024 * 1024
    )


def _copy_regular_file(source: Path, target: Path, source_root: Path) -> None:
    if (
        source.is_symlink()
        or not source.is_file()
        or not source.resolve(strict=True).is_relative_to(source_root)
    ):
        raise ValidationError(f"refusing unsafe KiCad library resource: {source}")
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValidationError(f"refusing to replace library symlink: {target}")
    shutil.copyfile(source, target)
    target.chmod(0o644)


def _project_uri(relative: Path) -> str:
    return "${KIPRJMOD}/" + relative.as_posix()


def _merge_table(
    path: Path, table_name: str, entries: tuple[tuple[str, str], ...]
) -> None:
    if path.is_symlink():
        raise ValidationError(f"refusing to replace library table symlink: {path}")
    if path.exists():
        if not path.is_file():
            raise ValidationError(f"existing library table is not a file: {path}")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValidationError(
                f"cannot read existing library table: {path}"
            ) from exc
        if not text.lstrip().startswith(f"({table_name}") or not text.rstrip().endswith(
            ")"
        ):
            raise ValidationError(f"existing library table is malformed: {path}")
    else:
        text = f"({table_name}\n\t(version 7)\n)\n"
    existing: dict[str, str] = {}
    for match in _TABLE_LIBRARY.finditer(text):
        name = _unescape_table_string(match.group(1))
        uri = _unescape_table_string(match.group(2))
        prior = existing.get(name)
        if prior is not None and prior != uri:
            raise ValidationError(f"existing library table has duplicate name: {name}")
        existing[name] = uri
    declared_names = {
        _unescape_table_string(match.group(1)) for match in _TABLE_NAME.finditer(text)
    }
    requested: dict[str, str] = {}
    for name, uri in entries:
        prior = requested.get(name)
        if prior is not None and prior != uri:
            raise ValidationError(f"duplicate library table entry: {name}")
        requested[name] = uri
    missing = []
    for name, uri in requested.items():
        prior = existing.get(name)
        if name in declared_names and prior is None:
            raise ValidationError(f"existing library table entry is malformed: {name}")
        if prior is not None and prior != uri:
            raise ValidationError(
                f"library table entry conflicts with existing URI: {name}"
            )
        if name not in existing:
            missing.append((name, uri))
    if missing:
        close = text.rstrip().rfind(")")
        if close < 0:
            raise ValidationError(f"existing library table is malformed: {path}")
        rendered = "".join(
            f'\t(lib (name {_quote(name)}) (type "KiCad") (uri {_quote(uri)}) '
            '(options "") (descr "PCBDraft project-local library"))\n'
            for name, uri in missing
        )
        text = text.rstrip()[:close] + rendered + text.rstrip()[close:] + "\n"
    if not path.exists() or missing:
        atomic_write_bytes(path, text.encode("utf-8"), mode=0o644)


def _quote(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError("KiCad library table values must be non-empty strings")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unescape_table_string(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")
