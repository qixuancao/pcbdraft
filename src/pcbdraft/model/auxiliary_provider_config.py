"""Provider catalog, model rules, headers, and endpoint normalization."""

# The broad exception guards are retained from auxiliary_client: provider
# discovery and optional configuration must continue to fail closed.
# ruff: noqa: BLE001, S110

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from pcbdraft.agent.portal_tags import nous_portal_tags as _default_nous_portal_tags
from pcbdraft.core.runtime_utils import (
    base_url_host_matches,
    is_truthy_value,
)
from pcbdraft.interfaces.tui import __version__ as _PCBDRAFT_VERSION

logger = logging.getLogger(__name__)


def _default_read_main_provider() -> str:
    try:
        from pcbdraft.model.configuration import load_config_readonly

        config = load_config_readonly()
        model = config.get("model", {}) if isinstance(config, dict) else {}
        if isinstance(model, dict):
            return str(model.get("provider", "") or "")
    except Exception:
        pass
    return ""


def _default_get_auxiliary_task_config(task: str) -> dict[str, Any]:
    try:
        from pcbdraft.model.configuration import load_config_readonly

        config = load_config_readonly()
        auxiliary = config.get("auxiliary", {}) if isinstance(config, dict) else {}
        task_config = auxiliary.get(task, {}) if isinstance(auxiliary, dict) else {}
        return task_config if isinstance(task_config, dict) else {}
    except Exception:
        return {}


_read_main_provider_hook: Callable[[], str] = _default_read_main_provider
_get_auxiliary_task_config_hook: Callable[[str], dict[str, Any]] = (
    _default_get_auxiliary_task_config
)
_nous_portal_tags_hook: Callable[[], list[str]] = _default_nous_portal_tags
_fast_model_from_catalog_hook: Callable[[str], str]


def _configure_auxiliary_provider_config_runtime(
    *,
    read_main_provider: Callable[[], str] | None = None,
    get_auxiliary_task_config: Callable[[str], dict[str, Any]] | None = None,
    nous_portal_tags: Callable[[], list[str]] | None = None,
    fast_model_from_catalog: Callable[[str], str] | None = None,
) -> None:
    """Connect config helpers to the owning auxiliary-client runtime."""
    global _read_main_provider_hook
    global _get_auxiliary_task_config_hook
    global _nous_portal_tags_hook
    global _fast_model_from_catalog_hook

    if read_main_provider is not None:
        _read_main_provider_hook = read_main_provider
    if get_auxiliary_task_config is not None:
        _get_auxiliary_task_config_hook = get_auxiliary_task_config
    if nous_portal_tags is not None:
        _nous_portal_tags_hook = nous_portal_tags
    if fast_model_from_catalog is not None:
        _fast_model_from_catalog_hook = fast_model_from_catalog


_PROVIDER_ALIASES = {
    "google": "gemini",
    "google-gemini": "gemini",
    "google-ai-studio": "gemini",
    "x-ai": "xai",
    "x.ai": "xai",
    "grok": "xai",
    "glm": "zai",
    "z-ai": "zai",
    "z.ai": "zai",
    "zhipu": "zai",
    "kimi": "kimi-coding",
    "moonshot": "kimi-coding",
    "kimi-cn": "kimi-coding-cn",
    "moonshot-cn": "kimi-coding-cn",
    "gmi-cloud": "gmi",
    "gmicloud": "gmi",
    "actual-computer": "actual",
    "actualcomputer": "actual",
    "aci": "actual",
    "minimax-china": "minimax-cn",
    "minimax_cn": "minimax-cn",
    "claude": "anthropic",
    "claude-code": "anthropic",
    "github": "copilot",
    "github-copilot": "copilot",
    "github-model": "copilot",
    "github-models": "copilot",
    "github-copilot-acp": "copilot-acp",
    "copilot-acp-agent": "copilot-acp",
    "tencent": "tencent-tokenhub",
    "tokenhub": "tencent-tokenhub",
    "tencent-cloud": "tencent-tokenhub",
    "tencentmaas": "tencent-tokenhub",
}


def _normalize_aux_provider(provider: str | None) -> str:
    normalized = (provider or "auto").strip().lower()
    if normalized.startswith("custom:"):
        suffix = normalized.split(":", 1)[1].strip()
        if not suffix:
            return "custom"
        normalized = suffix
    if normalized == "codex":
        return "openai-codex"
    if normalized == "main":
        # Resolve to the user's actual main provider so named custom providers
        # and non-aggregator providers (DeepSeek, Alibaba, etc.) work correctly.
        main_prov = (_read_main_provider_hook() or "").strip().lower()
        if main_prov and main_prov not in {"auto", "main", ""}:
            normalized = main_prov
        else:
            return "custom"
    return _PROVIDER_ALIASES.get(normalized, normalized)


