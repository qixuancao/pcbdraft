"""PCBDraft model-wizard and TUI helpers.

The public command tree belongs to ``pcbdraft.interfaces.cli``. Importing this
module does not parse argv, load credentials, repair an install, contact an
updater, build a desktop app, or change terminal/process state.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
import sys

from pcbdraft.interfaces.tui.update_cmd import unsupported_lifecycle

logger = logging.getLogger(__name__)

_LAZY_COMMAND_EXPORTS = {
    "pcbdraft.interfaces.tui.dashboard_procs": (
        "_detect_concurrent_pcbdraft_instances",
        "_filter_dashboard_respawn_candidates",
        "_kill_stale_dashboard_processes",
        "_scan_dashboard_processes",
    ),
    "pcbdraft.interfaces.tui.update_cmd": ("_cmd_update_check", "_cmd_update_impl"),
}
_LAZY_COMMAND_ATTR_TO_MODULE = {
    attr: module for module, attrs in _LAZY_COMMAND_EXPORTS.items() for attr in attrs
}
_MODEL_FLOW_NAMES = frozenset(
    {
        "_model_flow_ai_gateway",
        "_model_flow_anthropic",
        "_model_flow_api_key_provider",
        "_model_flow_azure_foundry",
        "_model_flow_bedrock",
        "_model_flow_bedrock_api_key",
        "_model_flow_copilot",
        "_model_flow_copilot_acp",
        "_model_flow_custom",
        "_model_flow_kimi",
        "_model_flow_minimax_oauth",
        "_model_flow_moa",
        "_model_flow_named_custom",
        "_model_flow_nous",
        "_model_flow_openai_codex",
        "_model_flow_openrouter",
        "_model_flow_qwen_oauth",
        "_model_flow_stepfun",
        "_model_flow_vertex",
        "_model_flow_xai_oauth",
        "_prompt_auth_credentials_choice",
    }
)


def __getattr__(name):
    module = _LAZY_COMMAND_ATTR_TO_MODULE.get(name)
    if name == "_PROVIDER_MODELS":
        module = "pcbdraft.model.catalog"
    elif name in _MODEL_FLOW_NAMES:
        module = "pcbdraft.model.model_setup_flows"
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def _self():
    return sys.modules[__name__]


def _require_tty(command_name: str) -> None:
    if not sys.stdin.isatty():
        print(
            f"'pcbdraft {command_name}' requires an interactive terminal.",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _relative_time(ts) -> str:
    from pcbdraft.interfaces.tui.timefmt import relative_time

    return relative_time(ts)


def get_runtime_home():
    """Retain the call-time path helper used by internal session reports."""
    from pcbdraft.core.runtime_environment import get_runtime_home as resolve_home

    return resolve_home()


def _size_delta_label(saved_mb):
    return (
        f"{saved_mb:.1f} MB saved"
        if saved_mb >= 0
        else f"{abs(saved_mb):.1f} MB larger"
    )


def _print_version_info(*, check_updates: bool = True) -> None:
    """Retained argument never starts a network update check."""
    from pcbdraft.interfaces.tui._startup_fast import print_fast_version_info

    print_fast_version_info()


def _resolve_session_by_name_or_id(name_or_id: str) -> str | None:
    db = None
    try:
        from pcbdraft.services.session_db import SessionDB

        db = SessionDB()
        session = db.get_session(name_or_id)
        resolved = session["id"] if session else db.resolve_session_by_title(name_or_id)
        if resolved:
            try:
                resolved = db.get_compression_tip(resolved) or resolved
            except Exception as exc:  # noqa: BLE001 — optional database capability
                logger.debug("Compression-tip lookup failed (%s)", type(exc).__name__)
        return resolved
    except Exception as exc:  # noqa: BLE001 — session lookup is best-effort
        logger.debug("Session lookup failed (%s)", type(exc).__name__)
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception as exc:  # noqa: BLE001 — cleanup must not mask lookup
                logger.debug("Session database close failed (%s)", type(exc).__name__)


def _is_profile_api_key_provider(provider_id: str) -> bool:
    from pcbdraft.model.provider_profiles import get_provider_profile

    profile = get_provider_profile(provider_id)
    return profile is not None and profile.auth_type == "api_key"


def _named_custom_provider_map(config) -> dict:
    """Preserve unexpanded secret/URL references while displaying loaded config."""
    from pcbdraft.model.configuration import (
        get_compatible_custom_providers,
        read_raw_config,
    )
    from pcbdraft.model.provider_config import custom_provider_slug

    refs: dict[str, dict[tuple, str]] = {"api_key": {}, "base_url": {}}
    raw = read_raw_config()
    entries = []
    custom = raw.get("custom_providers")
    if isinstance(custom, list):
        entries.extend(("", item) for item in custom if isinstance(item, dict))
    providers = raw.get("providers")
    if isinstance(providers, dict):
        entries.extend(
            (key, item) for key, item in providers.items() if isinstance(item, dict)
        )
    for provider_key, item in entries:
        name = str(item.get("name") or provider_key).strip().lower()
        key = str(provider_key).strip().lower()
        model = str(item.get("model") or item.get("default_model") or "").strip()
        for field, references in refs.items():
            value = item.get(field, "")
            if field == "base_url":
                value = value or item.get("url") or item.get("api") or ""
            value = str(value).strip()
            if "${" in value:
                for identity in (name, key):
                    if identity:
                        references.setdefault((identity,), value)
                        references.setdefault((identity, model), value)
    result = {}
    for entry in get_compatible_custom_providers(config):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        url = str(entry.get("base_url") or "").strip()
        if not name or not url:
            continue
        key = str(entry.get("provider_key") or "").strip()
        model = str(entry.get("model") or "").strip()
        info = {
            "name": name,
            "base_url": url,
            "provider_key": key,
            "api_key": entry.get("api_key", ""),
            "key_env": entry.get("key_env", ""),
            "model": entry.get("model", ""),
            "models": entry.get("models", {}),
            "discover_models": entry.get("discover_models", True),
            "api_mode": entry.get("api_mode", ""),
        }
        for field, references in refs.items():
            identities = (
                (key.lower(), model),
                (key.lower(),),
                (name.lower(), model),
                (name.lower(),),
            )
            info[field + "_ref"] = next(
                (
                    references[identity]
                    for identity in identities
                    if identity[0] and identity in references
                ),
                "",
            )
        result[custom_provider_slug(name, key)] = info
    return result


def select_provider_and_model(args=None):
    """Provider picker, credential/model flow, and configuration persistence."""
    from pcbdraft.model.auth import AuthError, format_auth_error, resolve_provider
    from pcbdraft.model.catalog import (
        _PROVIDER_ALIASES,
        _PROVIDER_LABELS,
        CANONICAL_PROVIDERS,
        group_providers,
        provider_group_for_slug,
    )
    from pcbdraft.model.configuration import (
        get_compatible_custom_providers,
        get_env_value,
        load_config,
    )
    from pcbdraft.model.provider_config import (
        custom_provider_aliases,
        resolve_provider_full,
    )

    config = load_config()
    model_cfg = config.get("model")
    current_model = (
        model_cfg.get("default", "") if isinstance(model_cfg, dict) else model_cfg
    )
    current_model = current_model or "(not set)"
    effective_provider = (
        (model_cfg.get("provider") if isinstance(model_cfg, dict) else None)
        or os.getenv("PCBDRAFT_RUNTIME_INFERENCE_PROVIDER")
        or "auto"
    )
    custom_map = _named_custom_provider_map(config)
    active = ""
    if effective_provider == "custom" and isinstance(model_cfg, dict):
        current_url = str(model_cfg.get("base_url") or "").strip().rstrip("/").lower()
        if current_url:
            active = next(
                (
                    key
                    for key, info in custom_map.items()
                    if info["base_url"].rstrip("/").lower() == current_url
                ),
                "",
            )
    if not active and effective_provider != "auto":
        definition = resolve_provider_full(
            effective_provider,
            config.get("providers"),
            get_compatible_custom_providers(config),
        )
        if definition is not None:
            active = definition.id
            if definition.source == "user-config":
                active = next(
                    (
                        key
                        for key, info in custom_map.items()
                        if active.lower()
                        in custom_provider_aliases(info["name"], info["provider_key"])
                    ),
                    active,
                )
        else:
            print(
                f"Unknown provider '{effective_provider}'; run `pcbdraft doctor` for diagnostics."
            )
    if not active:
        try:
            active = resolve_provider("auto")
        except AuthError as exc:
            if effective_provider == "auto":
                print(f"Warning: {format_auth_error(exc)}")
            active = None
    if active == "openrouter" and get_env_value("OPENAI_BASE_URL"):
        active = "custom"
    labels = dict(_PROVIDER_LABELS)
    active_label = (
        custom_map[active]["name"]
        if active in custom_map
        else labels.get(active, active or "none")
    )
    print(
        f"\n  Current model:    {current_model}\n  Active provider:  {active_label}\n"
    )
    descriptions = {
        provider.slug: provider.tui_desc for provider in CANONICAL_PROVIDERS
    }
    excluded = {
        str(p).strip().lower()
        for p in (config.get("model_catalog", {}) or {}).get("excluded_providers") or []
        if p
    }
    names = {provider.slug: {provider.slug.lower()} for provider in CANONICAL_PROVIDERS}
    for alias, canonical in _PROVIDER_ALIASES.items():
        names.setdefault(canonical, {canonical.lower()}).add(alias.lower())
    visible = [
        provider.slug
        for provider in CANONICAL_PROVIDERS
        if not names.get(provider.slug, {provider.slug.lower()}) & excluded
    ]
    active_group = provider_group_for_slug(active) if active else ""
    ordered = []
    default = 0
    for row in group_providers(visible):
        if row["kind"] == "group":
            key = "group:" + row["group_id"]
            label = row["label"] + " ▸"
            if row.get("description"):
                label += f" ({row['description']})"
            members = row["members"]
            selected = bool(active_group) and row["group_id"] == active_group
        else:
            key, members = row["slug"], []
            label = descriptions.get(key, labels.get(key, key))
            selected = bool(active) and key == active
        if selected:
            default = len(ordered)
            label += "  ← currently active"
        ordered.append((key, label, members))
    for key, info in custom_map.items():
        url = (
            info["base_url"].replace("https://", "").replace("http://", "").rstrip("/")
        )
        label = f"{info['name']} ({url})"
        if info.get("model"):
            label += f" — {info['model']}"
        if key == active:
            default = len(ordered)
            label += "  ← currently active"
        ordered.append((key, label, []))
    ordered.append(("custom", "Custom endpoint (enter URL manually)", []))
    if isinstance(config.get("custom_providers"), list) and config["custom_providers"]:
        ordered.append(("remove-custom", "Remove a saved custom provider", []))
    ordered.extend(
        (
            ("aux-config", "Configure auxiliary models...", []),
            ("cancel", "Leave unchanged", []),
        )
    )
    index = _prompt_provider_choice([label for _, label, _ in ordered], default=default)
    if index is None or ordered[index][0] == "cancel":
        print("No change.")
        return
    selected, label, members = ordered[index]
    if members:
        index = _prompt_provider_choice(
            [labels.get(member, member) for member in members],
            default=members.index(active) if active in members else 0,
            title=f"Select {label.split(' ▸', 1)[0]} provider:",
        )
        if index is None:
            print("No change.")
            return
        selected = members[index]
    if selected == "aux-config":
        return _aux_config_menu()
    if selected == "custom":
        _self()._model_flow_custom(config)
    elif selected.startswith("custom:") or selected in custom_map:
        info = _named_custom_provider_map(load_config()).get(selected)
        if info is None:
            print("The selected custom provider is no longer available. No change.")
            return
        _self()._model_flow_named_custom(config, info)
    elif selected == "remove-custom":
        _remove_custom_provider(config)
    else:
        flows = {
            "openrouter": "openrouter",
            "moa": "moa",
            "ai-gateway": "ai_gateway",
            "nous": "nous",
            "openai-codex": "openai_codex",
            "xai-oauth": "xai_oauth",
            "qwen-oauth": "qwen_oauth",
            "minimax-oauth": "minimax_oauth",
            "copilot-acp": "copilot_acp",
            "copilot": "copilot",
            "anthropic": "anthropic",
            "kimi-coding": "kimi",
            "stepfun": "stepfun",
            "bedrock": "bedrock",
            "vertex": "vertex",
            "azure-foundry": "azure_foundry",
        }
        if selected in flows:
            flow = getattr(_self(), "_model_flow_" + flows[selected])
            kwargs = (
                {"args": args}
                if selected in {"nous", "xai-oauth", "minimax-oauth"}
                else {}
            )
            flow(config, current_model, **kwargs)
        elif selected in {
            "openai-api",
            "gemini",
            "deepseek",
            "xai",
            "zai",
            "kimi-coding-cn",
            "minimax",
            "minimax-cn",
            "kilocode",
            "opencode-zen",
            "opencode-go",
            "alibaba",
            "huggingface",
            "xiaomi",
            "arcee",
            "gmi",
            "nvidia",
            "ollama-cloud",
            "tencent-tokenhub",
            "lmstudio",
        } or _is_profile_api_key_provider(selected):
            _self()._model_flow_api_key_provider(config, selected, current_model)
    if selected not in {
        "custom",
        "cancel",
        "remove-custom",
    } and not selected.startswith("custom:"):
        _clear_stale_openai_base_url()


def _clear_stale_openai_base_url():
    from pcbdraft.model.configuration import get_env_value, load_config, save_env_value

    model = load_config().get("model", {})
    provider = (
        str(model.get("provider") or "").strip().lower()
        if isinstance(model, dict)
        else ""
    )
    if provider and provider != "custom" and get_env_value("OPENAI_BASE_URL"):
        save_env_value("OPENAI_BASE_URL", "")
        print("Cleared stale OPENAI_BASE_URL from .env.")


_AUX_TASKS = [
    ("vision", "Vision", "image/screenshot analysis"),
    ("compression", "Compression", "context summarization"),
    ("web_extract", "Web extract", "web page summarization"),
    ("approval", "Approval", "smart command approval"),
    ("mcp", "MCP", "MCP tool reasoning"),
    ("title_generation", "Title generation", "session titles"),
    ("memory_query_rewrite", "Memory query rewrite", "memory retrieval queries"),
    ("tts_audio_tags", "TTS audio tags", "Gemini TTS tag insertion"),
    ("skills_hub", "Skills hub", "skills search/install"),
    ("triage_specifier", "Triage specifier", "kanban spec fleshing"),
    ("kanban_decomposer", "Kanban decomposer", "task decomposition"),
    ("profile_describer", "Profile describer", "auto profile descriptions"),
    ("curator", "Curator", "skill-usage review pass"),
]


def _all_aux_tasks():
    tasks = list(_AUX_TASKS)
    try:
        from pcbdraft.agent.extensions.manager import get_plugin_auxiliary_tasks

        tasks.extend(
            (entry["key"], entry["display_name"], entry["description"])
            for entry in get_plugin_auxiliary_tasks()
        )
    except Exception as exc:  # noqa: BLE001 — extension isolation boundary
        logger.debug("Plugin auxiliary tasks unavailable (%s)", type(exc).__name__)
    return tasks


def _format_aux_current(task_cfg: dict) -> str:
    if not isinstance(task_cfg, dict):
        return "auto"
    url = str(task_cfg.get("base_url") or "").strip()
    provider = str(task_cfg.get("provider") or "auto").strip() or "auto"
    model = str(task_cfg.get("model") or "").strip()
    if url:
        short = url.replace("https://", "").replace("http://", "").rstrip("/")
        provider = f"custom ({short})"
    return provider + (f" · {model}" if model else "")


def _delegation_cfg_as_task(config):
    value = config.get("delegation")
    value = value if isinstance(value, dict) else {}
    return {
        key: str(value.get(key) or "").strip()
        for key in ("provider", "model", "base_url", "api_key")
    }


def _aux_task_display_name(task):
    return (
        "Delegation"
        if task == "delegation"
        else next((name for key, name, _ in _all_aux_tasks() if key == task), task)
    )


def _save_aux_choice(task, *, provider, model="", base_url="", api_key=""):
    from pcbdraft.model.configuration import load_config, save_config

    config = load_config()
    parent = config
    if task != "delegation":
        if not isinstance(config.get("auxiliary"), dict):
            config["auxiliary"] = {}
        parent = config["auxiliary"]
    if not isinstance(parent.get(task), dict):
        parent[task] = {}
    parent[task].update(
        {
            "provider": "" if task == "delegation" and provider == "auto" else provider,
            "model": model or "",
            "base_url": base_url or "",
            "api_key": api_key or "",
        }
    )
    save_config(config)


def _reset_aux_to_auto() -> int:
    from pcbdraft.model.configuration import load_config, save_config

    config = load_config()
    if not isinstance(config.get("auxiliary"), dict):
        config["auxiliary"] = {}
    count = 0
    for task, _, _ in _all_aux_tasks():
        if not isinstance(config["auxiliary"].get(task), dict):
            config["auxiliary"][task] = {}
        entry = config["auxiliary"][task]
        changed = entry.get("provider") not in {None, "", "auto"}
        if changed:
            entry["provider"] = "auto"
        for field in ("model", "base_url", "api_key"):
            if entry.get(field):
                entry[field] = ""
                changed = True
        count += int(changed)
    delegation = config.get("delegation")
    if isinstance(delegation, dict):
        changed = False
        for field in ("provider", "model", "base_url", "api_key"):
            if delegation.get(field):
                delegation[field] = ""
                changed = True
        count += int(changed)
    save_config(config)
    return count


def _aux_config_menu():
    from pcbdraft.model.configuration import load_config

    while True:
        config = load_config()
        aux = config.get("auxiliary")
        aux = aux if isinstance(aux, dict) else {}
        tasks = _all_aux_tasks() + [("delegation", "Delegation", "subagent model")]
        entries = []
        for key, name, description in tasks:
            value = (
                _delegation_cfg_as_task(config)
                if key == "delegation"
                else aux.get(key, {})
            )
            entries.append(
                (key, f"{name} ({description})  {_format_aux_current(value)}")
            )
        entries.extend((("__reset__", "Reset all to auto"), ("__back__", "Back")))
        index = _prompt_provider_choice([label for _, label in entries])
        if index is None or entries[index][0] == "__back__":
            return
        if entries[index][0] == "__reset__":
            print(f"Reset {_reset_aux_to_auto()} auxiliary task(s) to auto.")
        else:
            _aux_select_for_task(entries[index][0])


def _aux_select_for_task(task):
    from pcbdraft.interfaces.tui.inventory import (
        build_aux_picker_rows,
        format_aux_picker_entries,
    )
    from pcbdraft.model.configuration import load_config

    config = load_config()
    aux = config.get("auxiliary")
    aux = aux if isinstance(aux, dict) else {}
    value = (
        _delegation_cfg_as_task(config) if task == "delegation" else aux.get(task, {})
    )
    value = value if isinstance(value, dict) else {}
    provider = str(value.get("provider") or "auto").strip() or "auto"
    model = str(value.get("model") or "").strip()
    url = str(value.get("base_url") or "").strip()
    try:
        rows = build_aux_picker_rows(
            current_provider=provider, current_model=model, current_base_url=url
        )
    except Exception as exc:  # noqa: BLE001 — optional provider discovery
        print(f"Could not detect authenticated providers: {exc}")
        rows = []
    entries = [("__auto__", "auto (inherit main model)", [])]
    entries.extend(
        format_aux_picker_entries(rows, current_provider=provider, current_base_url=url)
    )
    entries.extend(
        (("__custom__", "Custom endpoint (direct URL)", []), ("__back__", "Back", []))
    )
    index = _prompt_provider_choice([label for _, label, _ in entries])
    if index is None or entries[index][0] == "__back__":
        return
    slug, _, models = entries[index]
    if slug == "__auto__":
        _save_aux_choice(task, provider="auto")
    elif slug == "__custom__":
        _aux_flow_custom_endpoint(task, value)
    else:
        _aux_flow_provider_model(task, slug, models, model)


def _aux_flow_provider_model(task, provider_slug, curated_models, current_model=""):
    from pcbdraft.model.auth import _prompt_model_selection
    from pcbdraft.model.catalog import get_pricing_for_provider

    try:
        pricing = get_pricing_for_provider(provider_slug) or {}
    except Exception as exc:  # noqa: BLE001 — pricing cannot block model selection
        logger.debug("Provider pricing unavailable (%s)", type(exc).__name__)
        pricing = {}
    if curated_models:
        selected = _prompt_model_selection(
            list(curated_models),
            current_model=current_model,
            pricing=pricing,
            confirm_provider=provider_slug,
        )
    else:
        try:
            selected = input("Model (blank = provider default): ").strip()
        except (EOFError, KeyboardInterrupt):
            return
    if selected is not None:
        _save_aux_choice(task, provider=provider_slug, model=selected)


def _aux_flow_custom_endpoint(task, task_cfg):
    from pcbdraft.interfaces.tui.secret_prompt import masked_secret_prompt

    current_url = str(task_cfg.get("base_url") or "").strip()
    current_model = str(task_cfg.get("model") or "").strip()
    try:
        url = input(f"Base URL [{current_url}]: ").strip() or current_url
        if not url:
            return
        model = input(f"Model [{current_model}]: ").strip() or current_model
        key = masked_secret_prompt("API key (blank = OPENAI_API_KEY): ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    _save_aux_choice(task, provider="custom", model=model, base_url=url, api_key=key)


def _prompt_provider_choice(choices, *, default=0, title="Select provider:"):
    try:
        from pcbdraft.interfaces.tui.setup import _curses_prompt_choice

        index = _curses_prompt_choice(title, choices, default)
        if index >= 0:
            print()
            return index
    except Exception as exc:  # noqa: BLE001 — curses fallback across platforms
        logger.debug("Curses picker unavailable (%s)", type(exc).__name__)
    print(title)
    for index, choice in enumerate(choices, 1):
        print(f"  {'→' if index - 1 == default else ' '} {index}. {choice}")
    while True:
        try:
            value = input(f"Choice [1-{len(choices)}] ({default + 1}): ").strip()
            if not value:
                return default
            index = int(value) - 1
            if 0 <= index < len(choices):
                return index
            print(f"Please enter 1-{len(choices)}")
        except ValueError:
            print("Please enter a number")
        except (EOFError, KeyboardInterrupt):
            return None


_DEFAULT_QWEN_PORTAL_MODELS = ["qwen3-coder-plus", "qwen3-coder"]


def _prompt_custom_api_mode_selection(
    base_url: str, current_api_mode: str = ""
) -> str | None:
    from pcbdraft.model.runtime_provider import _detect_api_mode_for_url

    default = (
        str(current_api_mode or "").strip().lower()
        or _detect_api_mode_for_url(base_url)
        or ""
    )
    print(
        "1. Auto-detect\n2. Chat Completions\n3. Responses / Codex\n4. Anthropic Messages"
    )
    raw = input("API mode [Enter to keep current/detected]: ").strip().lower()
    if not raw:
        return default or None
    choices = {
        "chat_completions": {"2", "chat", "chat_completions", "completions"},
        "codex_responses": {"3", "responses", "codex", "codex_responses"},
        "anthropic_messages": {"4", "anthropic", "anthropic_messages", "messages"},
    }
    return next((mode for mode, aliases in choices.items() if raw in aliases), None)


def _auto_provider_name(base_url: str) -> str:
    clean = base_url.replace("https://", "").replace("http://", "").rstrip("/")
    name = re.sub(r"/v1/?$", "", clean).split("/")[0]
    if "localhost" in name or "127.0.0.1" in name:
        return f"Local ({name})"
    if "runpod" in name.lower():
        return f"RunPod ({name})"
    return name.capitalize()


def _custom_provider_api_key_config_value(provider_info, resolved_api_key=""):
    reference = str(provider_info.get("api_key_ref") or "").strip()
    if reference:
        return reference
    key_env = str(provider_info.get("key_env") or "").strip()
    if key_env and not str(provider_info.get("api_key") or "").strip():
        return f"${{{key_env}}}"
    return str(resolved_api_key or "").strip()


def _custom_provider_base_url_config_value(provider_info, resolved_base_url=""):
    return str(provider_info.get("base_url_ref") or resolved_base_url or "").strip()


def _save_custom_provider(
    base_url,
    api_key="",
    model="",
    context_length=None,
    name=None,
    api_mode=None,
    key_env="",
):
    from pcbdraft.model.configuration import load_config, save_config

    config = load_config()
    providers = config.get("custom_providers")
    providers = providers if isinstance(providers, list) else []
    for entry in providers:
        if not isinstance(entry, dict) or entry.get("base_url", "").rstrip(
            "/"
        ) != base_url.rstrip("/"):
            continue
        before = repr(entry)
        if model:
            entry["model"] = model
        if model and context_length:
            models = entry.get("models")
            models = models if isinstance(models, dict) else {}
            models[model] = {"context_length": context_length}
            entry["models"] = models
        if api_mode:
            entry["api_mode"] = api_mode
        else:
            entry.pop("api_mode", None)
        if key_env:
            entry["key_env"] = key_env
            entry.pop("api_key", None)
        if repr(entry) != before:
            config["custom_providers"] = providers
            save_config(config)
        return
    entry = {"name": name or _auto_provider_name(base_url), "base_url": base_url}
    if key_env:
        entry["key_env"] = key_env
    elif api_key:
        entry["api_key"] = api_key
    if model:
        entry["model"] = model
    if api_mode:
        entry["api_mode"] = api_mode
    if model and context_length:
        entry["models"] = {model: {"context_length": context_length}}
    providers.append(entry)
    config["custom_providers"] = providers
    save_config(config)
    print(f"Saved custom provider {entry['name']} in config.yaml.")


def _remove_custom_provider(config):
    from pcbdraft.model.configuration import load_config, save_config

    config = load_config()
    providers = config.get("custom_providers")
    if not isinstance(providers, list) or not providers:
        print("No custom providers configured.")
        return
    labels = [
        str(item.get("name", "unnamed")) if isinstance(item, dict) else str(item)
        for item in providers
    ] + ["Cancel"]
    index = _prompt_provider_choice(
        labels, default=len(providers), title="Select provider to remove:"
    )
    if index is None or index >= len(providers):
        print("No change.")
        return
    providers.pop(index)
    config["custom_providers"] = providers
    save_config(config)


def _current_reasoning_effort(config) -> str:
    agent = config.get("agent")
    return (
        str(agent.get("reasoning_effort") or "").strip().lower()
        if isinstance(agent, dict)
        else ""
    )


def _set_reasoning_effort(config, effort: str) -> None:
    if not isinstance(config.get("agent"), dict):
        config["agent"] = {}
    config["agent"]["reasoning_effort"] = effort


def _prompt_reasoning_effort_selection(efforts, current_effort=""):
    import subprocess

    deduped = list(
        dict.fromkeys(
            str(effort).strip().lower() for effort in efforts if str(effort).strip()
        )
    )
    canonical = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
    ordered = [effort for effort in canonical if effort in deduped]
    ordered.extend(effort for effort in deduped if effort not in canonical)
    if not ordered:
        return None
    choices = [
        f"{effort}  ← currently in use" if effort == current_effort else effort
        for effort in ordered
    ] + ["Disable reasoning", "Skip (keep current)"]
    if current_effort == "none":
        default = len(ordered)
    elif current_effort in ordered:
        default = ordered.index(current_effort)
    elif "medium" in ordered:
        default = ordered.index("medium")
    else:
        default = 0
    try:
        from pcbdraft.interfaces.tui.curses_ui import curses_radiolist

        index = curses_radiolist(
            "Select reasoning effort:", choices, selected=default, cancel_returns=-1
        )
        if index < 0 or index == len(ordered) + 1:
            return None
        return "none" if index == len(ordered) else ordered[index]
    except (ImportError, NotImplementedError, OSError, subprocess.SubprocessError):
        logger.debug("Reasoning picker is using numbered input")
    for index, label in enumerate(choices, 1):
        print(f"  {index}. {label}")
    while True:
        try:
            raw = input(f"Choice [1-{len(choices)}] (default: keep current): ").strip()
            if not raw:
                return None
            index = int(raw) - 1
            if index == len(ordered) + 1:
                return None
            if index == len(ordered):
                return "none"
            if 0 <= index < len(ordered):
                return ordered[index]
            print(f"Please enter 1-{len(choices)}")
        except ValueError:
            print("Please enter a number")
        except (EOFError, KeyboardInterrupt):
            return None


def _prompt_api_key(
    pconfig, existing_key: str, provider_id: str = "", existing_source: str = ""
) -> tuple:
    from pcbdraft.interfaces.tui.secret_prompt import masked_secret_prompt
    from pcbdraft.model.auth import LMSTUDIO_NOAUTH_PLACEHOLDER
    from pcbdraft.model.configuration import save_env_value

    key_env = pconfig.api_key_env_vars[0] if pconfig.api_key_env_vars else ""

    def new_key(*, allow_default):
        noauth = provider_id == "lmstudio" and allow_default
        prompt = (
            f"{key_env} (Enter for no-auth default): "
            if noauth
            else f"{key_env} (Enter to cancel): "
        )
        try:
            value = masked_secret_prompt(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return ""
        return value or (LMSTUDIO_NOAUTH_PLACEHOLDER if noauth else "")

    if not existing_key:
        if not key_env:
            return "", True
        value = new_key(allow_default=True)
        if not value:
            return "", True
        save_env_value(key_env, value)
        print("API key saved.")
        return value, False
    print(f"  {pconfig.name} API key is configured.")
    if not key_env:
        return existing_key, False
    pool_backed = existing_source.startswith("credential_pool:")
    try:
        choice = (
            input(
                "[K]eep / [R]eplace (default K): "
                if pool_backed
                else "[K]eep / [R]eplace / [C]lear (default K): "
            )
            .strip()
            .lower()
        )
    except (EOFError, KeyboardInterrupt):
        choice = "k"
    if choice.startswith("r"):
        value = new_key(allow_default=False)
        if value:
            save_env_value(key_env, value)
            return value, False
    if choice.startswith("c") and not pool_backed:
        save_env_value(key_env, "")
        print("API key cleared. Run `pcbdraft connect` to configure it again.")
        return "", True
    return existing_key, False


def _infer_stepfun_region(base_url: str) -> str:
    return (
        "china"
        if "api.stepfun.com" in (base_url or "").strip().lower()
        else "international"
    )


def _stepfun_base_url_for_region(region: str) -> str:
    from pcbdraft.model.auth import (
        STEPFUN_STEP_PLAN_CN_BASE_URL,
        STEPFUN_STEP_PLAN_INTL_BASE_URL,
    )

    return (
        STEPFUN_STEP_PLAN_CN_BASE_URL
        if region == "china"
        else STEPFUN_STEP_PLAN_INTL_BASE_URL
    )


def _run_anthropic_oauth_flow(save_env_value):
    from pcbdraft.model.anthropic_adapter import (
        run_pcbdraft_oauth_login_pure,
        save_pcbdraft_oauth_credentials,
    )

    credentials = run_pcbdraft_oauth_login_pure()
    if not credentials:
        print("Anthropic OAuth login cancelled or did not return credentials.")
        return False
    save_pcbdraft_oauth_credentials(credentials)
    print("PCBDraft OAuth credentials saved.")
    return True


# Explicit compatibility failures for retired command handlers. No old parser,
# source updater, platform bridge, PID cleanup or installation implementation
# remains reachable through these names.
cmd_gateway = unsupported_lifecycle
cmd_gateway_enroll = unsupported_lifecycle
cmd_proxy = unsupported_lifecycle
cmd_whatsapp = unsupported_lifecycle
cmd_whatsapp_cloud = unsupported_lifecycle
cmd_update = unsupported_lifecycle
cmd_uninstall = unsupported_lifecycle
cmd_gui = unsupported_lifecycle
cmd_profile = unsupported_lifecycle
cmd_sessions = unsupported_lifecycle
_recover_from_interrupted_install = unsupported_lifecycle
_run_install_with_heartbeat = unsupported_lifecycle
_quarantine_running_pcbdraft_exe = unsupported_lifecycle
_cleanup_quarantined_exes = unsupported_lifecycle
_detect_venv_python_processes = unsupported_lifecycle
_session_browse_picker = unsupported_lifecycle


def main(argv=None):
    from pcbdraft.interfaces.cli import main as public_main

    return public_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
