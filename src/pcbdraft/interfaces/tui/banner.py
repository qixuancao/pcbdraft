"""PCBDraft welcome banner and terminal display helpers.

Pure display functions with no TerminalApp state dependency.
"""

import json
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pcbdraft.core.runtime_environment import get_runtime_home

# rich and prompt_toolkit are imported lazily (inside the functions that use
# them) rather than at module level.  Importing this module is on the TUI
# gateway's critical startup path purely to reach the lightweight update-check
# helpers (``prefetch_update_check``); pulling rich.console + prompt_toolkit
# eagerly added ~50ms of wasted imports before ``gateway.ready`` could fire.
# Keep the type-only reference available to checkers without the runtime cost.
if TYPE_CHECKING:
    from rich.console import Console

logger = logging.getLogger(__name__)


# =========================================================================
# ANSI building blocks for conversation display
# =========================================================================

_GOLD = "\033[1;38;2;255;215;0m"  # True-color #FFD700 bold
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RST = "\033[0m"


def cprint(text: str):
    """Print ANSI-colored text through prompt_toolkit's renderer."""
    from prompt_toolkit import print_formatted_text as _pt_print
    from prompt_toolkit.formatted_text import ANSI as _PT_ANSI

    try:
        _pt_print(_PT_ANSI(text))
    except Exception:
        # prompt_toolkit needs a real console. On Windows, a redirected or
        # absent stdout (pythonw.exe, CI, `pcbdraft ... > file`) raises
        # NoConsoleScreenBufferError from its Win32Output — display helpers
        # must never crash the caller over that, so degrade to plain print.
        print(text)


# =========================================================================
# Skin-aware color helpers
# =========================================================================


def _skin_color(key: str, fallback: str) -> str:
    """Get a color from the active skin, or return fallback."""
    try:
        from pcbdraft.interfaces.tui.skin_engine import get_active_skin

        return get_active_skin().get_color(key, fallback)
    except Exception:
        return fallback


# =========================================================================
# ASCII Art & Branding
# =========================================================================

from pcbdraft.interfaces.tui import __version__ as VERSION

PCBDRAFT_RUNTIME_AGENT_LOGO = "[bold #22c55e]PCBDraft[/bold #22c55e]"

PCBDRAFT_RUNTIME_CADUCEUS = "[bold #22c55e]PCBDraft[/bold #22c55e]"


# =========================================================================
# Skills scanning
# =========================================================================

_available_skills_cache: tuple | None = None  # (result,) once computed


def get_available_skills() -> dict[str, list[str]]:
    """Return skills grouped by category, filtered by platform and disabled state.

    Delegates to ``_find_all_skills()`` from ``tools/skills_tool`` which already
    handles platform gating (``platforms:`` frontmatter) and respects the
    user's ``skills.disabled`` config list.

    Cached per-process: this feeds only the startup banner, whose snapshot
    is taken once anyway, and the underlying skills-tree walk costs ~100ms.
    ``prefetch_banner_data()`` uses the cache to pay that walk off-thread.
    """
    global _available_skills_cache
    if _available_skills_cache is not None:
        return _available_skills_cache[0]
    try:
        from pcbdraft.tools.skills_tool import _find_all_skills

        all_skills = _find_all_skills()  # already filtered
    except Exception:
        return {}

    skills_by_category: dict[str, list[str]] = {}
    for skill in all_skills:
        category = skill.get("category") or "general"
        skills_by_category.setdefault(category, []).append(skill["name"])
    _available_skills_cache = (skills_by_category,)
    return skills_by_category


# =========================================================================
# Update check
# =========================================================================

# Sentinel returned when we know an update exists but can't count commits
# (e.g. nix-built pcbdraft — no local git history to count against).
UPDATE_AVAILABLE_NO_COUNT = -1


def check_for_updates() -> int | None:
    """Updates are managed by the PCBDraft installer, not a second checkout."""
    return None