# Sentinel: when returned by _fixed_temperature_for_model(), callers must
# strip the ``temperature`` key from API kwargs entirely so the provider's
# server-side default applies.  Kimi/Moonshot models manage temperature
# internally — sending *any* value (even the "correct" one) can conflict
# with gateway-side mode selection (thinking → 1.0, non-thinking → 0.6).
OMIT_TEMPERATURE: object = object()


def _is_kimi_model(model: str | None) -> bool:
    """True for any Kimi / Moonshot model that manages temperature server-side."""
    bare = (model or "").strip().lower().rsplit("/", 1)[-1]
    return bare.startswith("kimi-") or bare == "kimi"


def _is_arcee_trinity_thinking(model: str | None) -> bool:
    """True for Arcee Trinity Large Thinking (direct or via OpenRouter)."""
    bare = (model or "").strip().lower().rsplit("/", 1)[-1]
    return bare == "trinity-large-thinking"


# Context window enforced by ChatGPT's Codex OAuth backend for the
# gpt-5.4 / gpt-5.5 / gpt-5.6 families. The raw OpenAI API and OpenRouter
# expose 1.05M for the same slugs, but the Codex backend hard-caps at 272K
# (verified live for 5.4/5.5: a ~330K-token request to
# chatgpt.com/backend-api/codex/responses is rejected with
# ``context_length_exceeded`` while ~250K succeeds; gpt-5.6 shares the same
# 272K Codex cap — see _CODEX_OAUTH_CONTEXT_FALLBACK in model_metadata.py).
# With a 272K ceiling the default 50% compaction trigger fires at ~136K —
# wasteful, since the model can hold far more raw context before
# summarization actually buys anything. We raise the trigger to 85% (~231K)
# on this exact route so Codex gpt-5.4 / gpt-5.5 / gpt-5.6 sessions use the
# window they actually have.
_CODEX_GPT54_GPT55_COMPACTION_THRESHOLD = 0.85

# gpt-5.3-codex-spark is Codex-OAuth-only (ChatGPT Pro entitlement) with a
# native 128K context window.  The default 50% compaction trigger fires at
# ~64K — wasting half the usable window, often before the session has enough
# turns to summarize meaningfully.  We raise the trigger to 70% (~90K) so
# spark sessions use more of the window before summarization, while still
# leaving ~38K headroom for the summary and continued conversation before
# the 128K hard limit.
_CODEX_SPARK_COMPACTION_THRESHOLD = 0.70


def _is_codex_gpt54_or_gpt55(model: str | None, provider: str | None = None) -> bool:
    """True for gpt-5.4 / gpt-5.5 / gpt-5.6 on the ChatGPT Codex OAuth backend.

    Matches only the Codex OAuth route (provider ``openai-codex``), not the
    direct OpenAI API, OpenRouter, or GitHub Copilot paths — those expose a
    larger context window for the same slug and must keep the user's default
    compaction threshold. ``-pro`` variants and dated snapshots are matched
    via prefix so the override tracks every 272K-capped family (5.4, 5.5,
    5.6 sol/terra/luna incl. their ``-pro`` modes) without re-listing every
    variant. (Name kept for backward compatibility with the
    ``compression.codex_gpt55_autoraise`` config key.)
    """
    prov = (provider or "").strip().lower()
    if prov != "openai-codex":
        return False
    bare = (model or "").strip().lower().rsplit("/", 1)[-1]
    return bare in {"gpt-5.4", "gpt-5.5", "gpt-5.6"} or bare.startswith(
        ("gpt-5.4-", "gpt-5.4.", "gpt-5.5-", "gpt-5.5.", "gpt-5.6-", "gpt-5.6.")
    )


def _is_codex_spark(model: str | None, provider: str | None = None) -> bool:
    """True for ``gpt-5.3-codex-spark`` on the ChatGPT Codex OAuth backend.

    The model is Codex-OAuth-only (ChatGPT Pro entitlement) with a native
    128K context window.  Only the Codex OAuth route (provider
    ``openai-codex``) is matched — the slug is not available on other
    routes.
    """
    prov = (provider or "").strip().lower()
    if prov != "openai-codex":
        return False
    bare = (model or "").strip().lower().rsplit("/", 1)[-1]
    return bare == "gpt-5.3-codex-spark"


