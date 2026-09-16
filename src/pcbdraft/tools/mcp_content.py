"""MCP content-block normalization, rendering, and local caching.

This module is intentionally independent of MCP connections, authentication,
configuration, registration, and lifecycle management.  It accepts SDK model
objects by shape so it works across MCP 1.x and 2.x field naming.
"""

from __future__ import annotations

import logging
from typing import Any

from pcbdraft.tools.ansi_strip import strip_unicode_tags

logger = logging.getLogger("pcbdraft.tools.mcp_tool")

_MISSING = object()


def _mcp_field(obj, snake: str, camel: str, default=None):
    """Read an MCP model field across the 1.x -> 2.x field rename."""
    value = getattr(obj, snake, _MISSING)
    if value is not _MISSING:
        return value
    value = getattr(obj, camel, _MISSING)
    return default if value is _MISSING else value


def _read_resource_tool_name(server_name: str) -> str:
    """Build the established MCP read-resource tool name without registry imports."""
    import re

    safe_server = re.sub(r"[^A-Za-z0-9_]", "_", str(server_name or ""))
    return f"mcp__{safe_server}__read_resource"


def _is_reserved_mcp_meta_key(key: str) -> bool:
    """Return True if an MCP ``_meta`` key uses a protocol-reserved prefix.

    Per the MCP spec's key-name rules, a prefix is reserved when a
    ``modelcontextprotocol`` or ``mcp`` label is followed by at least one
    more label (``modelcontextprotocol.io/...``, ``tools.mcp.com/...``).
    A trailing reserved word (``com.example.mcp/...``) is a legitimate
    vendor namespace and passes through. Ported from
    MoonshotAI/kimi-code#2600.
    """
    slash = key.find("/")
    if slash <= 0:
        return False
    labels = key[:slash].split(".")
    return any(
        label in ("modelcontextprotocol", "mcp") and i < len(labels) - 1
        for i, label in enumerate(labels)
    )


def _strip_reserved_meta_keys(meta) -> "dict[str, Any] | None":
    """Drop protocol-reserved keys from a tool result's ``_meta`` mapping.

    Returns the filtered dict, or ``None`` when there is nothing
    model-facing left (or the input wasn't a mapping).
    """
    if not isinstance(meta, dict):
        return None
    out = {
        k: v
        for k, v in meta.items()
        if isinstance(k, str) and not _is_reserved_mcp_meta_key(k)
    }
    return out or None


def _mcp_image_extension_for_mime_type(mime_type: str) -> str:
    """Return a reasonable file extension for an MCP image MIME type."""
    import mimetypes

    normalized = (mime_type or "").split(";", 1)[0].strip().lower()
    if normalized in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    return mimetypes.guess_extension(normalized) or ".png"


def _cache_mcp_image_block(block) -> str:
    """Cache an MCP ``ImageContent`` block to the shared image cache and
    return a ``MEDIA:<path>`` tag that Hermes gateways know how to render.

    Returns an empty string when *block* is not an image, when the base64
    payload is malformed, or when the cache helper rejects the bytes (e.g.
    non-image MIME masquerading as an image). Errors are logged, not raised:
    a single bad block shouldn't kill the tool result, and the caller will
    fall through to any text blocks that did parse.
    """
    import base64

    data = getattr(block, "data", None)
    mime_type = _mcp_field(block, "mime_type", "mimeType")
    normalized_mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    if data is None or not normalized_mime.startswith("image/"):
        return ""
    if len(data) > _MCP_RESOURCE_MAX_B64_CHARS:
        return "[MCP image resource too large to cache]"

    try:
        raw_bytes = base64.b64decode(data, validate=True)
    except (TypeError, ValueError) as exc:
        logger.warning("MCP image block decode failed (%s): %s", normalized_mime, exc)
        return ""

    try:
        from pcbdraft.tools.media_cache import cache_image_from_bytes

        image_path = cache_image_from_bytes(
            raw_bytes,
            ext=_mcp_image_extension_for_mime_type(normalized_mime),
        )
    except ImportError:
        # gateway.platforms.base not importable in this process (e.g. cron
        # without gateway deps). Fall back to silently dropping — callers
        # get any text blocks that did parse.
        logger.debug("MCP image caching skipped — gateway.platforms.base unavailable")
        return ""
    except Exception as exc:
        logger.warning("MCP image block cache failed: %s", exc)
        return ""

    return f"MEDIA:{image_path}"


# ---------------------------------------------------------------------------
# MCP resource blocks (ResourceLink / EmbeddedResource / AudioContent)
# ---------------------------------------------------------------------------

# Hard cap on decoded resource bytes materialized from an MCP tool result.
# Prevents a misbehaving server from filling the cache disk via one block.
_MCP_RESOURCE_MAX_BYTES = 50 * 1024 * 1024

# Base64 expands raw bytes by ~4/3; reject oversized payloads before decoding
# so a multi-GB blob string is never transiently doubled in memory.
_MCP_RESOURCE_MAX_B64_CHARS = _MCP_RESOURCE_MAX_BYTES * 4 // 3 + 4


