"""KiCad project discovery, safe paths, and bounded hash manifests."""

from __future__ import annotations

from collections.abc import Iterator
import hashlib
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import PcbAgentError, ValidationError
from .io import privatize_tree

REVIEW_MAX_FILES = 500
REVIEW_MAX_FILE_BYTES = 64 * 1024 * 1024
REVIEW_MAX_TOTAL_BYTES = 256 * 1024 * 1024
BASELINE_MAX_FILES = 2_000
BASELINE_MAX_FILE_BYTES = 128 * 1024 * 1024
BASELINE_MAX_TOTAL_BYTES = 512 * 1024 * 1024
TREE_MEMBER_LIMIT = 10_000


@dataclass(frozen=True)
class ProjectFiles:
    schematic: Path
    board: Path

    def relative(self, root: Path) -> dict[str, str]:
        return {
            "schematic": self.schematic.relative_to(root).as_posix(),
            "board": self.board.relative_to(root).as_posix(),
        }


def canonical_project(value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"project does not exist: {candidate}") from exc
    if not resolved.is_dir():
        raise ValidationError(f"project must be a directory: {resolved}")
    return resolved


def validate_agent_tree(root: Path) -> None:
    """Reject links/special files so an agent cannot leave the declared tree through them."""
    root = canonical_project(root)
    for member in _iter_members(root, max_members=TREE_MEMBER_LIMIT):
        relative = member.relative_to(root).as_posix()
        try:
            mode = member.lstat().st_mode
        except OSError as exc:
            raise PcbAgentError(f"cannot inspect project member: {relative}") from exc
        if stat.S_ISLNK(mode):
            raise ValidationError(f"project symlinks are not accepted for agent workflows: {relative}")
        if not stat.S_ISREG(mode):
            raise ValidationError(f"project special files are not accepted for agent workflows: {relative}")