def _fixed_temperature_for_model(
    model: str | None,
    base_url: str | None = None,
) -> float | None | object:
    """Return a temperature directive for models with strict contracts.

    Returns:
        ``OMIT_TEMPERATURE`` — caller must remove the ``temperature`` key so the
            provider chooses its own default.  Used for all Kimi / Moonshot
            models whose gateway selects temperature server-side.
        ``float`` — a specific value the caller must use (reserved for future
            models with fixed-temperature contracts).
        ``None`` — no override; caller should use its own default.
    """
    if _is_kimi_model(model):
        logger.debug("Omitting temperature for Kimi model %r (server-managed)", model)
        return OMIT_TEMPERATURE
    if _is_arcee_trinity_thinking(model):
        return 0.5
    return None


def _compression_threshold_for_model(
    model: str | None,
    provider: str | None = None,
    *,
    allow_codex_gpt55_autoraise: bool = True,
) -> float | None:
    """Return a context-compression threshold override for specific models.

    The threshold is the fraction of the model's context window that must be
    consumed before Hermes triggers summarization.  Higher values delay
    compression and preserve more raw context.

    Per-model/route overrides:
      - Arcee Trinity Large Thinking → 0.75 (preserve reasoning context).
      - gpt-5.4 / gpt-5.5 / gpt-5.6 on the Codex OAuth route → 0.85, because
        Codex caps all three families at 272K and the default 50% trigger
        would compact at ~136K. Gated by ``allow_codex_gpt55_autoraise``
        (historical config-key name kept for backward compatibility) so the
        user can opt back down to the global default (the caller passes the
        config flag through here).
      - gpt-5.3-codex-spark on the Codex OAuth route → 0.70, because the model
        has a native 128K window and the default 50% trigger would compact at
        ~64K — wasting half the usable context. Not gated by the gpt-5.5
        opt-out flag: 128K is the model's native window, so the raise is
        unambiguously correct.

    Returns a float in (0, 1] to override the global ``compression.threshold``
    config value, or ``None`` to leave the user's config value unchanged.
    """
    if _is_arcee_trinity_thinking(model):
        return 0.75
    if allow_codex_gpt55_autoraise and _is_codex_gpt54_or_gpt55(model, provider):
        return _CODEX_GPT54_GPT55_COMPACTION_THRESHOLD
    if _is_codex_spark(model, provider):
        return _CODEX_SPARK_COMPACTION_THRESHOLD
    return None


# Model-family priority for the auxiliary "fast tier", fastest first.
#
# Matched as substrings against the provider's LIVE /v1/models catalog rather
# than pinned as exact ids, because exact ids rot: a hardcoded
# "google/gemini-3-flash" kept 404ing here once Nous dropped it upstream, and
# every aux call paid a wasted round-trip before the retry net caught it.
# Families outlive their version numbers, so a new mini/flash/haiku release is
# picked up with no source edit.
#
# Rolling "-latest" aliases come first where a provider publishes them (Nous
# serves ~openai/gpt-mini-latest, ~google/gemini-flash-latest, …): they are the
# only ids that are structurally rot-proof.
#
# Order is measured, not guessed — p50 on a real titling prompt against the
# Nous catalog: gpt-mini-latest 1.40s, claude-haiku-latest 1.55s,
# gemini-flash-latest 2.13s, step-3.7-flash 7.84s, grok-4.1-fast 8.05s. So the
# first family a provider actually serves is also the fastest it can offer.
_FAST_MODEL_FAMILIES: tuple = (
    "gpt-mini-latest",
    "gpt-nano-latest",
    "claude-haiku-latest",
    "gemini-flash-latest",
    "gpt-5.4-nano",
    "gpt-5.4-mini",
    "gpt-5-mini",
    "haiku-4.5",
    "gemini-3.6-flash",
    "flash-lite",
    "-nano",
    "-mini",
    "-flash",
    "haiku",
)

# Substrings that disqualify an otherwise-matching id. Reasoning variants
# ("o3-mini", "gpt-5.4-mini-thinking") think before answering, which is the
# opposite of what a titler wants; ":batch" is an async queue, not a live
# endpoint; embedding models ("all-minilm") match "-mini" but aren't chat
# models at all; ":free" tiers are heavily rate-limited and measured slowest.
# The modality suffixes are the same trap as the embedders — a provider names
# its speech and image endpoints after the chat model they're paired with, so
# "gpt-4o-mini-tts" satisfies the "-mini" rung and cannot answer a prompt.
_FAST_MODEL_EXCLUDE: tuple = (
    "thinking",
    "reason",
    "-r1",
    "minilm",
    ":batch",
    ":free",
    "o1-",
    "o3-",
    "o4-",
    "codex",
    "audio",
    "-vl",
    "embed",
    "-tts",
    "-transcribe",
    "-realtime",
    "-image",
    "-search-preview",
)


