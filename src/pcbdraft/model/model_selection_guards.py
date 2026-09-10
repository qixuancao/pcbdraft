"""Unified selection-time guard registry for model switching surfaces.

Hermes has multiple model-selection surfaces (CLI picker, TUI, dashboard,
gateway ``/model``, Telegram/Discord pickers, TUI-gateway RPC). Each of them
previously imported ``model_cost_guard.expensive_model_warning`` directly, so
every new guard class (e.g. the data-training-tier guard) had to be wired into
every surface by hand — and inevitably missed some.

This module is the single evaluation point: ``selection_warnings()`` runs every
registered guard and returns the warnings that fired. Surfaces render the
result with their own confirm UX (stdin prompt, modal, inline keyboard,
``confirm_required`` JSON) — that half stays per-surface; the *evaluation* half
lives here. Adding a guard to ``_GUARDS`` makes it appear on every surface at
once.

Guard modules (``model_cost_guard``, ``model_data_policy_guard``) keep their
public APIs — existing tests and mock patch points remain valid; this module
only aggregates them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pcbdraft.agent.models_dev import ModelInfo


@dataclass(frozen=True)
class SelectionWarning:
    """A selection-time warning a surface must confirm before applying."""

    kind: str  # "cost" | "data_policy" | future guard kinds
    title: str
    model: str
    provider: str
    message: str


def _cost_guard(
    model_name: str,
    provider: str | None,
    base_url: str | None,
    api_key: str | None,
    model_info: ModelInfo | None,
) -> SelectionWarning | None:
    from pcbdraft.model.model_cost_guard import expensive_model_warning

    warning = expensive_model_warning(
        model_name,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model_info=model_info,
    )
    if warning is None:
        return None
    # Duck-typed access: tests (and future guard payloads) may supply objects
    # carrying only ``.message``.
    return SelectionWarning(
        kind="cost",
        title="Expensive Model Warning",
        model=getattr(warning, "model", model_name),
        provider=getattr(warning, "provider", provider or ""),
        message=warning.message,
    )


def _data_policy_guard(
    model_name: str,
    provider: str | None,
    base_url: str | None,
    api_key: str | None,
    model_info: ModelInfo | None,
) -> SelectionWarning | None:
    from pcbdraft.model.model_data_policy_guard import data_training_warning

    warning = data_training_warning(
        model_name,
        provider=provider,
        base_url=base_url,
    )
    if warning is None:
        return None
    return SelectionWarning(
        kind="data_policy",
        title="Data-Training Tier Warning",
        model=getattr(warning, "model", model_name),
        provider=getattr(warning, "provider", provider or ""),
        message=warning.message,
    )


# Registry, evaluated in order. Add new guard classes here — never at the
# individual surfaces.
_GUARDS = (
    _cost_guard,
    _data_policy_guard,
)


def selection_warnings(
    model_name: str,
    *,
    provider: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model_info: ModelInfo | None = None,
    include_kinds: Iterable[str] | None = None,
) -> list[SelectionWarning]:
    """Run every registered selection guard and return the warnings that fired.

    Returns an empty list in the common case (no guard fired). Callers should
    run this after model resolution so aliases / provider-specific ids have
    settled, then surface the messages as a confirm step. ``include_kinds``
    optionally restricts which guard kinds run (e.g. auth.py's picker only runs
    the cost guard when a provider is known, but always runs the data-policy
    guard).

    A misbehaving guard must never break model selection: individual guard
    exceptions are swallowed.
    """
    wanted = set(include_kinds) if include_kinds is not None else None
    results: list[SelectionWarning] = []
    for guard in _GUARDS:
        try:
            warning = guard(model_name, provider, base_url, api_key, model_info)
        except Exception:
            continue
        if warning is None:
            continue
        if wanted is not None and warning.kind not in wanted:
            continue
        results.append(warning)
    return results


def combined_message(warnings: list[SelectionWarning]) -> str:
    """Join multiple warnings into one confirm-prompt body.

    Surfaces that show a single confirm dialog use this when more than one
    guard fires (rare) — one prompt showing both blocks beats two sequential
    prompts.
    """
    return "\n\n".join(w.message for w in warnings)


def combined_selection_warning(
    model_name: str,
    *,
    provider: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model_info: ModelInfo | None = None,
) -> SelectionWarning | None:
    """Drop-in replacement for ``expensive_model_warning`` call sites.

    Returns ``None`` when no guard fired, a single :class:`SelectionWarning`
    when exactly one fired, or a merged warning (``kind="multiple"``) whose
    ``message`` stacks every fired guard. Surfaces that render one confirm
    dialog with ``warning.message`` can switch to this without reshaping their
    control flow.
    """
    warnings = selection_warnings(
        model_name,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model_info=model_info,
    )
    if not warnings:
        return None
    if len(warnings) == 1:
        return warnings[0]
    return SelectionWarning(
        kind="multiple",
        title="Model Selection Warning",
        model=warnings[0].model,
        provider=warnings[0].provider,
        message=combined_message(warnings),
    )
