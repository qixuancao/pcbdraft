"""Cross-platform discovery and first-run preparation for KiCad 10."""

from __future__ import annotations

import os
import platform
import shutil
import sys
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import atomic_write_bytes, make_directory, read_bytes_limited
from pcbdraft.core.process import run_command

KICAD_SERIES = "10.0"
LIBRARY_TABLE_LIMIT = 16 * 1024 * 1024
_DATA_ENVIRONMENT = {
    "symbols": ("KICAD_SYMBOL_DIR", "KICAD10_SYMBOL_DIR"),
    "footprints": ("KICAD_FOOTPRINT_DIR", "KICAD10_FOOTPRINT_DIR"),
    "template": ("KICAD_TEMPLATE_DIR", "KICAD10_TEMPLATE_DIR"),
}


def _system_name(value: str | None = None) -> str:
    return (value or platform.system()).casefold()


def _existing_executable(candidates: list[Path]) -> str | None:
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return str(resolved)
    return None


def _windows_install_roots(environment: Mapping[str, str]) -> list[Path]:
    roots: list[Path] = []
    for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        value = environment.get(variable, "").strip()
        if value:
            roots.append(Path(value) / "KiCad" / KICAD_SERIES)
    if not roots:
        drive = environment.get("SystemDrive", "C:").rstrip("\\/")
        roots.append(Path(f"{drive}/Program Files") / "KiCad" / KICAD_SERIES)
    return list(dict.fromkeys(roots))


