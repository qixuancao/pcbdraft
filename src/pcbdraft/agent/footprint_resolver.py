"""Installed KiCad footprint search and bounded local description."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import read_bytes_limited
from pcbdraft.domain.ir import _string
from pcbdraft.kicad.runtime import kicad_data_directory

FOOTPRINT_FILE_LIMIT = 64 * 1024 * 1024
MAX_FOOTPRINT_INDEX = 100_000
_FOOTPRINT_ID = re.compile(r"^[A-Za-z0-9_.+-]+:[A-Za-z0-9_.+~{}/-]+$")
_PAD = re.compile(rb'\(pad\s+(?:"((?:[^"\\]|\\.)*)"|([^\s()]+))(?=\s)')


@dataclass(frozen=True)
class FootprintCandidate:
    footprint: str
    pad_numbers: tuple[str, ...]
    bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "footprint": self.footprint,
            "pad_numbers": list(self.pad_numbers),
            "pad_count": len(self.pad_numbers),
            "bytes": self.bytes,
            "sha256": self.sha256,
            "source": "installed_local_kicad_library",
        }


class LocalKiCadFootprintResolver:
    """Read only installed stock footprint facts; never infer qualification."""

    def __init__(self, footprint_root: str | Path | None = None) -> None:
        self.footprint_root = Path(footprint_root or kicad_data_directory("footprints"))

    @cached_property
    def index(self) -> tuple[str, ...]:
        root = self.footprint_root
        if root.is_symlink() or not root.is_dir():
            raise ValidationError(
                "installed KiCad footprint library directory is unavailable"
            )
        result: list[str] = []
        for library in sorted(root.glob("*.pretty")):
            if library.is_symlink() or not library.is_dir():
                continue
            for path in sorted(library.glob("*.kicad_mod")):
                if path.is_symlink() or not path.is_file():
                    continue
                result.append(f"{library.stem}:{path.stem}")
                if len(result) > MAX_FOOTPRINT_INDEX:
                    raise ValidationError("installed footprint index exceeds its bound")
        return tuple(result)

    def find(self, query: str, *, limit: int = 12) -> tuple[FootprintCandidate, ...]:
        query = _string(query, "footprint query", limit=256)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 64
        ):
            raise ValidationError("footprint candidate limit must be from 1 to 64")
        key = re.sub(r"[^a-z0-9]", "", query.casefold())

        def rank(identifier: str) -> tuple[int, int, str]:
            library, name = identifier.split(":", 1)
            name_key = re.sub(r"[^a-z0-9]", "", name.casefold())
            library_key = re.sub(r"[^a-z0-9]", "", library.casefold())
            score = (
                0
                if name_key == key
                else 1
                if name_key.startswith(key)
                else 2
                if key in name_key
                else 3
                if key in library_key
                else 99
            )
            return score, len(name_key), identifier

        matches = [identifier for identifier in self.index if rank(identifier)[0] < 99]
        return tuple(
            self.describe(identifier)
            for identifier in sorted(matches, key=rank)[:limit]
        )

    def describe(self, footprint: str) -> FootprintCandidate:
        if not isinstance(footprint, str) or not _FOOTPRINT_ID.fullmatch(footprint):
            raise ValidationError("footprint must be a KiCad library id")
        library, name = footprint.split(":", 1)
        path = self.footprint_root / f"{library}.pretty" / f"{name}.kicad_mod"
        if path.is_symlink() or not path.is_file():
            raise ValidationError(
                f"installed KiCad footprint is unavailable: {footprint}"
            )
        raw = read_bytes_limited(path, FOOTPRINT_FILE_LIMIT)
        pads: set[str] = set()
        for match in _PAD.finditer(raw):
            value = match.group(1) if match.group(1) is not None else match.group(2)
            if value is None:
                continue
            try:
                number = value.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValidationError(
                    "installed footprint has a non-UTF-8 pad"
                ) from exc
            if number:
                pads.add(number)
        return FootprintCandidate(
            footprint=footprint,
            pad_numbers=tuple(sorted(pads)),
            bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
        )
