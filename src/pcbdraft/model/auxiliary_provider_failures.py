"""Classify auxiliary-provider failures without routing or cache state."""

from __future__ import annotations

from collections.abc import Callable

_is_connection_error_hook: Callable[[Exception], bool]
_is_unsupported_parameter_error_hook: Callable[[Exception, str], bool]
_is_model_not_found_error_hook: Callable[[Exception], bool]


def configure_auxiliary_provider_failure_runtime(
    *,
    is_connection_error: Callable[[Exception], bool] | None = None,
    is_unsupported_parameter_error: Callable[[Exception, str], bool] | None = None,
    is_model_not_found_error: Callable[[Exception], bool] | None = None,
) -> None:
    """Connect composed predicates to legacy monkeypatch targets."""
    global _is_connection_error_hook
    global _is_unsupported_parameter_error_hook
    global _is_model_not_found_error_hook

    if is_connection_error is not None:
        _is_connection_error_hook = is_connection_error
    if is_unsupported_parameter_error is not None:
        _is_unsupported_parameter_error_hook = is_unsupported_parameter_error
    if is_model_not_found_error is not None:
        _is_model_not_found_error_hook = is_model_not_found_error


def _is_payment_error(exc: Exception) -> bool:
    """Detect payment/credit/quota exhaustion errors.

    Returns True for HTTP 402 (Payment Required) and for 429/other errors
    whose message indicates billing exhaustion or daily quota exhaustion
    rather than transient rate limiting.

    Daily token quota errors (e.g. Bedrock "Too many tokens per day",
    Vertex AI "quota exceeded") are functionally equivalent to credit
    exhaustion — the provider cannot serve the request until the quota
    resets — and should trigger the same provider-fallback logic.
    """
    status = getattr(exc, "status_code", None)
    if status == 402:
        return True
    err_lower = str(exc).lower()
    # OpenRouter and other providers include "credits" or "afford" in 402 bodies,
    # but sometimes wrap them in 429 or other codes.
    # Daily quota exhaustion from Bedrock, Vertex AI, and similar providers
    # uses different language but is semantically identical to credit exhaustion.
    return status in {402, 403, 404, 429, None} and any(
        kw in err_lower
        for kw in (
            "credits",
            "insufficient funds",
            "can only afford",
            "billing",
            "payment required",
            "out of funds",
            "run out of funds",
            "balance_depleted",
            "no usable credits",
            "model_not_supported_on_free_tier",
            "not available on the free tier",
            "requires a subscription",
            "upgrade for access",
            "upgrade for higher limits",
            "reached your session usage limit",
            # Daily / monthly / weekly quota exhaustion keywords
            "quota exceeded",
            "quota_exceeded",
            "too many tokens per day",
            "daily limit",
            "tokens per day",
            "daily quota",
            "resource exhausted",  # Vertex AI / gRPC quota errors
            "weekly usage limit",
            "weekly limit",  # OpenCode Go weekly subscription cap
        )
    )


def _is_rate_limit_error(exc: Exception) -> bool:
    """Detect rate-limit errors that warrant provider fallback.

    Returns True for HTTP 429 errors whose message indicates rate limiting
    (as opposed to billing/quota exhaustion, which _is_payment_error handles).
    Also catches OpenAI SDK RateLimitError instances that may not set
    .status_code on the exception object.
    """
    status = getattr(exc, "status_code", None)
    err_lower = str(exc).lower()

    # OpenAI SDK's RateLimitError sometimes omits .status_code —
    # detect by class name so we don't miss these.  (PR #8023 pattern)
    if type(exc).__name__ == "RateLimitError":
        return True

    if status == 429:
        # Distinguish rate-limit from billing: billing keywords are handled
        # by _is_payment_error, everything else on 429 is a rate limit.
        if any(
            kw in err_lower
            for kw in (
                "rate limit",
                "rate_limit",
                "too many requests",
                "try again",
                "retry after",
                "resets in",
            )
        ):
            return True
        # Generic 429 without billing keywords = likely a rate limit
        if not any(
            kw in err_lower
            for kw in (
                "credits",
                "insufficient funds",
                "billing",
                "payment required",
                "can only afford",
                "out of funds",
                "run out of funds",
                "balance_depleted",
                "no usable credits",
                "model_not_supported_on_free_tier",
                "not available on the free tier",
            )
        ):
            return True
    return False


def _is_timeout_error(exc: Exception) -> bool:
    """Detect a request timeout — the full-budget stall, distinct from a fast
    connection drop.

    A timeout burns the entire configured ``timeout`` before surfacing, so a
    same-provider retry on the critical compression path doubles the
    user-visible wall time (issue #54465). A streaming-close / dropped
    connection, by contrast, fails fast and is cheap to retry — those stay on
    the retry path even for compression.
    """
    try:
        from openai import APITimeoutError

        if isinstance(exc, APITimeoutError):
            return True
    except ImportError:
        pass
    if "Timeout" in type(exc).__name__:
        return True
    return "timed out" in str(exc).lower()


