"""Explicit read compatibility for persisted pre-PCBDraft runtime data.

Normal discovery uses native names only. This module never discovers, moves or
modifies a standalone user's ``~/.hermes`` directory. External registration IDs
and cryptographic formats are not product branding and must not be rewritten.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# External Honcho registration: changing this requires a real OAuth registration.
HONCHO_OAUTH_CLIENT_ID = "hermes-agent"
# HKDF domain separation is part of the existing encrypted Bitwarden wire format.
BITWARDEN_CACHE_KDF_INFO = b"hermes-bws-encrypted-cache-v1"


def read_memory_store_config(plugins: dict) -> dict:
    """Read the renamed holographic provider configuration without rewriting it."""
    value = (
        plugins.get("pcbdraft-memory-store")
        if "pcbdraft-memory-store" in plugins
        else plugins.get("hermes-memory-store")
    )
    return value if isinstance(value, dict) else {}


def read_memory_bank(config: dict) -> dict:
    """Keep a configured legacy Hindsight bank, defaulting new installs natively."""
    banks = config.get("banks") or {}
    value = banks.get("pcbdraft") if "pcbdraft" in banks else banks.get("hermes")
    return value if isinstance(value, dict) else {}


def read_skill_metadata(frontmatter: dict[str, Any]) -> dict[str, Any]:
    """Merge legacy-only fields; each native field (even empty) wins."""
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    legacy = metadata.get("hermes")
    native = metadata.get("pcbdraft")
    return {
        **(legacy if isinstance(legacy, dict) else {}),
        **(native if isinstance(native, dict) else {}),
    }


def normalize_compaction_mode(value: str) -> str:
    """Convert the persisted legacy engine selection to its native name."""
    return "pcbdraft" if value == "hermes" else value


def configured_host_key(hosts: dict, native: str) -> str:
    """Keep an existing Honcho namespace verbatim; new hosts use native names."""
    if native in hosts:
        return native
    if native == "pcbdraft":
        candidates = ("hermes",)
    elif native.startswith("pcbdraft_"):
        suffix = native[len("pcbdraft_") :]
        candidates = (f"pcbdraft.{suffix}", f"hermes_{suffix}", f"hermes.{suffix}")
    elif native.startswith("hermes_"):
        candidates = (f"hermes.{native[len('hermes_') :]}",)
    else:
        candidates = ()
    return next((key for key in candidates if key in hosts), native)


NAMESPACE_VERSION_KEY = "_pcbdraft_namespace_version"
NAMESPACE_ORIGIN_KEY = "_pcbdraft_namespace_origin"
NAMESPACE_SOURCE_KEY = "_pcbdraft_namespace_source"
_LEGACY_NAMESPACES = {
    "mem0": {"user_id": "hermes-user", "agent_id": "hermes"},
    "hindsight": {"bank_id": "hermes", "profile": "hermes"},
    "supermemory": {"container_tag": "hermes"},
    "openviking": {"agent": "hermes"},
    "retaindb": {"agent_id": "hermes"},
}


def memory_profile_environment(runtime_home: str | Path | None = None) -> dict:
    """Resolve namespace sources for the target profile without changing globals.

    An installed scope belongs only to the current runtime home. Other target
    profiles use their own dotenv and cached external-secret mapping, never
    the active profile's scope or process credentials. For the active profile,
    preserve single-profile overlay and multiplex isolation semantics.
    """
    from pcbdraft.agent.secret_scope import (
        UnscopedSecretError,
        build_profile_secret_scope,
        current_secret_scope,
        is_multiplex_active,
    )
    from pcbdraft.core.runtime_environment import get_runtime_home, runtime_home_key

    active_home = get_runtime_home()
    target = Path(runtime_home) if runtime_home is not None else active_home
    same_home = runtime_home_key(target) == runtime_home_key(active_home)
    scope = current_secret_scope()
    multiplex = is_multiplex_active()
    if runtime_home is None and multiplex and scope is None:
        raise UnscopedSecretError(
            "Memory configuration requires a profile secret scope"
        )
    profile = (
        scope if same_home and scope is not None else build_profile_secret_scope(target)
    )
    environment = dict(os.environ) if same_home and not multiplex else {}
    environment.update(profile)
    return environment


def saved_memory_namespaces(provider: str, effective: dict) -> dict:
    """Project only namespace fields/provenance, never resolved credentials."""
    fields = (
        *_LEGACY_NAMESPACES[provider],
        NAMESPACE_VERSION_KEY,
        NAMESPACE_ORIGIN_KEY,
    )
    return {key: effective[key] for key in fields}


def legacy_namespace_source(
    provider: str,
    config: dict,
    *,
    existing: bool = False,
    environ: dict | None = None,
) -> bool:
    """Unversioned persisted/env configuration predates native defaults.

    Reads materialize effective IDs in a copy. Writers persist that copy with
    provenance, making subsequent saves independent of omitted fields. Merely
    having a runtime directory does not make a newly selected backend legacy.
    Env-only setups without a versioned provider config are conservatively old;
    native setup writes explicit IDs and the version/origin before first use.
    """
    if config.get(NAMESPACE_ORIGIN_KEY) in ("legacy", "native"):
        return config[NAMESPACE_ORIGIN_KEY] == "legacy"
    if config.get(NAMESPACE_VERSION_KEY) == 1:
        return False
    env = os.environ if environ is None else environ
    return existing or any(
        key.startswith(f"{provider.upper()}_") and bool(value)
        for key, value in env.items()
    )


def effective_memory_namespaces(
    provider: str,
    config: dict,
    *,
    existing: bool = False,
    runtime_home: str | Path | None = None,
    environ: dict | None = None,
) -> dict:
    """Freeze implicit namespace defaults without changing explicit identifiers."""
    result = dict(config)
    legacy = legacy_namespace_source(
        provider, config, existing=existing, environ=environ
    )
    defaults = dict(_LEGACY_NAMESPACES[provider])
    if not legacy:
        defaults = {
            key: value.replace("hermes", "pcbdraft") for key, value in defaults.items()
        }
    if provider == "hindsight":
        defaults["bank_id"] = (
            read_memory_bank(config).get("bankId") or defaults["bank_id"]
        )
    if provider == "retaindb":
        profile = Path(runtime_home).name if runtime_home else ""
        # This is the pre-migration rule, including custom home basenames.
        # A migrated default runtime directory used to be named 'hermes'.
        if legacy and profile == "runtime" and runtime_home is not None:
            record_path = Path(runtime_home).parent / "runtime-migration.json"
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                record = {}
            if isinstance(record, dict) and record.get("outcome") == "migrated":
                profile = record.get("source_directory", profile)
        excluded = {"", ".hermes"} if legacy else {"", "runtime", ".pcbdraft"}
        prefix = "hermes" if legacy else "pcbdraft"
        defaults["project"] = (
            f"{prefix}-{profile}" if profile not in excluded else "default"
        )
    # These backends already honor explicit environment IDs. Preserve that
    # resolution when a setup/save materializes an omitted JSON/YAML field.
    env = os.environ if environ is None else environ
    env_fields = {
        "mem0": {"user_id": "MEM0_USER_ID", "agent_id": "MEM0_AGENT_ID"},
        "supermemory": {"container_tag": "SUPERMEMORY_CONTAINER_TAG"},
        "openviking": {"agent": "OPENVIKING_AGENT"},
        "retaindb": {"project": "RETAINDB_PROJECT"},
    }
    for key, variable in env_fields.get(provider, {}).items():
        if env.get(variable):
            defaults[key] = env[variable]
    for key, value in defaults.items():
        if result.get(key) is None or result.get(key) == "":
            result[key] = value
    result[NAMESPACE_VERSION_KEY] = 1
    result[NAMESPACE_ORIGIN_KEY] = "legacy" if legacy else "native"
    return result


def memory_user_identity(configured: str | None, gateway_user: str | None) -> str:
    """Wizard placeholders never collapse distinct gateway principals."""
    if configured and configured not in {"hermes-user", "pcbdraft-user"}:
        return configured
    return gateway_user or configured or "pcbdraft-user"


def honcho_effective_namespaces(
    config: dict,
    requested: str,
    *,
    existing: bool = False,
    env_only: bool = False,
    environ: dict | None = None,
) -> tuple[str, str, str]:
    """Return effective host/workspace/peer, keeping block aliases out of IDs."""
    hosts = config.get("hosts") or {}
    key = configured_host_key(hosts, requested)
    block = hosts.get(key) or {}
    legacy = legacy_namespace_source(
        "honcho", config, existing=existing, environ=environ
    )
    identity = key
    if key not in hosts and legacy and key.startswith("pcbdraft"):
        identity = "hermes" + key[len("pcbdraft") :]
    for prefix in ("hermes.", "pcbdraft."):
        if identity.startswith(prefix):
            identity = identity.replace(".", "_", 1)
    env_only = env_only or config.get(NAMESPACE_SOURCE_KEY) == "env_only"
    workspace_default = ("hermes" if legacy else "pcbdraft") if env_only else identity
    return (
        identity,
        block.get("workspace") or config.get("workspace") or workspace_default,
        block.get("aiPeer") or config.get("aiPeer") or identity,
    )


def materialize_honcho_namespaces(
    config: dict,
    *,
    existing: bool,
    env_only: bool = False,
    environ: dict | None = None,
) -> dict:
    """Freeze every configured host's implicit IDs before a native-format save."""
    result = dict(config)
    legacy = legacy_namespace_source(
        "honcho", config, existing=existing, environ=environ
    )
    if env_only:
        _, workspace, _ = honcho_effective_namespaces(
            config, "pcbdraft", existing=existing, env_only=True, environ=environ
        )
        # Env-only deployments share a workspace but derive peers per profile.
        # Freeze that shared value, not a root aiPeer that would merge profiles.
        result["workspace"] = workspace
        result[NAMESPACE_SOURCE_KEY] = "env_only"
    hosts = config.get("hosts") or {}
    if hosts:
        source = dict(result)
        result["hosts"] = {}
        for key, block in hosts.items():
            _, workspace, peer = honcho_effective_namespaces(
                source, key, existing=existing, environ=environ
            )
            result["hosts"][key] = {**block, "workspace": workspace, "aiPeer": peer}
    else:
        _, workspace, peer = honcho_effective_namespaces(
            result, "pcbdraft", existing=existing, environ=environ
        )
        # Root defaults apply to *every* profile. Do not synthesize root fields:
        # an old flat config without them used a different effective identity
        # for each named profile. Freeze the default host only and retain the
        # origin for unresolved profile hosts.
        default_host = "hermes" if legacy else "pcbdraft"
        result["hosts"] = {default_host: {"workspace": workspace, "aiPeer": peer}}
    result[NAMESPACE_VERSION_KEY] = 1
    result[NAMESPACE_ORIGIN_KEY] = "legacy" if legacy else "native"
    return result


def read_honcho_host(config: dict, host: str) -> dict:
    """Read a host through storage aliases, without inventing a new block."""
    hosts = config.get("hosts") or {}
    block = hosts.get(configured_host_key(hosts, host))
    return block if isinstance(block, dict) else {}
