# mypy: disable-error-code="attr-defined,has-type"
# Credential refresh is deliberately fail-soft across provider SDK failures.
# ruff: noqa: BLE001, S110
"""Provider credential refresh and route client reconfiguration.

``loop`` retains provider/auth authority and injects its live namespace so
legacy monkeypatch paths remain authoritative. This module never imports
``loop`` and owns no credential pool, client, or provider state.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pcbdraft.agent.error_classifier import FailoverReason

_runtime_namespace: Callable[[], dict[str, Any]] | None = None


def configure_provider_credential_refresh_runtime(
    *, namespace: Callable[[], dict[str, Any]]
) -> None:
    """Inject the compatibility module's live namespace."""

    global _runtime_namespace
    _runtime_namespace = namespace


def _runtime() -> dict[str, Any]:
    if _runtime_namespace is None:
        raise RuntimeError("provider credential refresh runtime is not configured")
    return _runtime_namespace()


class ProviderCredentialRefreshMixin:
    """Refresh active credentials and rebuild the matching provider client."""

    base_url: str | None
    api_key: str | None
    _anthropic_api_key: str | None
    _anthropic_base_url: str | None

    def _try_refresh_codex_client_credentials(self, *, force: bool = True) -> bool:
        logger = _runtime()["logger"]
        if self.api_mode != "codex_responses" or self.provider not in {
            "openai-codex",
            "xai-oauth",
        }:
            return False

        # Guard against silent account swap.
        #
        # When an agent is using a non-singleton credential — e.g. a manual
        # pool entry (``hermes auth add xai-oauth``) whose tokens belong to
        # a different account than the device_code singleton, or an agent
        # constructed with an explicit ``api_key=`` arg — force-refreshing
        # the singleton here and adopting its tokens silently re-routes the
        # rest of the conversation onto the singleton's account.  The
        # credential pool's reactive recovery (``_recover_with_credential_pool``)
        # is the right channel for that case; this path is the
        # singleton-only fallback used when the pool can't recover, and
        # MUST only fire when the agent really is on singleton tokens.
        try:
            if self.provider == "openai-codex":
                from pcbdraft.model.auth import resolve_codex_runtime_credentials

                singleton_now = resolve_codex_runtime_credentials(
                    refresh_if_expiring=False,
                )
            else:
                from pcbdraft.model.auth import resolve_xai_oauth_runtime_credentials

                singleton_now = resolve_xai_oauth_runtime_credentials(
                    refresh_if_expiring=False,
                )
        except Exception as exc:
            logger.debug("%s singleton read failed: %s", self.provider, exc)
            return False

        singleton_key = str(singleton_now.get("api_key") or "").strip()
        active_key = str(self.api_key or "").strip()
        if singleton_key and active_key and singleton_key != active_key:
            logger.debug(
                "%s singleton tokens differ from the active api_key; "
                "skipping singleton force-refresh to avoid silent account swap. "
                "Reactive credential rotation should go through the pool.",
                self.provider,
            )
            return False

        try:
            if self.provider == "openai-codex":
                from pcbdraft.model.auth import resolve_codex_runtime_credentials

                old_key = str(self.api_key or "").strip()
                creds = resolve_codex_runtime_credentials(force_refresh=force)
            else:
                from pcbdraft.model.auth import resolve_xai_oauth_runtime_credentials

                old_key = str(self.api_key or "").strip()
                creds = resolve_xai_oauth_runtime_credentials(force_refresh=force)
        except Exception as exc:
            logger.debug("%s credential refresh failed: %s", self.provider, exc)
            return False

        api_key = creds.get("api_key")
        base_url = creds.get("base_url")
        if not isinstance(api_key, str) or not api_key.strip():
            return False
        if not isinstance(base_url, str) or not base_url.strip():
            return False

        # Defect 2 fix: return False when no NEW token was actually minted.
        # resolve_codex_runtime_credentials returns the same stale token
        # when the underlying refresh fails (failure is debug-only).
        # Comparing the access token (api_key) before/after detects this.
        new_key = api_key.strip()
        if old_key and new_key == old_key:
            logger.debug(
                "%s credential refresh returned the same token; "
                "refresh likely failed silently",
                self.provider,
            )
            return False

        self.api_key = api_key.strip()
        self.base_url = base_url.strip().rstrip("/")
        self._client_kwargs["api_key"] = self.api_key
        self._client_kwargs["base_url"] = self.base_url

        return self._replace_primary_openai_client(
            reason=f"{self.provider}_credential_refresh"
        )

    def _try_refresh_nous_client_credentials(
        self,
        *,
        force: bool = True,
    ) -> bool:
        runtime = _runtime()
        logger = runtime["logger"]
        env_float = runtime["env_float"]
        if self.provider != "nous":
            return False
        # Portal serves anthropic/* on the native Messages route, so a session
        # can be holding either client kind when its short-lived invoke JWT
        # expires. Both need the refresh or the turn dies on a 401.
        if self.api_mode not in ("chat_completions", "anthropic_messages"):
            return False

        try:
            from pcbdraft.model.auth import resolve_nous_runtime_credentials

            creds = resolve_nous_runtime_credentials(
                timeout_seconds=env_float("PCBDRAFT_RUNTIME_NOUS_TIMEOUT_SECONDS", 15),
                force_refresh=force,
            )
        except Exception as exc:
            logger.debug("Nous credential refresh failed: %s", exc)
            return False

        api_key = creds.get("api_key")
        base_url = creds.get("base_url")
        if not isinstance(api_key, str) or not api_key.strip():
            return False
        if not isinstance(base_url, str) or not base_url.strip():
            return False

        self.api_key = api_key.strip()
        self.base_url = base_url.strip().rstrip("/")

        if self.api_mode == "anthropic_messages":
            self._anthropic_api_key = self.api_key
            self._anthropic_base_url = self.base_url
            self._rebuild_anthropic_client()
            return True

        self._client_kwargs["api_key"] = self.api_key
        self._client_kwargs["base_url"] = self.base_url
        # Nous requests should not inherit OpenRouter-only attribution headers.
        self._client_kwargs.pop("default_headers", None)

        return self._replace_primary_openai_client(reason="nous_credential_refresh")

    def _try_refresh_env_client_credentials(self) -> bool:
        """Adopt runtime .env credential/base-url edits at the turn boundary.

        A Settings save (desktop ``PUT /api/env``, ``hermes setup``) updates
        ``.env`` and the *saving* process's os.environ, but a live session
        worker keeps the base_url/api_key captured at agent init until it
        restarts — so an open chat silently keeps calling the old endpoint
        (#67821). Called at the start of each conversation turn, this
        re-resolves the provider's env-sourced credentials (load_env() is
        mtime-memoized, so an unchanged file costs one stat()) and rebuilds
        the client when the user edited them.

        Reacts only to env *edits* (resolved values changed since the last
        look), never to mere divergence from the agent's current values —
        credential-pool rotation and failover legitimately move the session
        off the env credential, and stomping those back every turn would
        flap. A config.yaml ``model.base_url`` (or a pool entry with a
        custom endpoint) also wins: edits are only adopted while the
        session's current base_url is still the registry default or the
        previously-seen env value.

        Covers api-key registry providers and named custom providers with a
        ``key_env`` (#67935) — the latter resolve to ``provider="custom"``
        with no registry entry, so they are matched through the runtime
        provider's config lookup instead.
        """
        logger = _runtime()["logger"]
        if self.api_mode != "chat_completions":
            return False
        if getattr(self, "_fallback_activated", False):
            return False
        try:
            from pcbdraft.model.auth import PROVIDER_REGISTRY
            from pcbdraft.model.credential_pool import get_env_prefer_dotenv
        except ImportError:
            return False

        pconfig = PROVIDER_REGISTRY.get(self.provider)
        if (
            pconfig
            and getattr(pconfig, "auth_type", "") == "api_key"
            and getattr(pconfig, "api_key_env_vars", ())
        ):
            api_key = ""
            for env_var in pconfig.api_key_env_vars:
                api_key = get_env_prefer_dotenv(env_var).strip()
                if api_key:
                    break
            if not api_key:
                return False

            env_url = ""
            if pconfig.base_url_env_var:
                env_url = (
                    get_env_prefer_dotenv(pconfig.base_url_env_var).strip().rstrip("/")
                )
            default_base = (pconfig.inference_base_url or "").strip().rstrip("/")
            base_url = env_url or default_base
            if self.provider == "kimi-coding":
                from pcbdraft.model.auth import _resolve_kimi_base_url

                base_url = _resolve_kimi_base_url(
                    api_key, pconfig.inference_base_url, env_url
                ).rstrip("/")
            elif self.provider == "zai":
                from pcbdraft.model.auth import _resolve_zai_base_url

                base_url = _resolve_zai_base_url(
                    api_key, pconfig.inference_base_url, env_url
                ).rstrip("/")
        elif self.provider == "custom":
            # Named custom provider (#67935): identity lives in config
            # (``providers.<name>`` / ``custom_providers``), the credential in
            # the env var it names via ``key_env``. Re-resolve through the
            # same config lookup the runtime resolver uses; entries without
            # ``key_env`` (inline ``api_key``, pool-backed) have no
            # env-sourced credential to watch.
            try:
                from pcbdraft.model.runtime_provider import _get_named_custom_provider
            except ImportError:
                return False
            custom_provider = _get_named_custom_provider(
                getattr(self, "requested_provider", "") or ""
            )
            if not custom_provider:
                return False
            key_env = str(custom_provider.get("key_env") or "").strip()
            if not key_env:
                return False
            api_key = get_env_prefer_dotenv(key_env).strip()
            if not api_key:
                return False
            # Custom providers pin their endpoint in config, not env — the
            # config base_url is both the resolved and the "default" base, so
            # only key edits are ever adopted here.
            default_base = (
                str(custom_provider.get("base_url") or "").strip().rstrip("/")
            )
            base_url = default_base
        else:
            return False

        if not base_url:
            return False

        resolved = (base_url, api_key)
        prev = getattr(self, "_env_creds_seen", None)
        current_base = (self.base_url or "").strip().rstrip("/")

        if prev is None:
            # First look — no baseline to diff against. Adopt only the
            # boot-default case (worker spawned before the user saved an
            # override); anything else is unattributable on turn one.
            adopt = current_base == default_base and not (
                base_url == current_base and api_key == self.api_key
            )
            # #79156: if the session already holds a pool-rotated key, do
            # not treat that divergence as a boot-time env adoption. First
            # look would otherwise stomp the rotated key with the env value
            # while leaving ``_credential_pool_entry_id`` on the fallback.
            if (
                adopt
                and api_key != self.api_key
                and getattr(self, "_credential_pool", None) is not None
                and getattr(self, "_credential_pool_entry_id", None)
            ):
                adopt = False
        else:
            # Env unchanged → no-op; any drift from self.* is rotation/
            # failover or config precedence — leave it alone. An edit is
            # only adopted while the session still runs on the registry
            # default or the previously-seen env value.
            adopt = (
                resolved != prev
                and current_base in {default_base, prev[0]}
                and not (base_url == current_base and api_key == self.api_key)
            )

        if not adopt:
            self._env_creds_seen = resolved
            return False

        from pcbdraft.interfaces.tui.route_identity import normalize_route_base_url

        route_changed = normalize_route_base_url(
            self.base_url
        ) != normalize_route_base_url(base_url)
        prior_api_key = self.api_key
        prior_base_url = self.base_url
        prior_client_kwargs = dict(self._client_kwargs)

        self.api_key = api_key
        self.base_url = base_url
        self._client_kwargs["api_key"] = self.api_key
        self._client_kwargs["base_url"] = self.base_url
        # A base-url change moves the route: TLS material and default
        # headers derived from the old endpoint must be recomputed, exactly
        # as on credential-pool rotation.
        self._reapply_route_client_config(route_changed=route_changed)

        if not self._replace_primary_openai_client(reason="env_credential_refresh"):
            # Leave the baseline un-advanced so the unchanged edit is
            # retried next turn, and roll the agent back so its state keeps
            # matching the still-live old client.
            self.api_key = prior_api_key
            self.base_url = prior_base_url
            self._client_kwargs.clear()
            self._client_kwargs.update(prior_client_kwargs)
            return False

        # Rebind the pool entry id to the key we just adopted. Leaving a
        # stale id after a key rewrite makes mark_exhausted_and_rotate
        # quarantine the wrong credential on the next 429 (#79156).
        try:
            from pcbdraft.agent.agent_runtime_helpers import (
                sync_credential_pool_entry_id,
            )

            sync_credential_pool_entry_id(self)
        except Exception:
            logger.debug(
                "sync_credential_pool_entry_id after env refresh failed",
                exc_info=True,
            )

        self._env_creds_seen = resolved
        logger.info(
            "Applied updated .env credentials for %s: endpoint %s",
            self.provider,
            self.base_url,
        )
        return True

    def _try_refresh_vertex_client_credentials(self) -> bool:
        """Re-mint the Vertex OAuth2 access token and rebuild the OpenAI client.

        Vertex tokens live ~1 hour. On a long-lived agent (gateway session) a
        cached client's bearer token will expire mid-session, producing a 401.
        This re-resolves credentials via the adapter (which refreshes the
        underlying google-auth Credentials object when near expiry), swaps the
        new token into the client kwargs, and rebuilds the primary OpenAI
        client. Returns True when a usable token+base_url were obtained.
        """
        logger = _runtime()["logger"]
        if self.api_mode != "chat_completions" or self.provider != "vertex":
            return False

        try:
            from pcbdraft.agent.vertex_adapter import get_vertex_config

            token, base_url = get_vertex_config()
        except Exception as exc:
            logger.debug("Vertex credential refresh failed: %s", exc)
            return False

        if not isinstance(token, str) or not token.strip():
            return False
        if not isinstance(base_url, str) or not base_url.strip():
            return False

        self.api_key = token.strip()
        self.base_url = base_url.strip().rstrip("/")
        self._client_kwargs["api_key"] = self.api_key
        self._client_kwargs["base_url"] = self.base_url

        if not self._replace_primary_openai_client(reason="vertex_credential_refresh"):
            return False

        logger.info("Vertex AI OAuth token refreshed")
        return True

    def _try_refresh_copilot_client_credentials(self) -> bool:
        """Refresh Copilot credentials and rebuild the shared OpenAI client.

        The raw GitHub OAuth token (`gh auth token`) is usually stable, but the
        short-TTL *exchanged* IDE token minted from it is what Copilot actually
        authenticates — and it expires mid-session. A heavy/long turn whose
        request straddles that expiry gets a clean `401 IDE token expired:
        unauthorized: token expired`. Simply re-resolving the (unchanged) raw
        token and rebuilding the client leaves the SAME expired IDE token on the
        wire, so the retry 401s again and the turn aborts as non-retryable —
        only a gateway restart helped, because a cold process re-runs the
        exchange. Fix: force a fresh exchange (evict the cached exchanged JWT,
        then mint a new one) so the retry carries a valid IDE token. Mirrors the
        400 stale-credential recovery; the caller enforces the single-shot guard.
        """
        logger = _runtime()["logger"]
        if not self._is_copilot_provider():
            return False

        try:
            from pcbdraft.interfaces.tui.copilot_auth import (
                evict_cached_exchanged_token,
                get_copilot_api_token,
                resolve_copilot_token,
            )

            new_token, token_source = resolve_copilot_token()
        except Exception as exc:
            logger.debug("Copilot credential refresh failed: %s", exc)
            return False

        if not isinstance(new_token, str) or not new_token.strip():
            return False

        new_token = new_token.strip()

        # Force a fresh IDE-token exchange: the cached exchanged JWT is the thing
        # that expired ("401 IDE token expired"), so evict it and re-mint before
        # rebuilding the client. Fall back to the resolved (raw) token only if the
        # exchange itself is unavailable (network blip) — a client rebuild on the
        # raw token still clears stale client state and may recover on enterprise
        # seats where headers matter.
        try:
            evict_cached_exchanged_token(new_token)
            api_token, enterprise_base_url = get_copilot_api_token(new_token)
            if isinstance(api_token, str) and api_token.strip():
                new_token = api_token.strip()
                if enterprise_base_url:
                    self.base_url = enterprise_base_url.rstrip("/")
        except Exception as exc:
            logger.debug(
                "Copilot 401 re-exchange failed, using resolved token: %s", exc
            )

        self.api_key = new_token
        self._client_kwargs["api_key"] = self.api_key
        self._client_kwargs["base_url"] = self.base_url
        self._apply_client_headers_for_base_url(str(self.base_url or ""))

        if not self._replace_primary_openai_client(reason="copilot_credential_refresh"):
            return False

        logger.info("Copilot credentials refreshed from %s", token_source)
        return True

    def _try_recover_stale_copilot_credential(self) -> bool:
        """Force a fresh Copilot token exchange + client rebuild after a 400.

        Copilot surfaces a stale/degraded credential as a
        ``400 model_not_available_for_integrator`` /
        ``model_not_supported`` — NOT a clean 401 — so the normal 401 refresh
        path never fires. The most common trigger is a raw ``ghu_`` OAuth token
        that got seeded (and cached) when the startup token exchange degraded:
        the raw token routes the request to the restricted
        ``copilot-language-server`` integrator whose allowlist omits
        enterprise-only models (e.g. ``claude-opus-4.8``).

        Recovery = evict the poisoned cache entry, force a fresh exchange to
        mint the real ~437-char API token, re-apply the Copilot headers, and
        rebuild the shared client. Single-shot (guarded by the caller) so a
        genuinely unavailable model can't loop.
        """
        logger = _runtime()["logger"]
        if not self._is_copilot_provider():
            return False

        try:
            from pcbdraft.interfaces.tui.copilot_auth import (
                evict_cached_exchanged_token,
                get_copilot_api_token,
                resolve_copilot_token,
            )

            raw_token, token_source = resolve_copilot_token()
            if not isinstance(raw_token, str) or not raw_token.strip():
                return False
            raw_token = raw_token.strip()

            # Drop any cached (possibly degraded/raw) exchanged token so the
            # next exchange hits the network and mints a fresh one.
            evict_cached_exchanged_token(raw_token)

            api_token, enterprise_base_url = get_copilot_api_token(raw_token)
        except Exception as exc:
            logger.debug("Copilot stale-credential recovery failed: %s", exc)
            return False

        if not isinstance(api_token, str) or not api_token.strip():
            return False

        # If the exchange STILL degraded to the raw token, a rebuild won't help
        # — don't burn the single-shot retry on an identical request.
        if api_token == raw_token and not enterprise_base_url:
            logger.warning(
                "Copilot stale-credential recovery: exchange still degraded to "
                "raw token; skipping retry (network/exchange endpoint unavailable)."
            )
            return False

        self.api_key = api_token.strip()
        if enterprise_base_url:
            self.base_url = enterprise_base_url.rstrip("/")
        self._client_kwargs["api_key"] = self.api_key
        self._client_kwargs["base_url"] = self.base_url
        self._apply_client_headers_for_base_url(str(self.base_url or ""))

        if not self._replace_primary_openai_client(
            reason="copilot_stale_credential_recovery"
        ):
            return False

        logger.info(
            "Copilot credentials re-exchanged after stale-credential 400 (source=%s)",
            token_source,
        )
        return True

    def _try_refresh_anthropic_client_credentials(self) -> bool:
        runtime = _runtime()
        logger = runtime["logger"]
        base_url_host_matches = runtime["base_url_host_matches"]
        get_provider_request_timeout = runtime["get_provider_request_timeout"]
        if self.api_mode != "anthropic_messages" or not hasattr(
            self, "_anthropic_api_key"
        ):
            return False
        # Only refresh credentials for the native Anthropic provider.
        # Other anthropic_messages providers (MiniMax, Alibaba, etc.) use their own keys.
        if self.provider != "anthropic":
            return False
        # Azure endpoints use static API keys — OAuth token rotation doesn't apply.
        # Refreshing would pick up ~/.claude/.credentials.json OAuth token and break auth.
        _base = getattr(self, "_anthropic_base_url", "") or ""
        if base_url_host_matches(_base, "azure.com"):
            return False

        try:
            from pcbdraft.model.anthropic_adapter import (
                build_anthropic_client,
                resolve_anthropic_token,
            )

            new_token = resolve_anthropic_token()
        except Exception as exc:
            logger.debug("Anthropic credential refresh failed: %s", exc)
            return False

        if not isinstance(new_token, str) or not new_token.strip():
            return False
        new_token = new_token.strip()
        if new_token == self._anthropic_api_key:
            return False

        try:
            self._anthropic_client.close()
        except Exception:
            pass

        try:
            self._anthropic_client = build_anthropic_client(
                new_token,
                getattr(self, "_anthropic_base_url", None),
                timeout=get_provider_request_timeout(self.provider, self.model),
            )
        except Exception as exc:
            logger.warning(
                "Failed to rebuild Anthropic client after credential refresh: %s", exc
            )
            return False

        self._anthropic_api_key = new_token
        # Update OAuth flag — token type may have changed (API key ↔ OAuth).
        # Only treat as OAuth on native Anthropic; third-party endpoints using
        # the Anthropic protocol must not trip OAuth paths (#1739 & third-party
        # identity-injection guard).
        from pcbdraft.model.anthropic_adapter import _is_oauth_token

        self._is_anthropic_oauth = (
            _is_oauth_token(new_token) if self.provider == "anthropic" else False
        )
        return True

    def _apply_client_headers_for_base_url(
        self,
        base_url: str,
        *,
        apply_user_headers: bool = True,
    ) -> None:
        runtime = _runtime()
        logger = runtime["logger"]
        base_url_host_matches = runtime["base_url_host_matches"]
        _routermint_headers = runtime["_routermint_headers"]
        _qwen_portal_headers = runtime["_qwen_portal_headers"]
        from pcbdraft.model.auxiliary_client import (
            _AI_GATEWAY_HEADERS,
            build_nvidia_nim_headers,
            build_or_headers,
        )

        if base_url_host_matches(base_url, "openrouter.ai"):
            self._client_kwargs["default_headers"] = build_or_headers()
        elif base_url_host_matches(base_url, "ai-gateway.vercel.sh"):
            self._client_kwargs["default_headers"] = dict(_AI_GATEWAY_HEADERS)
        elif base_url_host_matches(base_url, "integrate.api.nvidia.com"):
            self._client_kwargs["default_headers"] = build_nvidia_nim_headers(base_url)
        elif base_url_host_matches(base_url, "api.routermint.com"):
            self._client_kwargs["default_headers"] = _routermint_headers()
        elif base_url_host_matches(base_url, "githubcopilot.com"):
            from pcbdraft.model.catalog import copilot_default_headers

            self._client_kwargs["default_headers"] = copilot_default_headers()
        elif base_url_host_matches(base_url, "api.kimi.com"):
            from pcbdraft.model.auxiliary_client import _AI_GATEWAY_HEADERS

            self._client_kwargs["default_headers"] = dict(_AI_GATEWAY_HEADERS)
        elif base_url_host_matches(base_url, "portal.qwen.ai"):
            self._client_kwargs["default_headers"] = _qwen_portal_headers()
        elif base_url_host_matches(base_url, "chatgpt.com"):
            from pcbdraft.model.auxiliary_client import _codex_cloudflare_headers

            self._client_kwargs["default_headers"] = _codex_cloudflare_headers(
                self._client_kwargs.get("api_key", "")
            )
        elif base_url_host_matches(base_url, "x.ai"):
            # Cover both provider=xai and provider=xai-oauth (api.x.ai).
            from pcbdraft.tools.xai_http import pcbdraft_xai_default_headers

            self._client_kwargs["default_headers"] = pcbdraft_xai_default_headers()
        else:
            # No URL-specific headers — check profile.default_headers before clearing.
            _ph_headers = None
            try:
                from pcbdraft.model.provider_profiles import (
                    get_provider_profile as _gpf2,
                )

                _ph2 = _gpf2(self.provider)
                if _ph2 and _ph2.default_headers:
                    _ph_headers = dict(_ph2.default_headers)
            except Exception:
                pass
            if _ph_headers:
                self._client_kwargs["default_headers"] = _ph_headers
            else:
                self._client_kwargs.pop("default_headers", None)

        # User-configured overrides win over URL/profile defaults for the same
        # route. A credential swap to another endpoint must not inherit them.
        if apply_user_headers:
            self._apply_user_default_headers()

        # Per-provider extra HTTP headers (providers.<name>.extra_headers /
        # custom_providers[].extra_headers) — applied last so the most
        # specific config level survives credential swaps and rebuilds too.
        # SECURITY: values may carry credentials — never log them.
        if self.api_mode not in ("anthropic_messages", "bedrock_converse"):
            try:
                from pcbdraft.model.configuration import (
                    apply_custom_provider_extra_headers_to_client_kwargs,
                )

                apply_custom_provider_extra_headers_to_client_kwargs(
                    self._client_kwargs,
                    base_url,
                )
            except Exception:
                logger.debug("custom-provider extra_headers skipped", exc_info=True)

    def _apply_user_default_headers(self) -> None:
        """Merge user-configured request headers onto the OpenAI client.

        Reads ``model.default_headers`` from config.yaml and merges it onto
        ``self._client_kwargs["default_headers"]``, with user values taking
        precedence over provider- and SDK-supplied defaults.

        This exists for ``custom`` OpenAI-compatible endpoints sitting behind
        a gateway/WAF that rejects the OpenAI Python SDK's identifying headers
        (``User-Agent: OpenAI/Python ...``, ``X-Stainless-*``). Setting e.g.
        ``model.default_headers: {User-Agent: curl/8.7.1}`` lets the request
        reach such an upstream instead of failing with an opaque 4xx/502 even
        though the same body works under ``curl``. (#40033)

        Delegates the config read + merge to
        ``agent.auxiliary_client._apply_user_default_headers`` so the main and
        auxiliary clients can never drift on precedence or value handling.

        No-op for Anthropic/Bedrock modes, which don't use the OpenAI client,
        and when no overrides are configured.
        """
        if self.api_mode in ("anthropic_messages", "bedrock_converse"):
            return
        from pcbdraft.model.auxiliary_client import (
            _apply_user_default_headers as _merge_user_headers,
        )

        merged = _merge_user_headers(self._client_kwargs.get("default_headers"))
        if merged:
            self._client_kwargs["default_headers"] = merged

    def _swap_credential(self, entry) -> None:
        get_provider_request_timeout = _runtime()["get_provider_request_timeout"]
        runtime_key = getattr(entry, "runtime_api_key", None) or getattr(
            entry, "access_token", ""
        )
        runtime_base = (
            getattr(entry, "runtime_base_url", None)
            or getattr(entry, "base_url", None)
            or self.base_url
        )
        self._credential_pool_entry_id = getattr(entry, "id", None)
        from pcbdraft.interfaces.tui.route_identity import normalize_route_base_url

        route_changed = normalize_route_base_url(
            self.base_url
        ) != normalize_route_base_url(runtime_base)

        if self.api_mode == "anthropic_messages":
            from pcbdraft.model.anthropic_adapter import (
                _is_oauth_token,
                build_anthropic_client,
            )

            try:
                self._anthropic_client.close()
            except Exception:
                pass

            self._anthropic_api_key = runtime_key
            self._anthropic_base_url = (
                runtime_base.rstrip("/")
                if isinstance(runtime_base, str)
                else None
            )
            self._anthropic_client = build_anthropic_client(
                runtime_key,
                self._anthropic_base_url,
                timeout=get_provider_request_timeout(self.provider, self.model),
            )
            self._is_anthropic_oauth = (
                _is_oauth_token(runtime_key) if self.provider == "anthropic" else False
            )
            self.api_key = runtime_key
            self.base_url = (
                runtime_base.rstrip("/")
                if isinstance(runtime_base, str)
                else None
            )
            return

        self.api_key = runtime_key
        self.base_url = (
            runtime_base.rstrip("/") if isinstance(runtime_base, str) else None
        )
        self._client_kwargs["api_key"] = self.api_key
        self._client_kwargs["base_url"] = self.base_url
        self._reapply_route_client_config(route_changed=route_changed)
        self._replace_primary_openai_client(reason="credential_rotation")

    def _reapply_route_client_config(self, *, route_changed: bool) -> None:
        """Recompute route-derived client kwargs for the current ``self.base_url``.

        TLS material (``ssl_verify``/``ssl_ca_cert``) and default headers are
        derived from the endpoint, not the credential — any client rebuild
        that may have moved ``base_url`` must recompute them or the new
        endpoint inherits configuration computed for the old one. Shared by
        credential-pool rotation and the per-turn env refresh so the two
        paths cannot drift.
        """
        logger = _runtime()["logger"]
        self._client_kwargs.pop("ssl_verify", None)
        self._client_kwargs.pop("ssl_ca_cert", None)
        try:
            from pcbdraft.model.configuration import (
                apply_custom_provider_tls_to_client_kwargs,
                get_compatible_custom_providers,
                load_config_readonly,
            )

            apply_custom_provider_tls_to_client_kwargs(
                self._client_kwargs,
                str(self.base_url or ""),
                get_compatible_custom_providers(load_config_readonly()),
            )
        except Exception:
            logger.debug(
                "custom-provider TLS resolution skipped on credential rotation",
                exc_info=True,
            )
        self._apply_client_headers_for_base_url(
            str(self.base_url or ""),
            apply_user_headers=not route_changed,
        )

    def _recover_with_credential_pool(
        self,
        *,
        status_code: int | None,
        has_retried_429: bool,
        classified_reason: FailoverReason | None = None,
        error_context: dict[str, Any] | None = None,
        billing_unverified: bool = False,
    ) -> tuple[bool, bool]:
        """Forwarder — see ``agent.agent_runtime_helpers.recover_with_credential_pool``."""
        from pcbdraft.agent.agent_runtime_helpers import recover_with_credential_pool

        return recover_with_credential_pool(
            self,
            status_code=status_code,
            has_retried_429=has_retried_429,
            classified_reason=classified_reason,
            error_context=error_context,
            billing_unverified=billing_unverified,
        )

    def _credential_pool_may_recover_rate_limit(self) -> bool:
        """Whether a rate-limit retry should wait for same-provider credentials."""
        pool = self._credential_pool
        if pool is None:
            return False
        return pool.has_available()