_VERSION_CHUNK_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _model_recency_key(model_id: str) -> tuple:
    """Sort key that puts a family's newest release first (descending).

    The rungs at the bottom of ``_FAST_MODEL_FAMILIES`` are bare family names —
    ``-mini``, ``-flash``, ``haiku`` — and a provider serves every generation of
    those it hasn't retired. Compared as plain strings, the oldest wins:
    ``gpt-3.5-mini`` sorts before ``gpt-5.4-mini``, and ``claude-3-haiku`` before
    ``claude-haiku-4.5``. So the rung meant to keep us current on a provider's
    small tier was pinning us to its most obsolete member.

    Splitting digit runs out and comparing them as numbers fixes both the
    generation order and the 9-vs-10 cliff a string sort walks off.
    """
    chunks = []
    for index, part in enumerate(_VERSION_CHUNK_RE.split(model_id.lower())):
        if not part:
            continue
        # re.split with one capturing group alternates text, number, text, …
        chunks.append((1, float(part), "") if index % 2 else (0, 0.0, part))
    return tuple(chunks)


def _fast_model_from_catalog(provider_id: str) -> str:
    """Pick the fastest small model the provider ACTUALLY serves right now.

    Reads the provider's live (cached) ``/v1/models`` catalog and returns the
    newest ``_FAST_MODEL_FAMILIES`` match. Returns "" when the catalog is
    unavailable or holds no small model, so the caller falls through to the
    provider's curated default. Never raises and never blocks on a cold
    network path — the underlying fetch is memory+disk cached with a
    last-known-good fallback.
    """
    try:
        from pcbdraft.model.auth import resolve_api_key_provider_credentials
        from pcbdraft.model.catalog import fetch_models_with_pricing
        from pcbdraft.model.provider_profiles import get_provider_profile

        # The provider's own credentials, because most ``/v1/models`` endpoints
        # are authenticated: fetched anonymously they 401, and the caller reads
        # that as "this provider serves no small model" and quietly falls back
        # to the curated default forever.
        api_key, base_url = "", ""
        try:
            creds = resolve_api_key_provider_credentials(provider_id) or {}
            api_key = str(creds.get("api_key", "")).strip()
            base_url = str(creds.get("base_url", "")).strip()
        except Exception:
            # Not an API-key provider, or nothing configured yet. The anonymous
            # fetch below still works for the catalogs that allow it.
            logger.debug("No credentials for %s catalog", provider_id, exc_info=True)

        if not base_url:
            base_url = str(
                getattr(get_provider_profile(provider_id), "base_url", "") or ""
            )
        base_url = base_url.rstrip("/")
        if not base_url:
            return ""
        # fetch_models_with_pricing appends its own /v1/models.
        base_url = base_url.removesuffix("/v1")
        catalog = (
            fetch_models_with_pricing(
                api_key=api_key or None, base_url=base_url, timeout=3.0
            )
            or {}
        )
    except Exception:
        logger.debug(
            "Fast-model catalog lookup failed for %s", provider_id, exc_info=True
        )
        return ""

    ids = sorted((str(m) for m in catalog), key=_model_recency_key, reverse=True)
    for family in _FAST_MODEL_FAMILIES:
        for model_id in ids:
            lowered = model_id.lower()
            if family in lowered and not any(x in lowered for x in _FAST_MODEL_EXCLUDE):
                return model_id
    return ""


# Default auxiliary models for direct API-key providers (cheap/fast for side tasks)
def _get_aux_model_for_provider(provider_id: str, *, prefer_fast: bool = False) -> str:
    """Return the cheap auxiliary model for a provider.

    Resolution ladder, fastest-and-most-live first:

    1. ``prefer_fast`` only — a family match against the provider's LIVE
       ``/v1/models`` catalog, preferring rolling ``-latest`` aliases. This is
       both rot-proof and latency-ordered.
    2. ``prefer_fast`` only — the provider's own recommendation hook
       (``ProviderProfile.resolve_aux_model``). Live, but tuned for *quality*
       on long-context side tasks (Nous returns its compaction pick), so it
       ranks below the catalog match for latency-critical work.
    3. ``ProviderProfile.default_aux_model`` — curated, hardcoded, may rot.
    4. The legacy hardcoded dict, for providers predating the profiles system.

    ``prefer_fast`` is opt-in so this only changes latency-critical tasks
    (titling). Every other auxiliary caller keeps the existing static
    behaviour and its cache keys.
    """
    profile = None
    try:
        from pcbdraft.model.provider_profiles import get_provider_profile

        profile = get_provider_profile(provider_id)
    except Exception:
        pass

    if prefer_fast:
        catalog_pick = _fast_model_from_catalog_hook(provider_id)
        if catalog_pick:
            return catalog_pick
        if profile is not None:
            try:
                live = profile.resolve_aux_model()
                if live:
                    return live
            except Exception:
                logger.debug(
                    "resolve_aux_model failed for %s", provider_id, exc_info=True
                )

    if profile is not None and profile.default_aux_model:
        return profile.default_aux_model
    return _API_KEY_PROVIDER_AUX_MODELS_FALLBACK.get(provider_id, "")