def _safe_candidate(root: Path, path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink() and path.resolve(strict=True).is_relative_to(root)
    except OSError:
        return False


def _format_candidates(root: Path, candidates: list[Path]) -> str:
    names = [path.relative_to(root).as_posix() for path in sorted(candidates)]
    return ", ".join(names) if names else "none"


def discover_project(root: Path) -> ProjectFiles:
    """Select exactly one matching schematic/board pair; never guess."""
    root = canonical_project(root)
    root_schematics = sorted(path for path in root.glob("*.kicad_sch") if _safe_candidate(root, path))
    root_boards = sorted(path for path in root.glob("*.kicad_pcb") if _safe_candidate(root, path))

    if root_schematics or root_boards:
        if len(root_schematics) != 1 or len(root_boards) != 1:
            raise ValidationError(
                "ambiguous root-level KiCad project; "
                f"schematics=[{_format_candidates(root, root_schematics)}], "
                f"boards=[{_format_candidates(root, root_boards)}]"
            )
        schematic, board = root_schematics[0], root_boards[0]
    else:
        schematics = sorted(path for path in root.rglob("*.kicad_sch") if _safe_candidate(root, path))
        boards = sorted(path for path in root.rglob("*.kicad_pcb") if _safe_candidate(root, path))
        if len(schematics) != 1 or len(boards) != 1:
            raise ValidationError(
                "ambiguous or incomplete nested KiCad project; "
                f"schematics=[{_format_candidates(root, schematics)}], "
                f"boards=[{_format_candidates(root, boards)}]"
            )
        schematic, board = schematics[0], boards[0]

    if schematic.parent != board.parent or schematic.stem != board.stem:
        raise ValidationError(
            "schematic and board do not form one matching KiCad project: "
            f"{schematic.relative_to(root).as_posix()} vs {board.relative_to(root).as_posix()}"
        )
    return ProjectFiles(schematic=schematic, board=board)


def sha256_file(path: Path, *, max_bytes: int | None = None) -> str:
    digest = hashlib.sha256()
    consumed = 0
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                consumed += len(chunk)
                if max_bytes is not None and consumed > max_bytes:
                    raise ValidationError(f"file exceeds hashing limit: {path.name}")
                digest.update(chunk)
    except OSError as exc:
        raise PcbAgentError(f"cannot hash project file: {path}") from exc
    return digest.hexdigest()


def _iter_members(root: Path, *, max_members: int) -> Iterator[Path]:
    scanned = 0
    for current, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        files.sort()
        current_path = Path(current)
        scanned += len(directories) + len(files)
        if scanned > max_members:
            raise ValidationError(f"project tree exceeds the {max_members} member traversal limit")
        for directory in list(directories):
            member = current_path / directory
            if member.is_symlink():
                directories.remove(directory)
                yield member
        for filename in files:
            yield current_path / filename


def build_manifest(
    root: Path,
    *,
    strict: bool,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> dict[str, Any]:
    """Hash a bounded tree without following symlinks or accepting special files."""
    root = canonical_project(root)
    entries: list[dict[str, Any]] = []
    omitted = {"file_limit": 0, "file_too_large": 0, "total_byte_limit": 0, "special_file": 0}
    total_bytes = 0

    for member in _iter_members(root, max_members=TREE_MEMBER_LIMIT):
        relative = member.relative_to(root).as_posix()
        try:
            metadata = member.lstat()
        except OSError as exc:
            raise PcbAgentError(f"cannot inspect project member: {relative}") from exc

        if len(entries) >= max_files:
            omitted["file_limit"] += 1
            continue
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(member)
            encoded = target.encode("utf-8", errors="surrogateescape")
            if total_bytes + len(encoded) > max_total_bytes:
                omitted["total_byte_limit"] += 1
                continue
            entries.append(
                {
                    "relative_path": relative,
                    "kind": "symlink",
                    "size": len(encoded),
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "link_target": target,
                }
            )
            total_bytes += len(encoded)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            omitted["special_file"] += 1
            continue
        if metadata.st_size > max_file_bytes:
            omitted["file_too_large"] += 1
            continue
        if total_bytes + metadata.st_size > max_total_bytes:
            omitted["total_byte_limit"] += 1
            continue
        entries.append(
            {
                "relative_path": relative,
                "kind": "file",
                "size": metadata.st_size,
                "mode": stat.S_IMODE(metadata.st_mode),
                "sha256": sha256_file(member, max_bytes=max_file_bytes),
            }
        )
        total_bytes += metadata.st_size

    omitted_total = sum(omitted.values())
    if strict and omitted_total:
        raise ValidationError(f"project exceeds safe baseline manifest bounds ({omitted_total} omitted members)")
    return {
        "root": str(root),
        "entries": entries,
        "entry_count": len(entries),
        "total_hashed_bytes": total_bytes,
        "complete": omitted_total == 0,
        "omitted": omitted,
        "limits": {
            "max_files": max_files,
            "max_file_bytes": max_file_bytes,
            "max_total_bytes": max_total_bytes,
            "max_tree_members": TREE_MEMBER_LIMIT,
        },
    }


def review_inventory(root: Path) -> dict[str, Any]:
    return build_manifest(
        root,
        strict=False,
        max_files=REVIEW_MAX_FILES,
        max_file_bytes=REVIEW_MAX_FILE_BYTES,
        max_total_bytes=REVIEW_MAX_TOTAL_BYTES,
    )


def baseline_manifest(root: Path) -> dict[str, Any]:
    return build_manifest(
        root,
        strict=True,
        max_files=BASELINE_MAX_FILES,
        max_file_bytes=BASELINE_MAX_FILE_BYTES,
        max_total_bytes=BASELINE_MAX_TOTAL_BYTES,
    )


def manifest_hashes(manifest: dict[str, Any]) -> dict[str, str]:
    return {entry["relative_path"]: entry["sha256"] for entry in manifest["entries"]}


def validate_baseline_document(manifest: Any, *, expected_root: Path) -> dict[str, str]:
    """Validate a persisted strict baseline before it can authorize apply."""
    if not isinstance(manifest, dict) or manifest.get("complete") is not True:
        raise ValidationError("transaction baseline manifest is invalid or incomplete")
    if manifest.get("root") != str(expected_root):
        raise ValidationError("transaction baseline project does not match the receipt")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or manifest.get("entry_count") != len(entries):
        raise ValidationError("transaction baseline entries are malformed")

    hashes: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValidationError("transaction baseline contains a malformed entry")
        relative_path = entry.get("relative_path")
        digest = entry.get("sha256")
        size = entry.get("size")
        if (
            entry.get("kind") != "file"
            or not isinstance(relative_path, str)
            or not relative_path
            or "\\" in relative_path
            or PurePosixPath(relative_path).is_absolute()
            or ".." in PurePosixPath(relative_path).parts
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or relative_path in hashes
        ):
            raise ValidationError("transaction baseline contains an unsafe or malformed entry")
        hashes[relative_path] = digest
    return hashes


def manifests_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    def comparable(manifest: dict[str, Any]) -> dict[str, tuple[Any, ...]]:
        return {
            entry["relative_path"]: (
                entry.get("kind"),
                entry.get("size"),
                entry.get("sha256"),
                entry.get("link_target"),
            )
            for entry in manifest.get("entries", [])
        }

    return comparable(left) == comparable(right)


def resolve_member(root: Path, relative_path: str, *, must_exist: bool = True) -> Path:
    if not isinstance(relative_path, str) or not relative_path or "\x00" in relative_path:
        raise ValidationError("relative_path must be a non-empty string")
    if "\\" in relative_path:
        raise ValidationError(f"backslashes are not allowed in relative_path: {relative_path}")
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise ValidationError(f"unsafe relative_path: {relative_path}")
    root_resolved = root.resolve(strict=True)
    lexical = root_resolved.joinpath(*pure.parts)
    cursor = root_resolved
    for part in pure.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValidationError(f"symlink path is not patchable: {relative_path}")
    try:
        resolved = lexical.resolve(strict=must_exist)
    except OSError as exc:
        raise ValidationError(f"path does not exist: {relative_path}") from exc
    if not resolved.is_relative_to(root_resolved):
        raise ValidationError(f"path escapes project root: {relative_path}")
    if must_exist and not resolved.is_file():
        raise ValidationError(f"path is not an existing file: {relative_path}")
    return resolved


def copy_project(source: Path, destination: Path) -> None:
    if destination.exists():
        raise PcbAgentError(f"staging destination already exists: {destination}")
    try:
        shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)
        privatize_tree(destination)
    except OSError as exc:
        raise PcbAgentError("failed to create private staging copy") from exc
