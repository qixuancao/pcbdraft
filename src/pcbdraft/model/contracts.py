"""Provider configuration contracts shared by catalog and transport layers.

This low-level module deliberately does not import the provider catalog or HTTP
client. Keeping validation and configuration-path ownership here prevents the
catalog/transport import cycle while preserving their public compatibility APIs.
"""

from __future__ import annotations

import ipaddress
import os
import urllib.parse
from pathlib import Path

from pcbdraft.core.errors import ValidationError


def validate_provider_base_url(value: str) -> urllib.parse.SplitResult:
    """Validate a provider URL without permitting plaintext remote credentials."""

    if "\\" in value or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise ValidationError("provider base URL is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        # Accessing port performs urllib's numeric/range validation.
        _ = parsed.port
    except ValueError as exc:
        raise ValidationError("provider base URL is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError("provider base URL is invalid")
    if parsed.scheme == "http" and not _is_literal_loopback(parsed.hostname):
        raise ValidationError(
            "provider base URL must use HTTPS; HTTP is allowed only for a "
            "literal loopback host"
        )
    return parsed


def validate_provider_model_id(value: str) -> str:
    """Validate a bounded model identifier before placing it in a request."""

    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("provider model id is invalid") from exc
    if (
        not value
        or len(encoded) > 256
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ValidationError("provider model id is invalid")
    return value


def validate_provider_credential(value: str) -> str:
    """Require a visible ASCII Bearer value that cannot split HTTP headers."""

    try:
        encoded = value.encode("ascii")
    except UnicodeError as exc:
        raise ValidationError("provider credential is invalid") from exc
    if (
        not encoded
        or len(encoded) > 16 * 1024
        or any(byte < 0x21 or byte > 0x7E for byte in encoded)
    ):
        raise ValidationError("provider credential is invalid")
    return value


def provider_config_path() -> Path:
    """Return the user-owned provider configuration path."""

    explicit = os.environ.get("PCBDRAFT_CONFIG", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return root / "pcbdraft" / "config.toml"


def _is_literal_loopback(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False
