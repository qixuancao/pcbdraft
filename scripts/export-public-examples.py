#!/usr/bin/env python3
"""Create a small, receipt-bound public bundle from a private example run.

The exporter deliberately does not publish the raw run.  It selects one
current managed project per case, verifies the managed-project hashes, and
accepts check/preview evidence only when the producer receipt is complete and
bound to the current design content and revision.  A worker timeout therefore
remains a timeout in the public manifest; it is never turned into a design or
electrical pass.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import struct
import sys
import tempfile
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

RUN_SCHEMA = "pcbdraft-public-examples-run"
COLLECTION_SCHEMA = "pcbdraft-public-examples-collection"
BUNDLE_SCHEMA = "pcbdraft-public-examples-bundle"
CASE_IDS = ("led-3v3-330r", "rc-1k-100nf", "i2c-3v3-pullups")

MAX_PROMPT_BYTES = 256 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_NATIVE_BYTES = 16 * 1024 * 1024
MAX_SVG_BYTES = 8 * 1024 * 1024
MAX_PNG_BYTES = 32 * 1024 * 1024
MAX_PUBLIC_TEXT_BYTES = 16 * 1024 * 1024

_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_FILE_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,255}\Z")
_COMMIT_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_PRIVATE_PATH_RE = re.compile(
    r"(?ix)(?:"
    r"(?<![A-Za-z0-9])/(?:Users|home|mnt|tmp|private/var|var/tmp|var/folders|root)"
    r"/[^\s\"'<>]+"
    r"|(?<![A-Za-z0-9])[A-Z]:[\\/][^\s\"'<>]+"
    r"|(?<![A-Za-z0-9])\\\\[^\\/\s\"'<>]+(?:[\\/][^\s\"'<>]+)+"
    r")"
)
_CREDENTIAL_RE = re.compile(
    r"(?ix)(?:access[_-]?token|refresh[_-]?token|api[_-]?key|"
    r"client[_-]?secret|authorization\s*[:=]|bearer\s+[A-Za-z0-9._-]+|"
    r"password\s*[:=]|private[_-]?key\s*[:=])"
)
_URL_RE = re.compile(r"(?i)\b(?:https?|ftp|file|ws|wss)://[^\s\"'<>]+")
# One reviewed stock KiCad footprint description contains this public link.
# Strip only its display query in the published COPY; record both file hashes.
_STOCK_DESCRIPTION_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1BsfQQcO9C6DZCsRaXUlFlo91Tg2WpOkGARC1WS5S8t0/edit"
)
_SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_XLINK_NAMESPACE = "http://www.w3.org/1999/xlink"
_XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"
_SVG_TAGS = {
    "svg",
    "g",
    "path",
    "line",
    "polyline",
    "polygon",
    "rect",
    "circle",
    "ellipse",
    "text",
    "tspan",
    "defs",
    "use",
    "clipPath",
    "mask",
    "pattern",
    "linearGradient",
    "radialGradient",
    "stop",
    "marker",
    "title",
    "desc",
}
_RULE_KEY_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_PNG_CHUNKS = {b"IHDR", b"sBIT", b"PLTE", b"tRNS", b"IDAT", b"IEND"}

_TEXT_SUFFIXES = {
    ".json",
    ".kicad_pcb",
    ".kicad_pro",
    ".kicad_sch",
    ".svg",
    ".txt",
}
_FORBIDDEN_OUTPUT_NAMES = {
    ".env",
    "auth.json",
    "config.json",
    "credentials.json",
    "runtime",
}


class _FileToCopy:
    __slots__ = ("bytes", "destination", "sha256", "source")

    def __init__(self, source: Path, destination: str, sha256: str, bytes: int) -> None:
        self.source = source
        self.destination = destination
        self.sha256 = sha256
        self.bytes = bytes


class _CollectionError(RuntimeError):
    """An input is not safe or complete enough for allowlisted collection."""


def _absolute(path: Path) -> Path:
    """Make a lexical absolute path without resolving symlinks."""

    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _ensure_no_symlink_parents(path: Path) -> None:
    """Reject a symlink anywhere in a path's existing lexical chain."""

    absolute = _absolute(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            if current.is_symlink():
                raise _CollectionError("source path contains a symlink")
        except OSError as exc:
            raise _CollectionError("source path cannot be inspected") from exc


def _require_directory(path: Path, label: str) -> None:
    _ensure_no_symlink_parents(path)
    if path.is_symlink() or not path.is_dir():
        raise _CollectionError(f"{label} directory is unavailable")


def _require_file(path: Path, label: str) -> None:
    _ensure_no_symlink_parents(path)
    if path.is_symlink() or not path.is_file():
        raise _CollectionError(f"{label} file is unavailable")


def _load_json(path: Path, *, limit: int = MAX_JSON_BYTES) -> dict[str, Any]:
    _require_file(path, "required JSON")
    try:
        if path.stat().st_size > limit:
            raise _CollectionError("JSON input is oversized")
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except _CollectionError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _CollectionError("invalid JSON input") from exc
    if not isinstance(value, dict):
        raise _CollectionError("JSON input must be an object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise _CollectionError(f"{label} is not a SHA-256 digest")
    return value


def _safe_name(value: Any, label: str, suffix: str | None = None) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise _CollectionError(f"{label} is not a safe file name")
    path = PurePosixPath(value)
    if len(path.parts) != 1 or path.parts[0] in {".", ".."}:
        raise _CollectionError(f"{label} escapes its managed directory")
    if ":" in value or "\x00" in value or _FILE_NAME_RE.fullmatch(value) is None:
        raise _CollectionError(f"{label} is not a safe file name")
    if suffix is not None and not value.endswith(suffix):
        raise _CollectionError(f"{label} has the wrong file type")
    return value


def _safe_relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise _CollectionError(f"{label} is not a safe relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise _CollectionError(f"{label} escapes its managed directory")
    if ":" in value:
        raise _CollectionError(f"{label} is not a safe relative path")
    return value


def _required_string(value: Any, label: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise _CollectionError(f"{label} is missing or invalid")
    if "\x00" in value:
        raise _CollectionError(f"{label} contains an invalid character")
    return value


def _optional_string(value: Any, label: str, *, maximum: int = 4096) -> str | None:
    if value is None:
        return None
    return _required_string(value, label, maximum=maximum)


def _required_int(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _CollectionError(f"{label} is missing or invalid")
    if minimum is not None and value < minimum:
        raise _CollectionError(f"{label} is out of range")
    return value


def _finite_number(value: Any, label: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _CollectionError(f"{label} is missing or invalid")
    number = float(value)
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise _CollectionError(f"{label} is out of range")
    return number


def _safe_child(directory: Path, name: Any, label: str) -> Path:
    safe = _safe_name(name, label)
    candidate = directory / safe
    _require_file(candidate, label)
    return candidate


def _file_size(path: Path, limit: int, label: str) -> int:
    _require_file(path, label)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise _CollectionError(f"{label} cannot be inspected") from exc
    if size <= 0:
        raise _CollectionError(f"{label} is empty")
    if size > limit:
        raise _CollectionError(f"{label} is oversized")
    return size


def _check_hash(path: Path, expected: str, label: str) -> int:
    size = _file_size(path, MAX_NATIVE_BYTES, label)
    if _sha256(path) != expected:
        raise _CollectionError(f"{label} changed after collection")
    return size


def _source_file(
    path: Path, destination: str, expected_hash: str, *, limit: int, label: str
) -> _FileToCopy:
    _safe_relative(destination, "public destination")
    size = _file_size(path, limit, label)
    actual = _sha256(path)
    if actual != expected_hash:
        raise _CollectionError(f"{label} changed after collection")
    return _FileToCopy(path, destination, actual, size)


def _private_endpoint(hostname: str | None) -> bool:
    if not hostname:
        return False
    host = hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain", "metadata.google.internal"}:
        return True
    if host.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    )


def _validate_safe_text(value: str, label: str) -> None:
    if _PRIVATE_PATH_RE.search(value):
        raise _CollectionError(f"{label} contains a private path")
    if _CREDENTIAL_RE.search(value):
        raise _CollectionError(f"{label} contains credential material")
    for match in _URL_RE.finditer(value):
        raw_url = match.group(0)
        try:
            parsed = urlsplit(raw_url)
            hostname = parsed.hostname
        except ValueError as exc:
            raise _CollectionError(f"{label} contains an invalid URL") from exc
        if parsed.scheme.lower() == "file":
            raise _CollectionError(f"{label} contains a file URL")
        if parsed.username is not None or parsed.password is not None:
            raise _CollectionError(f"{label} contains URL credentials")
        if _private_endpoint(hostname):
            raise _CollectionError(f"{label} contains a local endpoint")
        if "?" in raw_url:
            raise _CollectionError(f"{label} contains URL query data")


def _svg_name(value: str) -> tuple[str, str]:
    if value.startswith("{") and "}" in value:
        namespace, local = value[1:].split("}", 1)
        return namespace, local
    return "", value


def _validate_svg_xml(text: str) -> None:
    # KiCad emits this standard declaration. Ignore it for local parsing only:
    # never resolve a DTD, and preserve the original receipt-bound SVG bytes.
    text = re.sub(
        r'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1\.1//EN"\s+'
        r'"http://www\.w3\.org/Graphics/SVG/1\.1/DTD/svg11\.dtd">',
        "",
        text,
        count=1,
    )
    if re.search(
        r"(?is)<!\s*(?:doctype|entity)|<!\[|<\?xml-stylesheet|"
        r"<\?(?!xml(?:\s|$))",
        text,
    ):
        raise _CollectionError("SVG preview contains an unsafe XML declaration")
    for match in re.finditer(
        r"(?is)\bxmlns(?::(?P<prefix>[A-Za-z_][A-Za-z0-9_.-]*))?\s*=\s*"
        r"(?P<quote>['\"])(?P<uri>.*?)(?P=quote)",
        text,
    ):
        prefix = match.group("prefix")
        uri = match.group("uri")
        allowed = (
            (prefix in {None, "svg"} and uri == _SVG_NAMESPACE)
            or (prefix == "xlink" and uri == _XLINK_NAMESPACE)
            or (
                prefix == "inkscape"
                and uri == "http://www.inkscape.org/namespaces/inkscape"
            )
        )
        if not allowed:
            raise _CollectionError("SVG preview contains an unsafe XML namespace")
    try:
        root = ET.fromstring(text)  # noqa: S314 - DTD/entity syntax is rejected above
    except ET.ParseError as exc:
        raise _CollectionError("SVG preview is not well-formed XML") from exc
    root_namespace, root_name = _svg_name(root.tag)
    if root_name != "svg" or root_namespace not in {"", _SVG_NAMESPACE}:
        raise _CollectionError("SVG preview has an invalid root element")
    for element in root.iter():
        namespace, name = _svg_name(element.tag)
        if namespace != root_namespace or name not in _SVG_TAGS:
            raise _CollectionError("SVG preview contains a disallowed element")
        if name.lower() in {"script", "foreignobject"}:
            raise _CollectionError("SVG preview contains active content")
        for attribute, attribute_value in element.attrib.items():
            attribute_namespace, attribute_name = _svg_name(attribute)
            lowered_name = attribute_name.lower()
            if lowered_name.startswith("on"):
                raise _CollectionError("SVG preview contains an event attribute")
            if attribute_namespace not in {"", _XLINK_NAMESPACE, _XML_NAMESPACE}:
                raise _CollectionError("SVG preview contains a disallowed attribute")
            lowered_value = attribute_value.lower()
            if "\\" in attribute_value:
                raise _CollectionError("SVG preview contains escaped active content")
            for reference in re.findall(
                r"(?i)url\s*\(\s*['\"]?([^) '\"]+)", attribute_value
            ):
                if not reference.startswith("#"):
                    raise _CollectionError("SVG preview contains an external reference")
            if "javascript:" in lowered_value or lowered_name == "base":
                raise _CollectionError("SVG preview contains an external reference")
            if lowered_name == "href" and not attribute_value.strip().startswith("#"):
                raise _CollectionError("SVG preview contains an external reference")
            if lowered_name == "style" and re.search(
                r"(?i)(?:expression\s*\(|@import|url\s*\()", attribute_value
            ):
                references = re.findall(
                    r"(?i)url\s*\(\s*['\"]?([^)'\"]+)['\"]?\s*\)",
                    attribute_value,
                )
                if any(
                    not reference.strip().startswith("#") for reference in references
                ):
                    raise _CollectionError("SVG preview contains an external reference")
                if re.search(r"(?i)(?:expression\s*\(|@import)", attribute_value):
                    raise _CollectionError("SVG preview contains active CSS")


def _scan_public_text(path: Path) -> None:
    try:
        size = path.stat().st_size
        if size > MAX_PUBLIC_TEXT_BYTES:
            raise _CollectionError("public text asset is oversized")
        text = path.read_text(encoding="utf-8")
    except _CollectionError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise _CollectionError("public text asset is unreadable") from exc
    _validate_safe_text(text, "public text asset")
    if path.suffix.lower() == ".svg":
        _validate_svg_xml(text)


def _png_metadata(path: Path) -> dict[str, int]:
    size = _file_size(path, MAX_PNG_BYTES, "PNG preview")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _CollectionError("PNG preview cannot be read") from exc
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise _CollectionError("PNG preview has an invalid type")
    position = 8
    first = True
    saw_idat = False
    saw_iend = False
    seen: set[bytes] = set()
    metadata: dict[str, int] = {"bytes": size}
    color_type: int | None = None
    bit_depth: int | None = None
    palette_entries = 0
    while position < len(raw):
        if position + 12 > len(raw):
            raise _CollectionError("PNG preview has a truncated chunk")
        length = int.from_bytes(raw[position : position + 4], "big")
        kind = raw[position + 4 : position + 8]
        end = position + 12 + length
        if end > len(raw):
            raise _CollectionError("PNG preview has a truncated payload")
        payload = raw[position + 8 : position + 8 + length]
        expected_crc = int.from_bytes(raw[position + 8 + length : end], "big")
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            raise _CollectionError("PNG preview has an invalid checksum")
        if first and kind != b"IHDR":
            raise _CollectionError("PNG preview is missing IHDR metadata")
        first = False
        if kind not in _PNG_CHUNKS:
            raise _CollectionError("PNG preview contains an unallowlisted chunk")
        if kind != b"IDAT" and kind in seen:
            raise _CollectionError("PNG preview contains a duplicate chunk")
        if kind == b"IHDR":
            if length != 13:
                raise _CollectionError("PNG preview has invalid IHDR metadata")
            (
                width,
                height,
                bit_depth,
                color_type,
                compression,
                filter_method,
                interlace,
            ) = struct.unpack(">IIBBBBB", payload)
            if not 0 < width <= 8192 or not 0 < height <= 8192:
                raise _CollectionError("PNG preview dimensions are out of range")
            if compression != 0 or filter_method != 0 or interlace not in {0, 1}:
                raise _CollectionError("PNG preview has unsupported metadata")
            allowed_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if bit_depth not in allowed_depths.get(color_type, set()):
                raise _CollectionError("PNG preview has invalid color metadata")
            metadata.update(
                {
                    "width": width,
                    "height": height,
                    "bit_depth": bit_depth,
                    "color_type": color_type,
                    "interlace": interlace,
                }
            )
        elif kind == b"sBIT":
            if color_type is None or bit_depth is None or saw_idat:
                raise _CollectionError("PNG preview has misplaced sBIT metadata")
            expected_length = {0: 1, 2: 3, 3: 3, 4: 2, 6: 4}[color_type]
            if length != expected_length or any(
                not 0 < value <= (8 if color_type == 3 else bit_depth)
                for value in payload
            ):
                raise _CollectionError("PNG preview has invalid sBIT metadata")
        elif kind == b"PLTE":
            if color_type is None or saw_idat or color_type not in {2, 3, 6}:
                raise _CollectionError("PNG preview has misplaced palette metadata")
            if length == 0 or length % 3 != 0 or length > 768:
                raise _CollectionError("PNG preview has invalid palette metadata")
            palette_entries = length // 3
            if color_type == 3 and palette_entries > 1 << bit_depth:
                raise _CollectionError("PNG preview has invalid palette metadata")
        elif kind == b"tRNS":
            if color_type is None or saw_idat:
                raise _CollectionError(
                    "PNG preview has misplaced transparency metadata"
                )
            expected_length = {0: 2, 2: 6, 3: None, 4: 0, 6: 0}[color_type]
            if color_type == 3:
                if not 0 < length <= palette_entries:
                    raise _CollectionError(
                        "PNG preview has invalid transparency metadata"
                    )
            elif expected_length != length:
                raise _CollectionError("PNG preview has invalid transparency metadata")
        elif kind == b"IDAT":
            if length == 0:
                raise _CollectionError("PNG preview has an empty image chunk")
            saw_idat = True
        elif kind == b"IEND":
            if length != 0 or not saw_idat or end != len(raw):
                raise _CollectionError("PNG preview has trailing or incomplete data")
            saw_iend = True
        seen.add(kind)
        position = end
        if saw_iend:
            break
    if (
        "width" not in metadata
        or color_type is None
        or color_type == 3
        and palette_entries == 0
        or not saw_idat
        or not saw_iend
    ):
        raise _CollectionError("PNG preview is incomplete")
    return metadata


def _valid_tool_run(tool_run: Any, name: str) -> bool:
    if not isinstance(tool_run, dict) or "failure" not in tool_run:
        return False
    return (
        tool_run.get("name") == name
        and tool_run.get("exit_code") == 0
        and tool_run.get("timed_out") is False
        and tool_run.get("output_limited") is False
        and tool_run["failure"] is None
    )


def _validation_root(project_root: Path) -> Path | None:
    path = project_root / "validation"
    if not path.exists():
        return None
    _require_directory(path, "validation")
    return path


def _incomplete_check(
    check: str, design_hash: str, design_revision: int, reason: str
) -> dict[str, Any]:
    return {
        "check": check,
        "status": "incomplete",
        "outcome": None,
        "passed": None,
        "reported_item_count": None,
        "category_counts": None,
        "ignored_check_keys": [],
        "design_content_hash": design_hash,
        "source_design_revision": design_revision,
        "receipt_sha256": None,
        "report_sha256": None,
        "check_report_sha256": None,
        "normalized_report_sha256": None,
        "raw_report_sha256": None,
        "reason": reason,
    }


def _ignored_check_keys(report: dict[str, Any]) -> list[str]:
    value = report.get("ignored_checks", [])
    if not isinstance(value, list):
        raise _CollectionError("check report ignored-check metadata is incomplete")
    keys: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            raise _CollectionError("check report ignored-check metadata is incomplete")
        key = item.get("key")
        if not isinstance(key, str) or _RULE_KEY_RE.fullmatch(key) is None:
            raise _CollectionError("check report ignored-check key is invalid")
        if key in keys:
            raise _CollectionError("check report ignored-check keys are duplicated")
        keys.append(key)
    return keys


def _strict_report(
    check: str, report: dict[str, Any], schematic_name: str, board_name: str
) -> tuple[int, dict[str, int], list[str]]:
    severities = report.get("included_severities")
    if (
        not isinstance(severities, list)
        or not severities
        or not all(isinstance(item, str) for item in severities)
        or not {"error", "warning"}.issubset(severities)
    ):
        raise _CollectionError("check report has incomplete severity metadata")
    ignored_keys = _ignored_check_keys(report)

    def _count_violations(value: Any) -> int:
        if not isinstance(value, list) or not all(
            isinstance(item, dict) and item.get("severity") in {"error", "warning"}
            for item in value
        ):
            raise _CollectionError("check report violations are incomplete")
        return len(value)

    if check == "run_erc":
        if (
            report.get("source") != schematic_name
            or not isinstance(report.get("sheets"), list)
            or not report["sheets"]
        ):
            raise _CollectionError("ERC report is incomplete")
        count = 0
        for sheet in report["sheets"]:
            if not isinstance(sheet, dict):
                raise _CollectionError("ERC report is incomplete")
            count += _count_violations(sheet.get("violations"))
        return count, {"violations": count}, ignored_keys
    if report.get("source") != board_name:
        raise _CollectionError("DRC report is incomplete")
    counts: dict[str, int] = {}
    for key in ("violations", "unconnected_items", "schematic_parity"):
        value = report.get(key)
        counts[key] = _count_violations(value)
    return sum(counts.values()), counts, ignored_keys


def _strict_check(
    check_dir: Path,
    receipt: dict[str, Any],
    check: str,
    design_hash: str,
    design_revision: int,
    schematic_name: str,
    board_name: str,
) -> dict[str, Any]:
    if (
        receipt.get("schema") != "pcbdraft-individual-check-receipt"
        or receipt.get("version") != 1
        or receipt.get("check") != check
        or receipt.get("status") != "complete"
        or receipt.get("state") != "completed"
        or receipt.get("outcome") not in {"pass", "fail"}
        or receipt.get("design_content_hash") != design_hash
        or receipt.get("source_design_revision") != design_revision
        or not isinstance(receipt.get("source_revision"), int)
        or receipt.get("source_revision") < 0
        or receipt.get("report") != "check.json"
    ):
        raise _CollectionError("check receipt is incomplete or not current")
    report_hash = _digest(receipt.get("report_sha256"), "check report hash")
    check_report_path = check_dir / "check.json"
    _require_file(check_report_path, "check report")
    receipt_path = check_dir / "receipt.json"
    _require_file(receipt_path, "check receipt")
    if _sha256(check_report_path) != report_hash:
        raise _CollectionError("check report hash is not valid")
    check_report = _load_json(check_report_path)
    if (
        check_report.get("schema") != "pcbdraft-individual-check"
        or check_report.get("version") != 1
        or check_report.get("check") != check
        or check_report.get("state") != "completed"
        or check_report.get("outcome") != receipt.get("outcome")
        or check_report.get("design_content_hash") != design_hash
    ):
        raise _CollectionError("check report is incomplete or not current")
    details = check_report.get("details")
    tool_run = check_report.get("tool_run")
    if (
        not isinstance(details, dict)
        or "failure" not in details
        or not isinstance(details.get("violations"), list)
        or not isinstance(tool_run, dict)
        or tool_run.get("status") != "completed"
        or "failure" not in tool_run
    ):
        raise _CollectionError("check report is incomplete")
    raw_name = "erc.raw.json" if check == "run_erc" else "drc.raw.json"
    normalized_name = "erc.json" if check == "run_erc" else "drc.json"
    if (
        tool_run.get("raw_report") != raw_name
        or tool_run.get("report") != normalized_name
    ):
        raise _CollectionError("check report raw-report binding is incomplete")
    if not all(isinstance(item, dict) for item in details["violations"]):
        raise _CollectionError("check report violations are incomplete")
    if receipt["outcome"] == "pass" and (
        details["failure"] is not None or tool_run["failure"] is not None
    ):
        raise _CollectionError("pass check contains explicit failure evidence")
    raw_report_path = check_dir / raw_name
    normalized_report_path = check_dir / normalized_name
    _require_file(raw_report_path, "raw check report")
    _require_file(normalized_report_path, "normalized check report")
    raw_report = _load_json(raw_report_path)
    normalized_report = _load_json(normalized_report_path)
    expected_schema = f"https://schemas.kicad.org/{check[4:]}.v1.json"
    if (
        raw_report.get("$schema") != expected_schema
        or normalized_report.get("$schema") != expected_schema
    ):
        raise _CollectionError("check report schema is incomplete")
    raw_count, raw_category_counts, raw_ignored_keys = _strict_report(
        check, raw_report, schematic_name, board_name
    )
    normalized_count, normalized_category_counts, normalized_ignored_keys = (
        _strict_report(check, normalized_report, schematic_name, board_name)
    )
    if (
        raw_count != normalized_count
        or raw_category_counts != normalized_category_counts
        or raw_ignored_keys != normalized_ignored_keys
        or len(details["violations"]) != normalized_count
    ):
        raise _CollectionError("check report violation count is inconsistent")
    raw_report_hash = _sha256(raw_report_path)
    normalized_report_hash = _sha256(normalized_report_path)
    receipt_hash = _sha256(receipt_path)
    hashes = {
        "receipt_sha256": receipt_hash,
        "report_sha256": report_hash,
        "check_report_sha256": report_hash,
        "normalized_report_sha256": normalized_report_hash,
        "raw_report_sha256": raw_report_hash,
    }
    outcome = receipt["outcome"]
    if outcome == "pass" and normalized_count != 0:
        return {
            "check": check,
            "status": "inconsistent",
            "outcome": outcome,
            "passed": None,
            "reported_item_count": normalized_count,
            "category_counts": normalized_category_counts,
            "ignored_check_keys": normalized_ignored_keys,
            "design_content_hash": design_hash,
            "source_design_revision": design_revision,
            "receipt_source_revision": receipt["source_revision"],
            **hashes,
            "reason": "pass_receipt_has_reported_items",
        }
    if (
        outcome == "fail"
        and normalized_count == 0
        and details["failure"] is None
        and tool_run["failure"] is None
    ):
        return {
            "check": check,
            "status": "inconsistent",
            "outcome": outcome,
            "passed": None,
            "reported_item_count": 0,
            "category_counts": normalized_category_counts,
            "ignored_check_keys": normalized_ignored_keys,
            "design_content_hash": design_hash,
            "source_design_revision": design_revision,
            "receipt_source_revision": receipt["source_revision"],
            **hashes,
            "reason": "fail_receipt_has_no_failure_or_reported_items",
        }
    return {
        "check": check,
        "status": "complete",
        "outcome": outcome,
        "passed": outcome == "pass" and normalized_count == 0,
        "reported_item_count": normalized_count,
        "category_counts": normalized_category_counts,
        "ignored_check_keys": normalized_ignored_keys,
        "design_content_hash": design_hash,
        "source_design_revision": design_revision,
        "receipt_source_revision": receipt["source_revision"],
        **hashes,
    }


def _collect_check(
    project_root: Path,
    check: str,
    design_hash: str,
    design_revision: int,
    schematic_name: str,
    board_name: str,
) -> dict[str, Any]:
    validation = _validation_root(project_root)
    if validation is None:
        return _incomplete_check(
            check, design_hash, design_revision, "no_validation_receipt"
        )
    current: list[tuple[int, str, Path, dict[str, Any]]] = []
    hash_bound_incomplete = False
    for candidate in sorted(validation.iterdir(), key=lambda item: item.name):
        if candidate.is_symlink():
            raise _CollectionError("validation contains a symlink")
        if not candidate.is_dir():
            continue
        receipt_path = candidate / "receipt.json"
        if not receipt_path.exists():
            continue
        _require_file(receipt_path, "check receipt")
        try:
            receipt = _load_json(receipt_path)
        except _CollectionError:
            continue
        if (
            receipt.get("check") != check
            or receipt.get("design_content_hash") != design_hash
        ):
            continue
        source_revision = receipt.get("source_design_revision")
        if source_revision != design_revision:
            continue
        try:
            parsed_source = source_revision if isinstance(source_revision, int) else -1
            strict = _strict_check(
                candidate,
                receipt,
                check,
                design_hash,
                design_revision,
                schematic_name,
                board_name,
            )
        except _CollectionError:
            hash_bound_incomplete = True
            continue
        current.append((parsed_source, candidate.name, candidate, strict))
    if not current:
        reason = (
            "current_receipt_incomplete"
            if hash_bound_incomplete
            else "no_receipt_bound_to_current_design"
        )
        return _incomplete_check(check, design_hash, design_revision, reason)
    highest = max(item[0] for item in current)
    selected = [item for item in current if item[0] == highest]
    if len(selected) != 1:
        return _incomplete_check(
            check, design_hash, design_revision, "ambiguous_current_receipt"
        )
    return dict(selected[0][3])


def _preview_root(project_root: Path, receipt_relative: str) -> tuple[Path, str]:
    path = PurePosixPath(receipt_relative)
    if (
        len(path.parts) != 3
        or path.parts[0] != "previews"
        or path.parts[2] != "receipt.json"
    ):
        raise _CollectionError("preview receipt path is not allowlisted")
    run_id = _safe_name(path.parts[1], "preview run id")
    root = project_root / "previews" / run_id
    _require_directory(root, "preview")
    receipt_path = root / "receipt.json"
    _require_file(receipt_path, "preview receipt")
    return root, run_id


def _preview_file_meta(
    root: Path, value: Any, expected_name: str, limit: int, label: str
) -> tuple[Path, dict[str, Any]]:
    if not isinstance(value, dict) or value.get("path") != expected_name:
        raise _CollectionError(f"{label} metadata is incomplete")
    expected_bytes = _required_int(value.get("bytes"), f"{label} bytes", minimum=1)
    expected_hash = _digest(value.get("sha256"), f"{label} hash")
    path = _safe_child(root, value.get("path"), label)
    actual_bytes = _file_size(path, limit, label)
    if actual_bytes != expected_bytes or _sha256(path) != expected_hash:
        raise _CollectionError(f"{label} changed after collection")
    return path, {"bytes": actual_bytes, "sha256": expected_hash}


def _collect_preview(
    project_root: Path, project: dict[str, Any], design_hash: str, design_revision: int
) -> tuple[dict[str, Any], list[_FileToCopy]]:
    pointer = project.get("last_preview")
    if pointer is None:
        return (
            {
                "status": "unavailable",
                "files": [],
                "reason": "no_current_preview",
            },
            [],
        )
    if not isinstance(pointer, dict):
        return (
            {
                "status": "incomplete",
                "files": [],
                "reason": "current_preview_pointer_invalid",
            },
            [],
        )
    if (
        pointer.get("design_content_hash") != design_hash
        or pointer.get("source_design_revision") != design_revision
        or not isinstance(pointer.get("receipt"), str)
        or not isinstance(pointer.get("files"), dict)
    ):
        return (
            {
                "status": "unavailable",
                "files": [],
                "reason": "no_preview_bound_to_current_design",
            },
            [],
        )
    try:
        receipt_relative = _safe_relative(pointer["receipt"], "preview receipt")
        root, run_id = _preview_root(project_root, receipt_relative)
        expected_receipt = f"previews/{run_id}/receipt.json"
        if receipt_relative != expected_receipt:
            raise _CollectionError("preview receipt path is not current")
        if pointer.get("root") != f"previews/{run_id}":
            raise _CollectionError("preview root binding is incomplete")
        pointer_files = pointer["files"]
        if (
            pointer_files.get("board_svg") != f"previews/{run_id}/board.svg"
            or pointer_files.get("board_render") != f"previews/{run_id}/board-top.png"
        ):
            raise _CollectionError("preview file binding is incomplete")
        receipt_path = root / "receipt.json"
        receipt = _load_json(receipt_path)
        if (
            receipt.get("schema") != "pcbdraft-preview-bundle"
            or receipt.get("version") != 1
            or receipt.get("design_content_hash") != design_hash
            or not isinstance(receipt.get("files"), dict)
            or not isinstance(receipt.get("renders"), list)
            or "render_board" not in receipt["renders"]
            or not isinstance(receipt.get("tool_runs"), list)
        ):
            raise _CollectionError("preview receipt is incomplete or not current")
        svg_path, svg_meta = _preview_file_meta(
            root,
            receipt["files"].get("board_svg"),
            "board.svg",
            MAX_SVG_BYTES,
            "SVG preview",
        )
        tool_runs = receipt["tool_runs"]
        svg_run = next(
            (
                item
                for item in tool_runs
                if isinstance(item, dict) and item.get("name") == "board_svg"
            ),
            None,
        )
        if not _valid_tool_run(svg_run, "board_svg"):
            raise _CollectionError("SVG preview tool receipt is incomplete")
        files = [
            {
                "path": "previews/board.svg",
                "kind": "board_svg",
                **svg_meta,
            }
        ]
        copy_files = [
            _source_file(
                svg_path,
                "previews/board.svg",
                svg_meta["sha256"],
                limit=MAX_SVG_BYTES,
                label="SVG preview",
            )
        ]
        png_status = "included"
        try:
            png_path, png_meta = _preview_file_meta(
                root,
                receipt["files"].get("board_render"),
                "board-top.png",
                MAX_PNG_BYTES,
                "PNG preview",
            )
            png_metadata = _png_metadata(png_path)
            png_run = next(
                (
                    item
                    for item in tool_runs
                    if isinstance(item, dict) and item.get("name") == "board_render"
                ),
                None,
            )
            if not _valid_tool_run(png_run, "board_render"):
                raise _CollectionError("PNG preview tool receipt is incomplete")
            files.append(
                {
                    "path": "previews/board-top.png",
                    "kind": "board_render",
                    **png_meta,
                    "metadata": png_metadata,
                }
            )
            copy_files.append(
                _source_file(
                    png_path,
                    "previews/board-top.png",
                    png_meta["sha256"],
                    limit=MAX_PNG_BYTES,
                    label="PNG preview",
                )
            )
        except _CollectionError:
            png_status = "unavailable"
        return (
            {
                "status": "included" if png_status == "included" else "partial",
                "run_id": run_id,
                "design_content_hash": design_hash,
                "source_design_revision": design_revision,
                "files": files,
                "png_status": png_status,
                "receipt_sha256": _sha256(receipt_path),
            },
            copy_files,
        )
    except _CollectionError as exc:
        reason = str(exc)
        if reason not in {
            "preview receipt path is not allowlisted",
            "preview root binding is incomplete",
            "preview file binding is incomplete",
        }:
            reason = "current_preview_receipt_incomplete"
        return (
            {
                "status": "incomplete",
                "files": [],
                "reason": reason,
            },
            [],
        )


def _native_project(
    project_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], list[_FileToCopy], str, str]:
    project_json_path = project_root / "project.json"
    project = _load_json(project_json_path)
    if (
        project.get("schema") != "pcbdraft-application-project"
        or project.get("version") != 1
    ):
        raise _CollectionError("managed project metadata is invalid")
    project_id = _required_string(project.get("id"), "project id", maximum=128)
    if _ID_RE.fullmatch(project_id) is None or project_id != project_root.name:
        raise _CollectionError("managed project id is invalid")
    design_revision = _required_int(
        project.get("design_revision"), "design revision", minimum=1
    )
    design_root = project_root / "design"
    _require_directory(design_root, "design")
    managed_path = design_root / "project.pcbdraft.json"
    managed = _load_json(managed_path)
    if (
        managed.get("schema") != "pcbdraft-managed-project"
        or managed.get("version") != 1
    ):
        raise _CollectionError("managed project manifest is invalid")
    sync = managed.get("sync")
    if not isinstance(sync, dict) or sync.get("state") != "synchronized":
        raise _CollectionError("managed project is not synchronized")
    design = managed.get("design")
    if not isinstance(design, dict) or design.get("id") != project_id:
        raise _CollectionError("managed design identity is invalid")
    design_hash = _digest(design.get("content_hash"), "design content hash")
    files = managed.get("files")
    hashes = managed.get("hashes")
    if not isinstance(files, dict) or not isinstance(hashes, dict):
        raise _CollectionError("managed project file hashes are missing")
    required = {
        "kicad_project": ".kicad_pro",
        "schematic": ".kicad_sch",
        "board": ".kicad_pcb",
    }
    native_copies: list[_FileToCopy] = []
    native_names: dict[str, str] = {}
    for key, suffix in required.items():
        name = _safe_name(files.get(key), f"managed {key}", suffix)
        expected = _digest(hashes.get(key), f"managed {key} hash")
        source = _safe_child(design_root, name, f"managed {key}")
        _check_hash(source, expected, f"managed {key}")
        native_names[key] = name
        native_copies.append(
            _source_file(
                source,
                f"native/{name}",
                expected,
                limit=MAX_NATIVE_BYTES,
                label=f"managed {key}",
            )
        )
    # Verify every declared managed-project hash, including auxiliary evidence,
    # without publishing any of those auxiliary files.
    for key, expected_value in hashes.items():
        name = _safe_name(files.get(key), f"managed {key}")
        expected = _digest(expected_value, f"managed {key} hash")
        source = _safe_child(design_root, name, f"managed {key}")
        _check_hash(source, expected, f"managed {key}")
    if len(set(native_names.values())) != 3:
        raise _CollectionError("managed native files are not unique")
    if managed.get("design", {}).get("content_hash") != hashes.get("ir"):
        raise _CollectionError("managed design hash is not bound to its IR")
    records = [
        {
            "path": f"native/{native_names[key]}",
            "kind": key,
            "bytes": item.bytes,
            "sha256": item.sha256,
        }
        for key, item in zip(required, native_copies, strict=True)
    ]
    return (
        project,
        {
            "project_id": project_id,
            "design_content_hash": design_hash,
            "design_revision": design_revision,
            "native_artifacts": records,
        },
        native_copies,
        native_names["schematic"],
        native_names["board"],
    )


def _usage_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {
            "present": False,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "estimated_cost_usd": None,
            "cost_status": "unknown",
        }
    result: dict[str, Any] = {"present": bool(value.get("present"))}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        token_value = value.get(key)
        if token_value is not None:
            result[key] = _required_int(token_value, f"usage {key}", minimum=0)
        else:
            result[key] = None
    cost = value.get("estimated_cost_usd")
    result["estimated_cost_usd"] = (
        _finite_number(cost, "usage estimated cost", minimum=0)
        if cost is not None
        else None
    )
    cost_status = value.get("cost_status") or "unknown"
    result["cost_status"] = _required_string(
        cost_status, "usage cost status", maximum=64
    )
    return result


def _worker_status(entry: dict[str, Any]) -> str:
    if entry["timed_out"]:
        return "timed_out"
    if entry["returncode"] == 0:
        return "completed"
    if entry["returncode"] is None:
        return "aborted"
    return "failed"


def _electrical_status(checks: dict[str, dict[str, Any]]) -> str:
    if all(item.get("status") == "complete" for item in checks.values()):
        if all(item.get("passed") is True for item in checks.values()):
            return "passed"
        if any(item.get("outcome") == "fail" for item in checks.values()):
            return "failed"
        return "inconsistent"
    return "incomplete"


def _raw_inventory_counts(entry: dict[str, Any]) -> tuple[int | None, int | None]:
    artifacts = entry.get("artifacts")
    if not isinstance(artifacts, dict):
        return None, None
    project_count = artifacts.get("project_count")
    if isinstance(project_count, bool) or not isinstance(project_count, int):
        project_count = None
    native = artifacts.get("native_artifacts")
    native_count = len(native) if isinstance(native, list) else None
    return project_count, native_count


def _select_project_root(case_root: Path) -> tuple[Path, str]:
    """Select the sole managed project from one known isolated layout."""

    roots_with_projects: list[tuple[Path, str, list[Path]]] = []
    for relative_root in ("application/projects", "repository/projects"):
        project_parent = case_root / relative_root
        _ensure_no_symlink_parents(project_parent)
        if project_parent.is_symlink():
            raise _CollectionError("known project layout contains a symlink")
        if not project_parent.exists():
            continue
        _require_directory(project_parent, relative_root)
        projects: list[Path] = []
        for candidate in sorted(project_parent.iterdir(), key=lambda item: item.name):
            if candidate.is_symlink():
                raise _CollectionError(f"{relative_root} contains a symlink")
            if candidate.is_dir():
                projects.append(candidate)
        if projects:
            roots_with_projects.append((project_parent, relative_root, projects))
    if len(roots_with_projects) > 1:
        raise _CollectionError("both known project layouts contain managed projects")
    if not roots_with_projects:
        raise _CollectionError("case does not contain a managed project layout")
    _project_parent, relative_root, projects = roots_with_projects[0]
    if len(projects) != 1:
        raise _CollectionError(
            f"{relative_root} does not contain exactly one managed project"
        )
    return projects[0], relative_root


def _collect_case(
    run_root: Path, entry: dict[str, Any], case_id: str
) -> tuple[dict[str, Any], list[_FileToCopy], dict[str, Any]]:
    prompt = _required_string(entry.get("prompt"), "prompt", maximum=MAX_PROMPT_BYTES)
    prompt_hash = _digest(entry.get("prompt_sha256"), "prompt hash")
    if hashlib.sha256(prompt.encode("utf-8")).hexdigest() != prompt_hash:
        raise _CollectionError("prompt changed after collection")
    for key in ("started_at", "completed_at"):
        _required_string(entry.get(key), key, maximum=128)
    elapsed = _finite_number(entry.get("elapsed_seconds"), "elapsed time", minimum=0)
    if not isinstance(entry.get("timed_out"), bool):
        raise _CollectionError("timeout status is invalid")
    returncode = entry.get("returncode")
    if returncode is not None:
        _required_int(returncode, "worker return code")
    for key in ("provider", "model", "package_version"):
        _optional_string(entry.get(key), key, maximum=256)
    case_root = run_root / case_id
    _require_directory(case_root, "case")
    project_root, collection_root = _select_project_root(case_root)
    project, design_record, native_files, schematic_name, board_name = _native_project(
        project_root
    )
    checks = {
        "erc": _collect_check(
            project_root,
            "run_erc",
            design_record["design_content_hash"],
            design_record["design_revision"],
            schematic_name,
            board_name,
        ),
        "drc": _collect_check(
            project_root,
            "run_drc",
            design_record["design_content_hash"],
            design_record["design_revision"],
            schematic_name,
            board_name,
        ),
    }
    preview, preview_files = _collect_preview(
        project_root,
        project,
        design_record["design_content_hash"],
        design_record["design_revision"],
    )
    worker_status = _worker_status(entry)
    electrical_status = _electrical_status(checks)
    limitations = [
        "The exporter does not assert design correctness, fabrication readiness, or production readiness.",
        "Hardware power-on and human engineering sign-off were not established by this bundle.",
    ]
    if worker_status != "completed":
        limitations.insert(
            0,
            "The worker attempt did not complete successfully; native files are retained as partial attempt evidence.",
        )
    if electrical_status != "passed":
        limitations.insert(
            1,
            "Current ERC/DRC evidence is incomplete or failed; no electrical pass is asserted.",
        )
    if preview["status"] not in {"included", "partial"}:
        limitations.append("No current receipt-bound board preview was published.")
    public = {
        "case_id": case_id,
        "prompt": prompt,
        "prompt_sha256": prompt_hash,
        "worker": {
            "status": worker_status,
            "returncode": returncode,
            "timed_out": entry["timed_out"],
            "started_at": entry["started_at"],
            "completed_at": entry["completed_at"],
            "elapsed_seconds": elapsed,
            "provider": entry.get("provider"),
            "model": entry.get("model"),
            "package_version": entry.get("package_version"),
            "source_result_sha256": entry["_source_result_sha256"],
            "usage": _usage_summary(entry.get("usage")),
        },
        "design_assessment": "not_asserted_by_exporter",
        "design": {
            **design_record,
            "status": "native_artifacts_collected",
            "production_ready": False,
            "hardware_tested": False,
        },
        "checks": {
            "erc": checks["erc"],
            "drc": checks["drc"],
            "electrical_status": electrical_status,
        },
        "preview": preview,
        "failure_evidence": {
            "worker_timeout": entry["timed_out"],
            "worker_failed": worker_status in {"failed", "aborted"},
            "raw_trace_published": False,
        },
        "limitations": limitations,
    }
    raw_project_count, raw_native_count = _raw_inventory_counts(entry)
    correction_applied = (raw_project_count is not None and raw_project_count != 1) or (
        raw_native_count is not None and raw_native_count != len(native_files)
    )
    counts_complete = raw_project_count is not None and raw_native_count is not None
    if correction_applied:
        correction_status = "applied"
        correction_reason = (
            "runner_inventory_missed_application_project"
            if collection_root == "application/projects"
            else "runner_inventory_count_disagreed_with_selected_project"
        )
    elif counts_complete:
        correction_status = "not_required"
        correction_reason = None
    else:
        correction_status = "not_determined"
        correction_reason = None
    collection_record = {
        "case_id": case_id,
        "project_id": design_record["project_id"],
        "collection_root": collection_root,
        "source_result_sha256": entry["_source_result_sha256"],
        "runner_reported_project_count": raw_project_count,
        "runner_reported_native_artifact_count": raw_native_count,
        "corrected_project_count": 1,
        "corrected_native_artifact_count": len(native_files),
        "correction_applied": correction_applied,
        "correction_status": correction_status,
        "correction_reason": correction_reason,
        "current_preview_bound": preview["status"] in {"included", "partial"},
        "check_status": {
            "erc": checks["erc"]["status"],
            "drc": checks["drc"]["status"],
        },
    }
    return public, [*native_files, *preview_files], collection_record


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sanitize_native_description(data: bytes) -> bytes:
    """Remove a reviewed stock display query only inside a PCB description."""

    lines = data.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.lstrip().startswith(b'(descr "'):
            lines[index] = line.replace(
                (_STOCK_DESCRIPTION_URL + "?usp=sharing)").encode(),
                (_STOCK_DESCRIPTION_URL + ")").encode(),
            )
    return b"".join(lines)


def _copy_file(file: _FileToCopy, destination_root: Path) -> dict[str, Any]:
    destination = destination_root / file.destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    _ensure_no_symlink_parents(destination.parent)
    _require_file(file.source, "selected artifact")
    if (
        _file_size(
            file.source, max(MAX_NATIVE_BYTES, MAX_PNG_BYTES), "selected artifact"
        )
        != file.bytes
    ):
        raise _CollectionError("selected artifact changed after collection")
    if _sha256(file.source) != file.sha256:
        raise _CollectionError("selected artifact changed after collection")
    shutil.copyfile(file.source, destination)
    if _sha256(destination) != file.sha256:
        raise _CollectionError("selected artifact could not be copied safely")
    metadata: dict[str, Any] = {
        "source_sha256": file.sha256,
        "source_bytes": file.bytes,
        "sha256": file.sha256,
        "bytes": file.bytes,
    }
    if destination.suffix == ".kicad_pcb":
        original = destination.read_bytes()
        published = _sanitize_native_description(original)
        if published != original:
            destination.write_bytes(published)
            metadata.update(
                sha256=_sha256(destination),
                bytes=len(published),
                sanitization="removed_stock_footprint_description_display_query",
                check_evidence_applies_to="original_source_bytes_not_rechecked_public_copy",
            )
    return metadata


def _scan_public_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise _CollectionError("public bundle contains a symlink")
        if path.is_dir():
            continue
        if path.name.lower() in _FORBIDDEN_OUTPUT_NAMES or "trace" in path.name.lower():
            raise _CollectionError("public bundle contains a private artifact")
        if path.suffix.lower() in _TEXT_SUFFIXES:
            _scan_public_text(path)


def _manifest_case_entry(manifest: dict[str, Any], case_id: str) -> dict[str, Any]:
    cases = manifest.get("cases")
    if not isinstance(cases, list):
        raise _CollectionError("run manifest cases are missing")
    matches = [
        item
        for item in cases
        if isinstance(item, dict) and item.get("case_id") == case_id
    ]
    if len(matches) != 1:
        raise _CollectionError("run manifest does not contain exactly one case")
    return matches[0]


def _load_run(run_root: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
    _require_directory(run_root, "run root")
    manifest_path = run_root / "manifest.json"
    manifest = _load_json(manifest_path)
    if (
        manifest.get("schema") != RUN_SCHEMA
        or manifest.get("version") != 1
        or not manifest.get("completed_at")
    ):
        raise _CollectionError("run is not complete")
    source = manifest.get("source")
    if (
        not isinstance(source, dict)
        or _COMMIT_RE.fullmatch(str(source.get("commit", ""))) is None
    ):
        raise _CollectionError("run source identity is invalid")
    environment = manifest.get("environment")
    if not isinstance(environment, dict):
        raise _CollectionError("run environment metadata is invalid")
    for key in ("os", "python", "kicad"):
        _required_string(environment.get(key), f"environment {key}", maximum=512)
    limits = manifest.get("limits")
    if not isinstance(limits, dict) or limits.get("attempts_per_case") != 1:
        raise _CollectionError("run attempt limit is invalid")
    case_map: dict[str, dict[str, Any]] = {}
    for case_id in CASE_IDS:
        entry = _manifest_case_entry(manifest, case_id)
        result_path = run_root / case_id / "result.json"
        result = _load_json(result_path)
        if result.get("case_id") != case_id:
            raise _CollectionError("case result identity is invalid")
        if result != entry:
            raise _CollectionError("case result metadata changed after collection")
        case_map[case_id] = {
            **entry,
            "_source_result_sha256": _sha256(result_path),
        }
    if len(manifest.get("cases", [])) != len(CASE_IDS):
        raise _CollectionError("run contains an unexpected case")
    return manifest, case_map, _sha256(manifest_path)


def _collection_receipt(
    manifest: dict[str, Any], manifest_hash: str, records: list[dict[str, Any]]
) -> dict[str, Any]:
    roots = sorted({record["collection_root"] for record in records})
    corrected = [record for record in records if record["correction_applied"]]
    undetermined = [
        record for record in records if record["correction_status"] == "not_determined"
    ]
    corrected_roots = sorted({record["collection_root"] for record in corrected})
    if corrected:
        description = (
            "The preserved runner inventory disagreed with the selected managed "
            "project counts; the correction is recorded per case without mutating "
            "the source evidence."
        )
    elif undetermined:
        description = (
            "The selected managed project was collected, but the preserved runner "
            "inventory was incomplete; no discrepancy correction is claimed."
        )
    else:
        description = "No runner inventory discrepancy was corrected."
    return {
        "schema": COLLECTION_SCHEMA,
        "version": 1,
        "source_run_manifest_sha256": manifest_hash,
        "source_layout": {
            "runner_inventory_root": "repository/projects",
            "selected_collection_roots": roots,
            "corrected_collection_roots": corrected_roots,
            "runner_inventory_authoritative": False,
            "raw_attempt_evidence_mutated": False,
        },
        "discrepancy": {
            "correction_applied": bool(corrected),
            "application_home_project_collection_corrected": any(
                record["collection_root"] == "application/projects"
                for record in corrected
            ),
            "raw_runner_inventory_preserved": True,
            "cases_corrected": [record["case_id"] for record in corrected],
            "cases_not_determined": [record["case_id"] for record in undetermined],
            "description": description,
        },
        "source_commit": manifest["source"]["commit"],
        "cases": records,
    }


def _bundle_manifest(
    manifest: dict[str, Any],
    manifest_hash: str,
    cases: list[dict[str, Any]],
    reviewer_kind: str,
    human_engineering_review: bool,
) -> dict[str, Any]:
    environment = manifest["environment"]
    limits = manifest["limits"]
    return {
        "schema": BUNDLE_SCHEMA,
        "version": 1,
        "publication_status": "reviewed_allowlisted_bundle",
        "source": {
            "run_manifest_sha256": manifest_hash,
            "commit": manifest["source"]["commit"],
            "dirty": bool(manifest["source"].get("dirty")),
            "environment": {
                "os": environment["os"],
                "python": environment["python"],
                "kicad": environment["kicad"],
            },
            "limits": {
                "attempts_per_case": limits["attempts_per_case"],
                "pcb_tool_calls_per_case": limits.get("pcb_tool_calls_per_case"),
                "wall_timeout_seconds_per_case": limits.get(
                    "wall_timeout_seconds_per_case"
                ),
            },
        },
        "review": {
            "content_reviewed": True,
            "reviewer_kind": reviewer_kind,
            "human_engineering_review": human_engineering_review,
        },
        "claims": {
            "production_ready": False,
            "hardware_tested": False,
            "human_engineering_review": human_engineering_review,
            "benchmark": False,
        },
        "collection_receipt": "collection.json",
        "cases": cases,
        "evidence_boundary": {
            "raw_trace_published": False,
            "raw_terminal_output_published": False,
            "runtime_published": False,
            "credentials_published": False,
            "arbitrary_project_metadata_published": False,
            "raw_run_manifest_published": False,
            "terminal_capture_is_interactive_recording": False,
        },
    }


def create_bundle(
    run_root: Path,
    output: Path,
    *,
    reviewer_kind: str = "automated_assistant",
    human_engineering_review: bool = False,
) -> Path:
    """Collect a reviewed run into a fresh allowlisted directory.

    ``run_root`` is read-only from this function's point of view.  The output
    must not already exist and is assembled in a sibling temporary directory
    before being atomically moved into place.
    """

    if reviewer_kind not in {"automated_assistant", "human_operator"}:
        raise _CollectionError("reviewer kind is invalid")
    if not isinstance(human_engineering_review, bool):
        raise _CollectionError("human engineering review flag is invalid")
    if human_engineering_review and reviewer_kind != "human_operator":
        raise _CollectionError("automated review cannot claim human engineering review")
    run_root = _absolute(run_root)
    output = _absolute(output)
    if (
        output == run_root
        or output.is_relative_to(run_root)
        or run_root.is_relative_to(output)
    ):
        raise _CollectionError("output must be outside the private run")
    _ensure_no_symlink_parents(output.parent)
    if output.exists() or output.is_symlink():
        raise _CollectionError("output already exists")
    manifest_path = run_root / "manifest.json"
    manifest, entries, manifest_hash = _load_run(run_root)
    public_cases: list[dict[str, Any]] = []
    collection_records: list[dict[str, Any]] = []
    selected_files: dict[str, list[_FileToCopy]] = {}
    for case_id in CASE_IDS:
        public, files, collection_record = _collect_case(
            run_root, entries[case_id], case_id
        )
        public_cases.append(public)
        collection_records.append(collection_record)
        selected_files[case_id] = files
    collection = _collection_receipt(manifest, manifest_hash, collection_records)
    bundle_manifest = _bundle_manifest(
        manifest,
        manifest_hash,
        public_cases,
        reviewer_kind,
        human_engineering_review,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    _ensure_no_symlink_parents(output.parent)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    try:
        for public in public_cases:
            case_id = public["case_id"]
            case_dir = temporary / "cases" / case_id
            case_dir.mkdir(parents=True, exist_ok=True)
            (case_dir / "prompt.txt").write_text(
                public["prompt"] + "\n", encoding="utf-8"
            )
            for file in selected_files[case_id]:
                copy_metadata = _copy_file(file, case_dir)
                for artifact in public["design"]["native_artifacts"]:
                    if artifact["path"] == file.destination:
                        artifact.update(copy_metadata)
                        if "sanitization" in copy_metadata:
                            public["limitations"].append(
                                "A stock footprint description URL query was removed "
                                "from the public PCB copy. Checks apply to the "
                                "original source bytes; both hashes are recorded. "
                                "The public copy was not rechecked."
                            )
            checks_dir = case_dir / "checks"
            checks_dir.mkdir(parents=True, exist_ok=True)
            _write_json(checks_dir / "erc.json", public["checks"]["erc"])
            _write_json(checks_dir / "drc.json", public["checks"]["drc"])
        _write_json(temporary / "collection.json", collection)
        _write_json(temporary / "manifest.json", bundle_manifest)
        _scan_public_tree(temporary)
        if _sha256(manifest_path) != manifest_hash:
            raise _CollectionError("run manifest changed during collection")
        if output.exists() or output.is_symlink():
            raise _CollectionError("output was created concurrently")
        os.replace(temporary, output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--content-reviewed",
        "--reviewed",
        dest="content_reviewed",
        action="store_true",
        help="confirm publication-content review; this is not engineering review",
    )
    parser.add_argument(
        "--reviewer-kind",
        required=True,
        choices=("automated_assistant", "human_operator"),
    )
    parser.add_argument("--human-engineering-review", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.content_reviewed:
        print("export-public-examples: --content-reviewed is required", file=sys.stderr)
        return 2
    try:
        create_bundle(
            args.run_root,
            args.output,
            reviewer_kind=args.reviewer_kind,
            human_engineering_review=args.human_engineering_review,
        )
    except (OSError, RuntimeError) as exc:
        print(f"export-public-examples: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
