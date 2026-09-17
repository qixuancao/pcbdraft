"""Credential-pool persistence and source-suppression state.

This module owns the pool-shaped portion of ``auth.json`` while leaving the
auth store, OAuth tokens, provider selection, and routing in ``model.auth``.
The legacy module installs late-bound hooks for its storage primitives so old
import and monkeypatch paths keep working without a reverse import.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from pcbdraft.model.credential_persistence import (
    normalize_credential_source,
    sanitize_borrowed_credential_payload,
)

# Cooldown merging and suppression reads are deliberately best-effort at their
# established boundaries; failures retain the in-memory entry or fail open.
# ruff: noqa: BLE001


_POOL_STATUS_FIELDS = (
    "last_status",
    "last_status_at",
    "last_error_code",
    "last_error_reason",
    "last_error_message",
    "last_error_reset_at",
)


def _unconfigured(*_args, **_kwargs):
    raise RuntimeError("credential-pool store hooks are not configured")


_load_auth_store_hook: Callable[[], dict[str, Any]] = _unconfigured
_load_global_auth_store_hook: Callable[[], dict[str, Any]] = dict
_auth_store_lock_hook: Callable[[], AbstractContextManager[Any]] = _unconfigured
_save_auth_store_hook: Callable[[dict[str, Any]], Path] = _unconfigured
_normalize_source_hook: Callable[[str], str] = normalize_credential_source
_sanitize_payload_hook: Callable[[dict[str, Any], str], dict[str, Any]] = (
    sanitize_borrowed_credential_payload
)
_now_hook: Callable[[], float] = time.time
_status_fields_hook: Callable[[], tuple[str, ...]] = lambda: _POOL_STATUS_FIELDS
_merge_disk_cooldown_hook: Callable[
    [dict[str, Any], dict[str, Any] | None, str], dict[str, Any]
]


def _configure_legacy_auth_hooks(
    *,
    load_auth_store: Callable[[], dict[str, Any]],
    load_global_auth_store: Callable[[], dict[str, Any]],
    auth_store_lock: Callable[[], AbstractContextManager[Any]],
    save_auth_store: Callable[[dict[str, Any]], Path],
    normalize_source: Callable[[str], str],
    sanitize_payload: Callable[[dict[str, Any], str], dict[str, Any]],
    now: Callable[[], float],
    status_fields: Callable[[], tuple[str, ...]],
    merge_disk_cooldown: Callable[
        [dict[str, Any], dict[str, Any] | None, str], dict[str, Any]
    ],
) -> None:
    """Install late-bound adapters for legacy ``model.auth`` globals."""
    global _load_auth_store_hook
    global _load_global_auth_store_hook
    global _auth_store_lock_hook
    global _save_auth_store_hook
    global _normalize_source_hook
    global _sanitize_payload_hook
    global _now_hook
    global _status_fields_hook
    global _merge_disk_cooldown_hook

    _load_auth_store_hook = load_auth_store
    _load_global_auth_store_hook = load_global_auth_store
    _auth_store_lock_hook = auth_store_lock
    _save_auth_store_hook = save_auth_store
    _normalize_source_hook = normalize_source
    _sanitize_payload_hook = sanitize_payload
    _now_hook = now
    _status_fields_hook = status_fields
    _merge_disk_cooldown_hook = merge_disk_cooldown


def read_credential_pool(provider_id: str | None = None) -> dict[str, Any]:
    """Return the persisted credential pool, or one provider slice.

    In profile mode, the profile's credential pool is authoritative. If a
    provider has no entries in the profile, entries from the global-root
    ``auth.json`` are used as a read-only fallback — so workers spawned in a
    profile can see providers that were only authenticated at global scope.

    Profile entries always win: the global fallback only applies per-provider
    when the profile has zero entries for that provider. Once the user runs
    ``hermes auth add <provider>`` inside the profile, profile entries
    fully shadow global for that provider on the next read.

    Writes always go to the profile (``write_credential_pool`` is unchanged).
    See issue #18594 follow-up.
    """
    auth_store = _load_auth_store_hook()
    pool = auth_store.get("credential_pool")
    if not isinstance(pool, dict):
        pool = {}

    global_pool: dict[str, Any] = {}
    global_store = _load_global_auth_store_hook()
    maybe_global_pool = global_store.get("credential_pool") if global_store else None
    if isinstance(maybe_global_pool, dict):
        global_pool = maybe_global_pool

    if provider_id is None:
        merged = dict(pool)
        for gp_key, gp_entries in global_pool.items():
            if not isinstance(gp_entries, list) or not gp_entries:
                continue
            # Per-provider shadowing: profile wins whenever it has ANY entries.
            existing = merged.get(gp_key)
            if isinstance(existing, list) and existing:
                continue
            merged[gp_key] = list(gp_entries)
        return merged

    provider_entries = pool.get(provider_id)
    if isinstance(provider_entries, list) and provider_entries:
        return list(provider_entries)
    # Profile has no entries for this provider — fall back to global.
    global_entries = global_pool.get(provider_id)
    return list(global_entries) if isinstance(global_entries, list) else []


def _merge_disk_cooldown_state(
    entry: dict[str, Any],
    disk_entry: dict[str, Any] | None,
    provider_id: str,
) -> dict[str, Any]:
    """Keep a newer on-disk cooldown/quarantine over a stale in-memory one.

    ``write_credential_pool`` callers persist an in-memory snapshot that may
    predate another process marking the same credential exhausted or dead
    (last-writer-wins lost update). Without this merge, process B's later
    rewrite resurrects a rate-limited key as healthy. Adopt the on-disk status
    fields only when they are strictly more recent and still binding — a DEAD
    marker, or an EXHAUSTED cooldown that has not yet expired. Expired
    cooldowns are not resurrected.
    """
    if not isinstance(disk_entry, dict):
        return entry
    try:
        from pcbdraft.model.credential_pool import (
            STATUS_DEAD,
            STATUS_EXHAUSTED,
            PooledCredential,
            _exhausted_until,
            _parse_absolute_timestamp,
        )

        disk_status = disk_entry.get("last_status")
        if disk_status not in (STATUS_DEAD, STATUS_EXHAUSTED):
            return entry
        # A token change means the caller re-authenticated or refreshed this
        # entry and intentionally cleared its prior status.
        mem_access = entry.get("access_token") or ""
        disk_access = disk_entry.get("access_token") or ""
        if mem_access and disk_access and mem_access != disk_access:
            return entry
        disk_ts = _parse_absolute_timestamp(disk_entry.get("last_status_at")) or 0.0
        mem_ts = _parse_absolute_timestamp(entry.get("last_status_at")) or 0.0
        if disk_ts <= mem_ts:
            return entry
        if disk_status == STATUS_EXHAUSTED:
            until = _exhausted_until(
                PooledCredential.from_dict(provider_id, disk_entry)
            )
            if until is None or until <= _now_hook():
                return entry
        merged_entry = dict(entry)
        for status_field in _status_fields_hook():
            merged_entry[status_field] = disk_entry.get(status_field)
        return merged_entry
    except Exception:  # pragma: no cover - best-effort merge
        return entry


def write_credential_pool(
    provider_id: str,
    entries: list[dict[str, Any]],
    *,
    removed_ids: Iterable[str] | None = None,
) -> Path:
    """Persist one provider's credential pool under auth.json.

    This is the final disk-boundary guard for borrowed/reference-only
    credentials. Callers may pass raw dictionaries, so sanitize here even when
    ``PooledCredential.to_dict()`` already did the same work upstream.

    Re-read the on-disk pool under the same lock and merge entries present on
    disk but missing from ``entries``. Those were added by another process after
    the caller loaded its in-memory snapshot; without this merge a later
    rotation/exhaustion rewrite drops the concurrent credential.

    For entries present on both sides, status fields are merged by
    ``last_status_at`` recency so a stale snapshot cannot erase a cooldown.

    Pass ``removed_ids`` for entries the caller intentionally removed, so the
    merge does not resurrect them from the on-disk copy.
    """
    removed = {rid for rid in (removed_ids or ()) if rid}
    with _auth_store_lock_hook():
        auth_store = _load_auth_store_hook()
        pool = auth_store.get("credential_pool")
        if not isinstance(pool, dict):
            pool = {}
            auth_store["credential_pool"] = pool
        sanitized_entries = [
            _sanitize_payload_hook(entry, provider_id)
            if isinstance(entry, dict)
            else entry
            for entry in entries
        ]
        existing = pool.get(provider_id)
        existing_list = existing if isinstance(existing, list) else []
        existing_by_id = {
            entry.get("id"): entry
            for entry in existing_list
            if isinstance(entry, dict) and entry.get("id")
        }
        new_ids = {
            entry.get("id")
            for entry in sanitized_entries
            if isinstance(entry, dict) and entry.get("id")
        }
        merged: list[dict[str, Any]] = [
            _merge_disk_cooldown_hook(
                entry, existing_by_id.get(entry.get("id")), provider_id
            )
            if isinstance(entry, dict)
            else entry
            for entry in sanitized_entries
        ]
        for disk_entry in existing_list:
            if not isinstance(disk_entry, dict):
                continue
            disk_id = disk_entry.get("id")
            if not disk_id or disk_id in new_ids or disk_id in removed:
                continue
            merged.append(_sanitize_payload_hook(disk_entry, provider_id))
        pool[provider_id] = merged
        return _save_auth_store_hook(auth_store)


def suppress_credential_source(provider_id: str, source: str) -> None:
    """Mark a credential source as suppressed so it won't be re-seeded.

    Older auth stores may represent a provider's suppressed sources as a
    mapping. Treat its keys as source names and migrate the value to the
    canonical list form before appending the requested source.
    """
    source = _normalize_source_hook(source)
    with _auth_store_lock_hook():
        auth_store = _load_auth_store_hook()
        suppressed = auth_store.get("suppressed_sources")
        if not isinstance(suppressed, dict):
            suppressed = {}
            auth_store["suppressed_sources"] = suppressed

        raw_sources = suppressed.get(provider_id)
        if isinstance(raw_sources, list):
            provider_list = raw_sources
        elif isinstance(raw_sources, dict):
            provider_list = [str(name) for name in raw_sources]
            suppressed[provider_id] = provider_list
        else:
            provider_list = []
            suppressed[provider_id] = provider_list

        provider_list = list(dict.fromkeys(map(_normalize_source_hook, provider_list)))
        suppressed[provider_id] = provider_list
        if source not in provider_list:
            provider_list.append(source)
        _save_auth_store_hook(auth_store)


def is_source_suppressed(provider_id: str, source: str) -> bool:
    """Check if a credential source has been suppressed by the user."""
    try:
        auth_store = _load_auth_store_hook()
        suppressed = auth_store.get("suppressed_sources", {})
        return _normalize_source_hook(source) in {
            _normalize_source_hook(item) for item in suppressed.get(provider_id, [])
        }
    except Exception:
        return False


def unsuppress_credential_source(provider_id: str, source: str) -> bool:
    """Clear a suppression marker so the source is re-seeded on the next load.

    Returns True if a marker was cleared, False if no marker existed.
    """
    source = _normalize_source_hook(source)
    with _auth_store_lock_hook():
        auth_store = _load_auth_store_hook()
        suppressed = auth_store.get("suppressed_sources")
        if not isinstance(suppressed, dict):
            return False
        raw_sources = suppressed.get(provider_id)
        if isinstance(raw_sources, dict):
            provider_list = [str(name) for name in raw_sources]
            suppressed[provider_id] = provider_list
        elif isinstance(raw_sources, list):
            provider_list = raw_sources
        else:
            return False
        provider_list = list(dict.fromkeys(map(_normalize_source_hook, provider_list)))
        suppressed[provider_id] = provider_list
        if source not in provider_list:
            return False
        provider_list.remove(source)
        if not provider_list:
            suppressed.pop(provider_id, None)
        if not suppressed:
            auth_store.pop("suppressed_sources", None)
        _save_auth_store_hook(auth_store)
        return True


_merge_disk_cooldown_hook = _merge_disk_cooldown_state
