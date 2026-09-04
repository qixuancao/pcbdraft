"""Pydantic request/response models for the Hermes dashboard web server.

Extracted verbatim from ``hermes_cli/web_server.py`` (pure schema move).
``web_server`` re-exports every name here, so existing imports like
``from hermes_cli.web_server import ConfigUpdate`` keep working.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, SecretStr, field_validator

# --- from web_server.py (originally lines 1273-1372) ---


class ConfigUpdate(BaseModel):
    config: dict
    profile: str | None = None


class EnvVarUpdate(BaseModel):
    key: str
    value: str
    profile: str | None = None
    # Optional bearer key for the connectivity probe of a custom/local endpoint
    # (``key == "OPENAI_BASE_URL"``). Self-hosted endpoints that gate
    # ``/v1/models`` behind auth otherwise look "reachable but empty"; sending
    # the key lets the probe enumerate the served models. Ignored for the
    # regular PUT /api/env path (which only reads key/value).
    api_key: str = ""


class EnvVarDelete(BaseModel):
    key: str
    profile: str | None = None


class EnvVarReveal(BaseModel):
    key: str
    profile: str | None = None


class MemoryProviderConfigUpdate(BaseModel):
    values: dict[str, Any] = {}


class MemoryProviderSetupRequest(BaseModel):
    values: dict[str, Any] = {}


class CustomEndpointUpdate(BaseModel):
    id: str = ""
    name: str
    base_url: str
    model: str
    api_key: str | None = None
    context_length: int | None = None
    discover_models: bool = True
    make_default: bool = False
    models: list[str] | None = None


class MessagingPlatformUpdate(BaseModel):
    enabled: bool | None = None
    env: dict[str, str] = {}
    clear_env: list[str] = []
    # Explicit body profile beats the query param injected by the global
    # dashboard profile switcher (same precedence as other scoped writes).
    profile: str | None = None


class TelegramOnboardingStart(BaseModel):
    bot_name: str | None = None


class TelegramOnboardingApply(BaseModel):
    allowed_user_ids: list[str]
    profile: str | None = None


class WhatsAppOnboardingStart(BaseModel):
    mode: str | None = "bot"
    allowed_users: str | None = ""
    profile: str | None = None


class WhatsAppOnboardingApply(BaseModel):
    mode: str | None = None
    allowed_users: str | None = None
    profile: str | None = None


class AudioTranscriptionRequest(BaseModel):
    data_url: str
    mime_type: str | None = None


class ManagedFileUpload(BaseModel):
    path: str
    data_url: str
    overwrite: bool = True


class ChatImageUpload(BaseModel):
    data_url: str
    filename: str | None = None


class ManagedDirectoryCreate(BaseModel):
    path: str


class ManagedFileDelete(BaseModel):
    path: str
    recursive: bool = False


# --- from web_server.py (originally lines 1398-1491) ---


class ModelAssignment(BaseModel):
    """Payload for POST /api/model/set — assign a provider/model to a slot.

    scope="main"        → writes model.provider + model.default
    scope="auxiliary"   → writes auxiliary.<task>.provider + auxiliary.<task>.model
    scope="auxiliary" with task=""  → applied to every auxiliary.* slot
    scope="auxiliary" with task="__reset__"  → resets every slot to provider="auto"
    """

    scope: str
    provider: str
    model: str
    task: str = ""
    # Optional OpenAI-compatible endpoint URL. Honored for custom/local
    # providers on the main slot AND on auxiliary slots — lets the GUI wire a
    # self-hosted endpoint (vLLM, llama.cpp, Ollama, …) that needs no API key.
    # The runtime resolvers read model.base_url / auxiliary.<task>.base_url
    # from config (they ignore OPENAI_BASE_URL), so this is the path that
    # actually wires a local endpoint into resolution.
    base_url: str = ""
    # Optional API key for a custom/local endpoint. Persisted to
    # ``model.api_key`` (main slot) or ``auxiliary.<task>.api_key`` (aux
    # slots) — where the runtime resolvers read it — so a self-hosted
    # endpoint that requires auth works from the GUI. Mirrors the key the
    # ``hermes model`` custom flow collects.
    api_key: str = ""
    confirm_expensive_model: bool = False
    profile: str | None = None


class MoaModelSlot(BaseModel):
    provider: str = ""
    model: str = ""
    # Optional per-slot reasoning effort. Declared so a client round-tripping
    # the GET payload doesn't have it stripped at parse time and wiped on save.
    reasoning_effort: str | None = None
    enabled: bool = True


class _MoaReferenceControls(BaseModel):
    # None = no per-preset override; the fan-out inherits
    # auxiliary.moa_reference.timeout (900s default).
    reference_timeout: float | None = None
    degraded_reference_policy: Literal["loud", "silent"] = "loud"

    @field_validator("reference_timeout", mode="before")
    @classmethod
    def _validate_reference_timeout(cls, value: Any) -> float | None:
        """Reject JSON booleans/non-finite values before float coercion."""
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            raise ValueError("reference_timeout must be a finite positive number")
        try:
            timeout = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "reference_timeout must be a finite positive number"
            ) from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("reference_timeout must be a finite positive number")
        return timeout


class MoaPresetPayload(_MoaReferenceControls):
    reference_models: list[MoaModelSlot] = []
    aggregator: MoaModelSlot = MoaModelSlot()
    # None = temperature omitted from API calls (provider default), matching
    # single-model agent behavior.
    reference_temperature: float | None = None
    aggregator_temperature: float | None = None
    max_tokens: int = 4096
    # Newer per-preset knobs (see moa_config._normalize_preset). Optional so
    # older clients that never send them keep working; declared so clients
    # that round-trip the GET payload don't silently erase hand-set values.
    reference_max_tokens: int | None = None
    fanout: str | None = None
    enabled: bool = True


class MoaConfigPayload(_MoaReferenceControls):
    default_preset: str = "default"
    active_preset: str = ""
    presets: dict[str, MoaPresetPayload] = {}
    # Backward-compatible flat payload fields used by older dashboard/desktop
    # clients during this PR's transition window.
    reference_models: list[MoaModelSlot] = []
    aggregator: MoaModelSlot = MoaModelSlot()
    reference_temperature: float | None = None
    aggregator_temperature: float | None = None
    max_tokens: int = 4096
    reference_max_tokens: int | None = None
    fanout: str | None = None
    enabled: bool = True
    profile: str | None = None


# --- from web_server.py (originally lines 2718-2720) ---


class FsWriteText(BaseModel):
    path: str
    content: str


# --- from web_server.py (originally lines 2826-2856) ---


class GitPathBody(BaseModel):
    path: str


class GitFileBody(BaseModel):
    path: str
    file: str | None = None


class GitPrListBody(BaseModel):
    path: str
    branches: list[str] = []
    # PRs a session recovered from its transcript, which we know by number
    # rather than by the branch it came from.
    numbers: list[int] = []


class SessionPrScanBody(BaseModel):
    ids: list[str] = []


class GitCommitBody(BaseModel):
    path: str
    message: str
    push: bool = False


class GitWorktreeAddBody(BaseModel):
    path: str
    name: str | None = None
    branch: str | None = None
    base: str | None = None
    existingBranch: str | None = None


class GitWorktreeRemoveBody(BaseModel):
    path: str
    worktreePath: str
    force: bool = False


class GitBranchSwitchBody(BaseModel):
    path: str
    branch: str


# --- from web_server.py (originally lines 3610-3611) ---


class CuratorPause(BaseModel):
    paused: bool


# --- from web_server.py (originally lines 3649-3657) ---


class LearningNodeRef(BaseModel):
    id: str
    profile: str | None = None


class LearningNodeEdit(BaseModel):
    id: str
    content: str
    profile: str | None = None


# --- from web_server.py (originally lines 3786-3792) ---


class DebugShareRequest(BaseModel):
    # Redaction is ON by default — force-mode scrubs credential-shaped tokens
    # out of log content before it leaves the machine. The toggle exists so an
    # operator who knows the logs are clean can opt out for fuller fidelity.
    redact: bool = True
    # Recent log lines included in the summary tail (full logs are separate).
    lines: int = 200


# --- from web_server.py (originally lines 4492-4493) ---


class TTSSpeakRequest(BaseModel):
    text: str


# --- from web_server.py (originally lines 11549-11551) ---


class OAuthSubmitBody(BaseModel):
    session_id: str
    code: str


# --- from web_server.py (originally lines 11708-11715) ---


class BulkDeleteSessions(BaseModel):
    ids: list[str]
    profile: str | None = None


class SessionImport(BaseModel):
    sessions: list[dict[str, Any]]
    profile: str | None = None


# --- from web_server.py (originally lines 12082-12090) ---


class SessionRename(BaseModel):
    title: str | None = None
    archived: bool | None = None
    # Durable "keep" flag mirrored from the Desktop sidebar's pins; pinned
    # sessions are exempt from the sessions.auto_archive stale sweep.
    pinned: bool | None = None
    # Read-state watermark toggle (sessions.last_read_at): True marks the
    # session explicitly unread, False marks it read up to now. Mirrored from
    # the Desktop sidebar's "Mark as unread"/"Mark as read". None = leave alone.
    unread: bool | None = None
    # Mutate a session belonging to another profile (opens its state.db). Omit
    # for the current/default profile.
    profile: str | None = None


# --- from web_server.py (originally lines 12149-12174) ---


class SessionPrune(BaseModel):
    older_than_days: float | None = 90
    source: str | None = None
    profile: str | None = None
    # Extended filters (all optional, AND together — mirrors the CLI flags)
    started_before: float | None = None  # epoch seconds
    started_after: float | None = None  # epoch seconds
    title_like: str | None = None
    end_reason: str | None = None
    cwd_prefix: str | None = None
    min_messages: int | None = None
    max_messages: int | None = None
    model_like: str | None = None
    provider: str | None = None
    user_id: str | None = None
    chat_id: str | None = None
    chat_type: str | None = None
    branch_like: str | None = None
    min_tokens: int | None = None
    max_tokens: int | None = None
    min_cost: float | None = None
    max_cost: float | None = None
    min_tool_calls: int | None = None
    max_tool_calls: int | None = None
    include_archived: bool = False
    dry_run: bool = False


# --- from web_server.py (originally lines 12335-12352) ---


class CronJobCreate(BaseModel):
    prompt: str = ""
    schedule: str
    name: str = ""
    deliver: str = "local"
    skills: list[str] | None = None
    model: str | None = None
    provider: str | None = None
    base_url: str | None = None
    script: str | None = None
    context_from: Any | None = None
    enabled_toolsets: list[str] | None = None
    workdir: str | None = None
    no_agent: bool = False


class CronJobUpdate(BaseModel):
    updates: dict


# --- from web_server.py (originally lines 12924-12926) ---


class AutomationBlueprintInstantiate(BaseModel):
    blueprint: str  # blueprint key, e.g. "morning-brief"
    values: dict[str, Any] = {}  # filled slot values from the form


# --- from web_server.py (originally lines 13002-13019) ---


class MCPServerCreate(BaseModel):
    name: str
    url: str | None = None
    command: str | None = None
    args: list[str] = []
    # env: KEY=VALUE map for stdio servers (API keys, etc.)
    env: dict[str, str] = {}
    # auth: "none" | "oauth" | "header" | None
    auth: str | None = None
    # One-time provisioning input; persisted only to the profile's .env.
    bearer_token: SecretStr | None = None
    profile: str | None = None


class MCPServersReplace(BaseModel):
    # Whole-map replace (name → raw server config) for the GUI mcp.json editor.
    servers: dict[str, dict[str, Any]] = {}
    profile: str | None = None


# --- from web_server.py (originally lines 13518-13520) ---


class MCPEnabledToggle(BaseModel):
    enabled: bool
    profile: str | None = None


# --- from web_server.py (originally lines 13622-13627) ---


class MCPCatalogInstall(BaseModel):
    name: str
    # env: KEY=VALUE map for catalog entries that declare required env vars.
    env: dict[str, str] = {}
    enable: bool = True
    profile: str | None = None


# --- from web_server.py (originally lines 13716-13723) ---


class PairingApprove(BaseModel):
    platform: str
    code: str = ""
    request_id: str = ""
    profile: str | None = None


class PairingRevoke(BaseModel):
    platform: str
    user_id: str
    profile: str | None = None


# --- from web_server.py (originally lines 13793-13804) ---


class WebhookCreate(BaseModel):
    name: str
    description: str | None = None
    events: list[str] = []
    prompt: str | None = None
    script: str | None = None
    skills: list[str] = []
    deliver: str = "log"
    deliver_only: bool = False
    deliver_chat_id: str | None = None
    # secret: omit to auto-generate
    secret: str | None = None


# --- from web_server.py (originally lines 13930-13931) ---


class WebhookEnabledToggle(BaseModel):
    enabled: bool


# --- from web_server.py (originally lines 13997-14002) ---


class CredentialPoolAdd(BaseModel):
    provider: str
    # api_key for API-key providers; OAuth pooling stays CLI-only (it needs
    # an interactive browser flow that doesn't belong in a single POST).
    api_key: str
    label: str | None = None


# --- from web_server.py (originally lines 14171-14178) ---


class MemoryProviderSelect(BaseModel):
    # "" or "built-in" disables the external provider (built-in only).
    provider: str


class MemoryReset(BaseModel):
    # "all" | "memory" | "user"
    target: str = "all"


# --- from web_server.py (originally lines 14274-14276) ---


class BackupRequest(BaseModel):
    # Optional output path; defaults to a timestamped zip in the home dir.
    output: str | None = None


# --- from web_server.py (originally lines 14339-14348) ---


class ImportRequest(BaseModel):
    archive: str
    # Pass --force to `hermes import`. The spawned action runs with
    # stdin=DEVNULL, so the CLI's interactive "Continue? [y/N]" overwrite
    # prompt hits EOF and auto-aborts ("Aborted.", exit 1) whenever the
    # target already has a config — which it always does when the dashboard
    # itself is running from it. The dashboard shows its own confirm modal
    # before calling this endpoint, then sends force=True so the restore
    # proceeds non-interactively.
    force: bool = False


# --- from web_server.py (originally lines 14505-14513) ---


class HookCreate(BaseModel):
    event: str
    command: str
    matcher: str | None = None
    timeout: int | None = None
    # approve: write the consent allowlist entry too (the operator using the
    # authenticated dashboard is giving consent). Without it the hook is
    # configured but won't fire until approved.
    approve: bool = True


# --- from web_server.py (originally lines 14573-14575) ---


class HookDelete(BaseModel):
    event: str
    command: str


# --- from web_server.py (originally lines 14667-14669) ---


class SkillInstallRequest(BaseModel):
    identifier: str
    profile: str | None = None


# --- from web_server.py (originally lines 14724-14726) ---


class SkillUninstallRequest(BaseModel):
    name: str
    profile: str | None = None


# --- from web_server.py (originally lines 14748-14749) ---


class SkillsUpdateRequest(BaseModel):
    profile: str | None = None


# --- from web_server.py (originally lines 15116-15166) ---


class ProfileCreate(BaseModel):
    name: str
    clone_from: str | None = None
    # Backward compatibility for older dashboard/desktop clients. New clients
    # send clone_from="default" (or another profile name) explicitly.
    clone_from_default: bool = False
    clone_all: bool = False
    no_skills: bool = False
    description: str | None = None
    provider: str | None = None
    model: str | None = None
    # Profile-builder additions — all optional, all applied best-effort AFTER
    # the profile directory exists, so a hiccup in any of them never 500s the
    # create (the user can fix it from the relevant dashboard page afterward).
    # MCP servers to write into the new profile's config.yaml.
    mcp_servers: list[MCPServerCreate] = []
    # Built-in / optional skills to KEEP active. When this list is non-empty,
    # the builder uses "replace" semantics: the bundle is seeded, then every
    # seeded skill NOT in this list is added to the profile's disabled list.
    # Empty list = leave the seeded bundle untouched (legacy behaviour).
    keep_skills: list[str] = []
    # Skills-hub identifiers to install into the new profile. Installed async
    # via a subprocess scoped to the profile (`hermes -p <name> skills install`)
    # because skills_hub.SKILLS_DIR is import-time-bound and the PCBDRAFT_RUNTIME_HOME
    # override can't redirect it. Returns spawned PIDs for the UI to poll.
    hub_skills: list[str] = []


class ProfileRename(BaseModel):
    new_name: str


class ProfileExport(BaseModel):
    # Optional extra root-level files to stage into the archive, filename →
    # text content (e.g. desktop.json — the desktop appearance overlay).
    extra_files: dict[str, str] = {}
    # Where to write the archive. Empty → a staging path under PCBDRAFT_RUNTIME_HOME.
    output: str = ""


class ProfileImport(BaseModel):
    # Path to a profile .tar.gz on the backend's filesystem (the desktop's
    # local/pooled backends share the machine with the picker dialog).
    archive: str
    # Override the profile name inferred from the archive root.
    name: str | None = None


class ProfileSoulUpdate(BaseModel):
    content: str


class ProfileActiveUpdate(BaseModel):
    name: str


class ProfileDescriptionUpdate(BaseModel):
    description: str = ""


class ProfileModelUpdate(BaseModel):
    provider: str
    model: str


class ProfileDescribeAuto(BaseModel):
    overwrite: bool = False


# --- from web_server.py (originally lines 15831-15834) ---


class SkillToggle(BaseModel):
    name: str
    enabled: bool
    profile: str | None = None


# --- from web_server.py (originally lines 15883-15893) ---


class SkillCreate(BaseModel):
    name: str
    content: str
    category: str | None = None
    profile: str | None = None


class SkillContentUpdate(BaseModel):
    name: str
    content: str
    profile: str | None = None


# --- from web_server.py (originally lines 16022-16024) ---


class ToolsetToggle(BaseModel):
    enabled: bool
    profile: str | None = None


# --- from web_server.py (originally lines 16199-16204) ---


class ToolsetProviderSelect(BaseModel):
    provider: str
    # Web-only capability scope: 'search' | 'extract'. Omitted → whole-provider
    # selection through the legacy apply_provider_selection path (web.backend).
    capability: str | None = None
    profile: str | None = None


# --- from web_server.py (originally lines 16324-16327) ---


class ToolsetModelSelect(BaseModel):
    model: str
    provider: str | None = None
    profile: str | None = None


# --- from web_server.py (originally lines 16510-16512) ---


class ToolsetEnvUpdate(BaseModel):
    env: dict[str, str]
    profile: str | None = None


# --- from web_server.py (originally lines 16570-16572) ---


class ToolsetPostSetup(BaseModel):
    key: str
    profile: str | None = None


# --- from web_server.py (originally lines 16823-16825) ---


class TerminalBackendSelect(BaseModel):
    backend: str
    profile: str | None = None


# --- from web_server.py (originally lines 16919-16921) ---


class RawConfigUpdate(BaseModel):
    yaml_text: str
    profile: str | None = None


# --- from web_server.py (originally lines 19410-19411) ---


class ThemeSetBody(BaseModel):
    name: str


# --- from web_server.py (originally lines 19449-19450) ---


class FontSetBody(BaseModel):
    font: str


# --- from web_server.py (originally lines 19681-19684) ---


class _AgentPluginInstallBody(BaseModel):
    identifier: str
    force: bool = False
    enable: bool = True


# --- from web_server.py (originally lines 19896-19898) ---


class _PluginProvidersPutBody(BaseModel):
    memory_provider: str | None = None
    context_engine: str | None = None


# --- from web_server.py (originally lines 19919-19920) ---


class _PluginVisibilityBody(BaseModel):
    hidden: bool
