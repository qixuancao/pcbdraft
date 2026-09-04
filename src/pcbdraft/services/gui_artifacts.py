"""Read-only, receipt-bound artifact projections for the local GUI.

The GUI never receives a project path, a release path, or an arbitrary file
name.  This module resolves a small fixed inventory from retained receipts,
checks every exposed file against its recorded size and digest, and creates
deterministic archive views only in the GUI cache.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import load_json_limited, make_directory, read_bytes_limited
from pcbdraft.core.locking import ResourceLock
from pcbdraft.core.project import sha256_file

_PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{2,79}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SAFE_ARCHIVE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_TIMESTAMP = re.compile(r"[0-9T:.+\-Z]{1,64}")

MAX_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_RELEASE_FILES = 256
MAX_ZIP_SOURCE_BYTES = 256 * 1024 * 1024
MAX_RETAINED_RECEIPTS = 100
MAX_CHECK_DETAILS = 512
_GUI_READ_LOCK_TIMEOUT_SECONDS = 0.5

_INDIVIDUAL_CHECK_IDS = {
    "check_semantics": "semantics",
    "check_connectivity": "connectivity",
    "run_erc": "erc",
    "run_drc": "drc",
}
_INDIVIDUAL_EXPORT_KEYS = {
    "export_gerbers": "gerbers.zip",
    "export_drill": "drill.zip",
    "export_bom": "bom.csv",
    "export_pick_place": "positions.csv",
    "export_step": "board.step",
}


@dataclass(frozen=True)
class ArtifactDownload:
    """One already-verified fixed artifact for a ``FileResponse`` route."""

    path: Path
    media_type: str
    filename: str


@dataclass(frozen=True)
class _SourceFile:
    relative: str
    path: Path
    size: int
    digest: str


@dataclass(frozen=True)
class _ArtifactRecord:
    key: str
    label: str
    sources: tuple[_SourceFile, ...]
    created_at: str
    stale: bool

    def public(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "state": "stale" if self.stale else "ready",
            "file_count": len(self.sources),
            "bytes": sum(item.size for item in self.sources),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class _ArtifactSpec:
    key: str
    label: str
    media_type: str
    filename: str
    mode: str


_ARTIFACT_SPECS = (
    _ArtifactSpec("bom.csv", "BOM", "text/csv; charset=utf-8", "bom.csv", "one"),
    _ArtifactSpec("gerbers.zip", "Gerber", "application/zip", "gerbers.zip", "zip"),
    _ArtifactSpec("drill.zip", "Drill", "application/zip", "drill.zip", "zip"),
    _ArtifactSpec(
        "positions.csv", "PnP", "text/csv; charset=utf-8", "positions.csv", "one"
    ),
    _ArtifactSpec("board.step", "STEP", "model/step", "board.step", "one"),
    _ArtifactSpec(
        "schematic.pdf", "Schematic PDF", "application/pdf", "schematic.pdf", "one"
    ),
    _ArtifactSpec(
        "schematic.svg", "Schematic SVG", "image/svg+xml", "schematic.svg", "one"
    ),
)
_SPECS_BY_KEY = {item.key: item for item in _ARTIFACT_SPECS}


class GUIArtifactService:
    """Expose a fixed, integrity-checked subset of retained GUI evidence."""

    def __init__(self, service: Any, *, cache_root: str | Path) -> None:
        self.service = service
        requested = Path(cache_root).expanduser()
        if requested.exists() and requested.is_symlink():
            raise ValidationError("GUI artifact cache path is unsafe")
        self.cache_root = make_directory(requested.resolve(strict=False))

    def manifest(self, project_id: str) -> dict[str, Any]:
        """Return bounded public inventory fields without source paths."""

        root = self._project_root(project_id)
        with self._project_lock(root):
            state = self._state(root)
            catalog = self._catalog(root, state)
        public: list[dict[str, Any]] = []
        for spec in _ARTIFACT_SPECS:
            record = catalog.get(spec.key)
            if record is None:
                public.append(
                    {
                        "key": spec.key,
                        "label": spec.label,
                        "state": "missing",
                        "file_count": 0,
                        "bytes": 0,
                        "created_at": "",
                    }
                )
            else:
                public.append(record.public())
        return {
            "schema": "pcbdraft-gui-artifacts",
            "version": 1,
            "project_id": project_id,
            "artifacts": public,
        }

    def download(self, project_id: str, key: str) -> ArtifactDownload:
        """Resolve one fixed server-owned artifact key, never a client path."""

        spec = _SPECS_BY_KEY.get(key)
        if spec is None:
            raise ValidationError("unsupported GUI artifact")
        root = self._project_root(project_id)
        with self._project_lock(root):
            catalog = self._catalog(root, self._state(root))
            record = catalog.get(key)
            if record is None:
                raise ValidationError("GUI artifact is unavailable")
            if spec.mode == "zip":
                path = self._zip_cache(project_id, record)
            else:
                if len(record.sources) != 1:
                    raise ValidationError("artifact receipt is invalid")
                path = record.sources[0].path
            return ArtifactDownload(
                path=path,
                media_type=spec.media_type,
                filename=spec.filename,
            )

    def validation(self, project_id: str) -> dict[str, Any]:
        """Project only trusted validation state and short check summaries."""

        root = self._project_root(project_id)
        with self._project_lock(root):
            state = self._state(root)
            return self._validation(root, state)

    def _project_root(self, project_id: str) -> Path:
        if not isinstance(project_id, str) or _PROJECT_ID.fullmatch(project_id) is None:
            raise ValidationError("project id is invalid")
        try:
            raw = Path(self.service.project_root(project_id)).expanduser()
            if not raw.is_absolute() or raw.is_symlink():
                raise OSError("unsafe root")
            root = raw.resolve(strict=True)
            if not root.is_dir():
                raise OSError("not a directory")
        except (OSError, TypeError, ValueError, PCBDraftError) as exc:
            raise ValidationError("artifact receipt is invalid") from exc
        return root

    @contextmanager
    def _project_lock(self, root: Path) -> Iterator[None]:
        locks_root = getattr(self.service, "locks_root", None)
        if locks_root is None:
            with nullcontext():
                yield
            return
        try:
            lock_root = Path(locks_root)
            if lock_root.is_symlink() or not lock_root.is_dir():
                raise OSError("unsafe lock root")
            lock = ResourceLock(
                root, lock_root, timeout=_GUI_READ_LOCK_TIMEOUT_SECONDS
            ).acquire()
        except (OSError, TypeError, ValueError, PCBDraftError) as exc:
            raise ValidationError("artifact receipt is invalid") from exc
        try:
            yield
        finally:
            lock.release()

    def _state(self, root: Path) -> Mapping[str, Any]:
        value = self._json_file(root, "project.json", limit=MAX_RECEIPT_BYTES)
        if not isinstance(value, Mapping):
            raise ValidationError("artifact receipt is invalid")
        return value

    def _catalog(
        self, root: Path, state: Mapping[str, Any]
    ) -> dict[str, _ArtifactRecord]:
        current_revision = _non_negative_int(state.get("design_revision"))
        trusted_design_hash = _trusted_current_design_hash(state, current_revision)
        result: dict[str, _ArtifactRecord] = {}
        release = state.get("last_release")
        if release is not None:
            if not isinstance(release, Mapping):
                raise ValidationError("artifact receipt is invalid")
            result.update(
                self._release_catalog(
                    root, release, current_revision, trusted_design_hash
                )
            )
        for key, record in self._retained_individual_release_catalog(
            root, current_revision, trusted_design_hash
        ).items():
            prior = result.get(key)
            if prior is None or _newer_record(record, prior):
                result[key] = record
        preview = state.get("last_preview")
        if preview is not None:
            if not isinstance(preview, Mapping):
                raise ValidationError("artifact receipt is invalid")
            for key, value in self._preview_catalog(
                root, preview, current_revision
            ).items():
                result.setdefault(key, value)
        return result

    def _release_catalog(
        self,
        root: Path,
        summary: Mapping[str, Any],
        current_revision: int | None,
        trusted_design_hash: str | None,
    ) -> dict[str, _ArtifactRecord]:
        release_root = self._record_directory(root, summary.get("root"))
        receipt = self._json_file(release_root, "receipt.json", limit=MAX_RECEIPT_BYTES)
        if not isinstance(receipt, Mapping) or receipt.get("status") != "complete":
            raise ValidationError("artifact receipt is invalid")
        schema = receipt.get("schema")
        if schema == "pcbdraft-release-receipt":
            return self._full_release_catalog(
                root, release_root, summary, receipt, current_revision
            )
        if schema == "pcbdraft-individual-manufacturing-export":
            return self._individual_release_catalog(
                release_root,
                receipt,
                current_revision,
                trusted_design_hash,
            )
        raise ValidationError("artifact receipt is invalid")

    def _full_release_catalog(
        self,
        root: Path,
        release_root: Path,
        summary: Mapping[str, Any],
        receipt: Mapping[str, Any],
        current_revision: int | None,
    ) -> dict[str, _ArtifactRecord]:
        if receipt.get("version") != 1:
            raise ValidationError("artifact receipt is invalid")
        manifest_path = self._regular_file(
            release_root, "release-manifest.json", limit=MAX_RECEIPT_BYTES
        )
        expected_manifest = receipt.get("manifest_sha256")
        if not _digest_matches(manifest_path, expected_manifest, MAX_RECEIPT_BYTES):
            raise ValidationError("artifact receipt is invalid")
        expected_archive = receipt.get("archive_sha256")
        if expected_archive is not None:
            archive = self._regular_file(
                release_root, "release.zip", limit=MAX_ARCHIVE_BYTES
            )
            if not _digest_matches(archive, expected_archive, MAX_ARCHIVE_BYTES):
                raise ValidationError("artifact receipt is invalid")
        manifest = self._json_file(
            release_root, "release-manifest.json", limit=MAX_RECEIPT_BYTES
        )
        if (
            not isinstance(manifest, Mapping)
            or manifest.get("schema") != "pcbdraft-manufacturing-release"
            or manifest.get("version") not in {1, 2}
        ):
            raise ValidationError("artifact receipt is invalid")
        inventory = self._inventory(release_root, manifest.get("artifacts"))
        design = manifest.get("design")
        manifest_revision = (
            _non_negative_int(design.get("revision"))
            if isinstance(design, Mapping)
            else None
        )
        source_revision = _non_negative_int(summary.get("source_design_revision"))
        if source_revision is None:
            source_revision = manifest_revision
        stale = _is_stale(source_revision, current_revision)
        created_at = _timestamp(receipt.get("completed_at"))
        selections = {
            "bom.csv": ("manufacturing/bom.csv",),
            "gerbers.zip": _under(inventory, "manufacturing/gerber/"),
            "drill.zip": _under(inventory, "manufacturing/drill/"),
            "positions.csv": ("manufacturing/positions.csv",),
            "board.step": ("manufacturing/board.step",),
            "schematic.pdf": ("manufacturing/schematic.pdf",),
        }
        return self._records_from_selections(
            inventory, selections, stale=stale, created_at=created_at
        )

    def _individual_release_catalog(
        self,
        release_root: Path,
        receipt: Mapping[str, Any],
        current_revision: int | None,
        trusted_design_hash: str | None,
    ) -> dict[str, _ArtifactRecord]:
        if (
            receipt.get("version") != 1
            or receipt.get("status") != "complete"
            or not _is_sha256(receipt.get("design_content_hash"))
        ):
            raise ValidationError("artifact receipt is invalid")
        export = receipt.get("export")
        key = _INDIVIDUAL_EXPORT_KEYS.get(export) if isinstance(export, str) else None
        if key is None:
            raise ValidationError("artifact receipt is invalid")
        completed_at = _timestamp(receipt.get("completed_at"))
        if not completed_at:
            raise ValidationError("artifact receipt is invalid")
        inventory = self._inventory(release_root, receipt.get("artifacts"))
        sources = self._individual_export_sources(export, inventory)
        source_revision = _non_negative_int(receipt.get("source_design_revision"))
        stale = _individual_evidence_is_stale(
            source_revision,
            current_revision,
            receipt["design_content_hash"],
            trusted_design_hash,
        )
        record = self._record(
            key,
            sources,
            stale=stale,
            created_at=completed_at,
        )
        return {key: record}

    def _retained_individual_release_catalog(
        self,
        root: Path,
        current_revision: int | None,
        trusted_design_hash: str | None,
    ) -> dict[str, _ArtifactRecord]:
        """Use the newest complete retained receipt for each fixed export."""

        result: dict[str, _ArtifactRecord] = {}
        for release_root in self._retained_directories(root, "releases"):
            receipt = self._retained_receipt(release_root)
            if (
                not isinstance(receipt, Mapping)
                or receipt.get("schema") != "pcbdraft-individual-manufacturing-export"
                or receipt.get("status") != "complete"
            ):
                continue
            export = receipt.get("export")
            key = (
                _INDIVIDUAL_EXPORT_KEYS.get(export) if isinstance(export, str) else None
            )
            if key is None or key in result:
                continue
            result.update(
                self._individual_release_catalog(
                    release_root,
                    receipt,
                    current_revision,
                    trusted_design_hash,
                )
            )
        return result

    def _individual_export_sources(
        self,
        export: str,
        inventory: Mapping[str, _SourceFile],
    ) -> tuple[_SourceFile, ...]:
        names = tuple(sorted(inventory))
        expected = {
            "export_bom": ("bom.csv",),
            "export_pick_place": ("positions.csv",),
            "export_step": ("board.step",),
        }.get(export)
        if expected is not None:
            if names != expected:
                raise ValidationError("artifact receipt is invalid")
            return tuple(inventory[name] for name in names)
        prefix = {
            "export_gerbers": "gerber/",
            "export_drill": "drill/",
        }.get(export)
        if (
            prefix is None
            or not names
            or any(not name.startswith(prefix) for name in names)
        ):
            raise ValidationError("artifact receipt is invalid")
        return tuple(inventory[name] for name in names)

    def _preview_catalog(
        self,
        root: Path,
        summary: Mapping[str, Any],
        current_revision: int | None,
    ) -> dict[str, _ArtifactRecord]:
        preview_root = self._record_directory(root, summary.get("root"))
        receipt = self._json_file(preview_root, "receipt.json", limit=MAX_RECEIPT_BYTES)
        if (
            not isinstance(receipt, Mapping)
            or receipt.get("schema") != "pcbdraft-preview-bundle"
            or receipt.get("version") != 1
            or not isinstance(receipt.get("files"), Mapping)
        ):
            raise ValidationError("artifact receipt is invalid")
        files = receipt["files"]
        sources: dict[str, _SourceFile] = {}
        for receipt_key, public_key in (
            ("schematic_svg", "schematic.svg"),
            ("schematic_pdf", "schematic.pdf"),
        ):
            entry = files.get(receipt_key)
            if entry is None:
                continue
            if not isinstance(entry, Mapping) or set(entry) != {
                "path",
                "bytes",
                "sha256",
            }:
                raise ValidationError("artifact receipt is invalid")
            source = self._inventory_entry(
                preview_root,
                {
                    "path": entry.get("path"),
                    "size": entry.get("bytes"),
                    "sha256": entry.get("sha256"),
                },
            )
            sources[public_key] = source
        source_revision = _non_negative_int(summary.get("source_design_revision"))
        stale = _is_stale(source_revision, current_revision)
        created_at = _timestamp(receipt.get("created_at"))
        return {
            key: self._record(key, (source,), stale=stale, created_at=created_at)
            for key, source in sources.items()
        }

    def _records_from_selections(
        self,
        inventory: Mapping[str, _SourceFile],
        selections: Mapping[str, tuple[str, ...]],
        *,
        stale: bool,
        created_at: str,
    ) -> dict[str, _ArtifactRecord]:
        result: dict[str, _ArtifactRecord] = {}
        for key, names in selections.items():
            if not names or any(name not in inventory for name in names):
                continue
            result[key] = self._record(
                key,
                tuple(inventory[name] for name in sorted(names)),
                stale=stale,
                created_at=created_at,
            )
        return result

    def _record(
        self,
        key: str,
        sources: tuple[_SourceFile, ...],
        *,
        stale: bool,
        created_at: str,
    ) -> _ArtifactRecord:
        spec = _SPECS_BY_KEY.get(key)
        if spec is None or not sources:
            raise ValidationError("artifact receipt is invalid")
        if sum(item.size for item in sources) > MAX_ZIP_SOURCE_BYTES:
            raise ValidationError("artifact receipt is invalid")
        return _ArtifactRecord(
            key=key,
            label=spec.label,
            sources=sources,
            stale=stale,
            created_at=created_at,
        )

    def _inventory(self, release_root: Path, raw: Any) -> dict[str, _SourceFile]:
        if not isinstance(raw, list) or len(raw) > MAX_RELEASE_FILES:
            raise ValidationError("artifact receipt is invalid")
        result: dict[str, _SourceFile] = {}
        for entry in raw:
            source = self._inventory_entry(release_root, entry)
            if source.relative in result:
                raise ValidationError("artifact receipt is invalid")
            result[source.relative] = source
        return result

    def _inventory_entry(self, root: Path, entry: Any) -> _SourceFile:
        if not isinstance(entry, Mapping) or set(entry) != {"path", "size", "sha256"}:
            raise ValidationError("artifact receipt is invalid")
        relative = entry.get("path")
        size = entry.get("size")
        digest = entry.get("sha256")
        if (
            not isinstance(relative, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 < size <= MAX_ARTIFACT_BYTES
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise ValidationError("artifact receipt is invalid")
        path = self._regular_file(root, relative, limit=MAX_ARTIFACT_BYTES)
        try:
            actual_size = path.stat().st_size
        except OSError as exc:
            raise ValidationError("artifact receipt is invalid") from exc
        if actual_size != size or not _digest_matches(path, digest, MAX_ARTIFACT_BYTES):
            raise ValidationError("artifact receipt is invalid")
        return _SourceFile(relative=relative, path=path, size=size, digest=digest)

    def _validation(self, root: Path, state: Mapping[str, Any]) -> dict[str, Any]:
        current_revision = _non_negative_int(state.get("design_revision"))
        summary = state.get("last_validation")
        base = {
            "schema": "pcbdraft-gui-validation",
            "version": 1,
            "design_revision": current_revision,
            "checked_at": "",
            "checks": [],
            "counts": {"error": 0, "warning": 0, "unconnected": 0},
        }
        if summary is None:
            return self._retained_individual_validation(root, current_revision, base)
        if not isinstance(summary, Mapping):
            raise ValidationError("artifact receipt is invalid")
        report_name = summary.get("report")
        report_hash = summary.get("report_sha256")
        if (
            not isinstance(report_name, str)
            or _SHA256.fullmatch(str(report_hash)) is None
        ):
            raise ValidationError("artifact receipt is invalid")
        report_path = self._regular_file(root, report_name, limit=MAX_RECEIPT_BYTES)
        if not _digest_matches(report_path, report_hash, MAX_RECEIPT_BYTES):
            raise ValidationError("artifact receipt is invalid")
        report = self._json_file(root, report_name, limit=MAX_RECEIPT_BYTES)
        if (
            isinstance(report, Mapping)
            and report.get("schema") == "pcbdraft-individual-check"
            and report.get("version") == 1
        ):
            result = self._retained_individual_validation(root, current_revision, base)
            if result["state"] == "not_run":
                raise ValidationError("artifact receipt is invalid")
            return result
        if (
            not isinstance(report, Mapping)
            or report.get("schema") != "pcbdraft-validation"
            or report.get("version") != 2
        ):
            raise ValidationError("artifact receipt is invalid")
        tool_runs = report.get("tool_runs")
        checks = [
            *_validation_tool_checks(tool_runs),
            *_validation_checks(report.get("levels")),
        ]
        counts = _validation_counts(tool_runs)
        stale = _is_stale(
            _non_negative_int(summary.get("source_design_revision")), current_revision
        )
        result = {
            **base,
            "checked_at": _timestamp(summary.get("completed_at")),
            "checks": checks,
            "counts": counts,
        }
        if stale:
            return {**result, "state": "stale"}
        if _validation_passes(summary, report, checks):
            return {**result, "state": "pass"}
        if any(item["outcome"] == "fail" for item in checks):
            return {**result, "state": "failed"}
        return {**result, "state": "warning"}

    def _retained_individual_validation(
        self,
        root: Path,
        current_revision: int | None,
        base: Mapping[str, Any],
    ) -> dict[str, Any]:
        records = self._retained_individual_checks(root, current_revision)
        if not records:
            return {**base, "state": "not_run"}
        checks: list[dict[str, str]] = []
        counts = {"error": 0, "warning": 0, "unconnected": 0}
        stale = False
        all_current_pass = True
        has_failure = False
        completed_at: list[str] = []
        for kind, identifier in _INDIVIDUAL_CHECK_IDS.items():
            record = records.get(kind)
            if record is None:
                checks.append(
                    {"id": identifier, "state": "missing", "outcome": "unknown"}
                )
                all_current_pass = False
                continue
            checks.append(
                {
                    "id": identifier,
                    "state": record["state"],
                    "outcome": record["outcome"],
                }
            )
            completed_at.append(record["completed_at"])
            stale = stale or record["stale"]
            all_current_pass = all_current_pass and (
                record["state"] == "completed" and record["outcome"] == "pass"
            )
            has_failure = has_failure or record["outcome"] == "fail"
            for name in counts:
                counts[name] += record["counts"][name]
        result = {
            **base,
            "checked_at": max(completed_at, default=""),
            "checks": checks,
            "counts": counts,
        }
        if stale:
            return {**result, "state": "stale"}
        if all_current_pass:
            return {**result, "state": "pass"}
        if has_failure:
            return {**result, "state": "failed"}
        return {**result, "state": "warning"}

    def _retained_individual_checks(
        self, root: Path, current_revision: int | None
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for run_root in self._retained_directories(root, "validation"):
            receipt = self._retained_receipt(run_root)
            if (
                not isinstance(receipt, Mapping)
                or receipt.get("schema") != "pcbdraft-individual-check-receipt"
                or receipt.get("status") != "complete"
            ):
                continue
            kind = receipt.get("check")
            if not isinstance(kind, str) or kind not in _INDIVIDUAL_CHECK_IDS:
                continue
            if kind in result:
                continue
            result[kind] = self._individual_check_record(
                run_root, receipt, current_revision
            )
        return result

    def _individual_check_record(
        self,
        run_root: Path,
        receipt: Mapping[str, Any],
        current_revision: int | None,
    ) -> dict[str, Any]:
        kind = receipt.get("check")
        state = receipt.get("state")
        outcome = receipt.get("outcome")
        report_name = receipt.get("report")
        report_hash = receipt.get("report_sha256")
        content_hash = receipt.get("design_content_hash")
        completed_at = _timestamp(receipt.get("completed_at"))
        if (
            not isinstance(kind, str)
            or kind not in _INDIVIDUAL_CHECK_IDS
            or receipt.get("version") != 1
            or receipt.get("status") != "complete"
            or state not in {"completed", "failed", "unknown"}
            or outcome not in {"pass", "fail", "unknown"}
            or report_name != "check.json"
            or not _is_sha256(report_hash)
            or not _is_sha256(content_hash)
            or not completed_at
        ):
            raise ValidationError("artifact receipt is invalid")
        report_path = self._regular_file(
            run_root, "check.json", limit=MAX_RECEIPT_BYTES
        )
        if not _digest_matches(report_path, report_hash, MAX_RECEIPT_BYTES):
            raise ValidationError("artifact receipt is invalid")
        report = self._json_file(run_root, "check.json", limit=MAX_RECEIPT_BYTES)
        if (
            not isinstance(report, Mapping)
            or report.get("schema") != "pcbdraft-individual-check"
            or report.get("version") != 1
            or report.get("check") != kind
            or report.get("design_content_hash") != content_hash
            or report.get("state") != state
            or report.get("outcome") != outcome
            or not _timestamp(report.get("created_at"))
            or not isinstance(report.get("details"), Mapping)
        ):
            raise ValidationError("artifact receipt is invalid")
        details = report["details"]
        if kind in {"run_erc", "run_drc"}:
            items = details.get("violations")
            tool_run = report.get("tool_run")
            if (
                not isinstance(tool_run, Mapping)
                or tool_run.get("status") != state
                or not isinstance(items, list)
            ):
                raise ValidationError("artifact receipt is invalid")
        else:
            items = details.get("issues")
            if report.get("tool_run") is not None or not isinstance(items, list):
                raise ValidationError("artifact receipt is invalid")
        if len(items) > MAX_CHECK_DETAILS or any(
            not isinstance(item, Mapping) for item in items
        ):
            raise ValidationError("artifact receipt is invalid")
        if state == "completed":
            expected_outcome = "fail" if items else "pass"
            if outcome != expected_outcome:
                raise ValidationError("artifact receipt is invalid")
        elif outcome == "pass":
            raise ValidationError("artifact receipt is invalid")
        source_revision = _non_negative_int(receipt.get("source_design_revision"))
        return {
            "state": state,
            "outcome": outcome,
            "completed_at": completed_at,
            "stale": _individual_evidence_is_stale(
                source_revision, current_revision, content_hash, None
            ),
            "counts": _individual_check_counts(kind, items),
        }

    def _json_file(self, root: Path, relative: str, *, limit: int) -> Any:
        path = self._regular_file(root, relative, limit=limit)
        try:
            return load_json_limited(path, limit)
        except (OSError, PCBDraftError, TypeError, ValueError) as exc:
            raise ValidationError("artifact receipt is invalid") from exc

    def _retained_directories(self, root: Path, name: str) -> tuple[Path, ...]:
        """Return a fixed, newest-first bounded set of direct evidence roots."""

        collection = root / name
        try:
            info = collection.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise OSError("unsafe retained evidence root")
            resolved = collection.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise OSError("retained evidence is outside the project")
            candidates: list[Path] = []
            for candidate in resolved.iterdir():
                candidate_info = candidate.lstat()
                if stat.S_ISLNK(candidate_info.st_mode):
                    raise OSError("symbolic link")
                if stat.S_ISDIR(candidate_info.st_mode):
                    candidates.append(candidate)
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise ValidationError("artifact receipt is invalid") from exc
        return tuple(
            sorted(candidates, key=lambda candidate: candidate.name, reverse=True)[
                :MAX_RETAINED_RECEIPTS
            ]
        )

    def _retained_receipt(self, directory: Path) -> Mapping[str, Any] | None:
        """Read one local receipt without following links or trusting its path."""

        try:
            (directory / "receipt.json").lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValidationError("artifact receipt is invalid") from exc
        receipt = self._json_file(directory, "receipt.json", limit=MAX_RECEIPT_BYTES)
        return receipt if isinstance(receipt, Mapping) else None

    def _record_directory(self, root: Path, value: Any) -> Path:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValidationError("artifact receipt is invalid")
        raw = Path(value)
        try:
            if raw.is_absolute():
                candidate = raw
                if candidate.is_symlink():
                    raise OSError("symbolic link")
                raw_parts = candidate.relative_to(root).parts
                self._reject_linked_members(root, raw_parts)
            else:
                candidate = self._member(root, value)
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_dir():
                raise OSError("outside root")
            self._reject_linked_members(root, resolved.relative_to(root).parts)
        except (OSError, ValueError) as exc:
            raise ValidationError("artifact receipt is invalid") from exc
        return resolved

    def _regular_file(self, root: Path, relative: str, *, limit: int) -> Path:
        try:
            candidate = self._member(root, relative)
            self._reject_linked_members(root, PurePosixPath(relative).parts)
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise OSError("not a regular file")
            if info.st_size <= 0 or info.st_size > limit:
                raise OSError("size is invalid")
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise OSError("outside root")
        except (OSError, ValueError) as exc:
            raise ValidationError("artifact receipt is invalid") from exc
        return resolved

    @staticmethod
    def _member(root: Path, value: str) -> Path:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValidationError("artifact receipt is invalid")
        pure = PurePosixPath(value)
        if (
            pure.is_absolute()
            or not pure.parts
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise ValidationError("artifact receipt is invalid")
        return root.joinpath(*pure.parts)

    @staticmethod
    def _reject_linked_members(root: Path, parts: tuple[str, ...]) -> None:
        cursor = root
        for part in parts:
            cursor /= part
            if cursor.is_symlink():
                raise OSError("symbolic link")

    def _zip_cache(self, project_id: str, record: _ArtifactRecord) -> Path:
        digest = hashlib.sha256()
        digest.update(record.key.encode("ascii"))
        for source in record.sources:
            digest.update(source.relative.encode("utf-8"))
            digest.update(source.digest.encode("ascii"))
            digest.update(str(source.size).encode("ascii"))
        project_dir = self.cache_root / project_id
        if project_dir.exists() and (
            project_dir.is_symlink() or not project_dir.is_dir()
        ):
            raise ValidationError("GUI artifact cache path is unsafe")
        project_dir = make_directory(project_dir)
        target = (
            project_dir / f"{record.key.rsplit('.', 1)[0]}-{digest.hexdigest()}.zip"
        )
        descriptors: list[tuple[_SourceFile, str]] = []
        used: set[str] = set()
        for source in sorted(record.sources, key=lambda item: item.relative):
            name = _safe_archive_name(Path(source.relative).name, used)
            descriptors.append((source, name))
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise ValidationError("GUI artifact cache path is unsafe")
            if _cached_zip_matches(target, descriptors):
                return target
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".pcbdraft-gui-", suffix=".zip", dir=project_dir
        )
        os.close(descriptor)
        temporary: Path | None = Path(temporary_name)
        try:
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
            ) as archive:
                for source, name in descriptors:
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.create_system = 3
                    info.external_attr = 0o100644 << 16
                    archive.writestr(
                        info, read_bytes_limited(source.path, MAX_ARTIFACT_BYTES)
                    )
            if (
                temporary.stat().st_size <= 0
                or temporary.stat().st_size > MAX_ZIP_SOURCE_BYTES
            ):
                raise ValidationError("artifact receipt is invalid")
            os.replace(temporary, target)
            temporary = None
        except (OSError, PCBDraftError, zipfile.BadZipFile) as exc:
            raise ValidationError("artifact receipt is invalid") from exc
        finally:
            if temporary is not None and temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass
        return target


def _under(inventory: Mapping[str, _SourceFile], prefix: str) -> tuple[str, ...]:
    return tuple(sorted(name for name in inventory if name.startswith(prefix)))


def _non_negative_int(value: Any) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _is_stale(source_revision: int | None, current_revision: int | None) -> bool:
    return (
        source_revision is not None
        and current_revision is not None
        and source_revision != current_revision
    )


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _trusted_current_design_hash(
    state: Mapping[str, Any], current_revision: int | None
) -> str | None:
    """Accept a summary hash only when it is bound to the current revision."""

    if current_revision is None:
        return None
    hashes: set[str] = set()
    for name in ("last_validation", "last_preview", "last_release"):
        summary = state.get(name)
        if (
            isinstance(summary, Mapping)
            and _non_negative_int(summary.get("source_design_revision"))
            == current_revision
            and _is_sha256(summary.get("design_content_hash"))
        ):
            hashes.add(summary["design_content_hash"])
    return next(iter(hashes)) if len(hashes) == 1 else None


def _individual_evidence_is_stale(
    source_revision: int | None,
    current_revision: int | None,
    evidence_hash: str,
    trusted_design_hash: str | None,
) -> bool:
    """Never infer currentness for individual evidence with no revision binding."""

    if source_revision is not None:
        return current_revision is None or source_revision != current_revision
    return (
        current_revision is None
        or trusted_design_hash is None
        or evidence_hash != trusted_design_hash
    )


def _newer_record(candidate: _ArtifactRecord, prior: _ArtifactRecord) -> bool:
    return candidate.created_at >= prior.created_at


def _individual_check_counts(
    kind: str, items: list[Mapping[str, Any]]
) -> dict[str, int]:
    result = {"error": 0, "warning": 0, "unconnected": 0}
    for item in items:
        severity = item.get("severity")
        category = item.get("category")
        if (
            kind == "run_drc"
            and isinstance(category, str)
            and "unconnected" in category.casefold()
        ):
            result["unconnected"] += 1
        elif severity == "warning":
            result["warning"] += 1
        else:
            result["error"] += 1
    return result


def _timestamp(value: Any) -> str:
    return value if isinstance(value, str) and _TIMESTAMP.fullmatch(value) else ""


def _digest_matches(path: Path, expected: Any, limit: int) -> bool:
    if not isinstance(expected, str) or _SHA256.fullmatch(expected) is None:
        return False
    try:
        return sha256_file(path, max_bytes=limit) == expected
    except (OSError, PCBDraftError, ValidationError):
        return False


def _safe_archive_name(value: str, used: set[str]) -> str:
    cleaned = _SAFE_ARCHIVE_NAME.sub("-", value).strip(".-") or "artifact"
    stem, suffix = Path(cleaned).stem, Path(cleaned).suffix
    candidate = cleaned
    ordinal = 2
    while candidate.casefold() in used:
        candidate = f"{stem}-{ordinal}{suffix}"
        ordinal += 1
    used.add(candidate.casefold())
    return candidate[:180]


def _cached_zip_matches(path: Path, descriptors: list[tuple[_SourceFile, str]]) -> bool:
    """Accept a cache entry only when it is the exact verified archive view."""

    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if [item.filename for item in infos] != [name for _, name in descriptors]:
                return False
            for info, (source, name) in zip(infos, descriptors, strict=True):
                if (
                    info.filename != name
                    or info.date_time != (1980, 1, 1, 0, 0, 0)
                    or info.file_size != source.size
                ):
                    return False
                digest = hashlib.sha256()
                total = 0
                with archive.open(info, "r") as member:
                    while chunk := member.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_ARTIFACT_BYTES:
                            return False
                        digest.update(chunk)
                if total != source.size or digest.hexdigest() != source.digest:
                    return False
    except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return False
    return True


def _validation_checks(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for level in value[:16]:
        if not isinstance(level, Mapping):
            continue
        identifier = level.get("level")
        state = level.get("state")
        outcome = level.get("outcome")
        if not all(
            isinstance(item, str) and item for item in (identifier, state, outcome)
        ):
            continue
        result.append(
            {
                "id": identifier[:32],
                "state": state[:32],
                "outcome": outcome[:32],
            }
        )
    return result


def _validation_counts(value: Any) -> dict[str, int]:
    result = {"error": 0, "warning": 0, "unconnected": 0}
    if not isinstance(value, Mapping):
        return result
    for name in ("erc", "drc"):
        item = value.get(name)
        if not isinstance(item, Mapping):
            continue
        for source, target in (
            ("error_count", "error"),
            ("warning_count", "warning"),
            ("unconnected_count", "unconnected"),
        ):
            count = _non_negative_int(item.get(source))
            if count is not None:
                result[target] += count
        violations = _non_negative_int(item.get("violation_count"))
        if violations is not None and result["error"] == 0:
            result["error"] += violations
    return result


def _validation_tool_checks(value: Any) -> list[dict[str, str]]:
    """Give ERC, DRC, and connectivity distinct safe UI rows."""

    if not isinstance(value, Mapping):
        return [
            {"id": "erc", "state": "unavailable", "outcome": "unknown"},
            {"id": "drc", "state": "unavailable", "outcome": "unknown"},
            {"id": "unrouted", "state": "unavailable", "outcome": "unknown"},
        ]
    result: list[dict[str, str]] = []
    drc: Mapping[str, Any] | None = None
    for name in ("erc", "drc"):
        item = value.get(name)
        mapping = item if isinstance(item, Mapping) else {}
        if name == "drc":
            drc = mapping
        state = str(mapping.get("status", "unavailable"))[:32]
        complete = state == "completed"
        errors = _non_negative_int(mapping.get("error_count")) or 0
        violations = _non_negative_int(mapping.get("violation_count")) or 0
        unconnected = _non_negative_int(mapping.get("unconnected_count")) or 0
        outcome = (
            "pass"
            if complete and not (errors or violations or unconnected)
            else "fail"
            if complete
            else "unknown"
        )
        result.append({"id": name, "state": state, "outcome": outcome})
    drc_state = str((drc or {}).get("status", "unavailable"))[:32]
    unconnected = _non_negative_int((drc or {}).get("unconnected_count")) or 0
    result.append(
        {
            "id": "unrouted",
            "state": drc_state,
            "outcome": "pass"
            if drc_state == "completed" and not unconnected
            else "fail"
            if drc_state == "completed"
            else "unknown",
        }
    )
    return result


def _validation_passes(
    summary: Mapping[str, Any], report: Mapping[str, Any], checks: list[dict[str, str]]
) -> bool:
    readiness = report.get("readiness")
    ready = bool(summary.get("candidate_ready")) or (
        isinstance(readiness, Mapping)
        and readiness.get("engineering_candidate") is True
    )
    return (
        ready and bool(checks) and all(item.get("outcome") == "pass" for item in checks)
    )


__all__ = ("ArtifactDownload", "GUIArtifactService")
