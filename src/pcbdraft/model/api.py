"""Configured structured-model API used by every PCBDraft model-assisted feature."""

from __future__ import annotations

import http.client
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

from pcbdraft.core.errors import PCBDraftError, ValidationError
from pcbdraft.core.io import atomic_write_json
from pcbdraft.model.contracts import (
    provider_config_path,
    validate_provider_base_url,
    validate_provider_credential,
    validate_provider_model_id,
)
from pcbdraft.model.profiles import provider_wire_profile
from pcbdraft.model.retry import jittered_backoff, parse_retry_after_seconds

CONFIG_LIMIT = 64 * 1024
MAX_MODEL_RESPONSE_BYTES = 1_048_576
MAX_MODEL_PROMPT_BYTES = 2 * 1024 * 1024
MAX_MODEL_SCHEMA_BYTES = 512 * 1024
MAX_MODEL_REQUEST_BYTES = 3 * 1024 * 1024
MAX_MODEL_ATTEMPTS = 3
MAX_RETRY_AFTER_SECONDS = 300.0
MODEL_USER_AGENT = (
    "PCBDraft/0.1.0 "
    "(Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
MAX_MODEL_REPLY_BYTES = 16 * 1024
_RESPONSES_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


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


def _extract_json_object(value: str | bytes) -> Any:
    """Decode one strict JSON object, tolerating markdown fences and prose.

    Some models wrap the reply in ```json fences or add a short lead-in/outro
    around the payload.  When strict decoding fails, this extracts the first
    balanced JSON object (or array) span and re-parses it strictly.
    """

    text = value.decode("utf-8") if isinstance(value, bytes) else value
    try:
        return _strict_json_loads(text)
    except (TypeError, ValueError, RecursionError):
        pass
    stripped = text.strip()
    if stripped.startswith("```"):
        end = stripped.find("```", 3)
        if end != -1:
            candidate = stripped[end + 3 :]
            try:
                return _strict_json_loads(candidate)
            except (TypeError, ValueError, RecursionError):
                text = candidate
    opener = text.find("{")
    closer = text.find("}")
    if opener == -1 or closer <= opener:
        opener = text.find("[")
        closer = text.rfind("]")
        if opener == -1 or closer <= opener:
            raise ValueError("model content contains no JSON object")
        return _strict_json_loads(text[opener : closer + 1])
    depth = 0
    in_string = False
    escape = False
    for index in range(opener, len(text)):
        char = text[index]
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return _strict_json_loads(text[opener : index + 1])
    raise ValueError("model content contains an unterminated JSON object")


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
            "agent_protocol": profile.agent_protocol,
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
        document.update(self.profile.extra_request_fields)
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
        self,
        request: urllib.request.Request,
        *,
        timeout: float,
        max_attempts: int = MAX_MODEL_ATTEMPTS,
    ) -> tuple[bytes, int]:
        if (
            isinstance(max_attempts, bool)
            or not 1 <= max_attempts <= MAX_MODEL_ATTEMPTS
        ):
            raise ValidationError("model transport attempts are out of bounds")
        deadline = time.monotonic() + timeout
        for attempt in range(1, max_attempts + 1):
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
                if not error.retryable or attempt >= max_attempts:
                    raise error from exc
                delay = self._retry_delay(attempt, retry_after)
                if delay >= deadline - time.monotonic():
                    raise error from exc
                self._sleep(delay)
            except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
                error = _network_transport_error(exc, attempt)
                if not error.retryable or attempt >= max_attempts:
                    raise error from exc
                delay = self._retry_delay(attempt, None)
                if delay >= deadline - time.monotonic():
                    raise error from exc
                self._sleep(delay)
        raise ModelTransportError("network", attempts=max_attempts, retryable=True)

    def _decode_structured(
        self, payload: bytes, validator: Draft202012Validator
    ) -> tuple[dict[str, Any], str | None]:
        """Decode and locally validate one structured model response."""
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
            value = _extract_json_object(content)
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
        return value, finish_reason

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
                "User-Agent": MODEL_USER_AGENT,
            },
        )
        started = time.monotonic()
        deadline = started + timeout
        attempts = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModelTransportError("timeout", attempts=attempts, retryable=True)
            try:
                payload, transport_attempts = self._read_with_retries(
                    request, timeout=remaining, max_attempts=1
                )
                attempts += transport_attempts
                value, finish_reason = self._decode_structured(payload, validator)
                break
            except ModelTransportError as exc:
                attempts += exc.attempts
                if not exc.retryable or attempts >= MAX_MODEL_ATTEMPTS:
                    raise
                delay = self._retry_delay(attempts, None)
                if delay >= deadline - time.monotonic():
                    raise
                self._sleep(delay)
            except ValidationError:
                if attempts >= MAX_MODEL_ATTEMPTS:
                    raise
                delay = self._retry_delay(attempts, None)
                if delay >= deadline - time.monotonic():
                    raise
                self._sleep(delay)
        duration = time.monotonic() - started
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


