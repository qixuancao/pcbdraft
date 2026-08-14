"""Configured structured-model API used by every PCBDraft model-assisted feature."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import PCBDraftError, ValidationError
from .io import atomic_write_json

CONFIG_LIMIT = 64 * 1024
MAX_MODEL_RESPONSE_BYTES = 1_048_576
MAX_MODEL_PROMPT_BYTES = 2 * 1024 * 1024


def provider_config_path() -> Path:
    """Return the user-owned provider configuration path."""

    explicit = os.environ.get("PCBDRAFT_CONFIG", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return root / "pcbdraft" / "config.toml"


@dataclass(frozen=True)
class OpenAICompatibleSettings:
    """One user-configured Chat Completions endpoint."""

    base_url: str
    model: str
    api_key: str | None = field(default=None, repr=False)
    api_key_env: str | None = None
    source: str = "explicit"
    provider_id: str = "openai-compatible"
    provider_name: str = "OpenAI-compatible"

    @classmethod
    def from_config(cls) -> OpenAICompatibleSettings | None:
        # The catalog owns the multi-provider format used by /connect and
        # /models.  Keeping this adapter here lets the model transport stay
        # unaware of TUI and TOML details.
        from .model_config import load_model_config

        config = load_model_config()
        connection = config.active
        if connection is None:
            return None
        return cls(
            base_url=connection.base_url,
            model=config.active_model or connection.models[0],
            api_key=connection.api_key,
            source=connection.source,
            provider_id=connection.id,
            provider_name=connection.name,
        ).validated()

    def validated(self) -> OpenAICompatibleSettings:
        parsed = urllib.parse.urlsplit(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValidationError("provider.base_url is invalid")
        if not self.model or len(self.model) > 200:
            raise ValidationError("provider.model is invalid")
        if self.api_key_env is not None and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]{0,127}", self.api_key_env
        ):
            raise ValidationError("provider.api_key_env is invalid")
        if self.api_key is not None and not self.api_key:
            raise ValidationError("provider.api_key is empty")
        return self

    def credential(self) -> str:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            value = os.environ.get(self.api_key_env, "")
            if value:
                return value
            raise PCBDraftError(
                f"provider credential is absent from environment: {self.api_key_env}"
            )
        raise PCBDraftError("provider credential is not configured")

    def diagnostic(self) -> dict[str, Any]:
        secret_present = bool(self.api_key) or bool(
            self.api_key_env and os.environ.get(self.api_key_env)
        )
        return {
            "id": self.provider_id,
            "name": self.provider_name,
            "available": secret_present,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "secret_present": secret_present,
            "secret_storage": (
                "private PCBDraft config"
                if self.api_key
                else "runtime environment named by config"
            ),
            "config": self.source,
            "planning": "structured circuit plan over installed KiCad symbols",
        }


class StructuredModelClient:
    """Small strict JSON-Schema client for the configured model API."""

    def __init__(self, settings: OpenAICompatibleSettings) -> None:
        self.settings = settings.validated()
        self.endpoint = self.settings.base_url.rstrip("/") + "/chat/completions"

    def request(
        self,
        *,
        prompt: str,
        schema_name: str,
        schema: dict[str, Any],
        timeout: float,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if timeout <= 0 or timeout > 1800:
            raise ValidationError("model timeout must be in (0, 1800] seconds")
        prompt_bytes = prompt.encode("utf-8")
        if len(prompt_bytes) > MAX_MODEL_PROMPT_BYTES:
            raise ValidationError("model prompt exceeds 2 MiB")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,79}", schema_name):
            raise ValidationError("model output schema name is invalid")
        body = json.dumps(
            {
                "model": self.settings.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 6000,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "strict": True,
                        "schema": schema,
                    },
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.settings.credential()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "PCBDraft",
            },
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read(MAX_MODEL_RESPONSE_BYTES + 1)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            raise PCBDraftError(
                f"configured model request failed: {type(exc).__name__}"
            ) from exc
        duration = time.monotonic() - started
        if len(payload) > MAX_MODEL_RESPONSE_BYTES:
            raise PCBDraftError("model response exceeded the 1 MiB limit")
        try:
            envelope = json.loads(payload)
            content = envelope["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError
            value = json.loads(content)
            if not isinstance(value, dict):
                raise TypeError
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ValidationError(
                "model returned an invalid JSON response envelope"
            ) from exc
        receipt = {
            "completed": True,
            "schema_valid": True,
            "provider": "openai-compatible",
            "model": self.settings.model,
            "config": self.settings.source,
            "duration_seconds": round(duration, 6),
            "prompt_transport": "https-body",
            "prompt_persisted": False,
            "credential_persisted": False,
            "output_schema": schema_name,
        }
        return value, receipt


def configured_model_client() -> StructuredModelClient:
    settings = OpenAICompatibleSettings.from_config()
    if settings is None:
        raise PCBDraftError(
            f"model provider is not configured; create {provider_config_path()}"
        )
    return StructuredModelClient(settings)


def invoke_structured_model(
    *,
    run_dir: Path,
    prompt: str,
    schema_name: str,
    schema: dict[str, Any],
    timeout: float,
    artifact_prefix: str,
    settings: OpenAICompatibleSettings | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Invoke the configured API and retain only schema, output, and safe metadata."""

    if not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", artifact_prefix):
        raise ValidationError("model artifact prefix is invalid")
    schema_path = run_dir / f"{artifact_prefix}.schema.json"
    final_path = run_dir / f"{artifact_prefix}.final.json"
    receipt_path = run_dir / f"{artifact_prefix}.receipt.json"
    atomic_write_json(schema_path, schema)
    try:
        client = (
            StructuredModelClient(settings)
            if settings is not None
            else configured_model_client()
        )
        value, receipt = client.request(
            prompt=prompt,
            schema_name=schema_name,
            schema=schema,
            timeout=timeout,
        )
    except Exception as exc:
        atomic_write_json(
            receipt_path,
            {
                "completed": False,
                "schema_valid": False,
                "failure": type(exc).__name__,
                "prompt_persisted": False,
                "credential_persisted": False,
            },
        )
        raise
    atomic_write_json(final_path, value)
    receipt["schema_artifact"] = schema_path.name
    receipt["output_artifact"] = final_path.name
    atomic_write_json(receipt_path, receipt)
    return value, receipt
