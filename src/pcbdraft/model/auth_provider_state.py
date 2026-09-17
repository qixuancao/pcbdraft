# Provider detection intentionally treats unavailable optional configuration
# and credential sources as absent, matching the legacy best-effort behavior.
# ruff: noqa: BLE001, S110
"""Provider authentication state queries and scoped mutations.

Credential storage, provider resolution, OAuth/token flows, and credential-pool
ownership remain in :mod:`pcbdraft.model.auth`.  Its compatibility wrappers
inject the live store, registry, and secret hooks used by these helpers.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def get_provider_auth_state(
    provider_id: str,
    *,
    load_auth_store: Callable[[], dict[str, Any]],
    load_provider_state: Callable[[dict[str, Any], str], dict[str, Any] | None],
) -> dict[str, Any] | None:
    """Return persisted authentication state for one provider, if present."""

    return load_provider_state(load_auth_store(), provider_id)


def get_active_provider(
    *,
    load_auth_store: Callable[[], dict[str, Any]],
) -> str | None:
    """Return the active provider identifier from the authentication store."""

    return load_auth_store().get("active_provider")


def _slot_matches_provider(slot: Any, provider_id: str) -> bool:
    """Return whether a configured model slot selects ``provider_id``."""

    return (
        isinstance(slot, dict)
        and (slot.get("provider") or "").strip().lower() == provider_id
    )


def _config_selects_provider(config: Any, provider_id: str) -> bool:
    """Check the primary model and every MoA slot for an explicit provider."""

    if not isinstance(config, dict):
        return False
    model_config = config.get("model")
    if isinstance(model_config, dict):
        configured = (model_config.get("provider") or "").strip().lower()
        if configured == provider_id:
            return True

    moa_config = config.get("moa")
    if not isinstance(moa_config, dict):
        return False
    for slot in moa_config.get("reference_models") or []:
        if _slot_matches_provider(slot, provider_id):
            return True
    if _slot_matches_provider(moa_config.get("aggregator"), provider_id):
        return True
    presets = moa_config.get("presets")
    if not isinstance(presets, dict):
        return False
    for preset in presets.values():
        if not isinstance(preset, dict):
            continue
        for slot in preset.get("reference_models") or []:
            if _slot_matches_provider(slot, provider_id):
                return True
        if _slot_matches_provider(preset.get("aggregator"), provider_id):
            return True
    return False


def is_provider_explicitly_configured(
    provider_id: str,
    *,
    load_auth_store: Callable[[], dict[str, Any]],
    provider_registry: dict[str, Any],
    environment_getter: Callable[[str, str], str],
    has_usable_secret: Callable[[Any], bool],
    read_credential_pool: Callable[[str], list[Any]],
    normalize_credential_source: Callable[[Any], str],
) -> bool:
    """Return whether the user explicitly configured a provider."""

    normalized = (provider_id or "").strip().lower()

    try:
        auth_store = load_auth_store()
        active = (auth_store.get("active_provider") or "").strip().lower()
        if active and active == normalized:
            return True
    except Exception:
        pass

    try:
        from pcbdraft.model.configuration import load_config

        if _config_selects_provider(load_config(), normalized):
            return True
    except Exception:
        pass

    implicit_environment_variables = {"CLAUDE_CODE_OAUTH_TOKEN"}
    provider_config = provider_registry.get(normalized)
    if provider_config is None:
        from pcbdraft.model.provider_config import get_provider

        provider_config = get_provider(normalized)
    if provider_config and provider_config.auth_type == "api_key":
        for environment_variable in provider_config.api_key_env_vars:
            if environment_variable in implicit_environment_variables:
                continue
            if has_usable_secret(environment_getter(environment_variable, "")):
                return True

    try:
        for entry in read_credential_pool(normalized):
            if not isinstance(entry, dict):
                continue
            source = normalize_credential_source(
                str(entry.get("source") or "").strip().lower()
            )
            if not source:
                continue
            if source.startswith("env:"):
                environment_variable = entry.get("source", "").split(":", 1)[1].strip()
                if environment_variable and has_usable_secret(
                    environment_getter(environment_variable, "")
                ):
                    return True
                continue
            if source in {
                "device_code",
                "loopback_pkce",
                "pcbdraft_pkce",
                "manual",
            } or source.startswith("manual:"):
                return True
    except Exception:
        pass

    return False


def clear_provider_auth(
    provider_id: str | None = None,
    *,
    auth_store_lock: Callable[[], Any],
    load_auth_store: Callable[[], dict[str, Any]],
    save_auth_store: Callable[[dict[str, Any]], Any],
) -> bool:
    """Remove singleton and pool state for one provider."""

    with auth_store_lock():
        auth_store = load_auth_store()
        target = provider_id or auth_store.get("active_provider")
        if not target:
            return False

        providers = auth_store.get("providers", {})
        if not isinstance(providers, dict):
            providers = {}
            auth_store["providers"] = providers

        pool = auth_store.get("credential_pool")
        if not isinstance(pool, dict):
            pool = {}
            auth_store["credential_pool"] = pool

        cleared = False
        if target in providers:
            del providers[target]
            cleared = True
        if target in pool:
            del pool[target]
            cleared = True
        if auth_store.get("active_provider") == target:
            auth_store["active_provider"] = None
            cleared = True

        if not cleared:
            return False
        save_auth_store(auth_store)
    return True


def deactivate_provider(
    *,
    auth_store_lock: Callable[[], Any],
    load_auth_store: Callable[[], dict[str, Any]],
    save_auth_store: Callable[[dict[str, Any]], Any],
) -> None:
    """Clear the active provider without deleting stored credentials."""

    with auth_store_lock():
        auth_store = load_auth_store()
        auth_store["active_provider"] = None
        save_auth_store(auth_store)


def _get_config_hint_for_unknown_provider(provider_name: str) -> str:
    """Return configuration diagnostics relevant to provider resolution."""

    del provider_name
    try:
        from pcbdraft.model.configuration import validate_config_structure

        issues = validate_config_structure()
        if not issues:
            return ""

        lines = ["Config issue detected — run 'pcbdraft doctor' for full diagnostics:"]
        for issue in issues:
            prefix = "ERROR" if issue.severity == "error" else "WARNING"
            lines.append(f"  [{prefix}] {issue.message}")
            first_hint = issue.hint.splitlines()[0] if issue.hint else ""
            if first_hint:
                lines.append(f"    → {first_hint}")
        return "\n".join(lines)
    except Exception:
        return ""