def _is_connection_error(exc: Exception) -> bool:
    """Detect connection/network errors that warrant provider fallback.

    Returns True for errors indicating the provider endpoint is unreachable
    (DNS failure, connection refused, TLS errors, timeouts).  These are
    distinct from API errors (4xx/5xx) which indicate the provider IS
    reachable but returned an error.
    """
    try:
        from openai import APIConnectionError, APITimeoutError

        if isinstance(exc, (APIConnectionError, APITimeoutError)):
            return True
    except ImportError:
        pass
    # urllib3 / httpx / httpcore connection errors
    err_type = type(exc).__name__
    if any(kw in err_type for kw in ("Connection", "Timeout", "DNS", "SSL")):
        return True
    err_lower = str(exc).lower()
    return any(
        kw in err_lower
        for kw in (
            "connection refused",
            "name or service not known",
            "no route to host",
            "network is unreachable",
            "timed out",
            "connection reset",
            # httpcore / httpx streaming premature-close errors.  These surface
            # when a proxy or provider drops the connection mid-stream and are
            # transient by nature — the request should be retried or rerouted.
            # See issue #18458.
            "incomplete chunked read",
            "peer closed connection",
            "response ended prematurely",
            "unexpected eof",
            "remoteprotocolerror",
            "localprotocolerror",
        )
    )


def _is_transient_transport_error(exc: Exception) -> bool:
    """Return True for a one-off transport blip worth retrying ON the
    same provider before any provider/model fallback.

    Covers connection/streaming-close errors (via the canonical
    ``_is_connection_error`` detector, shared so the two cannot drift) plus a
    pure 5xx/408 HTTP status. Deliberately narrow: this is the "retry the
    same target once" gate, distinct from ``_is_payment_error`` /
    ``_is_auth_error`` / ``_is_rate_limit_error`` which the except-chain
    handles by switching provider, refreshing creds, or rotating the pool.
    """
    if _is_connection_error_hook(exc):
        return True
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    return isinstance(status, int) and (status == 408 or 500 <= status < 600)


def _is_auth_error(exc: Exception) -> bool:
    """Detect auth failures that should trigger provider-specific refresh."""
    status = getattr(exc, "status_code", None)
    if status == 401:
        return True
    err_lower = str(exc).lower()
    if (
        "error code: 401" in err_lower
        or "authenticationerror" in type(exc).__name__.lower()
    ):
        return True
    # xAI returns HTTP 403 with "unauthenticated:bad-credentials" when an OAuth2
    # access token has expired or is invalid — semantically a 401 auth failure,
    # even though the status code is 403 (PermissionDenied).
    if status == 403 and "bad-credentials" in err_lower:
        return True
    return "unauthenticated" in err_lower and "bad-credentials" in err_lower


def _is_unsupported_parameter_error(exc: Exception, param: str) -> bool:
    """Detect provider 400s for an unsupported request parameter.

    Different OpenAI-compatible endpoints phrase the same class of error a few
    ways: ``Unsupported parameter: X``, ``unsupported_parameter`` with a
    ``param`` field, ``X is not supported``, ``unknown parameter: X``,
    ``unrecognized request argument: X``.  We match on both the parameter
    name and a generic "unsupported/unknown/unrecognized parameter" marker so
    call sites can reactively retry without the offending key instead of
    surfacing a noisy auxiliary failure.

    Generalizes the temperature-specific detector that originally shipped
    with PR #15621 so the same retry strategy can cover ``max_tokens``,
    ``seed``, ``top_p``, and any future quirk. Credit @nicholasrae (PR #15416)
    for the generalization pattern.
    """
    param_lower = (param or "").lower()
    if not param_lower:
        return False
    err_lower = str(exc).lower()
    if param_lower not in err_lower:
        return False
    return any(
        marker in err_lower
        for marker in (
            "unsupported parameter",
            "unsupported_parameter",
            "not supported",
            "does not support",
            "unknown parameter",
            "unrecognized request argument",
            "unrecognized parameter",
            "invalid parameter",
        )
    )


def _is_unsupported_temperature_error(exc: Exception) -> bool:
    """Back-compat wrapper: detect API errors where the model rejects ``temperature``.

    Delegates to :func:`_is_unsupported_parameter_error`; kept as a separate
    public symbol because existing tests and call sites import it by name.
    """
    return _is_unsupported_parameter_error_hook(exc, "temperature")


