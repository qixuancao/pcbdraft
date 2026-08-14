"""Versioned, bounded transport for optional agent-harness providers.

The bridge is intentionally smaller than a model adapter.  It receives one
prompt and one output schema on stdin, returns one JSON envelope on stdout, and
never receives user text in argv.  PCBDraft remains responsible for
validating the returned intent or circuit plan and for every KiCad mutation.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from .errors import PCBDraftError, ValidationError
from .io import atomic_write_json, make_directory
from .process import run_command

HARNESS_BRIDGE_REQUEST_SCHEMA: Final = "pcbdraft-harness-provider-request"
HARNESS_BRIDGE_RESPONSE_SCHEMA: Final = "pcbdraft-harness-provider-response"
HARNESS_BRIDGE_VERSION: Final = 1
HARNESS_BRIDGE_INPUT_LIMIT: Final = 4 * 1024 * 1024
HARNESS_BRIDGE_OUTPUT_LIMIT: Final = 1024 * 1024
HARNESS_BRIDGE_OPERATIONS: Final = frozenset({"interpret", "plan", "revise_plan"})

HarnessOperation = Literal["interpret", "plan", "revise_plan"]

_RESPONSE_FIELDS = {
    "schema",
    "version",
    "request_id",
    "operation",
    "ok",
    "result",
    "error",
    "metadata",
}
_ERROR_FIELDS = {"code", "message", "retryable"}
_METADATA_FIELDS = {
    "provider",
    "model",
    "finish_reason",
    "structured_output",
    "session_id",
}
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


@dataclass(frozen=True)
class HarnessBridgeSettings:
    """Configuration for the optional DeepSeek Harness bridge executable."""

    executable: str | None = None
    provider: str = "deepseek-official"
    model: str = "deepseek-v4-flash"

    @classmethod
    def from_environment(cls) -> HarnessBridgeSettings:
        executable = os.environ.get("PCBDRAFT_DSH_BRIDGE", "").strip() or None
        provider = os.environ.get("PCBDRAFT_DSH_PROVIDER", "deepseek-official").strip()
        model = os.environ.get("DSH_MODEL", "deepseek-v4-flash").strip()
        if not _SAFE_IDENTIFIER.fullmatch(provider):
            raise ValidationError("DeepSeek Harness provider id is invalid")
        if not model or len(model.encode("utf-8")) > 200:
            raise ValidationError("DeepSeek Harness model id is invalid")
        return cls(executable=executable, provider=provider, model=model)

    def command(self) -> tuple[str, ...]:
        if self.executable is None:
            return (sys.executable, "-m", "pcbdraft.deepseek_bridge")
        candidate = shutil.which(self.executable)
        if candidate is None:
            path = Path(self.executable).expanduser()
            if not path.is_absolute() or not path.is_file():
                raise PCBDraftError(
                    "configured DeepSeek Harness bridge executable is unavailable"
                )
            candidate = str(path)
        return (str(Path(candidate).resolve()),)

    def diagnostic(self) -> dict[str, Any]:
        if self.executable is None:
            runtime_available = importlib.util.find_spec("deepseek_harness") is not None
            bridge = "python -m pcbdraft.deepseek_bridge"
            available = runtime_available and bool(os.environ.get("DEEPSEEK_API_KEY"))
        else:
            try:
                bridge = self.command()[0]
                runtime_available = True
                available = True
            except PCBDraftError:
                bridge = self.executable
                runtime_available = False
                available = False
        return {
            "id": "deepseek-harness",
            "available": available,
            "runtime_available": runtime_available,
            "credential_present": bool(os.environ.get("DEEPSEEK_API_KEY")),
            "provider": self.provider,
            "model": self.model,
            "bridge": bridge,
            "protocol": {
                "schema": HARNESS_BRIDGE_RESPONSE_SCHEMA,
                "version": HARNESS_BRIDGE_VERSION,
            },
            "secret_storage": "DeepSeek Harness runtime environment only",
            "planning": "versioned structured bridge; PCBDraft validates all output",
        }


class HarnessBridgeClient:
    """Invoke one harness turn through a strict stdin/stdout contract."""

    def __init__(self, settings: HarnessBridgeSettings) -> None:
        self.settings = settings

    def invoke(
        self,
        operation: HarnessOperation,
        *,
        prompt: str,
        output_schema: dict[str, Any],
        project_dir: Path,
        run_dir: Path,
        timeout: float,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if operation not in HARNESS_BRIDGE_OPERATIONS:
            raise ValidationError("unsupported DeepSeek Harness bridge operation")
        if timeout <= 0 or timeout > 1800:
            raise ValidationError("provider timeout must be in (0, 1800] seconds")
        if not isinstance(prompt, str) or not prompt:
            raise ValidationError("DeepSeek Harness prompt must be non-empty")
        if not isinstance(output_schema, dict):
            raise ValidationError("DeepSeek Harness output schema must be an object")

        make_directory(run_dir)
        request_id = hashlib.sha256(
            (
                operation
                + "\0"
                + prompt
                + "\0"
                + json.dumps(
                    output_schema,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ).encode("utf-8")
        ).hexdigest()[:24]
        request = {
            "schema": HARNESS_BRIDGE_REQUEST_SCHEMA,
            "version": HARNESS_BRIDGE_VERSION,
            "request_id": request_id,
            "operation": operation,
            "prompt": prompt,
            "output_schema": output_schema,
            "session_root": str((run_dir / "harness-session").resolve()),
        }
        body = json.dumps(
            request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > HARNESS_BRIDGE_INPUT_LIMIT:
            raise ValidationError("DeepSeek Harness bridge request exceeds 4 MiB")

        result = run_command(
            self.settings.command(),
            cwd=project_dir,
            timeout=timeout,
            max_output_bytes=HARNESS_BRIDGE_OUTPUT_LIMIT,
            stdin_data=body + b"\n",
        )
        if result.timed_out:
            raise PCBDraftError("DeepSeek Harness provider timed out")
        if result.output_limited:
            raise PCBDraftError("DeepSeek Harness bridge output exceeded 1 MiB")
        if result.returncode != 0:
            raise PCBDraftError(
                f"DeepSeek Harness bridge exited unsuccessfully ({result.returncode})"
            )
        try:
            envelope = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError(
                "DeepSeek Harness bridge returned invalid JSON"
            ) from exc
        value, metadata = _validate_response(envelope, request_id, operation)
        atomic_write_json(
            run_dir / f"harness-{operation}-receipt.json",
            {
                "schema": "pcbdraft-harness-provider-receipt",
                "version": 1,
                "request_id": request_id,
                "operation": operation,
                "metadata": metadata,
            },
            mode=0o600,
        )
        return value, metadata


def _validate_response(
    value: Any, request_id: str, operation: HarnessOperation
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(value, Mapping) or set(value) != _RESPONSE_FIELDS:
        raise ValidationError("DeepSeek Harness bridge response fields are invalid")
    if (
        value["schema"] != HARNESS_BRIDGE_RESPONSE_SCHEMA
        or value["version"] != HARNESS_BRIDGE_VERSION
    ):
        raise ValidationError("unsupported DeepSeek Harness bridge protocol")
    if value["request_id"] != request_id or value["operation"] != operation:
        raise ValidationError("DeepSeek Harness bridge response correlation failed")
    if not isinstance(value["ok"], bool):
        raise ValidationError("DeepSeek Harness bridge response status is invalid")

    metadata = value["metadata"]
    if not isinstance(metadata, Mapping) or set(metadata) != _METADATA_FIELDS:
        raise ValidationError("DeepSeek Harness bridge metadata is invalid")
    normalized_metadata = {
        "provider": _response_text(metadata["provider"], "provider", 160),
        "model": _response_text(metadata["model"], "model", 200),
        "finish_reason": _nullable_response_text(
            metadata["finish_reason"], "finish_reason", 80
        ),
        "structured_output": _response_text(
            metadata["structured_output"], "structured_output", 160
        ),
        "session_id": _response_text(metadata["session_id"], "session_id", 200),
    }

    if value["ok"]:
        if value["error"] is not None or not isinstance(value["result"], Mapping):
            raise ValidationError("DeepSeek Harness success envelope is invalid")
        return dict(value["result"]), normalized_metadata

    error = value["error"]
    if value["result"] is not None or not isinstance(error, Mapping):
        raise ValidationError("DeepSeek Harness failure envelope is invalid")
    if set(error) != _ERROR_FIELDS or not isinstance(error["retryable"], bool):
        raise ValidationError("DeepSeek Harness bridge error is invalid")
    code = _response_text(error["code"], "error.code", 80)
    message = _response_text(error["message"], "error.message", 512)
    raise PCBDraftError(f"DeepSeek Harness provider failed [{code}]: {message}")


def _response_text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"DeepSeek Harness bridge {field} is invalid")
    normalized = " ".join(value.split())
    if len(normalized.encode("utf-8")) > limit:
        raise ValidationError(f"DeepSeek Harness bridge {field} exceeds its limit")
    return normalized


def _nullable_response_text(value: Any, field: str, limit: int) -> str | None:
    if value is None:
        return None
    return _response_text(value, field, limit)
