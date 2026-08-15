"""User-facing provider catalog and private model configuration.

This module is intentionally independent from the terminal UI.  The TUI only
collects a provider, API key, and model; the application can then resolve the
same configuration without knowing which screen wrote it.
"""

from __future__ import annotations

import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import atomic_write_text, make_directory, read_bytes_limited
from pcbdraft.model.api import (
    validate_provider_base_url,
    validate_provider_credential,
    validate_provider_model_id,
)

CONFIG_LIMIT = 128 * 1024
_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_MAX_MODELS = 128


@dataclass(frozen=True)
class ProviderPreset:
    """A small curated starting point; users can override every value."""

    id: str
    name: str
    base_url: str
    models: tuple[str, ...]
    docs_url: str
    hint: str

    @property
    def default_model(self) -> str:
        return self.models[0]


# Keep this catalog deliberately small and editable.  It is a convenience list,
# not a claim that these providers expose only these models.  The custom entry
# lets users use any OpenAI-compatible service without waiting for a catalog update.
PROVIDER_PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        "deepseek",
        "DeepSeek",
        "https://api.deepseek.com",
        ("deepseek-v4-pro", "deepseek-v4-flash"),
        "https://platform.deepseek.com/",
        "适合复杂电路规划与约束分析",
    ),
    ProviderPreset(
        "minimax",
        "MiniMax",
        "https://api.minimaxi.com/v1",
        ("MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M2.5"),
        "https://platform.minimaxi.com/",
        "OpenAI 兼容接口，支持国内节点",
    ),
    ProviderPreset(
        "kimi",
        "Kimi / Moonshot",
        "https://api.moonshot.ai/v1",
        ("kimi-k2.6", "kimi-k2.5"),
        "https://platform.moonshot.ai/",
        "适合长上下文与中文需求",
    ),
    ProviderPreset(
        "openai",
        "OpenAI",
        "https://api.openai.com/v1",
        ("gpt-5", "gpt-5-mini"),
        "https://platform.openai.com/",
        "通用 OpenAI 兼容接口",
    ),
    ProviderPreset(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        ("deepseek/deepseek-v4-pro", "moonshotai/kimi-k2.6"),
        "https://openrouter.ai/",
        "一个 API Key 访问多家模型",
    ),
    ProviderPreset(
        "ollama",
        "Ollama（本地）",
        "http://127.0.0.1:11434/v1",
        ("qwen3:8b", "deepseek-r1:8b"),
        "https://ollama.com/",
        "本地运行；API Key 可填写任意非空值",
    ),
)

_PRESETS = {preset.id: preset for preset in PROVIDER_PRESETS}


def provider_config_path() -> Path:
    """Return the PCBDraft-owned TOML configuration path."""

    from pcbdraft.model.api import provider_config_path as _legacy_path

    return _legacy_path()


@dataclass(frozen=True)
class ProviderConnection:
    id: str
    name: str
    base_url: str
    models: tuple[str, ...]
    api_key: str = field(repr=False)
    docs_url: str | None = None
    source: str = "config"

    def validated(self) -> ProviderConnection:
        if not _ID.fullmatch(self.id):
            raise ValidationError(
                "provider id must use lowercase letters, digits, _ or -"
            )
        if not self.name.strip() or len(self.name) > 120:
            raise ValidationError("provider name is invalid")
        validate_provider_base_url(self.base_url)
        validate_provider_credential(self.api_key)
        if not self.models or len(self.models) > _MAX_MODELS:
            raise ValidationError("provider must have between 1 and 128 models")
        clean_models = tuple(model.strip() for model in self.models)
        for model in clean_models:
            validate_provider_model_id(model)
        if len(set(clean_models)) != len(clean_models):
            raise ValidationError("provider model ids must be unique")
        return ProviderConnection(
            id=self.id,
            name=" ".join(self.name.split()),
            base_url=self.base_url.rstrip("/"),
            models=clean_models,
            api_key=self.api_key,
            docs_url=self.docs_url,
            source=self.source,
        )


