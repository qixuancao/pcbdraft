"""Vision-backend routing helpers for auxiliary model clients.

``auxiliary_client`` remains the compatibility surface and injects its live
namespace here. Looking dependencies up for every call preserves legacy
monkeypatch paths without a reverse import. Client construction, credentials,
and network access stay owned by ``auxiliary_client``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_VISION_AUTO_PROVIDER_ORDER = (
    "openrouter",
    "nous",
    "deepinfra",
)

_runtime_namespace: Callable[[], dict[str, Any]] | None = None


def configure_auxiliary_vision_runtime(
    *, namespace: Callable[[], dict[str, Any]]
) -> None:
    """Inject the compatibility module's live namespace."""

    global _runtime_namespace
    _runtime_namespace = namespace


def _runtime() -> dict[str, Any]:
    if _runtime_namespace is None:
        raise RuntimeError("auxiliary vision runtime has not been configured")
    return _runtime_namespace()


def _main_model_supports_vision(provider: str, model: str | None) -> bool:
    """Return True when ``provider``/``model`` is known to accept image input.

    Used by the vision auto-detect chain to skip the user's main provider
    when it's known to be text-only (e.g. DeepSeek, gpt-oss without vision).
    Without this guard, ``resolve_vision_provider_client(provider="auto")``
    would happily return the main-provider client and any subsequent image
    payload would surface as a cryptic provider-side error
    (``unknown variant `image_url`, expected `text```, #31179).

    Returns True when capability lookup is unknown — preserves the historical
    behaviour of attempting the call, so providers we haven't catalogued yet
    don't silently regress to text-only.
    """
    try:
        from pcbdraft.agent.image_routing import _lookup_supports_vision
        from pcbdraft.model.configuration import load_config_readonly
    except ImportError:
        return True
    try:
        supports = _lookup_supports_vision(provider, model, load_config_readonly())
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive
        return True
    if supports is None:
        # No capability data — keep current behaviour and let the call attempt
        # happen rather than silently skipping. This avoids false-positive
        # skips for new/custom providers.
        return True
    return bool(supports)


def _normalize_vision_provider(provider: str | None) -> str:
    runtime = _runtime()
    _normalize_aux_provider = runtime["_normalize_aux_provider"]

    return _normalize_aux_provider(provider)


def _resolve_strict_vision_backend(
    provider: str,
    model: str | None = None,
) -> tuple[Any | None, str | None]:
    runtime = _runtime()
    _normalize_vision_provider = runtime["_normalize_vision_provider"]
    resolve_provider_client = runtime["resolve_provider_client"]
    _try_openrouter = runtime["_try_openrouter"]
    _try_anthropic = runtime["_try_anthropic"]
    _resolve_provider_vision_default = runtime["_resolve_provider_vision_default"]
    logger = runtime["logger"]
    _try_custom_endpoint = runtime["_try_custom_endpoint"]

    provider = _normalize_vision_provider(provider)
    if provider == "copilot":
        return resolve_provider_client("copilot", model, is_vision=True)
    if provider == "openrouter":
        return _try_openrouter(model=model)
    if provider == "nous":
        # Must go through resolve_provider_client so anthropic/* vision
        # recommendations wrap onto /v1/messages — _try_nous alone returns
        # a bare OpenAI client and the call 404s.
        return resolve_provider_client("nous", model, is_vision=True)
    if provider == "openai-codex":
        # Route through resolve_provider_client so the caller's explicit
        # model is used.  There is no safe default Codex model (shifting
        # allow-list); callers must specify via auxiliary.<task>.model.
        return resolve_provider_client("openai-codex", model, is_vision=True)
    if provider == "anthropic":
        return _try_anthropic()
    if provider == "deepinfra":
        # DeepInfra exposes vision-capable models (Llama-4 Scout/Maverick,
        # Qwen3-VL, Gemma 3, Gemini) on the same OpenAI-compatible endpoint
        # as its chat models. The default is discovered live via the profile's
        # default_vision_model() hook (key-gated, chat-surface + vision tag) so
        # we don't pin a hardcoded id that may rot when DeepInfra retires a
        # model, and this module stays provider-agnostic.
        vision_model = model or _resolve_provider_vision_default("deepinfra")
        if not vision_model:
            logger.debug(
                "Vision auto-detect: deepinfra catalog unreachable or "
                "returned no vision-tagged models — skipping"
            )
            return None, None
        return resolve_provider_client("deepinfra", vision_model, is_vision=True)
    if provider == "custom":
        return _try_custom_endpoint()
    return None, None


