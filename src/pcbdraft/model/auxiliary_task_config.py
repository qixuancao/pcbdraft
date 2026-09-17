"""Auxiliary task configuration, timeout, and concurrency helpers.

``auxiliary_client`` remains the compatibility surface and injects its live
namespace here. Per-call lookups preserve legacy monkeypatch paths without a
reverse import. Main runtime, client cache, and transport state remain owned by
``auxiliary_client``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

_DEFAULT_AUX_TIMEOUT = 30.0

# Compression of large histories may legitimately take longer than the generic
# auxiliary timeout. This floor applies only when the caller did not supply an
# explicit timeout.
_COMPRESSION_TIMEOUT_FLOOR_SECONDS = 300.0

# Per-task concurrency state is owned by this focused module and re-exported by
# the compatibility module. Async semaphores are keyed by event-loop identity.
_aux_sync_semaphores: dict[str, tuple[int, threading.BoundedSemaphore]] = {}
_aux_async_semaphores: dict[tuple[str, int], tuple[int, Any]] = {}
_aux_sem_lock = threading.Lock()

_runtime_namespace: Callable[[], dict[str, Any]] | None = None


def configure_auxiliary_task_config_runtime(
    *, namespace: Callable[[], dict[str, Any]]
) -> None:
    """Inject the compatibility module's live namespace."""

    global _runtime_namespace
    _runtime_namespace = namespace


def _runtime() -> dict[str, Any]:
    if _runtime_namespace is None:
        raise RuntimeError("auxiliary task config runtime has not been configured")
    return _runtime_namespace()


def _get_auxiliary_task_config(task: str) -> dict[str, Any]:
    """Return the config dict for auxiliary.<task>, or {} when unavailable.

    For plugin-registered auxiliary tasks (see
    :meth:`hermes_cli.plugins.PluginContext.register_auxiliary_task`) the
    plugin's declared *defaults* are layered underneath the user's config
    so an unconfigured plugin task still works:

        plugin defaults  ←  config.yaml auxiliary.<task>  (user wins)

    Built-in tasks ignore this path (their defaults live in DEFAULT_CONFIG).
    """
    if not task:
        return {}
    try:
        from pcbdraft.model.configuration import load_config_readonly

        config = load_config_readonly()
    except ImportError:
        return {}
    aux = config.get("auxiliary", {}) if isinstance(config, dict) else {}
    task_config = aux.get(task, {}) if isinstance(aux, dict) else {}
    if not isinstance(task_config, dict):
        task_config = {}

    # Layer plugin-declared defaults underneath user config so
    # ctx.register_auxiliary_task(defaults={...}) takes effect without
    # forcing the user to write config.yaml entries.
    try:
        from pcbdraft.agent.extensions.manager import get_plugin_auxiliary_tasks

        for _entry in get_plugin_auxiliary_tasks():
            if _entry.get("key") == task:
                _defaults = _entry.get("defaults") or {}
                if isinstance(_defaults, dict):
                    merged = dict(_defaults)
                    merged.update(task_config)
                    return merged
                break
    except Exception:  # noqa: BLE001, S110
        # Plugin discovery failure must not break aux task config reads.
        pass

    return task_config


def _get_task_timeout(task: str, default: float = _DEFAULT_AUX_TIMEOUT) -> float:
    """Read timeout from auxiliary.{task}.timeout in config, falling back to *default*."""
    runtime = _runtime()
    _get_auxiliary_task_config = runtime["_get_auxiliary_task_config"]

    if not task:
        return default
    task_config = _get_auxiliary_task_config(task)
    raw = task_config.get("timeout")
    if raw is not None:
        try:
            return float(raw)
        except (ValueError, TypeError):
            pass
    return default


def _effective_aux_timeout(task: str, timeout: float | None) -> float:
    """Resolve the effective timeout for an auxiliary LLM call.

    Uses the caller-provided ``timeout`` when given; otherwise reads
    ``auxiliary.{task}.timeout`` from config via :func:`_get_task_timeout`.
    For the ``compression`` task only, applies a bounded floor so a reasoning
    model summarising a large context is not cut off by the default timeout
    (#54915).  The floor is intentionally skipped when the caller passes an
    explicit ``timeout=`` — explicit per-call deadlines are always honoured —
    and it is a minimum (``max``), so a config value already above it is kept.
    """
    runtime = _runtime()
    _get_task_timeout = runtime["_get_task_timeout"]
    _COMPRESSION_TIMEOUT_FLOOR_SECONDS = runtime["_COMPRESSION_TIMEOUT_FLOOR_SECONDS"]

    effective = timeout if timeout is not None else _get_task_timeout(task)
    if timeout is None and task == "compression":
        effective = max(effective, _COMPRESSION_TIMEOUT_FLOOR_SECONDS)
    return effective