def _resolve_repo_dir() -> Path | None:
    """Return the active PCBDraft git checkout, or None if this isn't a git install.

    Prefers the running code's location over the profile-scoped path
    because ``$PCBDRAFT_RUNTIME_HOME/pcbdraft/`` may be a stale copy carried
    over by ``--clone-all``.
    """
    return next(
        (
            parent
            for parent in Path(__file__).resolve().parents
            if (parent / ".git").exists()
        ),
        None,
    )


def _git_short_hash(repo_dir: Path, rev: str) -> str | None:
    """Resolve a git revision to an 8-character short hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=8", rev],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            cwd=str(repo_dir),
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


_git_banner_state_cache: tuple | None = None  # (state_or_None,) once computed


def get_git_banner_state(repo_dir: Path | None = None) -> dict | None:
    """Return upstream/local git hashes for the startup banner.

    For source installs and dev images this runs ``git rev-parse`` against
    the active checkout.  When no checkout is available — the canonical case
    is the published Docker image, which excludes ``.git`` from the build
    context — we fall back to the baked-in build SHA (see
    ``pcbdraft.interfaces.tui/build_info.py``) and return it as a frozen
    ``upstream == local`` state with ``ahead=0``.  A built image is by
    definition pinned to one commit, so "ahead" is always zero and the
    banner correctly shows ``· upstream <sha>`` with no carried-commits
    annotation.

    Cached per-process (default ``repo_dir`` only): the state costs 2-3 git
    subprocesses (~100ms) and the checkout revision cannot change under a
    running CLI in a way the banner needs to observe live. The cache also
    lets ``prefetch_banner_data()`` pay this cost off-thread before the
    banner renders.
    """
    global _git_banner_state_cache
    if repo_dir is None and _git_banner_state_cache is not None:
        return _git_banner_state_cache[0]
    state = _compute_git_banner_state(repo_dir)
    if repo_dir is None:
        _git_banner_state_cache = (state,)
    return state


def _compute_git_banner_state(repo_dir: Path | None = None) -> dict | None:
    repo_dir = repo_dir or _resolve_repo_dir()
    if repo_dir is None:
        # No git checkout — try the baked build SHA (Docker image path).
        try:
            from pcbdraft.interfaces.tui.build_info import get_build_sha

            baked = get_build_sha(short=8)
            if baked:
                return {"upstream": baked, "local": baked, "ahead": 0}
        except Exception:
            pass
        return None

    upstream = _git_short_hash(repo_dir, "origin/main")
    local = _git_short_hash(repo_dir, "HEAD")
    if not upstream or not local:
        # Live-git lookup failed (e.g. shallow clone without origin/main).
        # Fall back to the baked build SHA if available.
        try:
            from pcbdraft.interfaces.tui.build_info import get_build_sha

            baked = get_build_sha(short=8)
            if baked:
                return {"upstream": baked, "local": baked, "ahead": 0}
        except Exception:
            pass
        return None

    ahead = 0
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "origin/main..HEAD"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            cwd=str(repo_dir),
        )
        if result.returncode == 0:
            ahead = int((result.stdout or "0").strip() or "0")
    except Exception:
        ahead = 0

    return {"upstream": upstream, "local": local, "ahead": max(ahead, 0)}


_RELEASE_URL_BASE = "https://github.com/qixuancao/pcbdraft/releases/tag"
_latest_release_cache: tuple | None = None  # (tag, url) once resolved


def get_latest_release_tag(repo_dir: Path | None = None) -> tuple | None:
    """Return ``(tag, release_url)`` for the latest git tag, or None.

    Local-only — runs ``git describe --tags --abbrev=0`` against the
    PCBDraft checkout. Cached per-process. Release URL always points at the
    PCBDraft repository.
    """
    global _latest_release_cache
    if _latest_release_cache is not None:
        return _latest_release_cache or None

    repo_dir = repo_dir or _resolve_repo_dir()
    if repo_dir is None:
        _latest_release_cache = ()  # falsy sentinel — skip future lookups
        return None

    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            cwd=str(repo_dir),
        )
    except Exception:
        _latest_release_cache = ()
        return None

    if result.returncode != 0:
        _latest_release_cache = ()
        return None

    tag = (result.stdout or "").strip()
    if not tag:
        _latest_release_cache = ()
        return None

    url = f"{_RELEASE_URL_BASE}/{tag}"
    _latest_release_cache = (tag, url)
    return _latest_release_cache


def format_banner_version_label() -> str:
    """Use the installed PCBDraft package identity."""
    return f"PCBDraft v{VERSION}"


# =========================================================================
# Non-blocking update check
# =========================================================================

_update_result: int | None = None
_update_check_done = threading.Event()


def prefetch_update_check():
    """Kick off update check in a background daemon thread."""

    def _run():
        global _update_result
        _update_result = check_for_updates()
        _update_check_done.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()


_banner_data_prefetch_started = False


def prefetch_banner_data():
    """Warm the local git state used in the PCBDraft version label."""
    global _banner_data_prefetch_started
    if _banner_data_prefetch_started:
        return
    _banner_data_prefetch_started = True

    def _run() -> None:
        try:
            get_git_banner_state()
        except Exception:
            pass

    threading.Thread(target=_run, name="banner-data-prefetch", daemon=True).start()


def get_update_result(timeout: float = 0.5) -> int | None:
    """Get result of prefetched check. Returns None if not ready."""
    _update_check_done.wait(timeout=timeout)
    return _update_result


def _format_update_notice(behind: int) -> str:
    """Render the update warning line for a non-zero ``behind`` result."""
    from pcbdraft.model.configuration import (
        get_managed_update_command,
        recommended_update_command,
    )

    if behind > 0:
        commits_word = "commit" if behind == 1 else "commits"
        return (
            f"[bold yellow]⚠ {behind} {commits_word} behind[/]"
            f"[dim yellow] — run [bold]{recommended_update_command()}[/bold] to update[/]"
        )
    # UPDATE_AVAILABLE_NO_COUNT: nix-built pcbdraft; we know an update
    # exists but not by how much, and we don't know how the user
    # installed it (nix run, profile, system flake, home-manager).
    managed_cmd = get_managed_update_command()
    line = "[bold yellow]⚠ update available[/]"
    if managed_cmd:
        line += f"[dim yellow] — run [bold]{managed_cmd}[/bold][/]"
    return line


_deferred_update_notice_started = False


def _defer_update_notice(console: "Console", max_wait: float = 30.0) -> None:
    """Print the update warning once the prefetched check completes.

    Used when the banner rendered before the update prefetch finished so
    startup never blocks on git/network. Prints at most once per process.
    """
    global _deferred_update_notice_started
    if _deferred_update_notice_started:
        return
    _deferred_update_notice_started = True

    def _wait_and_print() -> None:
        try:
            if not _update_check_done.wait(timeout=max_wait):
                return
            behind = _update_result
            if behind is None or behind == 0:
                return
            console.print(_format_update_notice(behind))
        except Exception:
            pass  # never break the session over an update notice

    threading.Thread(target=_wait_and_print, name="update-notice", daemon=True).start()


# =========================================================================
# Welcome banner
# =========================================================================


def _format_context_length(tokens: int) -> str:
    """Format a token count for display (e.g. 128000 → '128K', 1048576 → '1M')."""
    if tokens >= 1_000_000:
        val = tokens / 1_000_000
        rounded = round(val)
        if abs(val - rounded) < 0.05:
            return f"{rounded}M"
        return f"{val:.1f}M"
    elif tokens >= 1_000:
        val = tokens / 1_000
        rounded = round(val)
        if abs(val - rounded) < 0.05:
            return f"{rounded}K"
        return f"{val:.1f}K"
    return str(tokens)


def _display_toolset_name(toolset_name: str) -> str:
    """Normalize internal/legacy toolset identifiers for banner display."""
    if not toolset_name:
        return "unknown"
    return toolset_name.removesuffix("_tools")


# =========================================================================
# Banner snapshot — warm-launch fast path
# =========================================================================
# The banner's tool panel needs the full tool registry (get_tool_definitions:
# tools/*.py discovery + every check_fn), which costs ~0.5-0.9s cold and is
# the single largest chunk of CLI time-to-banner. The tool list shown in the
# banner is a pure function of (config.yaml, .env, code checkout, enabled
# toolsets), so we snapshot the rendered inputs to disk after each launch
# and replay them on the next one when the fingerprint matches. The agent's
# REAL tool list is still computed fresh at first message (agent init) —
# the snapshot only feeds the cosmetic startup panel, and a background
# refresh re-verifies it right after the banner renders (see
# cli.show_banner), so a stale panel self-heals within one launch.

_BANNER_SNAPSHOT_VERSION = 2


def _banner_snapshot_path() -> Path:
    return get_runtime_home() / "cache" / "banner_snapshot.json"


def banner_snapshot_fingerprint() -> str | None:
    """Fingerprint the inputs the banner tool panel depends on."""
    import hashlib

    parts = [f"v{_BANNER_SNAPSHOT_VERSION}"]
    try:
        from pcbdraft.model.configuration import get_config_path

        for p in (get_config_path(), get_runtime_home() / ".env"):
            try:
                st = p.stat()
                parts.append(f"{p.name}:{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                parts.append(f"{p.name}:absent")
    except Exception:
        return None
    # Code checkout: version + git HEAD when available (post-update change).
    parts.append(str(VERSION))
    state = get_git_banner_state()
    if state:
        parts.append(str(state.get("local", "")))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def load_banner_snapshot(
    enabled_toolsets: list[str] | None = None,
) -> dict[str, Any] | None:
    """Return the stored banner snapshot when its fingerprint is current."""
    try:
        blob = json.loads(_banner_snapshot_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(blob, dict):
        return None
    fp = banner_snapshot_fingerprint()
    if not fp or blob.get("fingerprint") != fp:
        return None
    if blob.get("enabled_toolsets") != sorted(enabled_toolsets or []):
        return None
    tools = blob.get("tools")
    toolset_map = blob.get("toolset_map")
    availability = blob.get("availability")
    if (
        not isinstance(tools, list)
        or not isinstance(toolset_map, dict)
        or not isinstance(availability, dict)
    ):
        return None
    return blob


def save_banner_snapshot(
    tools: list[dict],
    enabled_toolsets: list[str],
    availability: dict[str, Any],
    toolset_map: dict[str, str],
) -> None:
    """Persist the banner tool panel inputs for next launch (best-effort)."""
    fp = banner_snapshot_fingerprint()
    if not fp:
        return
    payload = {
        "fingerprint": fp,
        "enabled_toolsets": sorted(enabled_toolsets or []),
        "tools": [
            {"function": {"name": t["function"]["name"]}}
            for t in tools
            if isinstance(t, dict) and t.get("function", {}).get("name")
        ],
        "toolset_map": toolset_map,
        "availability": {
            "unavailable_toolsets": availability.get("unavailable_toolsets", []),
            "lazy_tools": list(availability.get("lazy_tools", [])),
            "disabled_tools": list(availability.get("disabled_tools", [])),
        },
    }
    path = _banner_snapshot_path()
    try:
        import os as _os
        import tempfile as _tempfile

        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = _tempfile.mkstemp(dir=str(path.parent), prefix=".banner_snap.")
        with _os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        _os.replace(tmp, path)
    except Exception:
        pass


def compute_toolset_availability(
    enabled_toolsets: list[str] | None = None,
) -> dict[str, Any]:
    """Compute the banner's toolset-availability payload.

    Returns ``{"unavailable_toolsets": [...], "lazy_tools": [...],
    "disabled_tools": [...]}`` — the exact inputs ``build_welcome_banner``
    needs to annotate disabled/lazy tools. Split out so the result can be
    snapshotted to disk and replayed on the next launch without importing
    ``model_tools`` (see ``load_banner_snapshot``).
    """
    from pcbdraft.tools.dispatch import TOOLSET_REQUIREMENTS, check_tool_availability

    enabled_toolsets = enabled_toolsets or []
    _, unavailable_toolsets = check_tool_availability(quiet=True)
    # The availability check walks the GLOBAL toolset registry, so it includes
    # toolsets that aren't part of this agent's platform set at all (e.g.
    # `discord`, `feishu_doc` on a CLI session). Those must never surface in the
    # banner's "Available Tools" — they aren't exposed to the agent. Restrict to
    # toolsets actually enabled for this agent; a toolset that's enabled but
    # currently has unmet deps legitimately shows as disabled/lazy below.
    _enabled_ts = {str(t) for t in enabled_toolsets}
    if _enabled_ts:
        unavailable_toolsets = [
            item
            for item in unavailable_toolsets
            if str(item.get("id", item.get("name", ""))) in _enabled_ts
        ]
    disabled_tools = set()
    # Tools whose toolset has a check_fn are lazy-initialized (e.g. honcho,
    # homeassistant) — they show as unavailable at banner time because the
    # check hasn't run yet, but they aren't misconfigured.
    lazy_tools = set()
    for item in unavailable_toolsets:
        toolset_name = item.get("name", "")
        ts_req = TOOLSET_REQUIREMENTS.get(toolset_name, {})
        tools_in_ts = item.get("tools", [])
        if ts_req.get("check_fn"):
            lazy_tools.update(tools_in_ts)
        else:
            disabled_tools.update(tools_in_ts)
    return {
        "unavailable_toolsets": unavailable_toolsets,
        "lazy_tools": sorted(lazy_tools),
        "disabled_tools": sorted(disabled_tools),
    }


def build_welcome_banner(
    console: "Console",
    model: str,
    cwd: str,
    tools: list[dict] | None = None,
    enabled_toolsets: list[str] | None = None,
    session_id: str | None = None,
    get_toolset_for_tool=None,
    context_length: int | None = None,
    provider: str | None = None,
    availability: dict[str, Any] | None = None,
    skills_by_category: dict[str, list[str]] | None = None,
):
    """Build and print a welcome banner with caduceus on left and info on right.

    Args:
        console: Rich Console instance.
        model: Current model name.
        cwd: Current working directory.
        tools: List of tool definitions.
        enabled_toolsets: List of enabled toolset names.
        session_id: Session identifier.
        get_toolset_for_tool: Callable to map tool name -> toolset name.
        context_length: Model's context window size in tokens.
        provider: Active provider id. When ``"moa"``, ``model`` is a MoA
            preset name and the banner renders the aggregator instead of a
            bare model slug.
        availability: Optional precomputed result of
            ``compute_toolset_availability`` (e.g. replayed from the banner
            snapshot). When provided together with ``get_toolset_for_tool``,
            this function performs no ``model_tools`` import at all.
    """
    from rich.panel import Panel
    from rich.table import Table

    if get_toolset_for_tool is None:
        from pcbdraft.tools.dispatch import get_toolset_for_tool

    tools = tools or []
    enabled_toolsets = enabled_toolsets or []

    if availability is None:
        availability = compute_toolset_availability(enabled_toolsets)
    unavailable_toolsets = availability.get("unavailable_toolsets", [])
    lazy_tools = set(availability.get("lazy_tools", []))
    disabled_tools = set(availability.get("disabled_tools", []))
    layout_table = Table.grid(padding=(0, 2))
    layout_table.add_column("left", justify="center")
    layout_table.add_column("right", justify="left")

    # Resolve skin colors once for the entire banner
    accent = _skin_color("banner_accent", "#FFBF00")
    dim = _skin_color("banner_dim", "#B8860B")
    session_color = _skin_color("session_border", "#8B8682")

    # Skins control the palette, while the product identity remains PCBDraft.
    left_lines = ["", PCBDRAFT_RUNTIME_CADUCEUS, ""]
    if (provider or "").strip().lower() == "moa":
        # MoA virtual provider: ``model`` is a preset name. Show the preset and
        # its aggregator so the banner is meaningful instead of a bare slug.
        preset_name = model
        agg_label = ""
        try:
            from pcbdraft.interfaces.tui.moa_config import normalize_moa_config
            from pcbdraft.model.configuration import load_config

            _moa = normalize_moa_config(load_config().get("moa") or {})
            _preset = _moa.get("presets", {}).get(preset_name)
            if _preset:
                _agg = _preset.get("aggregator") or {}
                _am = str(_agg.get("model") or "")
                agg_label = _am.split("/")[-1] if "/" in _am else _am
        except Exception:
            agg_label = ""
        if len(preset_name) > 28:
            preset_name = preset_name[:25] + "..."
        agg_str = f" [dim {dim}]·[/] [dim {dim}]agg {agg_label}[/]" if agg_label else ""
        ctx_str = (
            f" [dim {dim}]·[/] [dim {dim}]{_format_context_length(context_length)} context[/]"
            if context_length
            else ""
        )
        left_lines.append(f"[{accent}]MoA: {preset_name}[/]{agg_str}{ctx_str}")
    else:
        if not (model or "").strip() or (model or "").strip().lower() == "unknown":
            # Unconfigured install: say so in red instead of a blank/"unknown"
            # slug — this is the single clearest place to tell the user what
            # is wrong and how to fix it.
            left_lines.append(
                f"[bold red]no model configured[/] "
                f"[dim {dim}]— run /connect or /model[/]"
            )
        else:
            model_short = model.split("/")[-1] if "/" in model else model
            model_short = model_short.removesuffix(".gguf")
            if len(model_short) > 28:
                model_short = model_short[:25] + "..."
            ctx_str = (
                f" [dim {dim}]·[/] [dim {dim}]{_format_context_length(context_length)} context[/]"
                if context_length
                else ""
            )
            left_lines.append(f"[{accent}]{model_short}[/]{ctx_str}")

    if os.getenv("PCBDRAFT_RUNTIME_YOLO_MODE"):
        left_lines.append(
            f"[bold red]⚠ YOLO mode[/] [dim {dim}]— all approval prompts bypassed[/]"
        )
    left_lines.append(f"[dim {dim}]{cwd}[/]")
    if session_id:
        left_lines.append(f"[dim {session_color}]Session: {session_id}[/]")
    left_content = "\n".join(left_lines)

    right_lines = [f"[bold {accent}]Available Tools[/]"]
    toolsets_dict: dict[str, list] = {}

    for tool in tools:
        tool_name = tool["function"]["name"]
        toolset = _display_toolset_name(get_toolset_for_tool(tool_name) or "other")
        toolsets_dict.setdefault(toolset, []).append(tool_name)

    for item in unavailable_toolsets:
        toolset_id = item.get("id", item.get("name", "unknown"))
        display_name = _display_toolset_name(toolset_id)
        if display_name not in toolsets_dict:
            toolsets_dict[display_name] = []
        for tool_name in item.get("tools", []):
            if tool_name not in toolsets_dict[display_name]:
                toolsets_dict[display_name].append(tool_name)

    sorted_toolsets = sorted(toolsets_dict.keys())
    display_toolsets = sorted_toolsets[:8]
    remaining_toolsets = len(sorted_toolsets) - 8

    for toolset in display_toolsets:
        tool_names = toolsets_dict[toolset]
        colored_names = []
        for name in sorted(tool_names):
            if name in disabled_tools:
                colored_names.append(f"[red]{name}[/]")
            elif name in lazy_tools:
                colored_names.append(f"[yellow]{name}[/]")
            else:
                colored_names.append(name)

        tools_str = ", ".join(colored_names)
        if len(", ".join(sorted(tool_names))) > 45:
            short_names = []
            length = 0
            for name in sorted(tool_names):
                if length + len(name) + 2 > 42:
                    short_names.append("...")
                    break
                short_names.append(name)
                length += len(name) + 2
            colored_names = []
            for name in short_names:
                if name == "...":
                    colored_names.append("[dim]...[/]")
                elif name in disabled_tools:
                    colored_names.append(f"[red]{name}[/]")
                elif name in lazy_tools:
                    colored_names.append(f"[yellow]{name}[/]")
                else:
                    colored_names.append(name)
            tools_str = ", ".join(colored_names)

        right_lines.append(f"[dim {dim}]{toolset}:[/] {tools_str}")

    if remaining_toolsets > 0:
        right_lines.append(f"[dim {dim}](and {remaining_toolsets} more toolsets...)[/]")

    # MCP Servers section (only if configured). Probe cheaply first: the
    # full get_mcp_status() path resolves portable plugin MCP servers,
    # which JOINS the in-flight background plugin discovery (~100ms on the
    # startup path). When neither config.yaml nor the persisted plugin
    # key cache mentions any MCP server, skip the section outright.
    mcp_status = []
    try:
        from pcbdraft.model.configuration import load_config as _load_cfg

        _has_native_mcp = bool((_load_cfg() or {}).get("mcp_servers"))
    except Exception:
        _has_native_mcp = True  # can't tell — take the full path
    _has_portable_mcp = False
    if not _has_native_mcp:
        try:
            from pcbdraft.agent.extensions.manager import (
                get_portable_mcp_server_names_nowait,
            )

            _has_portable_mcp = bool(get_portable_mcp_server_names_nowait())
        except Exception:
            _has_portable_mcp = True  # can't tell — take the full path
    if _has_native_mcp or _has_portable_mcp:
        try:
            from pcbdraft.tools.mcp_tool import get_mcp_status

            mcp_status = get_mcp_status()
        except Exception:
            mcp_status = []

    if mcp_status:
        right_lines.append("")
        right_lines.append(f"[bold {accent}]MCP Servers[/]")
        for srv in mcp_status:
            status = srv.get("status")
            if srv["connected"]:
                right_lines.append(
                    f"[dim {dim}]{srv['name']}[/] ({srv['transport']}) "
                    f"[dim {dim}]—[/] {srv['tools']} tool(s)"
                )
            elif srv.get("disabled") or status == "disabled":
                right_lines.append(
                    f"[dim {dim}]{srv['name']}[/] [dim]({srv['transport']})[/] "
                    f"[dim {dim}]— disabled[/]"
                )
            elif status == "connecting":
                right_lines.append(
                    f"[dim {dim}]{srv['name']}[/] [dim]({srv['transport']})[/] "
                    f"[yellow]— connecting[/]"
                )
            elif status == "configured":
                right_lines.append(
                    f"[dim {dim}]{srv['name']}[/] [dim]({srv['transport']})[/] "
                    f"[dim {dim}]— configured[/]"
                )
            else:
                right_lines.append(
                    f"[red]{srv['name']}[/] [dim]({srv['transport']})[/] "
                    f"[red]— failed[/]"
                )

    right_lines.append("")
    mcp_connected = sum(1 for s in mcp_status if s["connected"]) if mcp_status else 0
    summary_parts = [f"{len(tools)} tools"]
    if mcp_connected:
        summary_parts.append(f"{mcp_connected} MCP servers")
    summary_parts.append("/help for commands")
    # Indicate when the codex_app_server runtime is active so users
    # understand why tool counts may not match what's actually reachable
    # (codex builds its own tool list inside the spawned subprocess).
    try:
        from pcbdraft.interfaces.tui.codex_runtime_switch import get_current_runtime
        from pcbdraft.model.configuration import load_config as _load_cfg

        if get_current_runtime(_load_cfg()) == "codex_app_server":
            right_lines.append(
                f"[bold {accent}]Runtime:[/] codex app-server "
                f"[dim {dim}](terminal/file ops/MCP run inside codex)[/]"
            )
    except Exception:
        pass
    right_lines.append(f"[dim {dim}]{' · '.join(summary_parts)}[/]")

    right_content = "\n".join(right_lines)
    layout_table.add_row(left_content, right_content)

    title_color = _skin_color("banner_title", "#FFD700")
    border_color = _skin_color("banner_border", "#CD7F32")
    version_label = format_banner_version_label()
    title_markup = f"[bold {title_color}]{version_label}[/]"
    outer_panel = Panel(
        layout_table,
        title=title_markup,
        border_style=border_color,
        padding=(0, 2),
    )

    console.print()
    term_width = shutil.get_terminal_size().columns
    if term_width >= 95:
        console.print(PCBDRAFT_RUNTIME_AGENT_LOGO)
        console.print()
    console.print(outer_panel)
