"""Boot and launch the vendored Hermes agent runtime for PCBDraft.

The Hermes checkout under ``vendor/hermes`` is deliberately kept as a flat,
top-level-import layout (``cli.py``, ``run_agent.py``, ``agent/``,
``tools/``, ``hermes_cli/``, ...) so that its internal imports work unchanged.
:func:`activate` inserts that directory at the front of ``sys.path`` before
any Hermes module is imported, points ``HERMES_HOME`` at a PCBDraft-owned
config directory, and wires the PCB tool registry into Hermes' tool system.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pcbdraft.model.config import (
    ModelConfig,
    load_model_config,
)

__all__ = (
    "DEFAULT_PCB_TOOL_TIMEOUT",
    "activate",
    "hermes_home",
    "hermes_vendor_dir",
    "launch_chat",
    "register_pcb_tools",
    "write_hermes_config",
    "write_soul",
)

DEFAULT_PCB_TOOL_TIMEOUT = 600.0

#: Tool result JSON is bounded so model context stays reviewable.
_MODEL_SUMMARY_EVENT_LIMIT = 8


def hermes_vendor_dir() -> Path:
    """Return the vendored Hermes checkout directory.

    Resolution order: ``PCBDRAFT_HERMES_DIR`` env override, then the in-repo
    ``vendor/hermes`` relative to this package, then a ``vendor/hermes`` next
    to an installed ``pcbdraft`` package.
    """

    explicit = os.environ.get("PCBDRAFT_HERMES_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    here = Path(__file__).resolve()
    for candidate in (
        here.parents[3] / "vendor" / "hermes",
        here.parent.parent / "vendor" / "hermes",
    ):
        if (candidate / "cli.py").is_file():
            return candidate
    raise RuntimeError(
        "vendored Hermes runtime not found; expected vendor/hermes next to "
        "the pcbdraft package or PCBDRAFT_HERMES_DIR"
    )


def hermes_home() -> Path:
    """Return the PCBDraft-owned Hermes home directory."""

    explicit = os.environ.get("HERMES_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    from pcbdraft.model.contracts import provider_config_path

    return provider_config_path().parent / "hermes"


def _install_vendor_path() -> Path:
    vendor = hermes_vendor_dir()
    value = str(vendor)
    if value not in sys.path:
        sys.path.insert(0, value)
    return vendor


def write_hermes_config(model: ModelConfig | None = None) -> Path:
    """Write Hermes ``config.yaml`` from the active PCBDraft model service.

    The active provider in ``~/.config/pcbdraft/config.toml`` becomes the
    Hermes model block (OpenAI-compatible endpoint).  Returns the config path.
    """

    model = model if model is not None else load_model_config()
    provider = model.active
    config_path = hermes_home() / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if provider is None:
        config_path.write_text(
            "model:\n  provider: auto\n", encoding="utf-8"
        )
        return config_path
    model_name = model.active_model or (provider.models[0] if provider.models else "")
    lines = [
        "model:",
        f'  default: "{model_name}"',
        "  provider: custom",
        f'  base_url: "{provider.base_url}"',
        f'  api_key: "{provider.api_key}"',
        "  allow_user_override: true",
        "",
        "display:",
        "  interface: cli",
        "",
        "platform_toolsets:",
        "  cli:",
        "    - hermes-cli",
        "    - pcbdraft",
        "",
        "tools:",
        "  tool_search:",
        "    enabled: off",
        "",
    ]
    config_path.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(config_path, 0o600)
    return config_path


def write_soul(text: str | None = None) -> Path:
    """Write the PCBDraft agent persona into the Hermes home directory."""

    from pcbdraft.hermes.persona import PCB_SOUL_MD

    target = hermes_home() / "SOUL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text if text is not None else PCB_SOUL_MD, encoding="utf-8")
    return target


def register_pcb_tools() -> None:
    """Register the existing PCBDraft PCB tools into the Hermes registry."""

    from pcbdraft.hermes.pcb_tools import register_all_pcb_tools

    register_all_pcb_tools()


def activate(*, model: ModelConfig | None = None) -> None:
    """Prepare the vendored Hermes runtime for one PCBDraft process.

    Safe to call repeatedly: vendor-path insertion, config and persona writes,
    and tool registration are idempotent.
    """

    _install_vendor_path()
    home = hermes_home()
    if not os.environ.get("HERMES_HOME"):
        os.environ["HERMES_HOME"] = str(home)
    home.mkdir(parents=True, exist_ok=True)
    # The vendored trim omits the messaging gateway; install no-op stubs for
    # the ``gateway.*`` modules Hermes tool code imports lazily at runtime.
    import gateway as _gateway_stub

    _gateway_stub.install()
    write_hermes_config(model)
    write_soul()
    register_pcb_tools()
    # Trigger Hermes built-in tool discovery (imports tools/*.py) so the
    # agent's schema includes both Hermes tools and the PCB tools.
    import model_tools  # noqa: F401


def launch_chat(argv: list[str] | None = None) -> int:
    """Launch the Hermes interactive chat (prompt_toolkit REPL) as PCBDraft.

    ``argv`` excludes the program name and defaults to the current process
    arguments.  Returns the process exit code.
    """

    activate()
    import hermes_cli.main as hermes_main

    tokens = list(argv) if argv is not None else sys.argv[1:]
    previous = list(sys.argv)
    sys.argv = ["pcbdraft"] + tokens
    try:
        hermes_main.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = previous
    return 0