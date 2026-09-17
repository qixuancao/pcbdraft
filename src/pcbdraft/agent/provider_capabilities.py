"""Resolve provider endpoints, timeouts, and API capability policy."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any

from pcbdraft.core.runtime_utils import (
    base_url_host_matches,
    base_url_hostname,
    env_float,
    model_forces_max_completion_tokens,
)
from pcbdraft.model.model_metadata import is_local_endpoint
from pcbdraft.model.timeouts import (
    get_provider_request_timeout,
    get_provider_stale_timeout,
)

_base_url_host_matches_hook: Callable[[str, str], bool]
_base_url_hostname_hook: Callable[[str], str]
_env_float_hook: Callable[[str, float], float]
_env_get_hook: Callable[[str], str | None]
_is_local_endpoint_hook: Callable[[str], bool]
_model_forces_max_completion_tokens_hook: Callable[[str], bool]
_model_requires_responses_api_hook: Callable[[str], bool]
_provider_request_timeout_hook: Callable[[str, str], float | None]
_provider_stale_timeout_hook: Callable[[str, str], float | None]
_regex_search_hook: Callable[[str, str], Any]


def configure_provider_capabilities_runtime(
    *,
    base_url_host_matches_fn: Callable[[str, str], bool] | None = None,
    base_url_hostname_fn: Callable[[str], str] | None = None,
    env_float_fn: Callable[[str, float], float] | None = None,
    env_get: Callable[[str], str | None] | None = None,
    is_local_endpoint_fn: Callable[[str], bool] | None = None,
    model_forces_max_completion_tokens_fn: Callable[[str], bool] | None = None,
    model_requires_responses_api: Callable[[str], bool] | None = None,
    provider_request_timeout: Callable[[str, str], float | None] | None = None,
    provider_stale_timeout: Callable[[str, str], float | None] | None = None,
    regex_search: Callable[[str, str], Any] | None = None,
) -> None:
    """Inject late-bound helpers exposed by the legacy agent module."""
    global _base_url_host_matches_hook
    global _base_url_hostname_hook
    global _env_float_hook
    global _env_get_hook
    global _is_local_endpoint_hook
    global _model_forces_max_completion_tokens_hook
    global _model_requires_responses_api_hook
    global _provider_request_timeout_hook
    global _provider_stale_timeout_hook
    global _regex_search_hook

    if base_url_host_matches_fn is not None:
        _base_url_host_matches_hook = base_url_host_matches_fn
    if base_url_hostname_fn is not None:
        _base_url_hostname_hook = base_url_hostname_fn
    if env_float_fn is not None:
        _env_float_hook = env_float_fn
    if env_get is not None:
        _env_get_hook = env_get
    if is_local_endpoint_fn is not None:
        _is_local_endpoint_hook = is_local_endpoint_fn
    if model_forces_max_completion_tokens_fn is not None:
        _model_forces_max_completion_tokens_hook = model_forces_max_completion_tokens_fn
    if model_requires_responses_api is not None:
        _model_requires_responses_api_hook = model_requires_responses_api
    if provider_request_timeout is not None:
        _provider_request_timeout_hook = provider_request_timeout
    if provider_stale_timeout is not None:
        _provider_stale_timeout_hook = provider_stale_timeout
    if regex_search is not None:
        _regex_search_hook = regex_search


class ProviderCapabilitiesMixin:
    """Classify provider endpoints and resolve request capability policy."""

    def _is_direct_openai_url(self, base_url: str | None = None) -> bool:
        """Return whether a base URL targets OpenAI's native API."""
        if base_url is not None:
            hostname = _base_url_hostname_hook(base_url)
        else:
            hostname = getattr(self, "_base_url_hostname", "") or (
                _base_url_hostname_hook(getattr(self, "_base_url_lower", ""))
            )
        return hostname == "api.openai.com"

    def _is_azure_openai_url(self, base_url: str | None = None) -> bool:
        """Return whether a base URL targets Azure OpenAI."""
        if base_url is not None:
            url = str(base_url).lower()
        else:
            url = getattr(self, "_base_url_lower", "") or ""
        return _base_url_host_matches_hook(url, "openai.azure.com")

    def _is_github_copilot_url(self, base_url: str | None = None) -> bool:
        """Return whether a URL targets GitHub Copilot's compatible API."""
        if base_url is not None:
            hostname = _base_url_hostname_hook(base_url)
        else:
            hostname = getattr(self, "_base_url_hostname", "") or (
                _base_url_hostname_hook(getattr(self, "_base_url_lower", ""))
            )
        if not hostname:
            return False
        return hostname == "api.githubcopilot.com" or hostname.endswith(
            ".githubcopilot.com"
        )

    def _resolved_api_call_timeout(self) -> float:
        """Resolve the effective per-call request timeout in seconds."""
        configured = _provider_request_timeout_hook(self.provider, self.model)
        if configured is not None:
            return configured
        return _env_float_hook("PCBDRAFT_RUNTIME_API_TIMEOUT", 1800.0)

    def _resolved_api_call_stale_timeout_base(self) -> tuple[float, bool]:
        """Resolve base stale timeout and whether the default is implicit."""
        configured = _provider_stale_timeout_hook(self.provider, self.model)
        if configured is not None:
            return configured, False

        env_timeout = _env_get_hook("PCBDRAFT_RUNTIME_API_CALL_STALE_TIMEOUT")
        if env_timeout is not None:
            return float(env_timeout), False

        from pcbdraft.agent.reasoning_timeouts import (
            get_reasoning_stale_timeout_floor,
        )

        reasoning_floor = get_reasoning_stale_timeout_floor(self.model)
        if reasoning_floor is not None:
            return reasoning_floor, False

        return 90.0, True

    def _compute_non_stream_stale_timeout(self, api_payload: Any) -> float:
        """Scale non-stream stale timeout with endpoint and request context."""
        stale_base, uses_implicit_default = self._resolved_api_call_stale_timeout_base()
        base_url = getattr(self, "_base_url", None) or self.base_url or ""
        if uses_implicit_default and base_url and _is_local_endpoint_hook(base_url):
            return float("inf")

        from pcbdraft.agent.chat_completion_helpers import (
            estimate_request_context_tokens,
        )

        estimated_tokens = estimate_request_context_tokens(api_payload)
        if estimated_tokens > 100_000:
            return max(stale_base, 240.0)
        if estimated_tokens > 50_000:
            return max(stale_base, 150.0)
        return stale_base

    def _codex_silent_hang_hint(self, model: str | None = None) -> str | None:
        """Return guidance for known silent Codex backend rejection patterns."""
        if self.api_mode != "codex_responses":
            return None
        is_codex_backend = self.provider == "openai-codex" or (
            getattr(self, "_base_url_hostname", "") == "chatgpt.com"
            and "/backend-api/codex" in (getattr(self, "_base_url_lower", "") or "")
        )
        if not is_codex_backend:
            return None
        effective_model = (model if model is not None else self.model) or ""
        model_lower = effective_model.lower()
        if not _regex_search_hook(r"(?:^|[/\-_])gpt-5\.5(?:$|[\-_])", model_lower):
            return None
        return (
            f"Codex backend appears to be silently rejecting {effective_model!r} "
            "on chatgpt.com/backend-api/codex (no stream events, no error). "
            "This is a known backend-side pattern that has affected ChatGPT "
            "Plus accounts intermittently. "
            "Workaround: try `gpt-5.4` on the same OAuth profile, or `gpt-5.3-codex`, "
            "or switch to a different model/provider in your fallback chain. "
            "Some ChatGPT Codex accounts do not support `gpt-5.4-codex`. "
            "See hermes-agent#21444 for symptom history."
        )

    def _is_openrouter_url(self) -> bool:
        """Return whether the base URL targets OpenRouter."""
        return _base_url_host_matches_hook(self._base_url_lower, "openrouter.ai")

    def _is_copilot_url(self) -> bool:
        """Return whether the base URL targets Copilot or GitHub Models."""
        return _base_url_host_matches_hook(
            self._base_url_lower, "api.githubcopilot.com"
        ) or _base_url_host_matches_hook(self._base_url_lower, "models.github.ai")

    def _is_copilot_provider(self) -> bool:
        """Return whether provider aliases or its URL identify GitHub Copilot."""
        if (self.provider or "").strip().lower() in {
            "copilot",
            "github-copilot",
            "github",
        }:
            return True
        return self._is_copilot_url()

    def _is_codex_backend(self) -> bool:
        """Return whether the active endpoint is the OAuth Codex backend."""
        return (
            getattr(self, "api_mode", None) == "codex_responses"
            and getattr(self, "_base_url_hostname", "") == "chatgpt.com"
            and "/backend-api/codex" in (getattr(self, "_base_url_lower", "") or "")
        )

    def _anthropic_prompt_cache_policy(
        self,
        *,
        provider: str | None = None,
        base_url: str | None = None,
        api_mode: str | None = None,
        model: str | None = None,
    ) -> tuple[bool, bool]:
        """Forward to the shared Anthropic prompt-cache policy owner."""
        from pcbdraft.agent.agent_runtime_helpers import anthropic_prompt_cache_policy

        return anthropic_prompt_cache_policy(
            self, provider=provider, base_url=base_url, api_mode=api_mode, model=model
        )

    def _direct_native_anthropic_tool_cache_capability(
        self,
        *,
        provider: str | None = None,
        base_url: str | None = None,
        api_mode: str | None = None,
        model: str | None = None,
    ) -> bool:
        """Forward to the native Anthropic tool-cache capability owner."""
        from pcbdraft.agent.agent_runtime_helpers import (
            _direct_native_anthropic_tool_cache_capability,
        )

        return _direct_native_anthropic_tool_cache_capability(
            self,
            provider=provider,
            base_url=base_url,
            api_mode=api_mode,
            model=model,
        )

    @staticmethod
    def _model_requires_responses_api(model: str) -> bool:
        """Return whether a model family requires the Responses API path."""
        normalized_model = model.lower()
        if "/" in normalized_model:
            normalized_model = normalized_model.rsplit("/", 1)[-1]
        return normalized_model.startswith("gpt-5")

    @staticmethod
    def _provider_model_requires_responses_api(
        model: str,
        *,
        provider: str | None = None,
    ) -> bool:
        """Return whether a provider/model pair should use Responses API."""
        normalized_provider = (provider or "").strip().lower()
        if normalized_provider in {"nous", "custom"}:
            return False
        if normalized_provider == "copilot":
            try:
                from pcbdraft.model.catalog import _should_use_copilot_responses_api

                return _should_use_copilot_responses_api(model)
            except Exception:  # noqa: BLE001, S110 - preserve generic fallback
                pass
        return _model_requires_responses_api_hook(model)

    def _max_tokens_param(self, value: int) -> dict:
        """Return the provider-compatible maximum-output-token parameter."""
        if (
            self._is_direct_openai_url()
            or self._is_azure_openai_url()
            or self._is_github_copilot_url()
            or _model_forces_max_completion_tokens_hook(self.model)
        ):
            return {"max_completion_tokens": value}
        return {"max_tokens": value}

    @staticmethod
    def _requested_output_cap_from_api_kwargs(api_kwargs: Any) -> int | None:
        """Extract the outgoing response token cap from a prepared request."""
        if not isinstance(api_kwargs, dict):
            return None
        for key in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
            raw = api_kwargs.get(key)
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None


_base_url_host_matches_hook = base_url_host_matches
_base_url_hostname_hook = base_url_hostname
_env_float_hook = env_float
_env_get_hook = os.getenv
_is_local_endpoint_hook = is_local_endpoint
_model_forces_max_completion_tokens_hook = model_forces_max_completion_tokens
_model_requires_responses_api_hook = (
    ProviderCapabilitiesMixin._model_requires_responses_api
)
_provider_request_timeout_hook = get_provider_request_timeout
_provider_stale_timeout_hook = get_provider_stale_timeout
_regex_search_hook = re.search
