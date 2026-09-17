"""Authentication-store paths, locking, atomic persistence, and fallback reads.

The owning authentication module injects its runtime paths, cache state,
normalizers, and compatibility hooks.  Credential interpretation, OAuth/token
flows, provider selection, and public provider-state APIs remain outside this
module.
"""

# Broad fallbacks and best-effort cleanup preserve the legacy persistence behavior.
# ruff: noqa: BLE001, S110

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger("pcbdraft.model.auth")


def _auth_file_path(
    *,
    runtime_home_getter: Callable[[], Path],
    default_runtime_home_getter: Callable[[], Path],
    environment: Mapping[str, str] = os.environ,
) -> Path:
    """Return the active auth store path with the pytest safety guard."""

    path = runtime_home_getter() / "auth.json"
    if environment.get("PYTEST_CURRENT_TEST"):
        real_home_auth = (default_runtime_home_getter() / "auth.json").resolve(
            strict=False
        )
        try:
            resolved = path.resolve(strict=False)
        except Exception:
            resolved = path
        if resolved == real_home_auth:
            raise RuntimeError(
                f"Refusing to touch real user auth store during test run: {path}. "
                "Set PCBDRAFT_RUNTIME_HOME to a tmp_path in your test fixture, or run "
                "via scripts/run_tests.sh for hermetic CI-parity env."
            )
    return path


def _global_auth_file_path(
    *,
    runtime_home_getter: Callable[[], Path],
    default_runtime_root_getter: Callable[[], Path] | None = None,
) -> Path | None:
    """Return the global auth store path only while running in profile mode."""

    try:
        if default_runtime_root_getter is None:
            from pcbdraft.core.runtime_environment import get_default_runtime_root

            default_runtime_root_getter = get_default_runtime_root
        global_root = default_runtime_root_getter()
    except Exception:
        return None
    profile_home = runtime_home_getter()
    if _same_path(profile_home, global_root):
        return None
    return global_root / "auth.json"


def _load_global_auth_store(
    *,
    global_auth_file_path: Callable[[], Path | None],
    load_auth_store: Callable[[Path], dict[str, Any]],
    cache_getter: Callable[[], tuple[str, int, dict[str, Any]] | None],
    cache_setter: Callable[[tuple[str, int, dict[str, Any]] | None], None],
    default_runtime_home_getter: Callable[[], Path],
    environment: Mapping[str, str] = os.environ,
) -> dict[str, Any]:
    """Load and mtime-cache the read-only global-root fallback store."""

    global_path = global_auth_file_path()
    if global_path is None or not global_path.exists():
        cache_setter(None)
        return {}
    try:
        resolved_path = str(global_path.resolve(strict=False))
        mtime_ns = global_path.stat().st_mtime_ns
        cache_key: tuple[str, int] | None = (resolved_path, mtime_ns)
    except Exception:
        cache_key = None
    cached = cache_getter()
    if cache_key is not None and cached is not None:
        cached_path, cached_mtime, cached_store = cached
        if cached_path == cache_key[0] and cached_mtime == cache_key[1]:
            return cached_store
    if environment.get("PYTEST_CURRENT_TEST"):
        real_home_environment = environment.get("HOME", "")
        if real_home_environment:
            real_root = default_runtime_home_getter() / "auth.json"
            try:
                if global_path.resolve(strict=False) == real_root.resolve(strict=False):
                    cache_setter(None)
                    return {}
            except Exception:
                pass
    try:
        store = load_auth_store(global_path)
    except Exception:
        cache_setter(None)
        return {}
    if cache_key is not None:
        cache_setter((cache_key[0], cache_key[1], store))
    return store


def _auth_lock_path(*, auth_file_path: Callable[[], Path]) -> Path:
    """Return the lock-file sibling for the active authentication store."""

    return auth_file_path().with_suffix(".lock")


def _same_path(left: Path, right: Path) -> bool:
    """Compare paths canonically, falling back when resolution fails."""

    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except Exception:
        return left == right