# Fallback for providers not yet migrated to ProviderProfile.default_aux_model,
# plus providers we intentionally keep pinned here (e.g. Anthropic predates
# profiles). New providers should set default_aux_model on their profile instead.
_API_KEY_PROVIDER_AUX_MODELS_FALLBACK: dict[str, str] = {
    "gemini": "gemini-3.6-flash",
    "zai": "glm-4.5-flash",
    "kimi-coding": "kimi-k2-turbo-preview",
    "stepfun": "step-3.5-flash",
    "kimi-coding-cn": "kimi-k2-turbo-preview",
    "gmi": "google/gemini-3.1-flash-lite-preview",
    "anthropic": "claude-haiku-4-5-20251001",
    "ai-gateway": "google/gemini-3-flash",
    "opencode-zen": "gemini-3-flash",
    "opencode-go": "glm-5",
    "kilocode": "google/gemini-3.6-flash",
    "ollama-cloud": "nemotron-3-nano:30b",
    "tencent-tokenhub": "hy3-preview",
    # NB: no "deepinfra" entry — its aux model lives on the ProviderProfile
    # (plugins/model-providers/deepinfra: default_aux_model), which
    # _get_aux_model_for_provider() reads first. Duplicating it here would be
    # dead data that drifts when the profile's value is bumped.
}

# Legacy alias — callers that haven't been updated to _get_aux_model_for_provider()
# can still use this dict directly. Kept in sync with _FALLBACK above.
_API_KEY_PROVIDER_AUX_MODELS: dict[str, str] = _API_KEY_PROVIDER_AUX_MODELS_FALLBACK

# Auxiliary tasks that may opt into the provider's fast/cheap model instead of
# the user's main chat model. The opt-in lives in
# ``auxiliary.<task>.prefer_fast_model`` so the default ``auto = main model``
# contract remains true on every settings surface.
_FAST_MODEL_TASKS: frozenset = frozenset({"title_generation"})


def _task_prefers_fast_model(task: str | None) -> bool:
    """Return whether an eligible task explicitly opts into fast-model routing."""
    if task not in _FAST_MODEL_TASKS:
        return False
    task_config = _get_auxiliary_task_config_hook(task)
    return is_truthy_value(task_config.get("prefer_fast_model"), default=False)


# Vision-specific model overrides for direct providers.
# When the user's main provider has a dedicated vision/multimodal model that
# differs from their main chat model, map it here.  The vision auto-detect
# "exotic provider" branch checks this before falling back to the main model.
_PROVIDER_VISION_MODELS: dict[str, str] = {
    "xiaomi": "mimo-v2.5",
    "zai": "glm-5v-turbo",
}


def _resolve_provider_vision_default(provider: str) -> str | None:
    """Return the provider's preferred default vision model id, or None.

    Static entries in :data:`_PROVIDER_VISION_MODELS` win first (xiaomi /
    zai have dedicated vision-only model names that don't live in any
    discoverable catalog). Otherwise the provider's :class:`ProviderProfile`
    gets a chance to supply one via its ``default_vision_model()`` hook —
    that's where catalog-backed providers (DeepInfra) resolve a live default,
    keeping the discovery logic inside their plugin instead of a name-check
    branch here.
    """
    static = _PROVIDER_VISION_MODELS.get(provider)
    if static:
        return static
    try:
        from pcbdraft.model.provider_profiles import get_provider_profile

        profile = get_provider_profile(provider)
    except Exception:
        return None
    if profile is None:
        return None
    try:
        return profile.default_vision_model()
    except Exception:
        return None


