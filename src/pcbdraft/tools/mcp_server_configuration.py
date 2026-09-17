# Configuration loading is intentionally fail-soft so one bad optional plugin
# or dotenv source cannot block all MCP discovery.
# ruff: noqa: BLE001, S110
"""MCP server configuration parsing and safe stdio environment assembly.

The public compatibility symbols remain in :mod:`pcbdraft.tools.mcp_tool`.
That module injects its live namespace so established monkeypatch paths keep
working.  This module never imports ``mcp_tool`` and owns no transport tasks,
connection recovery, authentication retry, or tool registration state.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Callable
from typing import Any

_SAFE_ENV_KEYS = frozenset(
    {"PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR"}
)
_SAFE_ENV_KEYS_CASE_INSENSITIVE = frozenset(
    {
        "ALLUSERSPROFILE",
        "APPDATA",
        "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)",
        "COMMONPROGRAMW6432",
        "COMPUTERNAME",
        "COMSPEC",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "PUBLIC",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERDOMAIN",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
    }
)
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")
_whitespace_warned: set[tuple[str, str]] = set()

_runtime_namespace: Callable[[], dict[str, Any]] | None = None


def configure_mcp_server_configuration_runtime(
    *, namespace: Callable[[], dict[str, Any]]
) -> None:
    """Inject the compatibility module's live namespace."""

    global _runtime_namespace
    _runtime_namespace = namespace


def _runtime() -> dict[str, Any]:
    if _runtime_namespace is None:
        raise RuntimeError("MCP server configuration runtime is not configured")
    return _runtime_namespace()


def _env_ref_name(ref: str) -> str:
    ref = ref.strip()
    if ref.startswith("env:"):
        ref = ref[len("env:") :].strip()
    return ref


def _workspace_folder() -> str:
    try:
        from pcbdraft.tools.file_tools import _authoritative_workspace_root

        root = _authoritative_workspace_root()
        if root:
            return root
    except Exception:
        pass
    return os.getcwd()


def _context_var_value(ref: str) -> str | None:
    runtime = _runtime()
    if ref == "userHome":
        return os.path.expanduser("~")
    if ref == "workspaceFolder":
        return runtime["_workspace_folder"]()
    if ref == "workspaceFolderBasename":
        root = runtime["_workspace_folder"]()
        return os.path.basename(root.rstrip("/\\")) or root
    if ref in ("pathSeparator", "/"):
        return os.sep
    return None


def _build_safe_env(user_env: dict | None) -> dict:
    runtime = _runtime()
    try:
        from pcbdraft.model.env_loader import get_secret_source
    except Exception:
        get_secret_source = None
    env = {}
    safe_keys = runtime["_SAFE_ENV_KEYS"]
    safe_casefolded = runtime["_SAFE_ENV_KEYS_CASE_INSENSITIVE"]
    for key, value in os.environ.items():
        if (
            key in safe_keys
            or key.upper() in safe_casefolded
            or key.startswith("XDG_")
            or (get_secret_source is not None and get_secret_source(key))
        ):
            env[key] = value
    if user_env:
        env.update(user_env)
    return env


def _prepend_path(env: dict, directory: str) -> dict:
    updated = dict(env or {})
    if not directory:
        return updated
    existing = updated.get("PATH", "")
    parts = [part for part in existing.split(os.pathsep) if part]
    if directory not in parts:
        parts = [directory, *parts]
    updated["PATH"] = os.pathsep.join(parts) if parts else directory
    return updated


def _resolve_stdio_command(command: str, env: dict) -> tuple[str, dict]:
    runtime = _runtime()
    resolved_command = os.path.expanduser(str(command).strip())
    resolved_env = dict(env or {})
    if os.sep not in resolved_command:
        path_arg = resolved_env.get("PATH")
        which_hit = shutil.which(resolved_command, path=path_arg)
        if which_hit is None and sys.platform == "win32" and resolved_env:
            configured_pathext = next(
                (
                    value
                    for key, value in resolved_env.items()
                    if key.upper() == "PATHEXT"
                    and isinstance(value, str)
                    and value.strip()
                ),
                None,
            )
            if configured_pathext and configured_pathext != os.environ.get("PATHEXT"):
                saved_pathext = os.environ.get("PATHEXT")
                try:
                    os.environ["PATHEXT"] = configured_pathext
                    which_hit = shutil.which(resolved_command, path=path_arg)
                finally:
                    if saved_pathext is None:
                        os.environ.pop("PATHEXT", None)
                    else:
                        os.environ["PATHEXT"] = saved_pathext
        if which_hit:
            resolved_command = which_hit
        elif resolved_command in {"npx", "npm", "node"}:
            from pcbdraft.core.runtime_environment import get_runtime_home

            runtime_home = str(get_runtime_home())
            candidates = [
                os.path.join(runtime_home, "node", "bin", resolved_command),
                os.path.join(
                    os.path.expanduser("~"), ".local", "bin", resolved_command
                ),
                os.path.join(os.sep, "usr", "local", "bin", resolved_command),
            ]
            for candidate in candidates:
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    resolved_command = candidate
                    break
    command_dir = os.path.dirname(resolved_command)
    if command_dir:
        resolved_env = runtime["_prepend_path"](resolved_env, command_dir)
    return resolved_command, resolved_env