def _get_task_extra_body(task: str) -> dict[str, Any]:
    """Read auxiliary.<task>.extra_body and return a shallow copy when valid.

    Also folds in ``auxiliary.<task>.reasoning_effort`` as an
    ``extra_body.reasoning`` config dict ({"enabled": ..., "effort": ...})
    when set. An explicit ``extra_body.reasoning`` in config wins over the
    ``reasoning_effort`` shorthand (it is the more specific wire control).
    Downstream, each wire already translates ``extra_body.reasoning``:
    chat.completions passes it through, the Codex Responses adapter maps it
    to top-level ``reasoning``/``include``, and the Anthropic auxiliary
    client maps it to ``build_anthropic_kwargs(reasoning_config=...)``.

    MoA tasks are excluded by design: reasoning depth for MoA is a per-slot
    setting in the MoA preset (``moa.presets.<name>.reference_models[].
    reasoning_effort`` / ``aggregator.reasoning_effort``), not an
    auxiliary-task knob — an ensemble-wide value would override the
    per-slot ones.
    """
    runtime = _runtime()
    _get_auxiliary_task_config = runtime["_get_auxiliary_task_config"]
    logger = runtime["logger"]

    task_config = _get_auxiliary_task_config(task)
    raw = task_config.get("extra_body")
    result = dict(raw) if isinstance(raw, dict) else {}
    if "reasoning" not in result:
        effort = task_config.get("reasoning_effort")
        if effort is not None and effort != "":
            if task in ("moa_reference", "moa_aggregator"):
                logger.warning(
                    "auxiliary.%s.reasoning_effort is not supported — MoA "
                    "reasoning depth is per-slot: set reasoning_effort on the "
                    "preset's reference_models entries / aggregator instead "
                    "(moa.presets.<name>...). Ignoring.",
                    task,
                )
                return result
            from pcbdraft.core.runtime_environment import parse_reasoning_effort

            parsed = parse_reasoning_effort(effort)
            if parsed is not None:
                result["reasoning"] = parsed
            else:
                logger.warning(
                    "auxiliary.%s.reasoning_effort %r is not a valid level "
                    "(none, minimal, low, medium, high, xhigh, max, ultra) — ignoring",
                    task,
                    effort,
                )
    return result


def _get_task_max_concurrency(task: str | None) -> int | None:
    """Return ``auxiliary.<task>.max_concurrency`` as a positive int, or None."""
    runtime = _runtime()
    _get_auxiliary_task_config = runtime["_get_auxiliary_task_config"]

    if not task or task == "vision":
        # Vision already uses this key for its encode/resize CPU worker pool;
        # its LLM calls deliberately remain concurrent.
        return None
    raw = _get_auxiliary_task_config(task).get("max_concurrency")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _acquire_sync_aux_semaphore(task: str | None) -> threading.BoundedSemaphore | None:
    """Get a per-task sync semaphore, rebuilding it after a config change."""
    runtime = _runtime()
    _get_task_max_concurrency = runtime["_get_task_max_concurrency"]
    _aux_sem_lock = runtime["_aux_sem_lock"]
    _aux_sync_semaphores = runtime["_aux_sync_semaphores"]
    threading = runtime["threading"]

    limit = _get_task_max_concurrency(task)
    if limit is None:
        return None
    with _aux_sem_lock:
        entry = _aux_sync_semaphores.get(task)
        if entry is None or entry[0] != limit:
            semaphore = threading.BoundedSemaphore(limit)
            _aux_sync_semaphores[task] = (limit, semaphore)
            return semaphore
        return entry[1]


def _acquire_async_aux_semaphore(task: str | None):
    """Get a per-task, per-event-loop async semaphore after config lookup."""
    runtime = _runtime()
    _get_task_max_concurrency = runtime["_get_task_max_concurrency"]
    _aux_sem_lock = runtime["_aux_sem_lock"]
    _aux_async_semaphores = runtime["_aux_async_semaphores"]

    limit = _get_task_max_concurrency(task)
    if limit is None:
        return None
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    key = (task, id(loop))
    with _aux_sem_lock:
        entry = _aux_async_semaphores.get(key)
        if entry is None or entry[0] != limit:
            semaphore = asyncio.Semaphore(limit)
            _aux_async_semaphores[key] = (limit, semaphore)
            return semaphore
        return entry[1]


def _reset_aux_semaphores() -> None:
    """Drop cached semaphores (test helper)."""
    runtime = _runtime()
    _aux_sem_lock = runtime["_aux_sem_lock"]
    _aux_sync_semaphores = runtime["_aux_sync_semaphores"]
    _aux_async_semaphores = runtime["_aux_async_semaphores"]

    with _aux_sem_lock:
        _aux_sync_semaphores.clear()
        _aux_async_semaphores.clear()