# Providers whose endpoint does not accept image input, even though the
# provider's broader ecosystem has vision models available elsewhere.  When
# `auxiliary.vision.provider: auto` sees one of these as the main provider,
# it must skip straight to the aggregator chain instead of returning a client
# that will 404 on every vision request.
#
# kimi-coding / kimi-coding-cn: the Kimi Coding Plan routes through
# api.kimi.com/coding (Anthropic Messages wire) which Kimi's own docs
# describe as having no image_in capability. Vision lives on the separate
# Kimi Platform (api.moonshot.ai, OpenAI-wire, pay-as-you-go).  See #17076.
_PROVIDERS_WITHOUT_VISION: frozenset = frozenset(
    {
        "kimi-coding",
        "kimi-coding-cn",
    }
)

# OpenRouter app attribution headers (base — always sent).
# `X-Title` is the canonical attribution header OpenRouter's dashboard
# reads; the previous `X-OpenRouter-Title` label was not recognized there.
_OR_HEADERS_BASE = {
    "HTTP-Referer": "https://github.com/qixuancao/pcbdraft",
    "X-Title": "PCBDraft",
    "X-OpenRouter-Categories": "productivity,cli-agent",
}

# Truthy values for boolean env-var parsing.
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def _apply_user_default_headers(headers: dict | None) -> dict | None:
    """Merge user-configured ``model.default_headers`` onto resolved headers.

    User values take precedence over provider/SDK defaults, mirroring the main
    agent client (``AIAgent._apply_user_default_headers``). This lets a
    ``custom`` OpenAI-compatible endpoint behind a gateway/WAF that rejects the
    OpenAI SDK's identifying headers (``User-Agent: OpenAI/Python ...``,
    ``X-Stainless-*``) override them for auxiliary calls too — otherwise the
    main turn would succeed but title/compression/vision calls to the same
    endpoint would still fail. (#40033)

    Returns the merged dict, or the original ``headers`` (possibly ``None``)
    when nothing is configured. No allocation when there are no overrides.
    """
    try:
        from pcbdraft.model.configuration import cfg_get, load_config

        _cfg = load_config()
        user_headers = cfg_get(_cfg, "model", "default_headers")
        # ``model.extra_headers`` is an accepted alias (matches the
        # per-provider ``extra_headers`` key on providers/custom_providers
        # entries). When both are set they merge, with ``extra_headers``
        # winning. SECURITY: values may carry credentials — never log them.
        alias_headers = cfg_get(_cfg, "model", "extra_headers")
        if isinstance(alias_headers, dict) and alias_headers:
            merged_user: dict = {}
            if isinstance(user_headers, dict):
                merged_user.update(user_headers)
            merged_user.update(alias_headers)
            user_headers = merged_user
    except Exception:
        return headers
    if not isinstance(user_headers, dict) or not user_headers:
        return headers
    merged = dict(headers or {})
    for key, value in user_headers.items():
        if value is None:
            continue
        merged[str(key)] = str(value)
    return merged or headers


def build_or_headers(or_config: dict | None = None) -> dict:
    """Build OpenRouter headers, optionally including response-cache headers.

    Precedence for response cache: env var > config.yaml > default (enabled).

    Environment variables:
        ``PCBDRAFT_RUNTIME_OPENROUTER_CACHE`` — truthy (``1``/``true``/``yes``/``on``)
            enables caching; ``0``/``false``/``no``/``off`` disables.
            Overrides ``openrouter.response_cache`` in config.yaml.
        ``PCBDRAFT_RUNTIME_OPENROUTER_CACHE_TTL`` — integer seconds (1-86400).
            Overrides ``openrouter.response_cache_ttl`` in config.yaml.

    *or_config* is the ``openrouter`` section from config.yaml.  When *None*,
    falls back to reading config from disk via ``load_config_readonly()``.
    """
    headers = dict(_OR_HEADERS_BASE)

    # Resolve config from disk if not provided.
    if or_config is None:
        try:
            from pcbdraft.model.configuration import load_config_readonly

            or_config = load_config_readonly().get("openrouter", {})
        except Exception:
            or_config = {}

    # Determine cache enabled: env var overrides config.
    env_cache = os.environ.get("PCBDRAFT_RUNTIME_OPENROUTER_CACHE", "").strip().lower()
    if env_cache:
        cache_enabled = env_cache in _TRUTHY_ENV_VALUES
    else:
        cache_enabled = or_config.get("response_cache", False)

    if not cache_enabled:
        return headers

    headers["X-OpenRouter-Cache"] = "true"

    # Determine TTL: env var overrides config.
    env_ttl = os.environ.get("PCBDRAFT_RUNTIME_OPENROUTER_CACHE_TTL", "").strip()
    if env_ttl:
        if env_ttl.isdigit():
            ttl = int(env_ttl)
            if 1 <= ttl <= 86400:
                headers["X-OpenRouter-Cache-TTL"] = str(ttl)
    else:
        ttl = or_config.get("response_cache_ttl", 300)
        if isinstance(ttl, (int, float)) and 1 <= ttl <= 86400:
            headers["X-OpenRouter-Cache-TTL"] = str(int(ttl))

    return headers