def _strict_vision_backend_available(provider: str) -> bool:
    runtime = _runtime()
    _resolve_strict_vision_backend = runtime["_resolve_strict_vision_backend"]

    return _resolve_strict_vision_backend(provider)[0] is not None


def get_available_vision_backends() -> list[str]:
    """Return the currently available vision backends in auto-selection order.

    Order: active provider → OpenRouter → Nous → stop.  This is the single
    source of truth for setup, tool gating, and runtime auto-routing of
    vision tasks.
    """
    runtime = _runtime()
    _read_main_provider = runtime["_read_main_provider"]
    _VISION_AUTO_PROVIDER_ORDER = runtime["_VISION_AUTO_PROVIDER_ORDER"]
    _strict_vision_backend_available = runtime["_strict_vision_backend_available"]
    resolve_provider_client = runtime["resolve_provider_client"]
    _read_main_model = runtime["_read_main_model"]

    available: list[str] = []
    # 1. Active provider — if the user configured a provider, try it first.
    main_provider = _read_main_provider()
    if main_provider and main_provider not in {"auto", ""}:
        if main_provider in _VISION_AUTO_PROVIDER_ORDER:
            if _strict_vision_backend_available(main_provider):
                available.append(main_provider)
        else:
            client, _ = resolve_provider_client(main_provider, _read_main_model())
            if client is not None:
                available.append(main_provider)
    # 2. OpenRouter, 3. Nous — skip if already covered by main provider.
    for p in _VISION_AUTO_PROVIDER_ORDER:
        if p not in available and _strict_vision_backend_available(p):
            available.append(p)
    return available


