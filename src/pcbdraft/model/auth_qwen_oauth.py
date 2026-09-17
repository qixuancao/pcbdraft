"""Qwen CLI OAuth token persistence, refresh, and runtime credentials.

The owning authentication module injects its error type, HTTP client, clock,
environment, and auth-store hooks. This keeps the Qwen lifecycle independent
from ``model.auth`` while preserving that module's historical patch points.
"""

from __future__ import annotations

import json
import os
import stat
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

# Network, parsing, and cleanup failures intentionally retain legacy error mapping.
# ruff: noqa: BLE001


def _qwen_cli_auth_path(*, home: Callable[[], Path] = Path.home) -> Path:
    """Return the credential path managed by the Qwen CLI."""

    return home() / ".qwen" / "oauth_creds.json"


def _read_qwen_cli_tokens(
    *,
    qwen_cli_auth_path: Callable[[], Path],
    auth_error: type[Exception],
    json_loads: Callable[[str], Any] = json.loads,
) -> dict[str, Any]:
    """Read and validate the Qwen CLI token document."""

    auth_path = qwen_cli_auth_path()
    if not auth_path.exists():
        raise auth_error(
            "Qwen CLI credentials not found. Run 'qwen auth qwen-oauth' first.",
            provider="qwen-oauth",
            code="qwen_auth_missing",
        )
    try:
        data = json_loads(auth_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise auth_error(
            f"Failed to read Qwen CLI credentials from {auth_path}: {exc}",
            provider="qwen-oauth",
            code="qwen_auth_read_failed",
        ) from exc
    if not isinstance(data, dict):
        raise auth_error(
            f"Invalid Qwen CLI credentials in {auth_path}.",
            provider="qwen-oauth",
            code="qwen_auth_invalid",
        )
    return data


def _save_qwen_cli_tokens(
    tokens: dict[str, Any],
    *,
    qwen_cli_auth_path: Callable[[], Path],
    secure_parent: Callable[[Path], Any],
    atomic_replace: Callable[[Path, Path], Any],
    os_module: Any = os,
    stat_module: Any = stat,
    json_dumps: Callable[..., str] = json.dumps,
    uuid4: Callable[[], Any] = uuid.uuid4,
) -> Path:
    """Atomically write Qwen CLI tokens with owner-only permissions."""

    auth_path = qwen_cli_auth_path()
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    secure_parent(auth_path)
    tmp_path = auth_path.with_name(
        f"{auth_path.name}.tmp.{os_module.getpid()}.{uuid4().hex}"
    )
    descriptor = os_module.open(
        str(tmp_path),
        os_module.O_WRONLY | os_module.O_CREAT | os_module.O_EXCL,
        stat_module.S_IRUSR | stat_module.S_IWUSR,
    )
    try:
        with os_module.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json_dumps(tokens, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os_module.fsync(handle.fileno())
        atomic_replace(tmp_path, auth_path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
    return auth_path


def _qwen_access_token_is_expiring(
    expiry_date_ms: Any,
    skew_seconds: int,
    *,
    time_now: Callable[[], float] = time.time,
) -> bool:
    """Return whether the Qwen access token is inside the refresh window."""

    try:
        expiry_ms = int(expiry_date_ms)
    except Exception:
        return True
    return (time_now() + max(0, int(skew_seconds))) * 1000 >= expiry_ms


def _refresh_qwen_cli_tokens(
    tokens: dict[str, Any],
    timeout_seconds: float = 20.0,
    *,
    http_post: Callable[..., Any],
    token_url: str,
    client_id: str,
    auth_error: type[Exception],
    save_qwen_cli_tokens: Callable[[dict[str, Any]], Path],
    time_now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Refresh and persist the Qwen CLI OAuth token chain."""

    refresh_token = str(tokens.get("refresh_token", "") or "").strip()
    if not refresh_token:
        raise auth_error(
            "Qwen OAuth refresh token missing. Re-run 'qwen auth qwen-oauth'.",
            provider="qwen-oauth",
            code="qwen_refresh_token_missing",
        )

    try:
        response = http_post(
            token_url,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise auth_error(
            f"Qwen OAuth refresh failed: {exc}",
            provider="qwen-oauth",
            code="qwen_refresh_failed",
        ) from exc

    if response.status_code >= 400:
        body = response.text.strip()
        raise auth_error(
            "Qwen OAuth refresh failed. Re-run 'qwen auth qwen-oauth'."
            + (f" Response: {body}" if body else ""),
            provider="qwen-oauth",
            code="qwen_refresh_failed",
        )

    try:
        payload = response.json()
    except Exception as exc:
        raise auth_error(
            f"Qwen OAuth refresh returned invalid JSON: {exc}",
            provider="qwen-oauth",
            code="qwen_refresh_invalid_json",
        ) from exc

    if (
        not isinstance(payload, dict)
        or not str(payload.get("access_token", "") or "").strip()
    ):
        raise auth_error(
            "Qwen OAuth refresh response missing access_token.",
            provider="qwen-oauth",
            code="qwen_refresh_invalid_response",
        )

    expires_in = payload.get("expires_in")
    try:
        expires_in_seconds = int(expires_in)
    except Exception:
        expires_in_seconds = 6 * 60 * 60

    refreshed = {
        "access_token": str(payload.get("access_token", "") or "").strip(),
        "refresh_token": str(
            payload.get("refresh_token", refresh_token) or refresh_token
        ).strip(),
        "token_type": str(
            payload.get("token_type", tokens.get("token_type", "Bearer")) or "Bearer"
        ).strip()
        or "Bearer",
        "resource_url": str(
            payload.get("resource_url", tokens.get("resource_url", "portal.qwen.ai"))
            or "portal.qwen.ai"
        ).strip(),
        "expiry_date": int(time_now() * 1000) + max(1, expires_in_seconds) * 1000,
    }
    save_qwen_cli_tokens(refreshed)
    return refreshed


def _mark_qwen_oauth_active(
    creds: dict[str, Any],
    *,
    auth_store_lock: Callable[[], AbstractContextManager[Any]],
    load_auth_store: Callable[[], dict[str, Any]],
    save_provider_state: Callable[[dict[str, Any], str, dict[str, Any]], None],
    save_auth_store: Callable[[dict[str, Any]], Any],
) -> None:
    """Record Qwen OAuth as the active provider in the main auth store."""

    with auth_store_lock():
        auth_store = load_auth_store()
        state: dict[str, Any] = {}
        if creds.get("base_url"):
            state["base_url"] = str(creds["base_url"])
        save_provider_state(auth_store, "qwen-oauth", state)
        save_auth_store(auth_store)


def resolve_qwen_runtime_credentials(
    *,
    force_refresh: bool = False,
    refresh_if_expiring: bool = True,
    refresh_skew_seconds: int,
    read_qwen_cli_tokens: Callable[[], dict[str, Any]],
    qwen_access_token_is_expiring: Callable[[Any, int], bool],
    refresh_qwen_cli_tokens: Callable[[dict[str, Any]], dict[str, Any]],
    qwen_cli_auth_path: Callable[[], Path],
    environment_getter: Callable[[str, str], str] = os.getenv,
    default_base_url: str,
    auth_error: type[Exception],
) -> dict[str, Any]:
    """Resolve a valid Qwen bearer token and OpenAI-compatible endpoint."""

    tokens = read_qwen_cli_tokens()
    access_token = str(tokens.get("access_token", "") or "").strip()
    should_refresh = bool(force_refresh)
    if not should_refresh and refresh_if_expiring:
        should_refresh = qwen_access_token_is_expiring(
            tokens.get("expiry_date"), refresh_skew_seconds
        )
    if should_refresh:
        tokens = refresh_qwen_cli_tokens(tokens)
        access_token = str(tokens.get("access_token", "") or "").strip()
    if not access_token:
        raise auth_error(
            "Qwen OAuth access token missing. Re-run 'qwen auth qwen-oauth'.",
            provider="qwen-oauth",
            code="qwen_access_token_missing",
        )

    base_url = (
        environment_getter("PCBDRAFT_RUNTIME_QWEN_BASE_URL", "").strip().rstrip("/")
        or default_base_url
    )
    return {
        "provider": "qwen-oauth",
        "base_url": base_url,
        "api_key": access_token,
        "source": "qwen-cli",
        "expires_at_ms": tokens.get("expiry_date"),
        "auth_file": str(qwen_cli_auth_path()),
    }


def get_qwen_auth_status(
    *,
    qwen_cli_auth_path: Callable[[], Path],
    resolve_qwen_runtime_credentials: Callable[..., dict[str, Any]],
    auth_error: type[Exception],
) -> dict[str, Any]:
    """Return a status projection for the Qwen CLI OAuth credentials."""

    auth_path = qwen_cli_auth_path()
    try:
        creds = resolve_qwen_runtime_credentials(refresh_if_expiring=True)
        return {
            "logged_in": True,
            "auth_file": str(auth_path),
            "source": creds.get("source"),
            "api_key": creds.get("api_key"),
            "expires_at_ms": creds.get("expires_at_ms"),
        }
    except auth_error as exc:
        return {
            "logged_in": False,
            "auth_file": str(auth_path),
            "error": str(exc),
        }