def find_kicad_cli(
    *,
    system: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> str | None:
    """Find ``kicad-cli`` even when a desktop installer did not edit PATH."""

    environment = os.environ if environment is None else environment
    explicit = environment.get("KICAD_CLI", "").strip()
    if explicit:
        found = _existing_executable([Path(explicit)])
        if found:
            return found
    on_path = shutil.which("kicad-cli", path=environment.get("PATH", os.defpath))
    if on_path:
        return on_path
    current = _system_name(system)
    candidates: list[Path] = []
    if current == "darwin":
        candidates.extend(
            (
                Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
                Path.home() / "Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
            )
        )
    elif current == "windows":
        candidates.extend(
            root / "bin" / "kicad-cli.exe"
            for root in _windows_install_roots(environment)
        )
    else:
        candidates.extend(
            (Path("/usr/bin/kicad-cli"), Path("/usr/local/bin/kicad-cli"))
        )
    return _existing_executable(candidates)


def find_kicad_app(
    *,
    system: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> str | None:
    """Find the KiCad desktop launcher on supported operating systems."""

    environment = os.environ if environment is None else environment
    explicit = environment.get("KICAD_APP", "").strip()
    if explicit:
        found = _existing_executable([Path(explicit)])
        if found:
            return found
    search_path = environment.get("PATH", os.defpath)
    on_path = shutil.which("kicad", path=search_path) or shutil.which(
        "kicad.exe", path=search_path
    )
    if on_path:
        return on_path
    current = _system_name(system)
    candidates: list[Path] = []
    if current == "darwin":
        candidates.extend(
            (
                Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad"),
                Path.home() / "Applications/KiCad/KiCad.app/Contents/MacOS/kicad",
            )
        )
    elif current == "windows":
        candidates.extend(
            root / "bin" / "kicad.exe" for root in _windows_install_roots(environment)
        )
    return _existing_executable(candidates)


def _data_candidates(
    kind: str,
    *,
    system: str,
    environment: Mapping[str, str],
    executable: str | None,
) -> list[Path]:
    candidates: list[Path] = []
    for variable in _DATA_ENVIRONMENT[kind]:
        value = environment.get(variable, "").strip()
        if value:
            candidates.append(Path(value).expanduser())
    if executable:
        executable_path = Path(executable).resolve(strict=False)
        if system == "darwin":
            for parent in executable_path.parents:
                if parent.name == "Contents":
                    candidates.append(parent / "SharedSupport" / kind)
                    break
        elif executable_path.parent.name.casefold() == "bin":
            candidates.append(executable_path.parent.parent / "share" / "kicad" / kind)
    if system == "darwin":
        candidates.extend(
            (
                Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport") / kind,
                Path.home()
                / "Applications/KiCad/KiCad.app/Contents/SharedSupport"
                / kind,
            )
        )
    elif system == "windows":
        candidates.extend(
            root / "share" / "kicad" / kind
            for root in _windows_install_roots(environment)
        )
    else:
        candidates.extend(
            (Path("/usr/share/kicad") / kind, Path("/usr/local/share/kicad") / kind)
        )
    return list(dict.fromkeys(candidates))


def kicad_data_directory(
    kind: str,
    *,
    system: str | None = None,
    environment: Mapping[str, str] | None = None,
    executable: str | None = None,
) -> Path:
    """Return the installed KiCad data directory for one known library kind."""

    if kind not in _DATA_ENVIRONMENT:
        raise ValidationError(f"unknown KiCad data directory kind: {kind}")
    environment = os.environ if environment is None else environment
    current = _system_name(system)
    candidates = _data_candidates(
        kind,
        system=current,
        environment=environment,
        executable=executable
        or find_kicad_cli(system=current, environment=environment),
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def kicad_user_config_directory(
    *,
    system: str | None = None,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return KiCad's documented versioned user-configuration directory."""

    environment = os.environ if environment is None else environment
    current = _system_name(system)
    user_home = home or Path.home()
    if current == "windows":
        appdata = environment.get("APPDATA", "").strip()
        root = Path(appdata) if appdata else user_home / "AppData" / "Roaming"
        return root / "kicad" / KICAD_SERIES
    if current == "darwin":
        return user_home / "Library" / "Preferences" / "kicad" / KICAD_SERIES
    xdg = environment.get("XDG_CONFIG_HOME", "").strip()
    root = Path(xdg).expanduser() if xdg else user_home / ".config"
    return root / "kicad" / KICAD_SERIES


def library_table_status(
    *,
    system: str | None = None,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> dict[str, dict[str, object]]:
    """Describe global symbol/footprint table readiness without changing it."""

    environment = os.environ if environment is None else environment
    config = kicad_user_config_directory(
        system=system, environment=environment, home=home
    )
    templates = kicad_data_directory("template", system=system, environment=environment)
    result: dict[str, dict[str, object]] = {}
    for name, header in (
        ("sym-lib-table", b"(sym_lib_table"),
        ("fp-lib-table", b"(fp_lib_table"),
    ):
        target = config / name
        source = templates / name
        configured = False
        template_available = False
        try:
            configured = (
                target.is_file()
                and not target.is_symlink()
                and header in read_bytes_limited(target, LIBRARY_TABLE_LIMIT)
            )
        except PCBDraftError:
            configured = False
        try:
            template_available = source.is_file() and header in read_bytes_limited(
                source, LIBRARY_TABLE_LIMIT
            )
        except PCBDraftError:
            template_available = False
        result[name] = {
            "configured": configured,
            "path": str(target),
            "template_available": template_available,
            "template": str(source),
        }
    return result


def ensure_kicad_library_tables(
    *,
    system: str | None = None,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> dict[str, dict[str, object]]:
    """Initialize missing stock-library tables without replacing user files."""

    environment = os.environ if environment is None else environment
    status = library_table_status(system=system, environment=environment, home=home)
    config = kicad_user_config_directory(
        system=system, environment=environment, home=home
    )
    if config.exists() and (config.is_symlink() or not config.is_dir()):
        raise ValidationError(f"KiCad configuration path is unsafe: {config}")
    make_directory(config)
    for item in status.values():
        if item["configured"]:
            continue
        target = Path(str(item["path"]))
        if target.exists() or target.is_symlink():
            raise ValidationError(
                f"existing KiCad library table is invalid and was not replaced: {target}"
            )
        if not item["template_available"]:
            raise PCBDraftError(
                f"KiCad library-table template is unavailable: {item['template']}"
            )
        source = Path(str(item["template"]))
        atomic_write_bytes(
            target,
            read_bytes_limited(source, LIBRARY_TABLE_LIMIT),
            mode=0o644,
        )
    return library_table_status(system=system, environment=environment, home=home)


def _pcbnew_python_candidates(
    *, system: str, environment: Mapping[str, str]
) -> list[Path]:
    candidates: list[Path] = []
    explicit = environment.get("KICAD_PYTHON", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    if system == "windows":
        for root in _windows_install_roots(environment):
            candidates.extend(
                (root / "bin" / "python.exe", root / "bin" / "python3.exe")
            )
    elif system == "darwin":
        contents_roots = (
            Path("/Applications/KiCad/KiCad.app/Contents"),
            Path.home() / "Applications/KiCad/KiCad.app/Contents",
        )
        for contents in contents_roots:
            candidates.append(
                contents / "Frameworks/Python.framework/Versions/Current/bin/python3"
            )
            candidates.extend(
                sorted(
                    contents.glob("Frameworks/Python.framework/Versions/*/bin/python3")
                )
            )
    else:
        candidates.extend((Path("/usr/bin/python3"), Path("/usr/local/bin/python3")))
    candidates.append(Path(sys.executable))
    return list(dict.fromkeys(candidates))


@lru_cache(maxsize=8)
def _python_imports_pcbnew(executable: str) -> bool:
    try:
        result = run_command(
            [
                executable,
                "-I",
                "-c",
                "import pcbnew; print(pcbnew.GetBuildVersion())",
            ],
            cwd=None,
            timeout=10.0,
            max_output_bytes=128 * 1024,
        )
    except PCBDraftError:
        return False
    return result.returncode == 0 and not result.timed_out and not result.output_limited


def find_pcbnew_python(
    *,
    system: str | None = None,
    environment: Mapping[str, str] | None = None,
    probe: bool = True,
) -> str | None:
    """Find a Python supplied with KiCad that can import the ``pcbnew`` bindings."""

    environment = os.environ if environment is None else environment
    for candidate in _pcbnew_python_candidates(
        system=_system_name(system), environment=environment
    ):
        found = _existing_executable([candidate])
        if found and (not probe or _python_imports_pcbnew(found)):
            return found
    return None
