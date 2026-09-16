"""Session activity and mutable metadata behavior for :class:`SessionDB`.

Mixin contract: this plain mixin is consumed by
``pcbdraft.services.session_db.SessionDB``. It defines no ``__init__`` and owns
no connection state. The host supplies ``_conn``, ``_execute_write``, token
queue flushing, prompt storage/cleanup, and session lookup. This module must
never import ``session_db`` so the store remains the composition root.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from pcbdraft.agent import session_activity as _session_activity
from pcbdraft.agent.session_activity import ActivityProvenance
from pcbdraft.core.runtime_environment import _exception_info_without_values

logger = logging.getLogger("pcbdraft.services.session_db")

# Sentinel returned by ``_merge_model_config_json`` when the session row does
# not exist and ``on_missing='skip'``. This differs from the legal ``None``
# result, which means the merged config is empty and should be stored as NULL.
_MODEL_CONFIG_ROW_MISSING = object()

# Billing buckets are accounting classes, not routable provider identities.
_BARE_BILLING_PROVIDERS = frozenset({"auto", "custom"})


class SessionMetadataMixin:
    """SessionDB activity labels, model metadata, and billing-route updates."""

    def touch_session_activity(
        self,
        session_id: str,
        ts: float | None = None,
        *,
        description: str | None = None,
        provenance: ActivityProvenance | None = None,
    ) -> None:
        """Stamp durable mid-turn session activity (observation-only).

        Called (rate-limited) from ``AIAgent._touch_activity`` so gateway/CLI
        surfaces and stall consumers observe API/tool/compaction activity
        even when no new message row has been written yet (#72016 / #72039).

        Never moves ``last_activity_at`` backwards. When the timestamp
        advances, bounded ``last_activity_description`` /
        ``last_activity_provenance`` are written with it. No-ops when
        ``session_id`` is empty or the row does not exist.
        """
        if not session_id:
            return

        when = float(ts if ts is not None else time.time())
        desc = _session_activity.bound_activity_description(description)
        prov = _session_activity.normalize_activity_provenance(provenance).value

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET "
                "last_activity_at = ?, "
                "last_activity_description = ?, "
                "last_activity_provenance = ? "
                "WHERE id = ? AND (last_activity_at IS NULL OR last_activity_at < ?)",
                (when, desc, prov, session_id, when),
            )

        # Observation-only write: never let it ride the full routine
        # write-patience budget (#76354 review S1). Under contention a
        # heartbeat that waits ~20s would delay the response-critical path
        # it is merely observing; give up after a sub-second budget instead
        # (the next due window retries naturally).
        self._execute_write(_do, patience_s=self._ACTIVITY_WRITE_PATIENCE_S)

    def clear_session_activity_labels(self, session_id: str) -> None:
        """Clear mid-turn activity labels after a turn ends.

        Keeps ``last_activity_at`` intact so idle / watchdog clocks stay
        continuous. Description and provenance are observation labels for
        *what was happening at* that timestamp during an active turn; once
        the turn is idle they must not keep advertising "compressing" /
        "executing tool" (#72039).

        Response-critical-path contract (#76354 review S1): runs in the
        turn's ``finally``; a no-op clear (labels already empty) skips the
        write transaction entirely, and a real clear uses the same short
        sub-second busy budget as :meth:`touch_session_activity` instead of
        the full routine write patience.
        """
        if not session_id:
            return

        # No-op fast path: skip the transaction when there is nothing to
        # clear. Read-only, no write lock.
        try:
            row = self._conn.execute(
                "SELECT last_activity_description, last_activity_provenance "
                "FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        except sqlite3.Error:
            row = None
        if row is not None:
            desc = (
                row[0]
                if not isinstance(row, sqlite3.Row)
                else row["last_activity_description"]
            )
            prov = (
                row[1]
                if not isinstance(row, sqlite3.Row)
                else row["last_activity_provenance"]
            )
            if not desc and (not prov or prov == ActivityProvenance.UNKNOWN.value):
                return

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET "
                "last_activity_description = ?, "
                "last_activity_provenance = ? "
                "WHERE id = ?",
                ("", ActivityProvenance.UNKNOWN.value, session_id),
            )

        self._execute_write(_do, patience_s=self._ACTIVITY_WRITE_PATIENCE_S)

    def get_session_activity(self, session_id: str) -> dict[str, Any] | None:
        """Return the durable activity snapshot for *session_id*, or None."""
        if not session_id:
            return None
        row = self.get_session(session_id)
        if not row:
            return None

        return _session_activity.build_activity_snapshot(
            last_activity_at=row.get("last_activity_at"),
            last_activity_description=row.get("last_activity_description"),
            last_activity_provenance=row.get("last_activity_provenance"),
        )

    def update_session_meta(
        self,
        session_id: str,
        model_config_json: str,
        model: str | None = None,
    ) -> None:
        """Update model_config and optionally model for an existing session.

        Uses COALESCE so that passing model=None leaves the stored model
        column unchanged. Routes through _execute_write for the standard
        BEGIN IMMEDIATE + jitter-retry + lock guarantee.
        """
        # Barrier against queued token deltas — see update_session_model.
        self.flush_token_counts()

        def _do(conn):
            conn.execute(
                "UPDATE sessions SET model_config = ?, model = COALESCE(?, model) WHERE id = ?",
                (model_config_json, model, session_id),
            )

        self._execute_write(_do)

    def update_system_prompt(self, session_id: str, system_prompt: str | None) -> None:
        """Store the full assembled system prompt snapshot."""

        def _do(conn):
            system_prompt_hash = self._store_system_prompt(conn, system_prompt)
            conn.execute(
                "UPDATE sessions "
                "SET system_prompt_hash = ?, system_prompt = NULL WHERE id = ?",
                (system_prompt_hash, session_id),
            )
            self._delete_unreferenced_system_prompts(conn)

        self._execute_write(_do)

    def update_session_model(
        self, session_id: str, model: str, provider: str | None = None
    ) -> None:
        """Update the model for a session after a mid-session switch.

        Unlike ``update_token_counts`` which uses ``COALESCE(model, ?)``
        (only filling in NULL), this unconditionally sets the model column
        so that the dashboard reflects the user's latest /model choice.
        Also nulls ``system_prompt`` so stale ``Model:`` / ``Provider:``
        footer metadata is rebuilt on the next turn. A successful /model
        switch explicitly replaces any confirmed Browser runtime lock while
        preserving unrelated lineage markers in ``model_config``.

        When *provider* is given, it is merged into ``model_config``
        alongside the model (``$.model`` / ``$.provider``) so a later
        resume recombines the persisted model with the provider that
        actually serves it instead of the config.yaml primary provider
        (#79536). Callers without provider knowledge leave any stored
        provider untouched.
        """
        # This write bypasses the token queue, so deltas enqueued before the
        # switch must land first: a still-queued first delta carries the
        # pre-switch route, and applying it after this UPDATE would trip the
        # first_accounted_route overwrite in update_token_counts (row sees
        # api_call_count == 0 + a route mismatch) and resurrect the old
        # model/provider. Flushing here restores the pre-queue ordering.
        self.flush_token_counts()

        def _do(conn):
            # Use the shared merge discipline so lineage markers like
            # _branched_from / _delegate_from survive. browser_model_lock
            # is deleted via a None patch value (same semantics as the
            # old json_remove).
            patch: dict[str, Any] = {"browser_model_lock": None}
            if model:
                patch["model"] = model
            if provider:
                patch["provider"] = provider
            merged = self._merge_model_config_json(conn, session_id, patch)
            if merged is _MODEL_CONFIG_ROW_MISSING:
                return
            conn.execute(
                "UPDATE sessions SET "
                "model = ?, model_config = ?, "
                "system_prompt = NULL, system_prompt_hash = NULL "
                "WHERE id = ?",
                (model, merged, session_id),
            )
            self._delete_unreferenced_system_prompts(conn)

        self._execute_write(_do)

    def _merge_model_config_json(
        self,
        conn,
        session_id: str,
        patch: dict[str, Any],
        *,
        on_missing: str = "skip",
    ):
        """SELECT + tolerant-parse + merge ``patch`` into model_config.

        Shared by every model_config writer so lineage markers such as
        ``_branched_from`` and ``_delegate_from`` survive. A ``None`` patch
        value deletes that key. Callers own the surrounding write transaction.
        """
        row = conn.execute(
            "SELECT model_config FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            if on_missing == "raise":
                raise ValueError(f"Session not found: {session_id}")
            return _MODEL_CONFIG_ROW_MISSING
        raw = row["model_config"] if isinstance(row, sqlite3.Row) else row[0]
        config: dict[str, Any] = {}
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    config = parsed
            except (json.JSONDecodeError, TypeError):
                config = {}
        elif isinstance(raw, dict):
            config = dict(raw)
        for key, value in patch.items():
            if value is None:
                config.pop(key, None)
            else:
                config[key] = value
        return json.dumps(config) if config else None

    def patch_session_model_config(
        self, session_id: str, patch: dict[str, Any]
    ) -> None:
        """Merge ``patch`` into a session's model_config JSON atomically."""
        if not session_id or not patch:
            return

        def _do(conn):
            merged = self._merge_model_config_json(conn, session_id, patch)
            if merged is _MODEL_CONFIG_ROW_MISSING:
                return
            conn.execute(
                "UPDATE sessions SET model_config = ? WHERE id = ?",
                (merged, session_id),
            )

        self._execute_write(_do)

    def get_session_model_config_value(
        self, session_id: str, key: str, default: Any = None
    ) -> Any:
        """Read one key out of a session's model_config JSON (tolerant parse)."""
        session = self.get_session(session_id) or {}
        raw = session.get("model_config")
        config: dict[str, Any] = {}
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    config = parsed
            except (json.JSONDecodeError, TypeError):
                config = {}
        elif isinstance(raw, dict):
            config = raw
        return config.get(key, default)

    def update_session_runtime_lock(
        self,
        session_id: str,
        *,
        model: str | None = None,
        provider: str | None = None,
        model_options: dict[str, Any] | None = None,
        route_source: str | None = None,
        confirmed: bool = False,
    ) -> None:
        """Persist a Browser/API runtime lock without clobbering lineage."""
        lock = {
            "provider": provider or "",
            "model": model or "",
            "model_options": model_options or {},
            "route_source": route_source or "",
            "confirmed": bool(confirmed),
            "updated_at": time.time(),
        }

        def _do(conn):
            merged = self._merge_model_config_json(
                conn, session_id, {"browser_model_lock": lock}
            )
            if merged is _MODEL_CONFIG_ROW_MISSING:
                return
            conn.execute(
                """UPDATE sessions SET
                   model_config = ?,
                   model = COALESCE(?, model),
                   system_prompt = NULL,
                   system_prompt_hash = NULL
                   WHERE id = ?""",
                (merged, model, session_id),
            )
            self._delete_unreferenced_system_prompts(conn)

        self._execute_write(_do)

    def set_session_yolo(self, session_id: str, enabled: bool) -> None:
        """Persist the per-session YOLO bypass flag into ``model_config``."""
        if not session_id:
            return

        def _do(conn):
            merged = self._merge_model_config_json(
                conn, session_id, {"yolo_mode": bool(enabled)}
            )
            if merged is _MODEL_CONFIG_ROW_MISSING:
                return
            conn.execute(
                "UPDATE sessions SET model_config = ? WHERE id = ?",
                (merged, session_id),
            )

        self._execute_write(_do)

    @staticmethod
    def session_yolo_enabled(session_meta: dict[str, Any] | None) -> bool:
        """Read the persisted YOLO flag off a session row dict."""
        raw = (session_meta or {}).get("model_config")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                logger.debug(
                    "Session yolo-mode config decode failed",
                    exc_info=_exception_info_without_values(),
                )
                return False
        if not isinstance(raw, dict):
            return False
        return bool(raw.get("yolo_mode"))

    @staticmethod
    def session_gateway_runtime(session_meta: dict[str, Any] | None) -> dict[str, Any]:
        """Read the persisted runtime route off a session row dict."""
        raw = (session_meta or {}).get("model_config")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                logger.debug(
                    "Session gateway runtime config decode failed",
                    exc_info=_exception_info_without_values(),
                )
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        runtime = raw.get("gateway_runtime")
        if isinstance(runtime, dict) and runtime.get("provider"):
            return {k: v for k, v in runtime.items() if v is not None}
        top_level = {
            key: raw.get(key)
            for key in ("provider", "base_url", "api_mode")
            if raw.get(key)
        }
        if top_level:
            return top_level
        billing_provider = str(
            (session_meta or {}).get("billing_provider") or ""
        ).strip()
        if billing_provider and billing_provider.lower() not in _BARE_BILLING_PROVIDERS:
            return {"provider": billing_provider}
        return (
            {k: v for k, v in (runtime or {}).items() if v is not None}
            if isinstance(runtime, dict)
            else {}
        )

    def update_session_billing_route(
        self,
        session_id: str,
        *,
        provider: str,
        base_url: str,
        billing_mode: str | None = None,
    ) -> None:
        """Unconditionally update the billing provider/base URL for a session."""
        # Barrier against queued token deltas — see update_session_model.
        self.flush_token_counts()

        def _do(conn):
            conn.execute(
                """UPDATE sessions SET
                   billing_provider = ?,
                   billing_base_url = ?,
                   billing_mode = COALESCE(?, billing_mode),
                   system_prompt = NULL,
                   system_prompt_hash = NULL
                   WHERE id = ?""",
                (provider, base_url, billing_mode, session_id),
            )
            self._delete_unreferenced_system_prompts(conn)

        self._execute_write(_do)