def _wrap_command_with_watchdog(command: str, args: list) -> tuple[str, list]:
    if os.name != "posix":
        return command, args
    try:
        parent_pid = os.getpid()
    except Exception:
        return command, args
    watchdog_args = [
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "mcp_stdio_watchdog.py"
        ),
        "--ppid",
        str(parent_pid),
        "--",
        command,
        *args,
    ]
    return sys.executable, watchdog_args


def _interpolate_env_vars(value: Any) -> Any:
    from pcbdraft.agent.secret_scope import get_secret

    runtime = _runtime()
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            context_value = runtime["_context_var_value"](match.group(1).strip())
            if context_value is not None:
                return context_value
            name = runtime["_env_ref_name"](match.group(1))
            return get_secret(name, match.group(0)) or match.group(0)

        return runtime["_ENV_VAR_PATTERN"].sub(replace, value)
    if isinstance(value, dict):
        return {
            key: runtime["_interpolate_env_vars"](nested)
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [runtime["_interpolate_env_vars"](nested) for nested in value]
    return value


def _warn_hidden_whitespace(server_name: str, config: dict) -> list[str]:
    runtime = _runtime()
    flagged: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, str):
            if value != value.strip():
                flagged.append(path)
        elif isinstance(value, dict):
            for key, nested in value.items():
                walk(nested, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                walk(nested, f"{path}[{index}]")

    walk(config, "")
    warned = runtime["_whitespace_warned"]
    for key_path in flagged:
        dedupe_key = (server_name, key_path)
        if dedupe_key in warned:
            continue
        warned.add(dedupe_key)
        runtime["logger"].warning(
            "MCP server '%s': config value '%s' has hidden leading or "
            "trailing whitespace — this often causes authentication or "
            "connection failures. Check for stray spaces/newlines in "
            "config.yaml (or the referenced env var).",
            server_name,
            key_path,
        )
    return flagged


def _filter_suspicious_mcp_servers(servers: dict[str, dict]) -> dict[str, dict]:
    try:
        from pcbdraft.interfaces.tui.mcp_security import validate_mcp_server_entry
    except Exception:
        validate_mcp_server_entry = None
    if validate_mcp_server_entry is None:
        return servers
    safe_servers = {}
    for name, config in servers.items():
        if not isinstance(config, dict):
            safe_servers[name] = config
            continue
        issues = validate_mcp_server_entry(name, config)
        if issues:
            _runtime()["logger"].warning(
                "Skipping suspicious MCP server '%s': %s",
                name,
                "; ".join(issues),
            )
            continue
        safe_servers[name] = config
    return safe_servers


def _load_mcp_config() -> dict[str, dict]:
    runtime = _runtime()
    try:
        from pcbdraft.core.runtime_utils import env_var_enabled
        from pcbdraft.model.configuration import load_config

        if env_var_enabled("PCBDRAFT_RUNTIME_SAFE_MODE"):
            return {}
        config = load_config()
        servers = config.get("mcp_servers")
        if not isinstance(servers, dict):
            servers = {}
        try:
            from pcbdraft.model.env_loader import load_pcbdraft_dotenv

            load_pcbdraft_dotenv()
        except Exception:
            pass
        safe_servers: dict[str, dict] = {}
        for name, server_config in runtime["_filter_suspicious_mcp_servers"](
            servers
        ).items():
            interpolated = runtime["_interpolate_env_vars"](server_config)
            if isinstance(interpolated, dict):
                runtime["_warn_hidden_whitespace"](name, interpolated)
                safe_servers[name] = interpolated
        try:
            from pcbdraft.agent.extensions.manager import (
                discover_plugins,
                get_plugin_manager,
            )

            discover_plugins()
            portable = get_plugin_manager().get_portable_mcp_servers()
            for name, server_config in runtime["_filter_suspicious_mcp_servers"](
                portable
            ).items():
                if name in safe_servers:
                    runtime["logger"].warning(
                        "Portable MCP server '%s' conflicts with native config; skipping",
                        name,
                    )
                    continue
                safe_servers[name] = dict(server_config)
        except Exception:
            runtime["logger"].debug(
                "Failed to load portable MCP servers", exc_info=True
            )
        return safe_servers
    except Exception as exc:
        runtime["logger"].debug("Failed to load MCP config: %s", exc)
        return {}
