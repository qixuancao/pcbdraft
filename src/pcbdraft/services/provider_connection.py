"""PCBDraft entry-point adapter for the vendored Hermes provider system."""

from __future__ import annotations

import argparse
import os
import signal
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.core.hermes_paths import hermes_home, install_vendor_path
from pcbdraft.core.io import atomic_write_bytes, make_directory, read_bytes_limited

ConnectionOutcome = Literal["configured", "changed", "cancelled", "unavailable"]
ConnectionState = Literal[
    "ready",
    "unconfigured",
    "cancelled",
    "expired",
    "invalid_credentials",
    "unreachable",
    "unsupported_endpoint",
    "unavailable",
]

_CONNECTION_STATE_LIMIT = 8 * 1024 * 1024
_CONNECTION_STATE_PATHS = (
    "config.yaml",
    ".env",
    "auth.json",
    ".anthropic_oauth.json",
    "auth/google_oauth.json",
    "google_oauth.json",
    "google_oauth_pending.json",
    "google_token.json",
    "shared/nous_auth.json",
)


@dataclass(frozen=True)
class _FileSnapshot:
    path: Path
    data: bytes | None
    mode: int = 0o600


@dataclass(frozen=True)
class ConnectionOptions:
    """Options shared by ``pcbdraft connect`` and interactive connection flows."""

    no_browser: bool = False
    timeout: float | None = None
    region: str | None = None
    refresh: bool = False
    reauthenticate: bool = False


@dataclass(frozen=True)
class ConnectionStatus:
    """Secret-free projection of active Hermes provider state."""

    configured: bool
    usable: bool
    provider: str | None
    model: str | None
    auth_kind: str | None
    source: str | None
    outcome: ConnectionOutcome = "configured"
    error: str | None = None
    state: ConnectionState = "ready"

    def to_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "usable": self.usable,
            "provider": self.provider,
            "model": self.model,
            "auth_kind": self.auth_kind,
            "source": self.source,
            "outcome": self.outcome,
            "error": self.error,
            "state": self.state,
        }


def activate_provider_runtime() -> None:
    """Bind imports and state to the packaged runtime and PCBDraft-owned home."""

    install_vendor_path()
    home = hermes_home()
    if home.is_symlink():
        raise PCBDraftError("model connection home must not be a symbolic link")
    home = make_directory(home)
    shared_auth = home / "shared"
    if shared_auth.is_symlink():
        raise PCBDraftError(
            "model connection state must not use a symbolic-link directory: shared"
        )
    shared_auth = make_directory(shared_auth)
    # Always replace generic Hermes paths inherited from a standalone install.
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_SHARED_AUTH_DIR"] = str(shared_auth)
    os.environ["HERMES_HOME_MODE"] = "0700"


def _snapshot_connection_state() -> tuple[_FileSnapshot, ...]:
    """Capture the bounded provider files that a Hermes setup flow may mutate."""

    home = hermes_home()
    snapshots: list[_FileSnapshot] = []
    for relative in _CONNECTION_STATE_PATHS:
        path = home / relative
        parent = path.parent
        while parent != home:
            if parent.is_symlink():
                raise PCBDraftError(
                    "model connection state must not use a symbolic-link directory: "
                    f"{parent.name}"
                )
            parent = parent.parent
        if path.is_symlink():
            raise PCBDraftError(
                f"model connection state must not be a symbolic link: {path.name}"
            )
        if not path.exists():
            snapshots.append(_FileSnapshot(path, None))
            continue
        try:
            mode = stat.S_IMODE(path.stat().st_mode) & 0o600 or 0o600
        except OSError as exc:
            raise PCBDraftError(
                f"cannot inspect model connection state: {path.name}"
            ) from exc
        try:
            data = read_bytes_limited(path, _CONNECTION_STATE_LIMIT)
        except PCBDraftError as exc:
            raise PCBDraftError(
                f"cannot snapshot model connection state: {path.name}"
            ) from exc
        snapshots.append(_FileSnapshot(path, data, mode))
    return tuple(snapshots)


def _restore_connection_state(snapshots: tuple[_FileSnapshot, ...]) -> None:
    """Restore provider files after cancellation or a failed setup flow."""

    for snapshot in snapshots:
        if snapshot.data is None:
            try:
                snapshot.path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise PCBDraftError(
                    f"cannot roll back model connection state: {snapshot.path.name}"
                ) from exc
            continue
        atomic_write_bytes(snapshot.path, snapshot.data, mode=snapshot.mode)
    try:
        from hermes_cli.config import invalidate_env_cache

        invalidate_env_cache()
    except ImportError:
        pass


def _config_signature() -> tuple[int, int, int] | None:
    """Return an atomic-write-sensitive signature for Hermes config.yaml."""

    try:
        details = (hermes_home() / "config.yaml").stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PCBDraftError(
            "cannot inspect model connection state: config.yaml"
        ) from exc
    return (details.st_ino, details.st_mtime_ns, details.st_ctime_ns)


