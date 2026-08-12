"""Deterministic replace-only patching, diffs, and gate regression policy."""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .io import atomic_write_text, read_bytes_limited
from .project import resolve_member

ALLOWED_SUFFIXES = {
    ".kicad_sch",
    ".kicad_pcb",
    ".kicad_pro",
    ".kicad_prl",
    ".kicad_sym",
    ".kicad_mod",
    ".kicad_wks",
    ".kicad_dru",
    ".kicad_jobset",
    ".kicad_worksheet",
    ".lib",
    ".dcm",
    ".pro",
    ".sch",
    ".brd",
    ".net",
    ".cmp",
    ".dsn",
    ".ses",
    ".pos",
    ".csv",
    ".tsv",
    ".txt",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".conf",
    ".md",
}
MAX_OPERATIONS = 20
MAX_TEXT_BYTES = 128 * 1024
MAX_TARGET_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class AppliedOperation:
    index: int
    relative_path: str
    old_bytes: int
    new_bytes: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "op": "replace_text",
            "relative_path": self.relative_path,
            "old_bytes": self.old_bytes,
            "new_bytes": self.new_bytes,
            "reason": self.reason,
        }


def _allowed_path(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in ALLOWED_SUFFIXES)


def apply_operations(
    staging: Path, operations: Sequence[Mapping[str, Any]]
) -> list[AppliedOperation]:
    """Validate the complete change set, then atomically replace unique text in staging."""
    if len(operations) > MAX_OPERATIONS:
        raise ValidationError(f"change set exceeds {MAX_OPERATIONS} operations")

    total_bytes = 0
    prepared: list[tuple[int, Path, str, str, str, str]] = []
    virtual_contents: dict[Path, str] = {}
    for index, operation in enumerate(operations):
        expected = {"op", "relative_path", "old_text", "new_text", "reason"}
        if not isinstance(operation, Mapping) or set(operation) != expected:
            raise ValidationError(f"operation {index} has unexpected fields")
        if operation["op"] != "replace_text":
            raise ValidationError(f"operation {index} is not replace_text")
        relative_path = operation["relative_path"]
        old_text = operation["old_text"]
        new_text = operation["new_text"]
        reason = operation["reason"]
        if not all(
            isinstance(value, str)
            for value in (relative_path, old_text, new_text, reason)
        ):
            raise ValidationError(f"operation {index} contains a non-string field")
        if not old_text:
            raise ValidationError(f"operation {index} old_text must be non-empty")
        if not reason:
            raise ValidationError(f"operation {index} reason must be non-empty")

        total_bytes += len(old_text.encode("utf-8")) + len(new_text.encode("utf-8"))
        if total_bytes > MAX_TEXT_BYTES:
            raise ValidationError(
                f"change set exceeds {MAX_TEXT_BYTES} replacement bytes"
            )

        target = resolve_member(staging, relative_path, must_exist=True)
        if not _allowed_path(target):
            raise ValidationError(f"file extension is not patchable: {relative_path}")
        if target not in virtual_contents:
            raw = read_bytes_limited(target, MAX_TARGET_BYTES)
            if b"\x00" in raw:
                raise ValidationError(f"binary file is not patchable: {relative_path}")
            try:
                virtual_contents[target] = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValidationError(
                    f"file is not UTF-8 text: {relative_path}"
                ) from exc
        content = virtual_contents[target]
        occurrences = content.count(old_text)
        if occurrences != 1:
            raise ValidationError(
                f"operation {index} old_text must match exactly once in {relative_path}; found {occurrences}"
            )
        virtual_contents[target] = content.replace(old_text, new_text, 1)
        prepared.append((index, target, relative_path, old_text, new_text, reason))

    # Validation is complete; write at most once per target so invalid later operations
    # cannot leave a partially applied staging transaction.
    for target, content in virtual_contents.items():
        atomic_write_text(target, content, mode=0o600)

    return [
        AppliedOperation(
            index=index,
            relative_path=relative_path,
            old_bytes=len(old_text.encode("utf-8")),
            new_bytes=len(new_text.encode("utf-8")),
            reason=reason,
        )
        for index, _target, relative_path, old_text, new_text, reason in prepared
    ]


def build_unified_patch(
    *,
    original: Path,
    staging: Path,
    relative_paths: Sequence[str],
) -> str:
    chunks: list[str] = []
    for relative_path in sorted(set(relative_paths)):
        original_path = resolve_member(original, relative_path, must_exist=True)
        staging_path = resolve_member(staging, relative_path, must_exist=True)
        old = (
            read_bytes_limited(original_path, MAX_TARGET_BYTES)
            .decode("utf-8")
            .splitlines(keepends=True)
        )
        new = (
            read_bytes_limited(staging_path, MAX_TARGET_BYTES)
            .decode("utf-8")
            .splitlines(keepends=True)
        )
        chunks.extend(
            difflib.unified_diff(
                old,
                new,
                fromfile=f"a/{relative_path}",
                tofile=f"b/{relative_path}",
                lineterm="\n",
            )
        )
    rendered = "".join(chunks)
    if rendered and not rendered.endswith("\n"):
        rendered += "\n"
    return rendered


def regression_reasons(
    baseline: Mapping[str, Any],
    after: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []
    for name in ("erc", "drc"):
        before = baseline.get(name)
        current = after.get(name)
        if before is None or current is None:
            reasons.append(f"{name}: missing gate result")
            continue
        before_status = getattr(
            before,
            "tool_status",
            before.get("tool_status") if isinstance(before, Mapping) else None,
        )
        current_status = getattr(
            current,
            "tool_status",
            current.get("tool_status") if isinstance(current, Mapping) else None,
        )
        if before_status == "ok" and current_status != "ok":
            reasons.append(f"{name}: gate changed from runnable to tool failure")
            continue
        if before_status != "ok":
            reasons.append(f"{name}: baseline gate was not runnable")
            continue
        if current_status != "ok":
            reasons.append(f"{name}: after gate is not runnable")
            continue

        before_errors = getattr(before, "error_count", None)
        current_errors = getattr(current, "error_count", None)
        if isinstance(before, Mapping):
            counts = before.get("counts")
            before_errors = counts.get("error") if isinstance(counts, Mapping) else None
        if isinstance(current, Mapping):
            counts = current.get("counts")
            current_errors = (
                counts.get("error") if isinstance(counts, Mapping) else None
            )
        if not isinstance(before_errors, int) or not isinstance(current_errors, int):
            reasons.append(f"{name}: error count unavailable")
        elif current_errors > before_errors:
            reasons.append(
                f"{name}: error count increased from {before_errors} to {current_errors}"
            )
    return reasons