def _auth_lock_holder_for(
    target_path: Path,
    *,
    holders: dict[str, threading.local],
    holders_guard: threading.Lock,
    local_factory: Callable[[], threading.local] = threading.local,
) -> threading.local:
    """Return the per-thread reentrancy tracker for an auth-store path."""

    try:
        key = str(target_path.resolve(strict=False))
    except Exception:
        key = str(target_path)
    with holders_guard:
        return holders.setdefault(key, local_factory())


@contextmanager
def _file_lock(
    lock_path: Path,
    holder: threading.local,
    timeout_seconds: float,
    timeout_message: str,
    *,
    fcntl_module: Any,
    msvcrt_module: Any,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[None]:
    """Hold a cross-process advisory file lock, reentrant in one thread."""

    if getattr(holder, "depth", 0) > 0:
        holder.depth += 1
        try:
            yield
        finally:
            holder.depth -= 1
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl_module is None and msvcrt_module is None:
        holder.depth = 1
        try:
            yield
        finally:
            holder.depth = 0
        return

    if msvcrt_module and (not lock_path.exists() or lock_path.stat().st_size == 0):
        lock_path.write_text(" ", encoding="utf-8")

    mode = "r+" if msvcrt_module else "a+"
    with lock_path.open(mode, encoding="utf-8") as lock_file:
        deadline = monotonic() + max(1.0, timeout_seconds)
        while True:
            try:
                if fcntl_module:
                    fcntl_module.flock(
                        lock_file.fileno(),
                        fcntl_module.LOCK_EX | fcntl_module.LOCK_NB,
                    )
                else:
                    lock_file.seek(0)
                    msvcrt_module.locking(
                        lock_file.fileno(),
                        msvcrt_module.LK_NBLCK,
                        1,
                    )
                break
            except (BlockingIOError, OSError, PermissionError) as exc:
                if monotonic() >= deadline:
                    raise TimeoutError(timeout_message) from exc
                sleep(0.05)

        holder.depth = 1
        try:
            yield
        finally:
            holder.depth = 0
            if fcntl_module:
                try:
                    fcntl_module.flock(lock_file.fileno(), fcntl_module.LOCK_UN)
                except OSError:
                    pass
            elif msvcrt_module:
                try:
                    lock_file.seek(0)
                    msvcrt_module.locking(
                        lock_file.fileno(),
                        msvcrt_module.LK_UNLCK,
                        1,
                    )
                except OSError:
                    pass


@contextmanager
def _auth_store_lock(
    timeout_seconds: float,
    *,
    target_path: Path | None,
    auth_file_path: Callable[[], Path],
    auth_lock_path: Callable[[], Path],
    auth_lock_holder_for: Callable[[Path], threading.local],
    file_lock: Callable[..., Any],
) -> Iterator[None]:
    """Hold the advisory lock for the active or explicitly targeted store."""

    auth_path = target_path if target_path is not None else auth_file_path()
    lock_path = (
        auth_path.with_suffix(".lock") if target_path is not None else auth_lock_path()
    )
    with file_lock(
        lock_path,
        auth_lock_holder_for(auth_path),
        timeout_seconds,
        "Timed out waiting for auth store lock",
    ):
        yield


def _empty_auth_store(auth_store_version: int) -> dict[str, Any]:
    return {"version": auth_store_version, "providers": {}}


def _load_auth_store(
    auth_file: Path | None = None,
    *,
    auth_file_path: Callable[[], Path],
    auth_store_version: int,
    migrate_stale_nous_portal_url: Callable[[dict[str, Any]], None],
    normalize_auth_store_sources: Callable[[dict[str, Any]], Any],
    runtime_logger: logging.Logger = logger,
) -> dict[str, Any]:
    """Read, migrate, and normalize an authentication store from disk."""

    auth_file = auth_file or auth_file_path()
    if not auth_file.exists():
        return _empty_auth_store(auth_store_version)

    try:
        raw = json.loads(auth_file.read_text(encoding="utf-8-sig"))
    except OSError:
        runtime_logger.warning(
            "auth: could not read %s, leaving the store on disk untouched "
            "rather than degrading to an empty one",
            auth_file,
            exc_info=True,
        )
        raise
    except Exception as exc:
        corrupt_path = auth_file.with_suffix(".json.corrupt")
        preserved = False
        try:
            shutil.copy2(auth_file, corrupt_path)
            preserved = True
        except Exception:
            runtime_logger.debug(
                "auth: could not preserve a copy of the corrupt store at %s",
                corrupt_path,
                exc_info=True,
            )
        if preserved:
            runtime_logger.warning(
                "auth: failed to parse %s (%s), starting with empty store. "
                "Corrupt file preserved at %s",
                auth_file,
                exc,
                corrupt_path,
            )
        else:
            runtime_logger.warning(
                "auth: failed to parse %s (%s), starting with empty store. "
                "A copy could NOT be preserved at %s",
                auth_file,
                exc,
                corrupt_path,
            )
        return _empty_auth_store(auth_store_version)

    if isinstance(raw, dict) and (
        isinstance(raw.get("providers"), dict)
        or isinstance(raw.get("credential_pool"), dict)
    ):
        raw.setdefault("providers", {})
        if isinstance(raw.get("providers"), dict):
            migrate_stale_nous_portal_url(raw["providers"])
        normalize_auth_store_sources(raw)
        return raw

    if isinstance(raw, dict) and isinstance(raw.get("systems"), dict):
        systems = raw["systems"]
        providers = {}
        if "nous_portal" in systems:
            providers["nous"] = systems["nous_portal"]
        return {
            "version": auth_store_version,
            "providers": providers,
            "active_provider": "nous" if providers else None,
        }

    return _empty_auth_store(auth_store_version)


def _save_auth_store(
    auth_store: dict[str, Any],
    target_path: Path | None = None,
    *,
    auth_file_path: Callable[[], Path],
    auth_store_version: int,
    secure_parent: Callable[[Path], Any],
    updated_at: Callable[[], str],
    normalize_auth_store_sources: Callable[[dict[str, Any]], Any],
    sanitize_borrowed_credential_payload: Callable[
        [dict[str, Any], str], dict[str, Any]
    ],
    atomic_replace: Callable[[Path, Path], Any],
    os_module: Any = os,
    stat_module: Any = stat,
    json_dumps: Callable[..., str] = json.dumps,
    uuid4: Callable[[], Any] = uuid.uuid4,
) -> Path:
    """Atomically persist an authentication store with owner-only mode."""

    auth_file = target_path if target_path is not None else auth_file_path()
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    secure_parent(auth_file)
    auth_store["version"] = auth_store_version
    auth_store["updated_at"] = updated_at()
    normalize_auth_store_sources(auth_store)
    pool = auth_store.get("credential_pool")
    if isinstance(pool, dict):
        for provider, entries in pool.items():
            if isinstance(entries, list):
                pool[provider] = [
                    sanitize_borrowed_credential_payload(entry, provider)
                    if isinstance(entry, dict)
                    else entry
                    for entry in entries
                ]
    payload = json_dumps(auth_store, indent=2) + "\n"
    tmp_path = auth_file.with_name(
        f"{auth_file.name}.tmp.{os_module.getpid()}.{uuid4().hex}"
    )
    try:
        descriptor = os_module.open(
            str(tmp_path),
            os_module.O_WRONLY | os_module.O_CREAT | os_module.O_EXCL,
            stat_module.S_IRUSR | stat_module.S_IWUSR,
        )
        with os_module.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os_module.fsync(handle.fileno())
        atomic_replace(tmp_path, auth_file)
        try:
            directory_descriptor = os_module.open(
                str(auth_file.parent), os_module.O_RDONLY
            )
        except OSError:
            directory_descriptor = None
        if directory_descriptor is not None:
            try:
                os_module.fsync(directory_descriptor)
            finally:
                os_module.close(directory_descriptor)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
    try:
        auth_file.chmod(stat_module.S_IRUSR | stat_module.S_IWUSR)
    except OSError:
        pass
    return auth_file


def _load_provider_state_with_source(
    auth_store: dict[str, Any],
    provider_id: str,
    *,
    auth_file_path: Callable[[], Path],
    global_auth_file_path: Callable[[], Path | None],
    load_global_auth_store: Callable[[], dict[str, Any]],
) -> tuple[dict[str, Any] | None, Path | None]:
    """Return provider state and the active or fallback path it came from."""

    providers = auth_store.get("providers")
    if isinstance(providers, dict):
        state = providers.get(provider_id)
        if isinstance(state, dict):
            return dict(state), auth_file_path()

    global_path = global_auth_file_path()
    global_store = load_global_auth_store()
    if global_store:
        global_providers = global_store.get("providers")
        if isinstance(global_providers, dict):
            global_state = global_providers.get(provider_id)
            if isinstance(global_state, dict):
                return dict(global_state), global_path
    return None, None


@contextmanager
def _provider_state_transaction(
    provider_id: str,
    *,
    auth_store_lock: Callable[..., Any],
    load_auth_store: Callable[..., dict[str, Any]],
    load_provider_state_with_source: Callable[
        [dict[str, Any], str], tuple[dict[str, Any] | None, Path | None]
    ],
    auth_file_path: Callable[[], Path],
    same_path: Callable[[Path, Path], bool],
) -> Iterator[tuple[dict[str, Any], dict[str, Any] | None, Path | None]]:
    """Lock the active store and any distinct fallback source in order."""

    with auth_store_lock():
        auth_store = load_auth_store()
        state, source_path = load_provider_state_with_source(auth_store, provider_id)
        active_path = auth_file_path()
        if source_path is None or same_path(source_path, active_path):
            yield auth_store, state, source_path
            return

        with auth_store_lock(target_path=source_path):
            source_store = load_auth_store(source_path)
            source_providers = source_store.get("providers")
            source_state = None
            if isinstance(source_providers, dict):
                raw_state = source_providers.get(provider_id)
                if isinstance(raw_state, dict):
                    source_state = dict(raw_state)
            yield auth_store, source_state, source_path


def _load_provider_state(
    auth_store: dict[str, Any],
    provider_id: str,
    *,
    load_provider_state_with_source: Callable[
        [dict[str, Any], str], tuple[dict[str, Any] | None, Path | None]
    ],
) -> dict[str, Any] | None:
    """Return provider state, including the profile-to-global fallback."""

    state, _source_path = load_provider_state_with_source(auth_store, provider_id)
    return state


def _store_provider_state(
    auth_store: dict[str, Any],
    provider_id: str,
    state: dict[str, Any],
    *,
    set_active: bool = True,
) -> None:
    """Store one provider state in memory and optionally mark it active."""

    providers = auth_store.setdefault("providers", {})
    if not isinstance(providers, dict):
        auth_store["providers"] = {}
        providers = auth_store["providers"]
    providers[provider_id] = state
    if set_active:
        auth_store["active_provider"] = provider_id


def _save_provider_state(
    auth_store: dict[str, Any], provider_id: str, state: dict[str, Any]
) -> None:
    """Store provider state in memory and mark it active."""

    _store_provider_state(auth_store, provider_id, state, set_active=True)


def _save_provider_state_to_source(
    auth_store: dict[str, Any],
    provider_id: str,
    state: dict[str, Any],
    source_path: Path | None,
    *,
    auth_file_path: Callable[[], Path],
    same_path: Callable[[Path, Path], bool],
    save_provider_state: Callable[[dict[str, Any], str, dict[str, Any]], None],
    save_auth_store: Callable[[dict[str, Any]], Path],
    persist_provider_state_to_store: Callable[..., Path],
) -> None:
    """Persist refreshed state back to the store it was read from."""

    active_path = auth_file_path()
    if source_path is None:
        source_path = active_path
    if same_path(source_path, active_path):
        save_provider_state(auth_store, provider_id, state)
        save_auth_store(auth_store)
        return
    persist_provider_state_to_store(
        provider_id,
        state,
        source_path,
        set_active=True,
    )


def _persist_provider_state_to_store(
    provider_id: str,
    state: dict[str, Any],
    target_path: Path,
    *,
    set_active: bool = False,
    auth_store_lock: Callable[..., Any],
    load_auth_store: Callable[[Path], dict[str, Any]],
    store_provider_state: Callable[..., None],
    save_auth_store: Callable[..., Path],
) -> Path:
    """Merge one provider into a specifically targeted store under its lock."""

    with auth_store_lock(target_path=target_path):
        auth_store = load_auth_store(target_path)
        store_provider_state(
            auth_store,
            provider_id,
            dict(state),
            set_active=set_active,
        )
        return save_auth_store(auth_store, target_path=target_path)
