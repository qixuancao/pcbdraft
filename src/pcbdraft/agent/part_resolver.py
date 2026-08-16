"""Local KiCad symbol inspection and provisional part-record resolution.

Installed-library data is executable input for the compiler, not manufacturer
qualification. This adapter deliberately owns that boundary separately from
plan parsing and engineering review.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from kicad_sch_api import Schematic

from pcbdraft.agent.plan import PlanComponent, _normal_key
from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import read_bytes_limited
from pcbdraft.domain.ir import Provenance, _string
from pcbdraft.domain.parts import PartRecord, PinDefinition

SYMBOL_FILE_LIMIT = 128 * 1024 * 1024
_SYMBOL_ID = re.compile(r"^[A-Za-z0-9_.+-]+:[A-Za-z0-9_.+~{}/-]+$")


def _part_id(symbol: str, footprint: str | None) -> str:
    library, name = symbol.split(":", 1)
    slug = re.sub(r"[^a-z0-9]+", "-", f"{library}-{name}".casefold()).strip("-")
    digest = hashlib.sha256(f"{symbol}\x00{footprint or ''}".encode()).hexdigest()[:12]
    return f"kicad.{slug[:88].rstrip('-')}.{digest}"


@lru_cache(maxsize=8)
def _installed_symbol_index(symbol_root: str) -> tuple[str, ...]:
    """Index public symbol IDs once per installed KiCad library directory.

    This is deliberately a byte-level index rather than model knowledge.  It lets
    an agent propose a symbol from the local KiCad installation without trusting
    an unverified static list or a hard-coded component profile.
    """

    root = Path(symbol_root)
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("installed KiCad symbol library directory is unavailable")
    result: set[str] = set()
    for path in sorted(root.glob("*.kicad_sym")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            raw = read_bytes_limited(path, SYMBOL_FILE_LIMIT)
        except (OSError, ValidationError):
            continue
        library = path.stem
        for match in re.finditer(rb'\(symbol\s+"([^"\\]+)"', raw):
            try:
                name = match.group(1).decode("utf-8")
            except UnicodeDecodeError:
                continue
            # KiCad's nested graphic/unit symbols are not public library IDs.
            if re.search(r"_[0-9]+_[0-9]+$", name):
                continue
            candidate = f"{library}:{name}"
            if _SYMBOL_ID.fullmatch(candidate):
                result.add(candidate)
    return tuple(sorted(result))


@dataclass(frozen=True)
class SymbolCandidate:
    symbol: str
    footprint: str | None
    description: str
    datasheet: str | None
    pins: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "footprint": self.footprint,
            "description": self.description,
            "datasheet": self.datasheet,
            "pins": [dict(pin) for pin in self.pins],
        }


class LocalKiCadPartResolver:
    """Resolve selected components from the installed KiCad symbol libraries."""

    def __init__(self, symbol_root: str | Path | None = None) -> None:
        raw = symbol_root or os.environ.get(
            "KICAD_SYMBOL_DIR", "/usr/share/kicad/symbols"
        )
        self.symbol_root = Path(raw)

    def _symbol_path(self, symbol: str) -> Path:
        if not _SYMBOL_ID.fullmatch(symbol):
            raise ValidationError("symbol must be a KiCad library id")
        library, _name = symbol.split(":", 1)
        path = self.symbol_root / f"{library}.kicad_sym"
        if path.is_symlink() or not path.is_file():
            raise ValidationError(
                f"installed KiCad symbol library is unavailable: {library}"
            )
        return path

    @property
    def _symbol_index(self) -> tuple[str, ...]:
        return _installed_symbol_index(str(self.symbol_root))

    def find(self, query: str, *, limit: int = 12) -> tuple[SymbolCandidate, ...]:
        query = _string(query, "symbol query", limit=256)
        if limit < 1 or limit > 64:
            raise ValidationError("symbol candidate limit must be from 1 to 64")
        key = _normal_key(query)
        if not key:
            return ()

        def rank(symbol: str) -> tuple[int, int, str]:
            library, name = symbol.split(":", 1)
            name_key = _normal_key(name)
            library_key = _normal_key(library)
            if name_key == key:
                score = 0
            elif name_key.startswith(key):
                score = 1
            elif key in name_key:
                score = 2
            elif key in library_key:
                score = 3
            else:
                score = 99
            return (score, len(name_key), symbol)

        matches = [symbol for symbol in self._symbol_index if rank(symbol)[0] < 99]
        return tuple(
            self.describe(symbol) for symbol in sorted(matches, key=rank)[:limit]
        )

    def describe(self, symbol: str) -> SymbolCandidate:
        self._symbol_path(symbol)
        component = _library_component(symbol)
        pins = _pins(component)
        properties = _symbol_properties(component)
        footprint = properties.get("Footprint") or None
        return SymbolCandidate(
            symbol=symbol,
            footprint=footprint,
            description=properties.get("Description") or f"KiCad symbol {symbol}",
            datasheet=properties.get("Datasheet") or None,
            pins=tuple(
                {
                    "number": pin["number"],
                    "name": pin["name"],
                    "electrical_type": pin["electrical_type"],
                }
                for pin in pins
            ),
        )

    def resolve(self, component: PlanComponent) -> PartRecord:
        candidate = self.describe(component.symbol)
        footprint = component.footprint or candidate.footprint
        if component.on_board and footprint is None:
            raise ValidationError(
                f"plan component {component.id} is on_board but {component.symbol} has no footprint; provide an explicit footprint"
            )
        if footprint is not None and ":" not in footprint:
            raise ValidationError(
                f"plan component {component.id} has an invalid footprint"
            )
        part_id = _part_id(component.symbol, footprint)
        evidence = [
            Provenance(
                id="local_kicad_symbol",
                kind="symbol_library",
                source="KiCad installed libraries",
                locator=component.symbol,
                acquired_at=None,
                method="local_library_extract",
                confidence=0.7,
                notes=(
                    "Pin names and the default footprint were read from the installed "
                    "stock KiCad libraries; electrical and layout suitability were not validated."
                ),
            )
        ]
        if candidate.datasheet:
            evidence.append(
                Provenance(
                    id="local_kicad_datasheet_reference",
                    kind="datasheet",
                    source="KiCad installed symbol metadata",
                    locator=candidate.datasheet,
                    acquired_at=None,
                    method="local_library_reference",
                    confidence=0.4,
                    notes=(
                        "The locator was copied from the installed symbol. PCBDraft "
                        "did not fetch, authenticate, or review the referenced document."
                    ),
                )
            )
        return PartRecord(
            id=part_id,
            manufacturer=component.manufacturer or "not specified",
            mpn=component.mpn or "not specified",
            description=candidate.description,
            kind="generic_component",
            symbol=component.symbol,
            footprint=footprint,
            pins=tuple(
                PinDefinition(
                    number=pin["number"],
                    name=pin["name"],
                    electrical_type=pin["electrical_type"],
                    functions=(),
                    required=False,
                    footprint_pad=pin["number"],
                )
                for pin in candidate.pins
            ),
            ratings={},
            lifecycle={"status": "unknown", "source": "local_kicad_library"},
            sourcing={
                "status": "not_checked",
                "manufacturer_identity": (
                    "planner_claim_unverified"
                    if component.manufacturer is not None
                    else "not_specified"
                ),
                "mpn_identity": (
                    "planner_claim_unverified"
                    if component.mpn is not None
                    else "not_specified"
                ),
            },
            manufacturing={},
            models={},
            bom=component.on_board,
            trust="extracted",
            evidence=tuple(evidence),
            alternates=(),
        )


def _library_component(symbol: str) -> Any:
    schematic = Schematic.create(name="pcbdraft-symbol-probe")
    try:
        return schematic.components.add(
            symbol,
            reference="U1",
            value=symbol.split(":", 1)[1],
            position=(0, 0),
            grid_units=False,
        )
    except Exception as exc:  # backend errors are not an executable agent instruction
        raise ValidationError(
            f"cannot load installed KiCad symbol {symbol}: {exc}"
        ) from exc


def _pins(component: Any) -> tuple[dict[str, str], ...]:
    raw = component.list_pins()
    if not isinstance(raw, list):
        raise ValidationError("KiCad symbol probe returned malformed pin data")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise ValidationError("KiCad symbol probe returned malformed pin entry")
        number = entry.get("number")
        name = entry.get("name")
        electrical_type = entry.get("type")
        if not isinstance(number, str):
            raise ValidationError("KiCad symbol probe returned invalid pin values")
        if not isinstance(name, str):
            raise ValidationError("KiCad symbol probe returned invalid pin values")
        if not isinstance(electrical_type, str):
            raise ValidationError("KiCad symbol probe returned invalid pin values")
        if not number or number in seen:
            # Hidden duplicate-unit pins do not represent a distinct footprint pad.
            continue
        seen.add(number)
        result.append(
            {
                "number": number,
                "name": name,
                "electrical_type": re.sub(
                    r"[^a-z0-9_]+", "_", electrical_type.casefold()
                ).strip("_")
                or "passive",
            }
        )
    if not result:
        raise ValidationError("KiCad symbol has no usable pins")
    return tuple(sorted(result, key=lambda entry: _pin_sort_key(entry["number"])))


def _pin_sort_key(value: str) -> tuple[Any, ...]:
    return tuple(
        int(item) if item.isdigit() else item for item in re.split(r"(\d+)", value)
    )


def _symbol_properties(component: Any) -> dict[str, str]:
    definition = component.get_symbol_definition()
    raw = getattr(definition, "raw_kicad_data", None)
    if not isinstance(raw, list):
        return {}
    result: dict[str, str] = {}
    for entry in raw:
        if not isinstance(entry, list) or len(entry) < 3:
            continue
        if str(entry[0]) != "property" or not isinstance(entry[1], str):
            continue
        name = entry[1]
        value = entry[2]
        if isinstance(value, str) and name in {"Footprint", "Datasheet", "Description"}:
            result[name] = value
    return result