def provider_identities() -> tuple[str, ...]:
    """Return the concrete identities used by the Hermes provider picker."""

    activate_provider_runtime()
    from hermes_cli.config import get_compatible_custom_providers, load_config_readonly
    from hermes_cli.models import CANONICAL_PROVIDERS
    from hermes_cli.providers import custom_provider_slug

    config = load_config_readonly()
    identities = [entry.slug for entry in CANONICAL_PROVIDERS]
    for entry in get_compatible_custom_providers(config):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if name:
            identities.append(
                custom_provider_slug(name, str(entry.get("provider_key") or ""))
            )
    identities.append("custom")
    return tuple(dict.fromkeys(identities))


def _auth_kind(provider: str) -> str | None:
    from hermes_cli.auth import PROVIDER_REGISTRY
    from providers import get_provider_profile

    definition = PROVIDER_REGISTRY.get(provider)
    if definition is not None:
        return str(definition.auth_type)
    profile = get_provider_profile(provider)
    if profile is not None:
        return str(profile.auth_type)
    if provider.startswith("custom:") or provider == "custom":
        return "custom"
    return None


def _source_kind(value: object) -> str:
    """Reduce runtime provenance to a safe category, never a credential path."""

    source = str(value or "").casefold()
    if "env" in source:
        return "environment"
    if "pool" in source:
        return "credential-pool"
    if any(marker in source for marker in ("oauth", "auth", "codex")):
        return "provider-auth"
    if any(marker in source for marker in ("vertex", "azure", "bedrock", "aws")):
        return "cloud-identity"
    if any(marker in source for marker in ("local", "loopback")):
        return "local"
    if any(marker in source for marker in ("explicit", "custom", "config")):
        return "hermes-config"
    return "provider-managed"


_STATE_MESSAGES: dict[ConnectionState, str | None] = {
    "ready": None,
    "unconfigured": "no model provider is configured; run `pcbdraft connect`",
    "cancelled": "connection unchanged",
    "expired": "provider login expired; run `pcbdraft connect --reauthenticate`",
    "invalid_credentials": "provider rejected the credential; reauthenticate or replace the API key",
    "unreachable": "provider is unreachable; check the network and endpoint",
    "unsupported_endpoint": "the selected endpoint does not support PCBDraft planning",
    "unavailable": "provider is unavailable; run `pcbdraft doctor`",
}


def classify_provider_error(exc: BaseException) -> ConnectionState:
    """Classify vendored failures without exposing their raw text."""

    code = str(getattr(exc, "code", "") or "").casefold()
    name = type(exc).__name__.casefold()
    status = getattr(exc, "status_code", getattr(exc, "status", None))
    evidence = f"{name} {code} {str(exc).casefold()}"
    if any(token in evidence for token in ("expired", "relogin", "login_required")):
        return "expired"
    if any(
        token in evidence
        for token in (
            "tier_denied",
            "unsupported_endpoint",
            "unsupported_api",
            "unsupported_plan",
            "model_not_found",
        )
    ):
        return "unsupported_endpoint"
    if status in {401, 403} or any(
        token in evidence
        for token in (
            "invalid_api_key",
            "invalid_credentials",
            "unauthorized",
            "forbidden",
        )
    ):
        return "invalid_credentials"
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)) or any(
        token in evidence for token in ("timeout", "network", "connection")
    ):
        return "unreachable"
    if status in {404, 405, 415, 422}:
        return "unsupported_endpoint"
    return "unavailable"


@contextmanager
def _wizard_timeout(seconds: float | None) -> Iterator[None]:
    """Apply one wall-clock deadline when a Hermes sub-flow ignores args."""

    if seconds is None:
        yield
        return
    if seconds <= 0:
        raise PCBDraftError("model connection timeout must be greater than zero")
    if threading.current_thread() is not threading.main_thread() or not hasattr(
        signal, "setitimer"
    ):
        # Python cannot safely interrupt an arbitrary worker thread. A timer
        # that interrupts the process main thread would leave the wizard
        # running and able to rewrite credentials after rollback, so reject
        # this unsupported execution mode before the wizard starts.
        raise PCBDraftError(
            "timed model connection requires the main thread on this platform; "
            "omit `--timeout` or run `pcbdraft connect` directly"
        )

    started = time.monotonic()
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def _raise_timeout(_signum: int, _frame: object) -> None:
        raise TimeoutError

    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            remaining = max(0.001, previous_timer[0] - (time.monotonic() - started))
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])


@contextmanager
def _reauthentication_override(enabled: bool) -> Iterator[None]:
    """Force selected Hermes flows past reusable cached credentials."""

    if not enabled:
        yield
        return
    from hermes_cli import auth, model_setup_flows

    original_state = auth.get_provider_auth_state
    original_choice = model_setup_flows._prompt_auth_credentials_choice
    auth.get_provider_auth_state = lambda _provider: None
    model_setup_flows._prompt_auth_credentials_choice = lambda _title: "reauth"
    try:
        yield
    finally:
        auth.get_provider_auth_state = original_state
        model_setup_flows._prompt_auth_credentials_choice = original_choice