def resolve_vision_provider_client(
    provider: str | None = None,
    model: str | None = None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    async_mode: bool = False,
    main_runtime: dict[str, Any] | None = None,
) -> tuple[str | None, Any | None, str | None]:
    """Resolve the client actually used for vision tasks.

    Direct endpoint overrides take precedence over provider selection. Explicit
    provider overrides still use the generic provider router for non-standard
    backends, so users can intentionally force experimental providers. Auto mode
    stays conservative and only tries vision backends known to work today.
    """
    runtime = _runtime()
    _normalize_main_runtime = runtime["_normalize_main_runtime"]
    _resolve_task_provider_model = runtime["_resolve_task_provider_model"]
    _normalize_vision_provider = runtime["_normalize_vision_provider"]
    _to_async_client = runtime["_to_async_client"]
    resolve_provider_client = runtime["resolve_provider_client"]
    _read_main_provider = runtime["_read_main_provider"]
    _read_main_model = runtime["_read_main_model"]
    _resolve_moa_aggregator = runtime["_resolve_moa_aggregator"]
    _resolve_provider_vision_default = runtime["_resolve_provider_vision_default"]
    _PROVIDERS_WITHOUT_VISION = runtime["_PROVIDERS_WITHOUT_VISION"]
    _main_model_supports_vision = runtime["_main_model_supports_vision"]
    _resolve_strict_vision_backend = runtime["_resolve_strict_vision_backend"]
    _resolve_custom_runtime = runtime["_resolve_custom_runtime"]
    _VISION_AUTO_PROVIDER_ORDER = runtime["_VISION_AUTO_PROVIDER_ORDER"]
    _get_cached_client = runtime["_get_cached_client"]
    logger = runtime["logger"]

    runtime = _normalize_main_runtime(main_runtime)
    (
        requested,
        resolved_model,
        resolved_base_url,
        resolved_api_key,
        resolved_api_mode,
    ) = _resolve_task_provider_model("vision", provider, model, base_url, api_key)
    requested = _normalize_vision_provider(requested)

    def _finalize(resolved_provider: str, sync_client: Any, default_model: str | None):
        if sync_client is None:
            return resolved_provider, None, None
        final_model = resolved_model or default_model
        if async_mode:
            async_client, async_model = _to_async_client(
                sync_client, final_model, is_vision=True
            )
            return resolved_provider, async_client, async_model
        return resolved_provider, sync_client, final_model

    if resolved_base_url:
        provider_for_base_override = (
            requested if requested and requested not in {"", "auto"} else "custom"
        )
        client, final_model = resolve_provider_client(
            provider_for_base_override,
            model=resolved_model,
            async_mode=async_mode,
            explicit_base_url=resolved_base_url,
            explicit_api_key=resolved_api_key,
            api_mode=resolved_api_mode,
            main_runtime=runtime,
        )
        if client is None:
            return provider_for_base_override, None, None
        return provider_for_base_override, client, final_model

    if requested == "auto":
        # Vision auto-detection order:
        #   1. User's main provider + main model (including aggregators).
        #      _PROVIDER_VISION_MODELS provides per-provider vision model
        #      overrides when the provider has a dedicated multimodal model
        #      that differs from the chat model (e.g. xiaomi → mimo-v2-omni,
        #      zai → glm-5v-turbo). DeepInfra is similar but resolves its
        #      default vision model live from the catalog (see
        #      :func:`_resolve_provider_vision_default`). Nous is the
        #      exception: it has a dedicated strict vision backend with
        #      tier-aware defaults, so it must not fall through to the
        #      user's text chat model here.
        #   2. OpenRouter (vision-capable aggregator fallback)
        #   3. Nous Portal (vision-capable aggregator fallback)
        #   4. DeepInfra   (OpenAI-compatible; vision model discovered
        #                   live from the catalog — tried when
        #                   DEEPINFRA_API_KEY is set)
        #   5. Stop
        main_provider = str(runtime.get("provider") or _read_main_provider())
        main_model = str(runtime.get("model") or _read_main_model())
        if main_provider.strip().lower() == "moa":
            # MoA virtual provider: main_model is a preset NAME, and every
            # capability probe below (_PROVIDERS_WITHOUT_VISION,
            # _main_model_supports_vision, _resolve_provider_vision_default)
            # would run against a provider/model pair that doesn't exist on
            # any wire. Unwrap to the preset's aggregator slot first so the
            # checks and the eventual client target the real acting model.
            _agg_provider, _agg_model = _resolve_moa_aggregator(main_model)
            if _agg_provider and _agg_model:
                main_provider, main_model = _agg_provider, _agg_model
                # Drop the moa:// facade endpoint from the runtime view used
                # below — it belongs to the virtual provider, not the
                # aggregator's real provider.
                runtime = dict(runtime)
                runtime["base_url"] = ""
                runtime["api_key"] = ""
                runtime["api_mode"] = ""
        if main_provider and main_provider not in {"auto", "", "moa"}:
            # A provider-specific vision default wins over the user's chat model:
            # static overrides (xiaomi/zai) and catalog-backed discovery (the
            # DeepInfra profile hook) both yield a *known* vision-capable model,
            # whereas the pinned chat model is usually NOT multimodal (e.g. the
            # DeepSeek-V4-Flash default) and _main_model_supports_vision can't be
            # trusted to catch that. Only fall back to the chat model when no
            # provider default is available (catalog unreachable).
            provider_vision_default = _resolve_provider_vision_default(main_provider)
            vision_model = provider_vision_default or main_model
            if main_provider == "nous":
                # Nous resolves its vision model from the Portal's tier-aware
                # recommended-models slots inside _try_nous(vision=True).
                # Passing the chat model here overrides that pick, so a
                # text-only chat default (e.g. a `:free` chat SKU) receives the
                # image and the upstream rejects it with a 404. Only an
                # explicit auxiliary.vision.model may override the Portal.
                sync_client, default_model = _resolve_strict_vision_backend(
                    main_provider, resolved_model or provider_vision_default
                )
                if sync_client is not None:
                    logger.info(
                        "Vision auto-detect: using main provider %s (%s)",
                        main_provider,
                        default_model or resolved_model or main_model,
                    )
                    return _finalize(main_provider, sync_client, default_model)
            elif main_provider in _PROVIDERS_WITHOUT_VISION:
                # Kimi Coding Plan's /coding endpoint (Anthropic Messages wire)
                # does not accept image input — Kimi's own docs say "Current
                # model does not support image input, switch to a model with
                # image_in capability" and vision lives on the separate Kimi
                # Platform (api.moonshot.ai). Skip the main provider and fall
                # through to the aggregator chain instead of returning a
                # client that will 404 on every vision request (#17076).
                logger.debug(
                    "Vision auto-detect: skipping main provider %s (no "
                    "vision support) — falling through to aggregator chain",
                    main_provider,
                )
            elif not _main_model_supports_vision(main_provider, vision_model):
                # The main model is known to be text-only (e.g. DeepSeek V4,
                # gpt-oss-120b without vision). Building a client and sending
                # an image would produce a cryptic provider-side error like
                # ``unknown variant `image_url`, expected `text``` (#31179).
                # Fall through to the aggregator chain instead.
                #
                # Only log the provider name (not the model) — mirrors the
                # sibling _PROVIDERS_WITHOUT_VISION branch above, and avoids
                # CodeQL py/clear-text-logging-sensitive-data heuristic false
                # positives on multi-value interpolations.
                logger.debug(
                    "Vision auto-detect: skipping main provider %s "
                    "(reports no vision capability) — falling through to "
                    "aggregator chain",
                    main_provider,
                )
            else:
                # Custom endpoints (``custom`` / ``custom:<name>``) carry no
                # built-in base_url/api_key — resolve_provider_client("custom")
                # would return None ("no endpoint credentials found") and the
                # whole chain would fall through to the aggregators, breaking
                # vision for every user on a custom provider that has no
                # separate ``auxiliary.vision`` block.  Recover the live main
                # endpoint that ``set_runtime_main()`` recorded for this turn so
                # Step 1 can build a working client.
                rpc_base_url = None
                rpc_api_key = None
                rpc_api_mode = resolved_api_mode
                if main_provider == "custom" or main_provider.startswith("custom:"):
                    runtime_base_url = runtime.get("base_url")
                    if runtime_base_url:
                        rpc_base_url = runtime_base_url
                        rpc_api_key = runtime.get("api_key") or None
                        rpc_api_mode = (
                            resolved_api_mode or runtime.get("api_mode") or None
                        )
                    else:
                        # No live runtime recorded (non-gateway caller): fall
                        # back to resolving the configured custom endpoint.
                        custom_base, custom_key, custom_mode = _resolve_custom_runtime()
                        if custom_base:
                            rpc_base_url = custom_base
                            rpc_api_key = custom_key
                            rpc_api_mode = resolved_api_mode or custom_mode or None
                rpc_client, rpc_model = resolve_provider_client(
                    main_provider,
                    vision_model,
                    api_mode=rpc_api_mode,
                    explicit_base_url=rpc_base_url,
                    explicit_api_key=rpc_api_key,
                    main_runtime=runtime,
                    is_vision=True,
                )
                if rpc_client is not None:
                    logger.info(
                        "Vision auto-detect: using main provider %s (%s)",
                        main_provider,
                        rpc_model or vision_model,
                    )
                    return _finalize(
                        main_provider, rpc_client, rpc_model or vision_model
                    )

        # Fall back through aggregators (uses their dedicated vision model,
        # not the user's main model) when main provider has no client.
        for candidate in _VISION_AUTO_PROVIDER_ORDER:
            if candidate == main_provider:
                continue  # already tried above
            sync_client, default_model = _resolve_strict_vision_backend(candidate)
            if sync_client is not None:
                return _finalize(candidate, sync_client, default_model)

        logger.debug("Auxiliary vision client: none available")
        return None, None, None

    if requested in _VISION_AUTO_PROVIDER_ORDER:
        sync_client, default_model = _resolve_strict_vision_backend(
            requested, resolved_model
        )
        return _finalize(requested, sync_client, default_model)

    # ZAI vision models must use the OpenAI-compatible endpoint, not the
    # Anthropic-compatible one (which may be the main-runtime default).
    # The Anthropic wire rejects max_tokens on multimodal calls (error 1210),
    # while the OpenAI wire handles it correctly.
    if requested == "zai" and not resolved_base_url:
        zai_openai_urls = [
            "https://open.bigmodel.cn/api/paas/v4",
            "https://api.z.ai/api/paas/v4",
        ]
        for _zai_url in zai_openai_urls:
            client, final_model = _get_cached_client(
                requested,
                resolved_model,
                async_mode,
                base_url=_zai_url,
                api_key=resolved_api_key or None,
                api_mode="chat_completions",
                main_runtime=runtime,
                is_vision=True,
            )
            if client is not None:
                return _finalize(requested, client, final_model)
        # Fallback: try without explicit base_url (old behavior)
        client, final_model = _get_cached_client(
            requested,
            resolved_model,
            async_mode,
            api_mode=resolved_api_mode,
            main_runtime=runtime,
            is_vision=True,
        )
        if client is None:
            return requested, None, None
        return requested, client, final_model

    client, final_model = _get_cached_client(
        requested,
        resolved_model,
        async_mode,
        api_mode=resolved_api_mode,
        main_runtime=runtime,
        is_vision=True,
    )
    if client is None:
        return requested, None, None
    return requested, client, final_model