def _is_model_not_found_error(exc: Exception) -> bool:
    """Detect "the requested model doesn't exist" errors (404 / invalid model).

    This fires when a resolved model name is no longer served by the endpoint
    — most commonly when a long-lived process pinned a Portal-recommended model
    that has since been dropped from the Nous → OpenRouter catalog. The Nous
    proxy returns 404 with a body like::

        Model 'gpt-5.4-mini' not found. The requested model does not exist
        in our configuration or OpenRouter catalog.

    Distinct from :func:`_is_payment_error` (which also matches some 404s for
    free-tier/credit language) — this one keys on "does not exist / not found /
    not a valid model" phrasing, and explicitly excludes the billing keywords
    that the payment path already owns so the two predicates don't overlap.
    """
    status = getattr(exc, "status_code", None)
    err_lower = str(exc).lower()
    # Billing/quota 404s belong to _is_payment_error — don't claim them here.
    if any(
        kw in err_lower
        for kw in (
            "credits",
            "insufficient funds",
            "billing",
            "out of funds",
            "balance_depleted",
            "no usable credits",
            "free tier",
            "free-tier",
            "not available on the free tier",
        )
    ):
        return False
    if status not in {404, 400, None}:
        return False
    return any(
        kw in err_lower
        for kw in (
            "model does not exist",
            "does not exist in our configuration",
            "openrouter catalog",
            "is not a valid model",
            "no such model",
            "model not found",
            "the model `",  # OpenAI-style: "The model `X` does not exist"
            "model_not_found",
            "unknown model",
        )
    )


def _is_model_incompatible_error(exc: Exception) -> bool:
    """Detect "this route cannot serve this model" 400s (capability mismatch).

    Distinct from :func:`_is_model_not_found_error` (the model does not exist
    anywhere): here the model name is valid but the *current provider/account*
    is structurally unable to run it. The canonical case is a configured
    fallback that cannot run the main model — e.g. an ``openai-codex`` /
    ChatGPT-account fallback asked to compress a ``glm-5.2`` conversation::

        Error code: 400 - {'detail': "The 'glm-5.2' model is not supported
        when using Codex with a ChatGPT account."}

    The candidate authenticates fine and builds a client, so the auth and
    payment predicates don't fire and the call would otherwise raise and
    abort the whole auxiliary task (commonly compression — which then drops
    middle turns and churns the session, destroying the prompt cache).
    Treating it as a fallback-worthy capability error lets the chain skip the
    incapable route and continue to the next candidate, mirroring the
    context-window feasibility screen (#52392).

    Billing/quota 400s belong to :func:`_is_payment_error`; "model does not
    exist" 400s belong to :func:`_is_model_not_found_error`. This predicate
    explicitly excludes both so the three don't overlap.
    """
    status = getattr(exc, "status_code", None)
    if status not in {400, None}:
        return False
    err_lower = str(exc).lower()
    # Not-found 400s ("invalid model ID", "model does not exist") are owned by
    # _is_model_not_found_error. Billing/free-tier 400s are owned by the
    # payment path — key on the billing keywords directly here rather than
    # calling _is_payment_error(), because that predicate is status-gated
    # ({402,403,404,429,None}) and would not recognise a 400-coded billing
    # body, letting it leak into this capability bucket.
    if _is_model_not_found_error_hook(exc):
        return False
    if any(
        kw in err_lower
        for kw in (
            "credits",
            "insufficient funds",
            "billing",
            "out of funds",
            "balance_depleted",
            "no usable credits",
            "payment required",
            "free tier",
            "free-tier",
            "not available on the free tier",
            "model_not_supported_on_free_tier",
            "quota",
        )
    ):
        return False
    return any(
        kw in err_lower
        for kw in (
            "is not supported when using",  # codex/ChatGPT-account model gating
            "model is not supported",
            "not supported with this",
            "not supported for this account",
            "model_not_supported",
            "does not support this model",
            "unsupported model",
        )
    )


def _is_invalid_aux_response_error(exc: Exception) -> bool:
    """Detect provider responses that authenticated but cannot serve aux shape.

    Some OpenAI-compatible routes return HTTP 200 with an empty/malformed
    ChatCompletion instead of a normal provider error.  That is still a
    provider/model capability failure for auxiliary tasks: downstream callers
    need ``choices[0].message`` and should be able to continue through the
    same fallback path as explicit model-incompatibility errors.
    """
    if not isinstance(exc, RuntimeError):
        return False
    msg = str(exc).lower()
    return (
        "auxiliary " in msg
        and "llm returned invalid response" in msg
        and "choices[0].message" in msg
    )


# Standalone imports compose the predicates within this module. The legacy
# module replaces these with late-bound callbacks to preserve monkeypatch paths.
_is_connection_error_hook = _is_connection_error
_is_unsupported_parameter_error_hook = _is_unsupported_parameter_error
_is_model_not_found_error_hook = _is_model_not_found_error