# NVIDIA NIM cloud billing attribution.  Keep this host-gated because the
# nvidia provider also supports local/on-prem NIM endpoints via NVIDIA_BASE_URL.
_NVIDIA_NIM_CLOUD_HEADERS = {
    "X-BILLING-INVOKE-ORIGIN": "PCBDraft",
}


def build_nvidia_nim_headers(base_url: str | None) -> dict:
    """Return NVIDIA NIM cloud attribution headers for build.nvidia.com traffic."""
    if base_url_host_matches(str(base_url or ""), "integrate.api.nvidia.com"):
        return dict(_NVIDIA_NIM_CLOUD_HEADERS)
    return {}


# Vercel AI Gateway app attribution headers. HTTP-Referer maps to
# referrerUrl and X-Title maps to appName in the gateway's analytics.
_AI_GATEWAY_HEADERS = {
    "HTTP-Referer": "https://github.com/qixuancao/pcbdraft",
    "X-Title": "PCBDraft",
    "User-Agent": f"PCBDraft/{_PCBDRAFT_VERSION}",
}

# Nous Portal extra_body for product attribution.
# Callers should pass this as extra_body in chat.completions.create()
# when the auxiliary client is backed by Nous Portal.
#
# The tags are computed from agent.portal_tags so the client= marker stays
# in lockstep with hermes_cli.__version__ across every Portal call site
# (main loop, aux, compression, web_extract). Do not inline a literal here;
# see agent/portal_tags.py for the rationale.


def _nous_extra_body() -> dict:
    """Return a fresh Nous Portal ``extra_body`` dict.

    Computed at call time so a hot-reloaded ``hermes_cli.__version__`` is
    reflected without restarting long-running processes.
    """
    return {"tags": _nous_portal_tags_hook()}


# Backwards-compatible module attribute. Some callers (tests, third-party
# plugins) read ``NOUS_EXTRA_BODY`` directly; keep it as a snapshot of the
# current tags. Callers that need the freshest value should call
# ``_nous_extra_body()`` or import ``nous_portal_tags`` directly.
NOUS_EXTRA_BODY = _nous_extra_body()

# Default auxiliary models per provider
_OPENROUTER_MODEL = "google/gemini-3.6-flash"
_NOUS_MODEL = "google/gemini-3.6-flash"
_NOUS_DEFAULT_BASE_URL = "https://inference-api.nousresearch.com/v1"
_ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"

# Codex OAuth endpoint used when a caller explicitly requests
# provider="openai-codex".  There is deliberately no hardcoded default
# model: the set of models OpenAI accepts on this endpoint for
# ChatGPT-account auth is an undocumented, shifting allow-list, and
# pinning one here has drifted silently twice (gpt-5.3-codex → gpt-5.2-codex
# → gpt-5.4 over 6 weeks in early 2026).  Callers must pass the model
# they want explicitly (from config.yaml model.model, auxiliary.<task>.model,
# or the user's active Codex model selection).
_CODEX_AUX_BASE_URL = "https://chatgpt.com/backend-api/codex"


def _codex_cloudflare_headers(access_token: str) -> dict[str, str]:
    """Headers required to avoid Cloudflare 403s on chatgpt.com/backend-api/codex.

    The Cloudflare layer in front of the Codex endpoint whitelists a small set of
    first-party originators (``codex_cli_rs``, ``codex_vscode``, ``codex_sdk_ts``,
    anything starting with ``Codex``). Requests from non-residential IPs (VPS,
    server-hosted agents) that don't advertise an allowed originator are served
    a 403 with ``cf-mitigated: challenge`` regardless of auth correctness.

    We pin ``originator: codex_cli_rs`` to match the upstream codex-rs CLI, set
    ``User-Agent`` to a codex_cli_rs-shaped string (beats SDK fingerprinting),
    and extract ``ChatGPT-Account-ID`` (canonical casing, from codex-rs
    ``auth.rs``) out of the OAuth JWT's ``chatgpt_account_id`` claim.

    Malformed tokens are tolerated — we drop the account-ID header rather than
    raise, so a bad token still surfaces as an auth error (401) instead of a
    crash at client construction.
    """
    headers = {
        "User-Agent": "codex_cli_rs/0.0.0 (PCBDraft)",
        "originator": "codex_cli_rs",
    }
    if not isinstance(access_token, str) or not access_token.strip():
        return headers
    try:
        import base64

        parts = access_token.split(".")
        if len(parts) < 2:
            return headers
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        acct_id = claims.get("https://api.openai.com/auth", {}).get(
            "chatgpt_account_id"
        )
        if isinstance(acct_id, str) and acct_id:
            headers["ChatGPT-Account-ID"] = acct_id
    except Exception:
        pass
    return headers


