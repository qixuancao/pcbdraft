"""Configured structured-model API used by every PCBDraft model-assisted feature."""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError

from pcbdraft import __version__
from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import atomic_write_json
from pcbdraft.model.profiles import provider_wire_profile
from pcbdraft.model.retry import jittered_backoff, parse_retry_after_seconds

CONFIG_LIMIT = 64 * 1024
MAX_MODEL_RESPONSE_BYTES = 1_048_576
MAX_MODEL_PROMPT_BYTES = 2 * 1024 * 1024
MAX_MODEL_SCHEMA_BYTES = 512 * 1024
MAX_MODEL_REQUEST_BYTES = 3 * 1024 * 1024
MAX_MODEL_ATTEMPTS = 3
MAX_RETRY_AFTER_SECONDS = 300.0


def validate_provider_base_url(value: str) -> urllib.parse.SplitResult:
    """Validate a provider URL without permitting plaintext remote credentials."""

    if "\\" in value or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise ValidationError("provider base URL is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        # Accessing port performs urllib's numeric/range validation.
        _ = parsed.port
    except ValueError as exc:
        raise ValidationError("provider base URL is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError("provider base URL is invalid")
    if parsed.scheme == "http" and not _is_literal_loopback(parsed.hostname):
        raise ValidationError(
            "provider base URL must use HTTPS; HTTP is allowed only for a "
            "literal loopback host"
        )
    return parsed


def validate_provider_model_id(value: str) -> str:
    """Validate a bounded model identifier before placing it in a request."""

    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("provider model id is invalid") from exc
    if (
        not value
        or len(encoded) > 256
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ValidationError("provider model id is invalid")
    return value


def validate_provider_credential(value: str) -> str:
    """Require a visible ASCII Bearer value that cannot split HTTP headers."""

    try:
        encoded = value.encode("ascii")
    except UnicodeError as exc:
        raise ValidationError("provider credential is invalid") from exc
    if (
        not encoded
        or len(encoded) > 16 * 1024
        or any(byte < 0x21 or byte > 0x7E for byte in encoded)
    ):
        raise ValidationError("provider credential is invalid")
    return value


def _is_literal_loopback(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward provider credentials to a redirected destination."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class ModelTransportError(PCBDraftError):
    """Sanitized provider failure with machine-readable retry metadata."""

    def __init__(
        self,
        category: str,
        *,
        attempts: int,
        retryable: bool,
        status: int | None = None,
    ) -> None:
        detail = f"HTTP {status}, " if status is not None else ""
        super().__init__(
            f"configured model request failed: {category} "
            f"({detail}{attempts} attempt{'s' if attempts != 1 else ''})"
        )
        self.category = category
        self.attempts = attempts
        self.retryable = retryable
        self.status = status

    def receipt(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "failure_category": self.category,
            "attempts": self.attempts,
            "retryable": self.retryable,
        }
        if self.status is not None:
            value["http_status"] = self.status
        return value


def _http_transport_error(status: int, attempts: int) -> ModelTransportError:
    retryable = status in {408, 409, 425, 429} or 500 <= status <= 599
    if status in {401, 403}:
        category = "authentication"
    elif status == 402:
        category = "billing"
    elif status == 404:
        category = "endpoint_or_model_not_found"
    elif status == 408:
        category = "timeout"
    elif status == 413:
        category = "request_too_large"
    elif status == 429:
        category = "rate_limit"
    elif status in {409, 425} or 500 <= status <= 599:
        category = "provider_unavailable"
    elif 300 <= status <= 399:
        category = "redirect_rejected"
    else:
        category = "request_rejected"
    return ModelTransportError(
        category, attempts=attempts, retryable=retryable, status=status
    )


def _network_transport_error(exc: BaseException, attempts: int) -> ModelTransportError:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLError):
        return ModelTransportError(
            "tls_verification", attempts=attempts, retryable=False
        )
    if isinstance(reason, TimeoutError):
        return ModelTransportError("timeout", attempts=attempts, retryable=True)
    return ModelTransportError("network", attempts=attempts, retryable=True)


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _strict_json_loads(value: str | bytes) -> Any:
    return json.loads(
        value,
        parse_constant=_reject_non_json_constant,
        object_pairs_hook=_unique_json_object,
    )


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
        from pcbdraft.model.config import load_model_config

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
        validate_provider_base_url(self.base_url)
        validate_provider_model_id(self.model)
        if self.api_key_env is not None and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]{0,127}", self.api_key_env
        ):
            raise ValidationError("provider.api_key_env is invalid")
        if self.api_key is not None:
            validate_provider_credential(self.api_key)
        return self

    def credential(self) -> str:
        if self.api_key:
            return validate_provider_credential(self.api_key)
        if self.api_key_env:
            value = os.environ.get(self.api_key_env, "")
            if value:
                return validate_provider_credential(value)
            raise PCBDraftError(
                f"provider credential is absent from environment: {self.api_key_env}"
            )
        raise PCBDraftError("provider credential is not configured")

    def diagnostic(self) -> dict[str, Any]:
        secret_present = bool(self.api_key) or bool(
            self.api_key_env and os.environ.get(self.api_key_env)
        )
        profile = provider_wire_profile(self.provider_id, self.base_url)
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
            "provider_protocol": "openai-chat-completions",
            "structured_output": profile.output_mode,
            "planning": "structured circuit plan over installed KiCad symbols",
        }