def _mcp_resource_filename(uri: str, mime_type: str) -> str:
    """Derive a safe display filename for an MCP resource.

    Only the last path segment of the URI is considered, and only as a
    *name hint* — `cache_document_from_bytes` re-sanitizes and prefixes it,
    so remote path components can't influence the cache location.
    """
    import mimetypes
    import re as _re
    from pathlib import Path
    from urllib.parse import unquote, urlparse

    name = ""
    if uri:
        try:
            name = Path(unquote(urlparse(str(uri)).path or "")).name
        except (ValueError, TypeError):
            name = ""
    # Strip control characters (newlines/ANSI escapes from hostile URIs would
    # otherwise land in the filename and the transcript marker) and cap the
    # length, preserving the extension.
    name = _re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 12:
            name = stem[: 150 - len(ext) - 1] + "." + ext
        else:
            name = name[:150]
    if not name or name in {".", ".."}:
        normalized = (mime_type or "").split(";", 1)[0].strip().lower()
        ext = mimetypes.guess_extension(normalized) or ".bin"
        name = f"resource{ext}"
    return name


def _cache_mcp_audio_block(block) -> str:
    """Cache an MCP ``AudioContent`` block and return a ``MEDIA:`` tag.

    Returns an empty string when *block* is not audio or on any failure —
    same fail-open contract as ``_cache_mcp_image_block``.
    """
    import base64

    data = getattr(block, "data", None)
    mime_type = (
        str(_mcp_field(block, "mime_type", "mimeType") or "")
        .split(";", 1)[0]
        .strip()
        .lower()
    )
    if data is None or not mime_type.startswith("audio/"):
        return ""
    if len(data) > _MCP_RESOURCE_MAX_B64_CHARS:
        return f"[MCP audio resource too large to cache: ~{len(data) * 3 // 4} bytes]"
    try:
        raw_bytes = base64.b64decode(data)
    except (TypeError, ValueError) as exc:
        logger.warning("MCP audio block decode failed (%s): %s", mime_type, exc)
        return ""
    if len(raw_bytes) > _MCP_RESOURCE_MAX_BYTES:
        return f"[MCP audio resource too large to cache: {len(raw_bytes)} bytes]"
    try:
        import mimetypes

        from pcbdraft.tools.media_cache import cache_audio_from_bytes

        ext = (
            {"audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav"}.get(
                mime_type
            )
            or mimetypes.guess_extension(mime_type)
            or ".ogg"
        )
        audio_path = cache_audio_from_bytes(raw_bytes, ext=ext)
    except ImportError:
        logger.debug("MCP audio caching skipped — gateway.platforms.base unavailable")
        return ""
    except Exception as exc:
        logger.warning("MCP audio block cache failed: %s", exc)
        return ""
    return f"MEDIA:{audio_path}"


def _render_mcp_resource_block(block, server_name: str = "") -> str:
    """Render an MCP ``ResourceLink`` or ``EmbeddedResource`` block as text.

    - ``EmbeddedResource`` with text contents → the text itself.
    - ``EmbeddedResource`` with blob contents → bytes are decoded (size-capped)
      and materialized into the Hermes document cache; returns a marker with
      the local path so file/terminal tools can consume it.
    - ``ResourceLink`` → the URI plus a pointer at the server's read_resource
      tool. No network fetch happens here; the link is only readable through
      the originating MCP session.

    Returns an empty string for non-resource blocks. Failures are logged and
    reported inline rather than silently dropping the block.
    """
    block_type = getattr(block, "type", "")

    if block_type == "resource_link" or (
        hasattr(block, "uri")
        and not hasattr(block, "resource")
        and block_type != "text"
    ):
        uri = getattr(block, "uri", None)
        if not uri:
            return ""
        name = getattr(block, "name", "") or ""
        mime = _mcp_field(block, "mime_type", "mimeType", "") or ""
        details = f"uri={uri}"
        if name:
            details += f", name={name}"
        if mime:
            details += f", mimeType={mime}"
        reader = (
            _read_resource_tool_name(server_name)
            if server_name
            else "the MCP server's read_resource tool"
        )
        return f"[MCP resource link: {details} — fetch it with {reader}]"

    resource = getattr(block, "resource", None)
    if resource is None:
        return ""

    text = getattr(resource, "text", None)
    if text is not None:
        return strip_unicode_tags(str(text))

    blob = getattr(resource, "blob", None)
    if blob is None:
        return ""

    import base64

    uri = str(getattr(resource, "uri", "") or "")
    mime = str(_mcp_field(resource, "mime_type", "mimeType", "") or "")
    if len(blob) > _MCP_RESOURCE_MAX_B64_CHARS:
        return f"[MCP embedded resource too large to cache: ~{len(blob) * 3 // 4} bytes, uri={uri}]"
    try:
        raw_bytes = base64.b64decode(blob)
    except (TypeError, ValueError) as exc:
        logger.warning("MCP embedded resource decode failed (%s): %s", mime or uri, exc)
        return f"[MCP embedded resource could not be decoded: {mime or uri}]"
    if len(raw_bytes) > _MCP_RESOURCE_MAX_BYTES:
        return f"[MCP embedded resource too large to cache: {len(raw_bytes)} bytes, uri={uri}]"
    try:
        from pcbdraft.tools.media_cache import cache_document_from_bytes

        path = cache_document_from_bytes(raw_bytes, _mcp_resource_filename(uri, mime))
    except ImportError:
        logger.debug(
            "MCP resource caching skipped — gateway.platforms.base unavailable"
        )
        return f"[MCP embedded resource received ({len(raw_bytes)} bytes, {mime or 'unknown type'}) but document cache unavailable in this process]"
    except Exception as exc:
        logger.warning("MCP embedded resource cache failed: %s", exc)
        return f"[MCP embedded resource could not be cached: {mime or uri}]"
    detail = mime or "unknown type"
    return f"[MCP resource saved to {path} ({detail}, {len(raw_bytes)} bytes) — read it with read_file or terminal tools]"