def connection_status(*, verify: bool = True) -> ConnectionStatus:
    """Read the active Hermes model and optionally verify runtime resolution."""

    activate_provider_runtime()
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly()
    raw_model = config.get("model")
    model_config = raw_model if isinstance(raw_model, dict) else {}
    provider_value = model_config.get("provider")
    model_value = model_config.get("default")
    provider = str(provider_value).strip() if provider_value else None
    model = str(model_value).strip() if model_value else None
    configured = bool(provider and provider != "auto" and model)
    if not configured:
        return ConnectionStatus(
            False,
            False,
            provider,
            model,
            None,
            None,
            error=_STATE_MESSAGES["unconfigured"],
            state="unconfigured",
        )
    provider = cast(str, provider)
    model = cast(str, model)
    if not verify:
        return ConnectionStatus(
            True,
            True,
            provider,
            model,
            _auth_kind(provider),
            "hermes-config",
            state="ready",
        )
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested=provider, target_model=model)
        usable = bool(runtime.get("provider"))
        source = _source_kind(runtime.get("source"))
        return ConnectionStatus(
            True,
            usable,
            provider,
            model,
            _auth_kind(provider),
            source,
            state="ready" if usable else "unavailable",
        )
    except Exception as exc:  # noqa: BLE001 - runtime supports many optional transports
        state = classify_provider_error(exc)
        return ConnectionStatus(
            True,
            False,
            provider,
            model,
            _auth_kind(provider),
            "hermes-config",
            outcome="unavailable",
            error=_STATE_MESSAGES[state],
            state=state,
        )


def connect(options: ConnectionOptions | None = None) -> ConnectionStatus:
    """Run Hermes' canonical provider/auth/model wizard and report safe state."""

    selected = options or ConnectionOptions()
    activate_provider_runtime()
    from pcbdraft.model.hermes_config import write_hermes_config

    write_hermes_config()
    from hermes_cli.config import read_raw_config
    from hermes_cli.main import select_provider_and_model

    before = read_raw_config()
    snapshots = _snapshot_connection_state()
    before_signature = _config_signature()
    args = argparse.Namespace(
        no_browser=selected.no_browser,
        timeout=selected.timeout,
        region=selected.region,
        refresh=selected.refresh,
        force=selected.reauthenticate,
        reauthenticate=selected.reauthenticate,
    )
    try:
        with (
            _wizard_timeout(selected.timeout),
            _reauthentication_override(selected.reauthenticate),
        ):
            select_provider_and_model(args=args)
    except TimeoutError as exc:
        _restore_connection_state(snapshots)
        raise PCBDraftError(
            "model connection timed out; retry with a larger `--timeout`"
        ) from exc
    except (EOFError, KeyboardInterrupt):
        _restore_connection_state(snapshots)
        return replace(
            connection_status(),
            outcome="cancelled",
            error=_STATE_MESSAGES["cancelled"],
            state="cancelled",
        )
    except SystemExit as exc:
        _restore_connection_state(snapshots)
        exit_code = exc.code if isinstance(exc.code, int) else 1
        if exit_code == 0:
            return replace(
                connection_status(),
                outcome="cancelled",
                error=_STATE_MESSAGES["cancelled"],
                state="cancelled",
            )
        raise PCBDraftError(
            "model connection failed; retry with `pcbdraft connect`"
        ) from exc
    except PCBDraftError:
        _restore_connection_state(snapshots)
        raise
    except Exception as exc:
        _restore_connection_state(snapshots)
        state = classify_provider_error(exc)
        raise PCBDraftError(
            _STATE_MESSAGES[state]
            or "model connection failed; retry with `pcbdraft connect`"
        ) from exc
    after = read_raw_config()
    config_was_written = _config_signature() != before_signature
    if not config_was_written:
        _restore_connection_state(snapshots)
        return replace(
            connection_status(),
            outcome="cancelled",
            error=_STATE_MESSAGES["cancelled"],
            state="cancelled",
        )
    status = connection_status()
    outcome: ConnectionOutcome = "changed" if after != before else "configured"
    values = status.to_dict()
    values["outcome"] = outcome
    return ConnectionStatus(**values)


def format_connection_status(status: ConnectionStatus) -> str:
    """Render a compact status containing no credentials or endpoint secrets."""

    if not status.configured:
        return "No model provider connected. Run `pcbdraft connect`."
    readiness = (
        "connection unchanged"
        if status.state == "cancelled"
        else status.state.replace("_", " ")
    )
    return (
        f"Active model: {status.provider} / {status.model}\n"
        f"  authentication: {status.auth_kind or 'provider managed'}\n"
        f"  status: {readiness}"
    )