@dataclass(frozen=True)
class ResponsesFunctionCall:
    """One locally validated function call returned by the Responses API."""

    call_id: str
    name: str
    arguments: dict[str, Any]


class OpenAIResponsesClient(StructuredModelClient):
    """Bounded client for one native OpenAI Responses function-call decision.

    This deliberately implements only the small protocol surface the PCB agent
    needs.  It does not execute a function and never receives filesystem or
    application-service authority.
    """

    def __init__(self, settings: OpenAICompatibleSettings) -> None:
        super().__init__(settings)
        self.endpoint = self.settings.base_url.rstrip("/") + "/responses"

    def request_tool_call(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any],
        timeout: float,
    ) -> tuple[ResponsesFunctionCall | None, dict[str, Any]]:
        """Request zero or one strict function call and reject loose envelopes."""

        if isinstance(timeout, bool) or timeout <= 0 or timeout > 1800:
            raise ValidationError("model timeout must be in (0, 1800] seconds")
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValidationError("Responses instructions must be non-empty text")
        document = {
            "model": self.settings.model,
            "instructions": instructions,
            "input": input_items,
            "tools": tools,
            "tool_choice": tool_choice,
            "parallel_tool_calls": False,
            "max_output_tokens": 2048,
            "store": False,
        }
        try:
            body = json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise ValidationError("Responses request is not strict JSON") from exc
        if len(body) > MAX_MODEL_REQUEST_BYTES:
            raise ValidationError("Responses request exceeds 3 MiB")
        request = urllib.request.Request(  # noqa: S310 - URL was strictly validated
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.settings.credential()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": MODEL_USER_AGENT,
            },
        )
        envelope, attempts, duration = self._post_responses(request, timeout)
        output = envelope.get("output")
        if not isinstance(output, list):
            raise ValidationError("Responses API output is malformed")
        raw_calls = [
            item
            for item in output
            if isinstance(item, dict) and item.get("type") == "function_call"
        ]
        if len(raw_calls) > 1:
            raise ValidationError("model returned more than one PCB tool call")
        call = self._decode_function_call(raw_calls[0]) if raw_calls else None
        if tool_choice == "required" and call is None:
            raise ValidationError("model omitted a required PCB tool call")
        receipt = {
            "completed": True,
            "provider": self.settings.provider_id,
            "provider_name": self.settings.provider_name,
            "model": self.settings.model,
            "config": self.settings.source,
            "duration_seconds": round(duration, 6),
            "attempts": attempts,
            "provider_protocol": "openai-responses",
            "response_id": envelope["id"],
            "prompt_persisted": False,
            "credential_persisted": False,
            "parallel_tool_calls": False,
        }
        return call, receipt

    def request_conversation(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout: float,
    ) -> tuple[str | None, ResponsesFunctionCall | None, dict[str, Any]]:
        """Request one conversational reply that may include zero or one call.

        Unlike :meth:`request_tool_call` the model is not forced to select a
        tool: it may answer in prose, select one eligible PCB tool, or do both.
        The reply text is validated and bounded, and a tool call, when present,
        must still be a single well-formed function call.
        """

        if isinstance(timeout, bool) or timeout <= 0 or timeout > 1800:
            raise ValidationError("model timeout must be in (0, 1800] seconds")
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValidationError("Responses instructions must be non-empty text")
        document = {
            "model": self.settings.model,
            "instructions": instructions,
            "input": input_items,
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "max_output_tokens": 4096,
            "store": False,
        }
        try:
            body = json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise ValidationError("Responses request is not strict JSON") from exc
        if len(body) > MAX_MODEL_REQUEST_BYTES:
            raise ValidationError("Responses request exceeds 3 MiB")
        request = urllib.request.Request(  # noqa: S310 - URL was strictly validated
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.settings.credential()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": MODEL_USER_AGENT,
            },
        )
        envelope, attempts, duration = self._post_responses(request, timeout)
        output = envelope.get("output")
        if not isinstance(output, list):
            raise ValidationError("Responses API output is malformed")
        raw_calls = [
            item
            for item in output
            if isinstance(item, dict) and item.get("type") == "function_call"
        ]
        if len(raw_calls) > 1:
            raise ValidationError("model returned more than one PCB tool call")
        call = self._decode_function_call(raw_calls[0]) if raw_calls else None
        text = self._decode_assistant_text(output)
        if text is None and call is None:
            raise ValidationError("model returned an empty conversational response")
        receipt = {
            "completed": True,
            "provider": self.settings.provider_id,
            "provider_name": self.settings.provider_name,
            "model": self.settings.model,
            "config": self.settings.source,
            "duration_seconds": round(duration, 6),
            "attempts": attempts,
            "provider_protocol": "openai-responses",
            "response_id": envelope["id"],
            "prompt_persisted": False,
            "credential_persisted": False,
            "parallel_tool_calls": False,
            "has_reply_text": text is not None,
        }
        return text, call, receipt

    def _post_responses(
        self,
        request: urllib.request.Request,
        timeout: float,
    ) -> tuple[dict[str, Any], int, float]:
        """POST one strict Responses envelope and validate its outer shape."""

        started = time.monotonic()
        # A router decision has its own durable dispatch journal. Retrying this
        # POST inside the transport would bypass that exactly-once boundary.
        payload, attempts = self._read_with_retries(
            request, timeout=timeout, max_attempts=1
        )
        duration = time.monotonic() - started
        try:
            envelope = _strict_json_loads(payload)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValidationError("Responses API returned invalid JSON") from exc
        if not isinstance(envelope, dict):
            raise ValidationError("Responses API returned an invalid response envelope")
        status = envelope.get("status")
        if not isinstance(status, str):
            raise ValidationError("Responses API response status is malformed")
        if status != "completed":
            raise PCBDraftError("model did not complete the tool-selection response")
        response_id = envelope.get("id")
        if (
            not isinstance(response_id, str)
            or not response_id
            or len(response_id) > 256
            or any(ord(character) < 32 for character in response_id)
        ):
            raise ValidationError("Responses API response id is invalid")
        return envelope, attempts, duration

    @staticmethod
    def _decode_assistant_text(output: list[Any]) -> str | None:
        """Extract the single bounded assistant message from Responses output."""

        fragments: list[str] = []
        refused = False
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "refusal":
                refused = True
                continue
            if item.get("type") != "message" or item.get("role") != "assistant":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "output_text" and isinstance(
                    block.get("text"), str
                ):
                    fragments.append(block["text"])
        if refused:
            raise PCBDraftError("model provider declined the conversational request")
        text = "".join(fragments)
        if not text.strip():
            return None
        if len(text.encode("utf-8")) > MAX_MODEL_REPLY_BYTES:
            raise ValidationError("model reply text exceeds the 16 KiB bound")
        return text

    @staticmethod
    def _decode_function_call(value: dict[str, Any]) -> ResponsesFunctionCall:
        call_id = value.get("call_id")
        name = value.get("name")
        encoded_arguments = value.get("arguments")
        if (
            not isinstance(call_id, str)
            or not call_id
            or len(call_id) > 256
            or any(ord(character) < 32 for character in call_id)
        ):
            raise ValidationError("Responses function call id is invalid")
        if not isinstance(name, str) or _RESPONSES_TOOL_NAME.fullmatch(name) is None:
            raise ValidationError("Responses function name is invalid")
        if (
            not isinstance(encoded_arguments, str)
            or len(encoded_arguments.encode("utf-8")) > 256 * 1024
        ):
            raise ValidationError("Responses function arguments are invalid")
        try:
            arguments = _strict_json_loads(encoded_arguments)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValidationError(
                "Responses function arguments are invalid JSON"
            ) from exc
        if not isinstance(arguments, dict):
            raise ValidationError("Responses function arguments must be an object")
        return ResponsesFunctionCall(call_id=call_id, name=name, arguments=arguments)


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