@dataclass(frozen=True)
class ModelChoice:
    provider_id: str
    provider_name: str
    model: str
    active: bool = False

    @property
    def label(self) -> str:
        return f"{self.provider_name} / {self.model}"


@dataclass(frozen=True)
class ModelConfig:
    active_provider: str | None
    active_model: str | None
    providers: tuple[ProviderConnection, ...]
    path: Path

    @property
    def active(self) -> ProviderConnection | None:
        for provider in self.providers:
            if provider.id == self.active_provider:
                return provider
        return None

    def choices(self, query: str = "") -> tuple[ModelChoice, ...]:
        needle = query.strip().casefold()
        choices = [
            ModelChoice(
                provider.id,
                provider.name,
                model,
                provider.id == self.active_provider and model == self.active_model,
            )
            for provider in self.providers
            for model in provider.models
        ]
        if needle:
            choices = [
                choice
                for choice in choices
                if needle in choice.provider_name.casefold()
                or needle in choice.provider_id.casefold()
                or needle in choice.model.casefold()
            ]
        return tuple(choices)


def preset(provider_id: str) -> ProviderPreset | None:
    return _PRESETS.get(provider_id)


def provider_presets() -> tuple[ProviderPreset, ...]:
    return PROVIDER_PRESETS


def _toml_string(value: str) -> str:
    # JSON basic strings and TOML basic strings share the escaping used here.
    import json

    return json.dumps(value, ensure_ascii=False)


def _render_config(
    *,
    active_provider: str | None,
    active_model: str | None,
    providers: tuple[ProviderConnection, ...],
) -> str:
    lines = ["version = 1"]
    if active_provider is not None:
        lines.append(f"active_provider = {_toml_string(active_provider)}")
    if active_model is not None:
        lines.append(f"active_model = {_toml_string(active_model)}")
    lines.append("")
    for provider in sorted(providers, key=lambda item: item.id):
        lines.extend(
            [
                f"[providers.{provider.id}]",
                f"name = {_toml_string(provider.name)}",
                f"base_url = {_toml_string(provider.base_url)}",
                f"api_key = {_toml_string(provider.api_key)}",
                "models = ["
                + ", ".join(_toml_string(model) for model in provider.models)
                + "]",
            ]
        )
        if provider.docs_url:
            lines.append(f"docs_url = {_toml_string(provider.docs_url)}")
        lines.append("")
    return "\n".join(lines)


def _validate_config_mode(path: Path, *, contains_key: bool) -> None:
    if not path.exists():
        return
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"model config must be a regular file: {path}")
    if contains_key:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise ValidationError(
                "model config contains an API key and must be chmod 600"
            )