class StructuredModelClient:
    """Small strict JSON-Schema client for the configured model API."""

    def __init__(self, settings: OpenAICompatibleSettings) -> None:
        self.settings = settings.validated()
        self.endpoint = self.settings.base_url.rstrip("/") + "/chat/completions"
        self.profile = provider_wire_profile(
            self.settings.provider_id, self.settings.base_url
        )
        self._opener = urllib.request.build_opener(_NoRedirectHandler())
        self._sleep = time.sleep

    @staticmethod
    def _schema_json(schema: dict[str, Any]) -> str:
        try:
            value = json.dumps(
                schema,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            encoded = value.encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise ValidationError("model output schema is not strict JSON") from exc
        if len(encoded) > MAX_MODEL_SCHEMA_BYTES:
            raise ValidationError("model output schema exceeds 512 KiB")
        return value

    def _request_body(
        self,
        *,
        prompt: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> bytes:
        schema_json = self._schema_json(schema)
        output_mode = self.profile.output_mode
        if output_mode == "json_schema":
            output_contract = (
                "\n\nReturn only one JSON object that satisfies the supplied JSON "
                "Schema. Do not wrap it in Markdown or add prose."
            )
        else:
            output_contract = (
                "\n\nReturn only one JSON object, without Markdown or prose, that "
                "satisfies this JSON Schema: " + schema_json
            )
        document: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [{"role": "user", "content": prompt + output_contract}],
            self.profile.max_tokens_field: self.profile.max_output_tokens,
            "stream": False,
        }
        if self.profile.temperature is not None:
            document["temperature"] = self.profile.temperature
        if output_mode == "json_schema":
            document["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            }
        elif output_mode == "json_object":
            document["response_format"] = {"type": "json_object"}
        elif output_mode != "prompt":
            raise PCBDraftError("configured provider output mode is unsupported")
        if self.profile.require_supported_parameters:
            document["provider"] = {"require_parameters": True}
        if self.profile.separate_reasoning:
            document["reasoning_split"] = True
        try:
            body = json.dumps(
                document, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise ValidationError("model request is not strict JSON") from exc
        if len(body) > MAX_MODEL_REQUEST_BYTES:
            raise ValidationError("model request exceeds 3 MiB")
        return body

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, MAX_RETRY_AFTER_SECONDS)
        return jittered_backoff(attempt)

    def _read_with_retries(
        self, request: urllib.request.Request, *, timeout: float
    ) -> tuple[bytes, int]:
        deadline = time.monotonic() + timeout
        for attempt in range(1, MAX_MODEL_ATTEMPTS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModelTransportError(
                    "timeout", attempts=attempt - 1, retryable=True
                )
            try:
                # The endpoint scheme was allow-listed and redirects are disabled.
                with self._opener.open(request, timeout=remaining) as response:
                    raw_length = response.headers.get("Content-Length")
                    if raw_length is not None:
                        try:
                            declared_length = int(raw_length)
                        except ValueError:
                            declared_length = -1
                        if declared_length > MAX_MODEL_RESPONSE_BYTES:
                            raise ModelTransportError(
                                "response_too_large",
                                attempts=attempt,
                                retryable=False,
                            )
                    payload = response.read(MAX_MODEL_RESPONSE_BYTES + 1)
                if len(payload) > MAX_MODEL_RESPONSE_BYTES:
                    raise ModelTransportError(
                        "response_too_large", attempts=attempt, retryable=False
                    )
                return payload, attempt
            except urllib.error.HTTPError as exc:
                error = _http_transport_error(exc.code, attempt)
                retry_after = parse_retry_after_seconds(exc.headers)
                exc.close()
                if not error.retryable or attempt >= MAX_MODEL_ATTEMPTS:
                    raise error from exc
                delay = self._retry_delay(attempt, retry_after)
                if delay >= deadline - time.monotonic():
                    raise error from exc
                self._sleep(delay)
            except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
                error = _network_transport_error(exc, attempt)
                if not error.retryable or attempt >= MAX_MODEL_ATTEMPTS:
                    raise error from exc
                delay = self._retry_delay(attempt, None)
                if delay >= deadline - time.monotonic():
                    raise error from exc
                self._sleep(delay)
        raise ModelTransportError(
            "network", attempts=MAX_MODEL_ATTEMPTS, retryable=True
        )

    def request(
        self,
        *,
        prompt: str,
        schema_name: str,
        schema: dict[str, Any],
        timeout: float,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if isinstance(timeout, bool) or timeout <= 0 or timeout > 1800:
            raise ValidationError("model timeout must be in (0, 1800] seconds")
        try:
            prompt_bytes = prompt.encode("utf-8")
        except UnicodeError as exc:
            raise ValidationError("model prompt is not valid Unicode") from exc
        if len(prompt_bytes) > MAX_MODEL_PROMPT_BYTES:
            raise ValidationError("model prompt exceeds 2 MiB")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", schema_name):
            raise ValidationError("model output schema name is invalid")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise ValidationError("model output schema is invalid") from exc
        validator = Draft202012Validator(schema)
        body = self._request_body(prompt=prompt, schema_name=schema_name, schema=schema)
        request = urllib.request.Request(  # noqa: S310 - URL was strictly validated
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.settings.credential()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": f"PCBDraft/{__version__}",
            },
        )
        started = time.monotonic()
        payload, attempts = self._read_with_retries(request, timeout=timeout)
        duration = time.monotonic() - started
        try:
            envelope = _strict_json_loads(payload)
            choice = envelope["choices"][0]
            message = choice["message"]
            if not isinstance(choice, dict) or not isinstance(message, dict):
                raise TypeError
        except (KeyError, IndexError, TypeError, ValueError, RecursionError) as exc:
            raise ValidationError(
                "model returned an invalid JSON response envelope"
            ) from exc
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            raise ValidationError("model output was truncated at its token limit")
        if finish_reason == "content_filter" or message.get("refusal"):
            raise PCBDraftError("model provider declined the structured request")
        content = message.get("content")
        if not isinstance(content, str):
            raise ValidationError("model response did not contain JSON text")
        try:
            value = _strict_json_loads(content)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValidationError("model returned invalid JSON content") from exc
        if not isinstance(value, dict):
            raise ValidationError("model output must be one JSON object")
        try:
            validator.validate(value)
        except JSONSchemaValidationError as exc:
            raise ValidationError(
                "model output does not satisfy the requested JSON schema"
            ) from exc
        receipt = {
            "completed": True,
            "schema_valid": True,
            "provider": self.settings.provider_id,
            "provider_name": self.settings.provider_name,
            "model": self.settings.model,
            "config": self.settings.source,
            "duration_seconds": round(duration, 6),
            "attempts": attempts,
            "finish_reason": (
                finish_reason if isinstance(finish_reason, str) else "unspecified"
            ),
            "provider_protocol": "openai-chat-completions",
            "structured_output": self.profile.output_mode,
            "prompt_transport": (
                "https-body"
                if urllib.parse.urlsplit(self.endpoint).scheme == "https"
                else "loopback-http-body"
            ),
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
        failure_receipt: dict[str, Any] = {
            "completed": False,
            "schema_valid": False,
            "failure": type(exc).__name__,
            "prompt_persisted": False,
            "credential_persisted": False,
        }
        if settings is not None:
            failure_receipt.update(
                {"provider": settings.provider_id, "model": settings.model}
            )
        if isinstance(exc, ModelTransportError):
            failure_receipt.update(exc.receipt())
        atomic_write_json(receipt_path, failure_receipt)
        raise
    atomic_write_json(final_path, value)
    receipt["schema_artifact"] = schema_path.name
    receipt["output_artifact"] = final_path.name
    atomic_write_json(receipt_path, receipt)
    return value, receipt