# Hosts that expose BOTH an Anthropic-style ``…/anthropic`` path and a sibling
# OpenAI-compatible ``…/v1`` (or vendor-specific OpenAI path). Unconditional
# ``/anthropic`` → ``/v1`` rewrites break Anthropic-only gateways such as
# Alibaba Bailian Token Plan (#83642).
#
# Matching is anchored to the URL *host* (exact domain or subdomain suffix /
# ``api.minimax.*`` prefix) — never a substring of the whole URL, so a path
# that merely contains ``api.minimax`` cannot false-positive.
_DUAL_SURFACE_ANTHROPIC_HOST_SUFFIXES = (
    "minimax.io",
    "minimax.chat",
    "minimaxi.com",
)
_DUAL_SURFACE_ANTHROPIC_HOST_PREFIXES = ("api.minimax.",)


def _is_dual_surface_anthropic_host(url: str) -> bool:
    """True when the URL's host is a known dual-surface (MiniMax-family) host."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    for suffix in _DUAL_SURFACE_ANTHROPIC_HOST_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return True
    return any(
        host.startswith(prefix) for prefix in _DUAL_SURFACE_ANTHROPIC_HOST_PREFIXES
    )


def _to_openai_base_url(base_url: str) -> str:
    """Normalize dual-surface Anthropic URLs to OpenAI-compatible format.

    MiniMax (and MiniMax-CN) expose an ``/anthropic`` endpoint for the Anthropic
    Messages API and a separate ``/v1`` endpoint for OpenAI chat completions.
    The auxiliary client often uses the OpenAI SDK, so those dual-surface hosts
    must hit ``/v1``.

    Anthropic-**only** custom gateways (path ends in ``/anthropic`` but has no
    sibling ``/v1``) must keep their path; rewriting them to ``/v1`` yields 404
    on compression/vision/title_generation (#83642).

    ZAI exposes its general API and Coding Plan on separate endpoints.  Its
    Anthropic-compatible Coding Plan endpoint maps to ``/api/coding/paas/v4``
    on the OpenAI wire, not the general ``/api/paas/v4`` endpoint.  Rewriting
    to the general endpoint changes the billing pool and can return a false
    insufficient-balance error for a valid Coding Plan key.
    """
    url = str(base_url or "").strip().rstrip("/")
    if url.endswith("/anthropic"):
        # ZAI uses /api/anthropic for the Coding Plan's Anthropic wire.  The
        # matching OpenAI-wire endpoint is /api/coding/paas/v4; /api/paas/v4
        # is the independently billed general API.
        if base_url_host_matches(url, "open.bigmodel.cn") or base_url_host_matches(
            url, "api.z.ai"
        ):
            rewritten = url[: -len("/anthropic")] + "/coding/paas/v4"
            logger.debug(
                "Auxiliary client: rewrote ZAI base URL %s → %s", url, rewritten
            )
            return rewritten
        if _is_dual_surface_anthropic_host(url):
            rewritten = url[: -len("/anthropic")] + "/v1"
            logger.debug(
                "Auxiliary client: rewrote dual-surface base URL %s → %s",
                url,
                rewritten,
            )
            return rewritten
        # Anthropic-only gateway: leave the /anthropic path alone.
        logger.debug(
            "Auxiliary client: keeping Anthropic-only base URL %s (no dual-surface host match)",
            url,
        )
        return url
    if base_url_host_matches(url, "api.kimi.com") and url.endswith("/coding"):
        # Kimi Code uses /coding/v1/messages for Anthropic SDK (appends /v1/messages)
        # but /coding/v1/chat/completions for OpenAI SDK (appends /chat/completions)
        # Without /v1 here, OpenAI SDK hits /coding/chat/completions — a 404.
        rewritten = url + "/v1"
        logger.debug("Auxiliary client: rewrote Kimi base URL %s → %s", url, rewritten)
        return rewritten
    return url


# Standalone imports use the local catalog implementation. The legacy module
# replaces this with a late-bound callback so patching its old symbol still works.
_fast_model_from_catalog_hook = _fast_model_from_catalog