def load_model_config(path: str | Path | None = None) -> ModelConfig:
    target = Path(path).expanduser() if path is not None else provider_config_path()
    if not target.exists():
        return ModelConfig(None, None, (), target)
    try:
        payload = tomllib.loads(
            read_bytes_limited(target, CONFIG_LIMIT).decode("utf-8")
        )
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValidationError(f"model config is invalid: {target}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("model config must be a TOML table")
    version = payload.get("version", 1)
    if version != 1:
        raise ValidationError("unsupported PCBDraft model config version")
    raw_providers = payload.get("providers", {})
    if not isinstance(raw_providers, dict):
        raise ValidationError("model config providers must be a table")
    connections: list[ProviderConnection] = []
    for provider_id, raw in raw_providers.items():
        if not isinstance(provider_id, str) or not isinstance(raw, dict):
            raise ValidationError("model config provider entry is invalid")
        allowed = {"name", "base_url", "api_key", "models", "docs_url"}
        if set(raw) - allowed:
            raise ValidationError(f"unknown fields in providers.{provider_id}")
        values = {key: raw.get(key) for key in allowed}
        if not isinstance(values["name"], str) or not isinstance(
            values["base_url"], str
        ):
            raise ValidationError(f"providers.{provider_id} requires name and base_url")
        if not isinstance(values["api_key"], str):
            raise ValidationError(f"providers.{provider_id} requires api_key")
        models = values["models"]
        if not isinstance(models, list) or not all(
            isinstance(model, str) for model in models
        ):
            raise ValidationError(f"providers.{provider_id}.models is invalid")
        docs_url = values["docs_url"] if isinstance(values["docs_url"], str) else None
        connections.append(
            ProviderConnection(
                id=provider_id,
                name=values["name"],
                base_url=values["base_url"],
                api_key=values["api_key"],
                models=tuple(models),
                docs_url=docs_url,
                source=str(target),
            ).validated()
        )
    active_provider = payload.get("active_provider")
    active_model = payload.get("active_model")
    if active_provider is not None and not isinstance(active_provider, str):
        raise ValidationError("active_provider is invalid")
    if active_model is not None and not isinstance(active_model, str):
        raise ValidationError("active_model is invalid")
    result = ModelConfig(
        active_provider,
        active_model,
        tuple(sorted(connections, key=lambda item: item.id)),
        target,
    )
    if result.active_provider is not None:
        active = result.active
        if active is None or result.active_model not in active.models:
            raise ValidationError("active model is not connected")
    _validate_config_mode(target, contains_key=bool(connections))
    return result


def save_model_config(config: ModelConfig) -> ModelConfig:
    providers = tuple(provider.validated() for provider in config.providers)
    active = None
    active_model = config.active_model
    if config.active_provider is not None:
        active = next(
            (
                provider
                for provider in providers
                if provider.id == config.active_provider
            ),
            None,
        )
        if active is None:
            raise ValidationError("active provider is not connected")
        if active_model not in active.models:
            raise ValidationError("active model is not connected")
    target = config.path
    make_directory(target.parent)
    rendered = _render_config(
        active_provider=config.active_provider,
        active_model=active_model,
        providers=providers,
    )
    atomic_write_text(target, rendered, mode=0o600)
    return ModelConfig(
        config.active_provider,
        active_model,
        tuple(sorted(providers, key=lambda item: item.id)),
        target,
    )


def connect_provider(
    provider_id: str,
    *,
    api_key: str,
    base_url: str | None = None,
    model: str | None = None,
    name: str | None = None,
    path: str | Path | None = None,
) -> ModelConfig:
    clean_id = provider_id.strip().casefold()
    selected = preset(clean_id)
    if selected is not None:
        provider_name = selected.name
        endpoint = (
            base_url.strip() if base_url and base_url.strip() else selected.base_url
        )
        models = list(selected.models)
        docs_url = selected.docs_url
        chosen = model.strip() if model and model.strip() else selected.default_model
    else:
        provider_name = (name or clean_id or "自定义服务").strip()
        endpoint = (base_url or "").strip()
        if not model or not model.strip():
            raise ValidationError("custom provider requires a model")
        models = [model.strip()]
        chosen = models[0]
        docs_url = None
    if chosen not in models:
        models.insert(0, chosen)
    target = Path(path).expanduser() if path is not None else provider_config_path()
    existing = load_model_config(target)
    connection = ProviderConnection(
        id=clean_id,
        name=provider_name,
        base_url=endpoint,
        models=tuple(dict.fromkeys(models)),
        api_key=api_key,
        docs_url=docs_url,
        source=str(target),
    ).validated()
    merged = tuple(
        provider for provider in existing.providers if provider.id != clean_id
    ) + (connection,)
    return save_model_config(
        ModelConfig(
            clean_id, chosen, tuple(sorted(merged, key=lambda item: item.id)), target
        )
    )


def select_model(
    provider_id: str, model: str, *, path: str | Path | None = None
) -> ModelConfig:
    target = Path(path).expanduser() if path is not None else provider_config_path()
    config = load_model_config(target)
    provider = next((item for item in config.providers if item.id == provider_id), None)
    if provider is None or model not in provider.models:
        raise ValidationError("model is not connected")
    return save_model_config(ModelConfig(provider_id, model, config.providers, target))


def configured_connection(path: str | Path | None = None) -> ProviderConnection | None:
    return load_model_config(path).active
