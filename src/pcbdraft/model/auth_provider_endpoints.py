"""Provider secret and inference-endpoint resolution helpers.

This module owns provider endpoint normalization, API-key discovery, and the
Z.AI endpoint probe. Persistent provider state remains in ``model.auth``; the
legacy module installs late-bound hooks for those primitives so its historical
import and monkeypatch paths keep working without a reverse import.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any
from urllib.parse import ParseResult, urlparse

# These best-effort boundaries intentionally preserve the legacy behavior:
# malformed URLs fail closed, optional credential sources fail open, and one
# failed probe or cache write must not abort provider resolution.
# ruff: noqa: BLE001, S104, S110


def _unconfigured(*_args, **_kwargs):
    raise RuntimeError("provider endpoint hooks are not configured")


_actual_default_base_url_hook: Callable[[], str] = _unconfigured
_urlparse_hook: Callable[[str], ParseResult] = urlparse
_is_actual_local_base_url_hook: Callable[[str], bool]
_provider_registry_hook: Callable[[], dict[str, Any]] = _unconfigured
_kimi_code_base_url_hook: Callable[[], str] = _unconfigured
_placeholder_secret_values_hook: Callable[[], set[str]] = _unconfigured
_has_usable_secret_hook: Callable[..., bool]
_logger_hook: Callable[[], Any] = _unconfigured
_httpx_hook: Callable[[], Any] = _unconfigured
_zai_endpoints_hook: Callable[[], list[tuple[Any, ...]]] = _unconfigured
_probe_single_zai_endpoint_hook: Callable[[], Callable[..., dict[str, str] | None]]
_load_auth_store_hook: Callable[[], dict[str, Any]] = _unconfigured
_load_provider_state_hook: Callable[[dict[str, Any], str], dict[str, Any] | None] = (
    _unconfigured
)
_detect_zai_endpoint_hook: Callable[..., dict[str, str] | None]
_auth_store_lock_hook: Callable[[], AbstractContextManager[Any]] = _unconfigured
_store_provider_state_hook: Callable[..., None] = _unconfigured
_save_auth_store_hook: Callable[[dict[str, Any]], Any] = _unconfigured


def _configure_legacy_auth_hooks(
    *,
    actual_default_base_url: Callable[[], str],
    parse_url: Callable[[str], ParseResult],
    is_actual_local_base_url_hook: Callable[[str], bool],
    provider_registry: Callable[[], dict[str, Any]],
    kimi_code_base_url: Callable[[], str],
    placeholder_secret_values: Callable[[], set[str]],
    has_usable_secret_hook: Callable[..., bool],
    logger: Callable[[], Any],
    httpx_module: Callable[[], Any],
    zai_endpoints: Callable[[], list[tuple[Any, ...]]],
    probe_single_zai_endpoint: Callable[[], Callable[..., dict[str, str] | None]],
    load_auth_store: Callable[[], dict[str, Any]],
    load_provider_state: Callable[[dict[str, Any], str], dict[str, Any] | None],
    detect_zai_endpoint_hook: Callable[..., dict[str, str] | None],
    auth_store_lock: Callable[[], AbstractContextManager[Any]],
    store_provider_state: Callable[..., None],
    save_auth_store: Callable[[dict[str, Any]], Any],
) -> None:
    """Install late-bound adapters for legacy ``model.auth`` globals."""
    global _actual_default_base_url_hook
    global _urlparse_hook
    global _is_actual_local_base_url_hook
    global _provider_registry_hook
    global _kimi_code_base_url_hook
    global _placeholder_secret_values_hook
    global _has_usable_secret_hook
    global _logger_hook
    global _httpx_hook
    global _zai_endpoints_hook
    global _probe_single_zai_endpoint_hook
    global _load_auth_store_hook
    global _load_provider_state_hook
    global _detect_zai_endpoint_hook
    global _auth_store_lock_hook
    global _store_provider_state_hook
    global _save_auth_store_hook

    _actual_default_base_url_hook = actual_default_base_url
    _urlparse_hook = parse_url
    _is_actual_local_base_url_hook = is_actual_local_base_url_hook
    _provider_registry_hook = provider_registry
    _kimi_code_base_url_hook = kimi_code_base_url
    _placeholder_secret_values_hook = placeholder_secret_values
    _has_usable_secret_hook = has_usable_secret_hook
    _logger_hook = logger
    _httpx_hook = httpx_module
    _zai_endpoints_hook = zai_endpoints
    _probe_single_zai_endpoint_hook = probe_single_zai_endpoint
    _load_auth_store_hook = load_auth_store
    _load_provider_state_hook = load_provider_state
    _detect_zai_endpoint_hook = detect_zai_endpoint_hook
    _auth_store_lock_hook = auth_store_lock
    _store_provider_state_hook = store_provider_state
    _save_auth_store_hook = save_auth_store


def is_actual_local_base_url(base_url: str) -> bool:
    """Return True for Actual's loopback local API endpoint."""
    try:
        host = (_urlparse_hook(base_url or "").hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def normalize_actual_base_url(base_url: str) -> str:
    """Return Actual's OpenAI-compatible base URL."""
    url = str(base_url or "").strip().rstrip("/")
    if not url:
        return _actual_default_base_url_hook()
    try:
        parsed = _urlparse_hook(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        path = parsed.path.rstrip("/")
    except Exception:
        return url
    if host == "api.actual.inc" and path in {"", "/"}:
        return url + "/v1"
    if _is_actual_local_base_url_hook(url) and path in {"", "/"}:
        return url + "/v1"
    return url


def get_anthropic_key() -> str:
    """Return the first usable Anthropic credential, or ``""``."""
    from pcbdraft.model.configuration import get_env_value_prefer_dotenv

    for var in _provider_registry_hook()["anthropic"].api_key_env_vars:
        value = get_env_value_prefer_dotenv(var) or ""
        if value:
            return value
    return ""


def _resolve_kimi_base_url(api_key: str, default_url: str, env_override: str) -> str:
    """Return the correct Kimi base URL based on the API key prefix."""
    if env_override:
        return env_override
    if not api_key:
        return default_url
    if api_key.startswith("sk-kimi-"):
        return _kimi_code_base_url_hook()
    return default_url


def has_usable_secret(value: Any, *, min_length: int = 4) -> bool:
    """Return True when a configured secret looks usable, not a placeholder."""
    if not isinstance(value, str):
        return False
    cleaned = value.strip()
    if len(cleaned) < min_length:
        return False
    return cleaned.lower() not in _placeholder_secret_values_hook()


def _resolve_api_key_provider_secret(provider_id: str, pconfig: Any) -> tuple[str, str]:
    """Resolve an API-key provider's token and indicate where it came from."""
    if provider_id == "copilot":
        try:
            from pcbdraft.interfaces.tui.copilot_auth import (
                get_copilot_api_token,
                resolve_copilot_token,
            )

            token, source = resolve_copilot_token()
            if token:
                api_token, _base_url = get_copilot_api_token(token)
                return api_token, source
        except ValueError as exc:
            _logger_hook().warning("Copilot token validation failed: %s", exc)
        except Exception:
            pass
        return "", ""

    from pcbdraft.model.configuration import get_env_value_prefer_dotenv

    for env_var in pconfig.api_key_env_vars:
        val = (get_env_value_prefer_dotenv(env_var) or "").strip()
        if _has_usable_secret_hook(val):
            return val, env_var

    try:
        from pcbdraft.model.credential_pool import load_pool

        pool = load_pool(provider_id)
        if pool and pool.has_credentials():
            entry = pool.peek()
            if entry:
                key = getattr(entry, "access_token", "") or getattr(
                    entry, "runtime_api_key", ""
                )
                key = str(key).strip()
                if _has_usable_secret_hook(key):
                    return key, f"credential_pool:{provider_id}"
    except Exception:
        pass

    return "", ""


def _probe_single_zai_endpoint(
    api_key: str,
    endpoint: tuple,
    timeout: float,
) -> dict[str, str] | None:
    """Probe one Z.AI endpoint and return its accepted model, if any."""
    ep_id, base_url, probe_models, label = endpoint
    for model in probe_models:
        try:
            resp = _httpx_hook().post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "stream": False,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                timeout=timeout,
            )
            if resp.status_code == 200:
                _logger_hook().debug(
                    "Z.AI endpoint probe: %s (%s) model=%s OK",
                    ep_id,
                    base_url,
                    model,
                )
                return {
                    "id": ep_id,
                    "base_url": base_url,
                    "model": model,
                    "label": label,
                }
            _logger_hook().debug(
                "Z.AI endpoint probe: %s model=%s returned %s",
                ep_id,
                model,
                resp.status_code,
            )
        except Exception as exc:
            _logger_hook().debug(
                "Z.AI endpoint probe: %s model=%s failed: %s", ep_id, model, exc
            )
    return None


def detect_zai_endpoint(api_key: str, timeout: float = 8.0) -> dict[str, str] | None:
    """Probe Z.AI endpoints in parallel, preserving configured priority."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    endpoints = _zai_endpoints_hook()
    probe_endpoint = _probe_single_zai_endpoint_hook()
    pool = ThreadPoolExecutor(max_workers=len(endpoints))
    try:
        futures = {
            pool.submit(probe_endpoint, api_key, ep, timeout): ep[0] for ep in endpoints
        }
        by_id = {ep_id: future for future, ep_id in futures.items()}
        results: dict[str, dict[str, str]] = {}
        for future in as_completed(futures):
            ep_id = futures[future]
            try:
                result = future.result()
                if result is not None:
                    results[ep_id] = result
            except Exception:
                pass
            for ep in endpoints:
                if not by_id[ep[0]].done():
                    break
                if ep[0] in results:
                    return results[ep[0]]

        for ep in endpoints:
            if ep[0] in results:
                return results[ep[0]]
        return None
    finally:
        pool.shutdown(wait=False)


def _resolve_zai_base_url(api_key: str, default_url: str, env_override: str) -> str:
    """Return the correct Z.AI base URL, using the cached or probed endpoint."""
    if env_override:
        return env_override
    if not api_key:
        return default_url

    auth_store = _load_auth_store_hook()
    state = _load_provider_state_hook(auth_store, "zai") or {}
    cached = state.get("detected_endpoint")
    if isinstance(cached, dict) and cached.get("base_url"):
        key_hash = cached.get("key_hash", "")
        if key_hash == hashlib.sha256(api_key.encode()).hexdigest()[:16]:
            _logger_hook().debug("Z.AI: using cached endpoint %s", cached["base_url"])
            return cached["base_url"]

    detected = _detect_zai_endpoint_hook(api_key)
    if detected and detected.get("base_url"):
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:16]
        detected_endpoint = {
            "base_url": detected["base_url"],
            "endpoint_id": detected.get("id", ""),
            "model": detected.get("model", ""),
            "label": detected.get("label", ""),
            "key_hash": key_hash,
        }
        try:
            with _auth_store_lock_hook():
                auth_store = _load_auth_store_hook()
                state_under_lock = _load_provider_state_hook(auth_store, "zai") or {}
                state_under_lock["detected_endpoint"] = detected_endpoint
                _store_provider_state_hook(
                    auth_store, "zai", state_under_lock, set_active=False
                )
                _save_auth_store_hook(auth_store)
        except Exception as exc:
            _logger_hook().warning(
                "Z.AI: could not persist detected endpoint (%s); will re-probe next start",
                exc,
            )
        _logger_hook().info(
            "Z.AI: auto-detected endpoint %s (%s)",
            detected["label"],
            detected["base_url"],
        )
        return detected["base_url"]

    _logger_hook().debug("Z.AI: probe failed, falling back to default %s", default_url)
    return default_url


def _normalize_lmstudio_runtime_base_url(base_url: str) -> str:
    """Return the OpenAI-compatible LM Studio runtime base URL."""
    root = str(base_url or "").strip().rstrip("/")
    for suffix in ("/api/v1", "/api", "/v1"):
        if root.endswith(suffix):
            root = root[: -len(suffix)].rstrip("/")
            break
    return (root or "http://127.0.0.1:1234") + "/v1"
