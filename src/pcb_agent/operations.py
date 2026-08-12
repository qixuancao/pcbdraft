"""Typed semantic change sets and deterministic design mutation."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import ValidationError
from .ir import (
    Design,
    _identifier,
    _json_value,
    _strict_mapping,
    _string,
    canonical_json_bytes,
)

CHANGE_SCHEMA = "pcb-agent-change-set"
CHANGE_VERSION = 1
MAX_OPERATIONS = 256
MAX_CHANGE_BYTES = 4 * 1024 * 1024
SUPPORTED_OPERATIONS = {
    "add_block",
    "remove_block",
    "add_component",
    "remove_component",
    "update_component",
    "add_net",
    "remove_net",
    "connect",
    "disconnect",
    "rename_net",
    "upsert_constraint",
    "remove_constraint",
    "update_board",
    "set_metadata",
}


@dataclass(frozen=True)
class SemanticOperation:
    id: str
    op: str
    args: dict[str, Any]
    expected: dict[str, Any]
    reason: str

    @classmethod
    def from_dict(cls, value: Any, path: str) -> SemanticOperation:
        item = _strict_mapping(
            value,
            path,
            required={"id", "op", "args", "expected", "reason"},
            optional=set(),
        )
        op = _identifier(item["op"], f"{path}.op")
        if op not in SUPPORTED_OPERATIONS:
            raise ValidationError(f"{path}.op is unsupported: {op}")
        for name in ("args", "expected"):
            if not isinstance(item[name], Mapping):
                raise ValidationError(f"{path}.{name} must be an object")
        return cls(
            id=_identifier(item["id"], f"{path}.id"),
            op=op,
            args=_json_value(item["args"], f"{path}.args"),
            expected=_json_value(item["expected"], f"{path}.expected"),
            reason=_string(item["reason"], f"{path}.reason", limit=2048),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "op": self.op,
            "args": _json_value(self.args),
            "expected": _json_value(self.expected),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ChangeSet:
    id: str
    base_hash: str
    intent: str
    actor: str
    operations: tuple[SemanticOperation, ...]
    provenance: tuple[str, ...]
    schema: str = CHANGE_SCHEMA
    version: int = CHANGE_VERSION

    @classmethod
    def from_dict(cls, value: Any) -> ChangeSet:
        item = _strict_mapping(
            value,
            "$",
            required={
                "schema",
                "version",
                "id",
                "base_hash",
                "intent",
                "actor",
                "operations",
                "provenance",
            },
            optional=set(),
        )
        if item["schema"] != CHANGE_SCHEMA or item["version"] != CHANGE_VERSION:
            raise ValidationError("unsupported semantic change-set schema/version")
        base_hash = item["base_hash"]
        if not isinstance(base_hash, str) or not re_full_sha256(base_hash):
            raise ValidationError("$.base_hash must be a lowercase SHA-256 digest")
        raw_operations = item["operations"]
        if (
            not isinstance(raw_operations, list)
            or not 1 <= len(raw_operations) <= MAX_OPERATIONS
        ):
            raise ValidationError(
                f"$.operations must contain 1-{MAX_OPERATIONS} operations"
            )
        operations = tuple(
            SemanticOperation.from_dict(operation, f"$.operations[{index}]")
            for index, operation in enumerate(raw_operations)
        )
        operation_ids = [operation.id for operation in operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValidationError("semantic operation ids must be unique")
        provenance = item["provenance"]
        if not isinstance(provenance, list) or not all(
            isinstance(entry, str) for entry in provenance
        ):
            raise ValidationError("$.provenance must be an array of strings")
        result = cls(
            id=_identifier(item["id"], "$.id"),
            base_hash=base_hash,
            intent=_string(item["intent"], "$.intent", limit=4096),
            actor=_string(item["actor"], "$.actor", limit=256),
            operations=operations,
            provenance=tuple(sorted(set(provenance))),
        )
        if len(result.canonical_bytes()) > MAX_CHANGE_BYTES:
            raise ValidationError(
                f"semantic change set exceeds {MAX_CHANGE_BYTES} bytes"
            )
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "id": self.id,
            "base_hash": self.base_hash,
            "intent": self.intent,
            "actor": self.actor,
            "operations": [operation.to_dict() for operation in self.operations],
            "provenance": list(self.provenance),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def re_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def apply_change_set(design: Design, change_set: ChangeSet) -> Design:
    """Apply every operation to an in-memory copy, then validate once atomically."""
    if design.content_hash() != change_set.base_hash:
        raise ValidationError(
            "semantic change-set base hash conflicts with the current design "
            f"({change_set.base_hash} != {design.content_hash()})"
        )
    document = copy.deepcopy(design.to_dict())
    for index, operation in enumerate(change_set.operations):
        try:
            _apply_operation(document, operation)
        except ValidationError as exc:
            raise ValidationError(
                f"operation {operation.id} ({index + 1}/{len(change_set.operations)}) failed: {exc}"
            ) from exc
    return Design.from_dict(document)


def _collection(document: dict[str, Any], name: str) -> list[dict[str, Any]]:
    value = document.get(name)
    if not isinstance(value, list):
        raise ValidationError(f"design collection is malformed: {name}")
    return value


def _find(
    entries: list[dict[str, Any]], entry_id: str, *, kind: str
) -> dict[str, Any] | None:
    matches = [entry for entry in entries if entry.get("id") == entry_id]
    if len(matches) > 1:
        raise ValidationError(f"design contains duplicate {kind} id: {entry_id}")
    return matches[0] if matches else None


def _expect(entry: Any, expected: Mapping[str, Any], *, label: str) -> None:
    if not expected:
        return
    if entry is None:
        if expected.get("absent") is True and len(expected) == 1:
            return
        raise ValidationError(f"precondition failed; {label} is absent")
    if expected.get("absent") is True:
        raise ValidationError(f"precondition failed; {label} already exists")
    if not isinstance(entry, Mapping):
        raise ValidationError(f"precondition target is malformed: {label}")
    for key, value in expected.items():
        if key == "absent":
            continue
        if key not in entry or entry[key] != value:
            raise ValidationError(
                f"precondition failed for {label}.{key}: expected {value!r}, found {entry.get(key)!r}"
            )


def _required_id(args: Mapping[str, Any], name: str = "id") -> str:
    if name not in args:
        raise ValidationError(f"operation is missing args.{name}")
    return _identifier(args[name], f"args.{name}")


def _apply_operation(document: dict[str, Any], operation: SemanticOperation) -> None:
    op, args, expected = operation.op, operation.args, operation.expected
    if op in {"add_block", "add_component", "add_net"}:
        collection_name = {
            "add_block": "blocks",
            "add_component": "components",
            "add_net": "nets",
        }[op]
        value = args.get("value")
        if not isinstance(value, Mapping):
            raise ValidationError("args.value must be an object")
        entry_id = _required_id(value)
        entries = _collection(document, collection_name)
        existing = _find(entries, entry_id, kind=collection_name[:-1])
        _expect(
            existing,
            expected or {"absent": True},
            label=f"{collection_name}.{entry_id}",
        )
        entries.append(copy.deepcopy(dict(value)))
        return

    if op in {"remove_block", "remove_component", "remove_net", "remove_constraint"}:
        collection_name = {
            "remove_block": "blocks",
            "remove_component": "components",
            "remove_net": "nets",
            "remove_constraint": "constraints",
        }[op]
        entry_id = _required_id(args)
        entries = _collection(document, collection_name)
        existing = _find(entries, entry_id, kind=collection_name[:-1])
        _expect(existing, expected, label=f"{collection_name}.{entry_id}")
        if existing is None:
            raise ValidationError(
                f"cannot remove absent {collection_name[:-1]}: {entry_id}"
            )
        if op == "remove_component":
            attached = [
                net.get("id")
                for net in _collection(document, "nets")
                if any(
                    endpoint.get("component") == entry_id
                    for endpoint in net.get("endpoints", [])
                )
            ]
            if attached:
                raise ValidationError(
                    f"component {entry_id} is still connected to nets: {', '.join(sorted(attached))}"
                )
            for block in _collection(document, "blocks"):
                if entry_id in block.get("components", []):
                    block["components"].remove(entry_id)
        if op == "remove_block" and any(
            component.get("block_id") == entry_id
            for component in _collection(document, "components")
        ):
            raise ValidationError(f"block {entry_id} still owns components")
        entries.remove(existing)
        return

    if op == "update_component":
        component_id = _required_id(args, "component_id")
        component = _find(
            _collection(document, "components"), component_id, kind="component"
        )
        _expect(component, expected, label=f"components.{component_id}")
        if component is None:
            raise ValidationError(f"component is absent: {component_id}")
        changes = args.get("changes")
        if not isinstance(changes, Mapping) or not changes:
            raise ValidationError("args.changes must be a non-empty object")
        allowed = {
            "reference",
            "part_id",
            "value",
            "block_id",
            "placement",
            "attributes",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValidationError(
                f"update_component fields are unsupported: {', '.join(sorted(unknown))}"
            )
        component.update(copy.deepcopy(dict(changes)))
        return

    if op in {"connect", "disconnect"}:
        net_id = _required_id(args, "net_id")
        net = _find(_collection(document, "nets"), net_id, kind="net")
        _expect(net, expected, label=f"nets.{net_id}")
        if net is None:
            raise ValidationError(f"net is absent: {net_id}")
        endpoint = args.get("endpoint")
        if not isinstance(endpoint, Mapping):
            raise ValidationError("args.endpoint must be an object")
        normalized = {
            "component": _identifier(
                endpoint.get("component"), "args.endpoint.component"
            ),
            "pin": _string(endpoint.get("pin"), "args.endpoint.pin", limit=64),
            "role": _identifier(endpoint.get("role", "signal"), "args.endpoint.role"),
        }
        endpoints = net.get("endpoints")
        if not isinstance(endpoints, list):
            raise ValidationError(f"net endpoints are malformed: {net_id}")
        matching = [
            current
            for current in endpoints
            if current.get("component") == normalized["component"]
            and current.get("pin") == normalized["pin"]
        ]
        if op == "connect":
            if matching:
                if matching[0] == normalized:
                    return  # Operation-level idempotency.
                raise ValidationError(
                    "pin is already connected with a different endpoint role"
                )
            for other in _collection(document, "nets"):
                if other is net:
                    continue
                if any(
                    current.get("component") == normalized["component"]
                    and current.get("pin") == normalized["pin"]
                    for current in other.get("endpoints", [])
                ):
                    raise ValidationError(
                        f"pin is already connected to net {other.get('id')}"
                    )
            endpoints.append(normalized)
        else:
            if not matching:
                raise ValidationError(
                    "cannot disconnect a pin that is not on the target net"
                )
            endpoints.remove(matching[0])
        return

    if op == "rename_net":
        net_id = _required_id(args, "net_id")
        net = _find(_collection(document, "nets"), net_id, kind="net")
        _expect(net, expected, label=f"nets.{net_id}")
        if net is None:
            raise ValidationError(f"net is absent: {net_id}")
        net["name"] = _string(args.get("name"), "args.name", limit=128)
        return

    if op == "upsert_constraint":
        value = args.get("value")
        if not isinstance(value, Mapping):
            raise ValidationError("args.value must be an object")
        constraint_id = _required_id(value)
        entries = _collection(document, "constraints")
        existing = _find(entries, constraint_id, kind="constraint")
        _expect(existing, expected, label=f"constraints.{constraint_id}")
        if existing is None:
            entries.append(copy.deepcopy(dict(value)))
        else:
            existing.clear()
            existing.update(copy.deepcopy(dict(value)))
        return

    if op == "update_board":
        board = document.get("board")
        if not isinstance(board, dict):
            raise ValidationError("design board contract is malformed")
        _expect(board, expected, label="board")
        changes = args.get("changes")
        if not isinstance(changes, Mapping) or not changes:
            raise ValidationError("args.changes must be a non-empty object")
        allowed = set(board)
        unknown = set(changes) - allowed
        if unknown:
            raise ValidationError(f"unknown board fields: {', '.join(sorted(unknown))}")
        board.update(copy.deepcopy(dict(changes)))
        document["scope"]["layers"] = board["layers"]
        return

    if op == "set_metadata":
        metadata = document.get("metadata")
        if not isinstance(metadata, dict):
            raise ValidationError("design metadata is malformed")
        _expect(metadata, expected, label="metadata")
        key = _string(args.get("key"), "args.key", limit=256)
        if key.startswith("_"):
            raise ValidationError("metadata keys beginning with '_' are reserved")
        metadata[key] = _json_value(args.get("value"), "args.value")
        return

    raise ValidationError(f"operation is not implemented: {op}")


def semantic_diff(before: Design, after: Design) -> dict[str, Any]:
    """Return a compact object-level diff without exposing backend file formatting."""
    before_doc, after_doc = before.to_dict(), after.to_dict()
    collections: dict[str, Any] = {}
    for name in (
        "blocks",
        "components",
        "nets",
        "constraints",
        "interfaces",
        "power_domains",
    ):
        old = {entry["id"]: entry for entry in before_doc[name]}
        new = {entry["id"]: entry for entry in after_doc[name]}
        modified: list[dict[str, Any]] = []
        for entry_id in sorted(old.keys() & new.keys()):
            if old[entry_id] != new[entry_id]:
                modified.append(
                    {
                        "id": entry_id,
                        "fields": _field_diff(old[entry_id], new[entry_id]),
                    }
                )
        collections[name] = {
            "added": sorted(new.keys() - old.keys()),
            "removed": sorted(old.keys() - new.keys()),
            "modified": modified,
        }
    board_fields = _field_diff(before_doc["board"], after_doc["board"])
    metadata_fields = _field_diff(before_doc["metadata"], after_doc["metadata"])
    summary = {
        "objects_added": sum(len(value["added"]) for value in collections.values()),
        "objects_removed": sum(len(value["removed"]) for value in collections.values()),
        "objects_modified": sum(
            len(value["modified"]) for value in collections.values()
        ),
        "requires_component_contract_validation": before_doc["components"]
        != after_doc["components"],
        "requires_connectivity_validation": before_doc["nets"] != after_doc["nets"],
        "requires_geometry_validation": (
            before_doc["board"] != after_doc["board"]
            or any(
                entry.get("placement")
                != {current["id"]: current for current in before_doc["components"]}.get(
                    entry["id"], {}
                ).get("placement")
                for entry in after_doc["components"]
            )
        ),
    }
    return {
        "schema": "pcb-agent-semantic-diff",
        "version": 1,
        "before_hash": before.content_hash(),
        "after_hash": after.content_hash(),
        "summary": summary,
        "collections": collections,
        "board_fields": board_fields,
        "metadata_fields": metadata_fields,
    }


def _field_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in sorted(set(before) | set(after)):
        old = before.get(key, {"missing": True})
        new = after.get(key, {"missing": True})
        if old != new:
            result[key] = {"before": old, "after": new}
    return result


def load_change_set_bytes(data: bytes) -> ChangeSet:
    if len(data) > MAX_CHANGE_BYTES:
        raise ValidationError(f"semantic change set exceeds {MAX_CHANGE_BYTES} bytes")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot parse semantic change set: {exc}") from exc
    return ChangeSet.from_dict(value)
