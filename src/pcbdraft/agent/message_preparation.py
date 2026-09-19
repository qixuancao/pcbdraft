# mypy: disable-error-code="attr-defined"
"""Prepare provider-safe messages and multimodal content for the agent."""

# These best-effort transforms retain broad guards from the original agent loop.
# ruff: noqa: BLE001, S110

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from pcbdraft.agent.tool_dispatch_helpers import (
    _is_multimodal_tool_result,
    _multimodal_text_summary,
)
from pcbdraft.core.runtime_utils import base_url_host_matches, base_url_hostname

logger = logging.getLogger(__name__)


class MessagePreparationMixin:
    """Normalize image, provider, and Qwen messages before API delivery."""

    @staticmethod
    def _content_has_image_parts(content: Any) -> bool:
        if not isinstance(content, list):
            return False
        for part in content:
            if isinstance(part, dict) and part.get("type") in {
                "image_url",
                "input_image",
            }:
                return True
        return False

    # 20 MB base64 ≈ 15 MB decoded image — generous but prevents OOM from an
    # oversized data: URL (a 100 MB+ payload creates ~275 MB of memory pressure,
    # and gateway users sharing the same process can trivially OOM it).
    _MAX_DATA_URL_BASE64_BYTES = 20 * 1024 * 1024

    @classmethod
    def _materialize_data_url_for_vision(
        cls, image_url: str
    ) -> tuple[str, Path | None]:
        header, _, data = str(image_url or "").partition(",")
        if len(data) > cls._MAX_DATA_URL_BASE64_BYTES:
            logger.warning("data-URL payload too large (%d bytes), skipping", len(data))
            return "", None
        mime = "image/jpeg"
        if header.startswith("data:"):
            mime_part = header[len("data:") :].split(";", 1)[0].strip()
            if mime_part.startswith("image/"):
                mime = mime_part
        suffix = {
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
        }.get(mime, ".jpg")
        tmp_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                prefix="anthropic_image_", suffix=suffix, delete=False
            ) as tmp:
                tmp_name = tmp.name
                tmp.write(base64.b64decode(data))
        except Exception:
            # delete=False means a corrupt/unsupported data URL would otherwise
            # leak a zero-byte temp file on every failed materialization.
            try:
                if tmp_name:
                    os.unlink(tmp_name)
            except OSError:
                pass
            raise
        path = Path(tmp_name)
        return str(path), path

    def _describe_image_for_anthropic_fallback(self, image_url: str, role: str) -> str:
        cache_key = hashlib.sha256(str(image_url or "").encode("utf-8")).hexdigest()
        cached = self._anthropic_image_fallback_cache.get(cache_key)
        if cached:
            return cached

        role_label = {
            "assistant": "assistant",
            "tool": "tool result",
        }.get(role, "user")
        analysis_prompt = (
            "Describe everything visible in this image in thorough detail. "
            "Include any text, code, UI, data, objects, people, layout, colors, "
            "and any other notable visual information."
        )

        vision_source = str(image_url or "")
        cleanup_path: Path | None = None
        if vision_source.startswith("data:"):
            vision_source, cleanup_path = self._materialize_data_url_for_vision(
                vision_source
            )

        description = ""
        try:
            from pcbdraft.tools.vision_tools import vision_analyze_tool

            result_json = asyncio.run(
                vision_analyze_tool(
                    image_url=vision_source, user_prompt=analysis_prompt
                )
            )
            result = json.loads(result_json) if isinstance(result_json, str) else {}
            description = (result.get("analysis") or "").strip()
        except Exception as e:
            description = f"Image analysis failed: {e}"
        finally:
            if cleanup_path and cleanup_path.exists():
                try:
                    cleanup_path.unlink()
                except OSError:
                    pass

        if not description:
            description = "Image analysis failed."

        note = f"[The {role_label} attached an image. Here's what it contains:\n{description}]"
        if vision_source and not str(image_url or "").startswith("data:"):
            note += f"\n[If you need a closer look, use vision_analyze with image_url: {vision_source}]"

        self._anthropic_image_fallback_cache[cache_key] = note
        return note

    def _model_supports_vision(self) -> bool:
        """Return True if the active provider+model reports native vision.

        Used to decide whether to strip image content parts from API-bound
        messages (for non-vision models) or let the provider adapter handle
        them natively (for vision-capable models).

        Resolution order (see ``agent.image_routing._supports_vision_override``):
          1. ``model.supports_vision`` (top-level, single-model shortcut)
          2. ``providers.<provider>.models.<model>.supports_vision``
          3. models.dev capability lookup
        Custom/local models absent from models.dev would otherwise be
        misclassified as non-vision and have their images stripped.
        """
        try:
            from pcbdraft.agent.image_routing import _lookup_supports_vision
            from pcbdraft.model.configuration import load_config

            cfg = load_config()
            provider = (getattr(self, "provider", "") or "").strip()
            model = (getattr(self, "model", "") or "").strip()
            return _lookup_supports_vision(provider, model, cfg) is True
        except Exception:
            return False

    def _provider_supports_vision_tool_messages(self) -> bool:
        """Return True if the active provider accepts list-type tool content.

        Some providers (e.g. Xiaomi MiMo) support multimodal user messages
        but reject list-type tool message content with 400 errors.  This
        checks the provider profile's ``supports_vision_tool_messages`` field.
        """
        try:
            from pcbdraft.model.provider_profiles import get_provider_profile

            provider = (getattr(self, "provider", "") or "").strip()
            profile = get_provider_profile(provider)
            if profile is not None:
                return getattr(profile, "supports_vision_tool_messages", True)
        except Exception:
            pass
        return True  # default: assume compatible

    def _preprocess_anthropic_content(self, content: Any, role: str) -> Any:
        if not self._content_has_image_parts(content):
            return content

        text_parts: list[str] = []
        image_notes: list[str] = []
        for part in content:
            if isinstance(part, str):
                if part.strip():
                    text_parts.append(part.strip())
                continue
            if not isinstance(part, dict):
                continue

            ptype = part.get("type")
            if ptype in {"text", "input_text"}:
                text = str(part.get("text", "") or "").strip()
                if text:
                    text_parts.append(text)
                continue

            if ptype in {"image_url", "input_image"}:
                image_data = part.get("image_url", {})
                image_url = (
                    image_data.get("url", "")
                    if isinstance(image_data, dict)
                    else str(image_data or "")
                )
                if image_url:
                    image_notes.append(
                        self._describe_image_for_anthropic_fallback(image_url, role)
                    )
                else:
                    image_notes.append(
                        "[An image was attached but no image source was available.]"
                    )
                continue

            text = str(part.get("text", "") or "").strip()
            if text:
                text_parts.append(text)

        prefix = "\n\n".join(note for note in image_notes if note).strip()
        suffix = "\n".join(text for text in text_parts if text).strip()
        if prefix and suffix:
            return f"{prefix}\n\n{suffix}"
        if prefix:
            return prefix
        if suffix:
            return suffix
        return (
            "[A multimodal message was converted to text for Anthropic compatibility.]"
        )

    def _prepare_anthropic_messages_for_api(self, api_messages: list) -> list:
        # Fast exit when no message carries image content at all.
        if not any(
            isinstance(msg, dict) and self._content_has_image_parts(msg.get("content"))
            for msg in api_messages
        ):
            return api_messages

        # The Anthropic adapter (agent/anthropic_adapter.py:_convert_content_part_to_anthropic)
        # already translates OpenAI-style image_url/input_image parts into
        # native Anthropic ``{"type": "image", "source": ...}`` blocks. When
        # the active model supports vision we let the adapter do its job and
        # skip this legacy text-fallback preprocessor entirely.
        if self._model_supports_vision():
            return api_messages

        # Non-vision Anthropic model (rare today, but keep the fallback for
        # compat): replace each image part with a vision_analyze text note.
        transformed = copy.deepcopy(api_messages)
        for msg in transformed:
            if not isinstance(msg, dict):
                continue
            msg["content"] = self._preprocess_anthropic_content(
                msg.get("content"),
                str(msg.get("role", "user") or "user"),
            )
        return transformed

    def _prepare_messages_for_non_vision_model(self, api_messages: list) -> list:
        """Strip native image parts when the active model lacks vision.

        Runs on the chat.completions / codex_responses paths. Vision-capable
        models pass through unchanged (provider and any downstream translator
        handle the image parts natively). Non-vision models get each image
        replaced by a cached vision_analyze text description so the turn
        doesn't fail with "model does not support image input".
        """
        if not any(
            isinstance(msg, dict) and self._content_has_image_parts(msg.get("content"))
            for msg in api_messages
        ):
            return api_messages

        if self._model_supports_vision():
            return api_messages

        transformed = copy.deepcopy(api_messages)
        for msg in transformed:
            if not isinstance(msg, dict):
                continue
            # Reuse the Anthropic text-fallback preprocessor — the behaviour is
            # identical (walk content parts, replace images with cached
            # descriptions, merge back into a single text or structured
            # content). Naming is historical.
            msg["content"] = self._preprocess_anthropic_content(
                msg.get("content"),
                str(msg.get("role", "user") or "user"),
            )
        return transformed

    def _tool_result_content_for_active_model(self, tool_name: str, result: Any) -> Any:
        """Return the tool message content that is safe for the active model.

        Multimodal tool results normally unwrap to OpenAI-style content parts so
        vision-capable models can inspect screenshots.  Text-only providers must
        not receive those image parts, because a rejected tool result becomes
        part of the canonical history and can make the next user turn fail before
        the agent has a chance to recover.
        """
        if not _is_multimodal_tool_result(result):
            return result

        content = result.get("content") or []
        if not self._content_has_image_parts(content):
            return content

        if self._model_supports_vision():
            # Vision-capable on paper — but if the provider rejects list-type
            # tool content (e.g. Xiaomi MiMo's 400 "text is not set"), or if
            # we've already learned this lesson in-session, short-circuit to
            # a text summary so we don't burn a round-trip relearning it.
            if not self._provider_supports_vision_tool_messages():
                if tool_name in {"pcb_render_board", "pcb_observe_board_region"}:
                    return self._visual_tool_result_error(
                        tool_name,
                        "visual_tool_result_unsupported",
                        "The active provider does not accept image content in tool results.",
                    )
                logger.debug(
                    "Tool %s: provider %s does not accept list-type tool "
                    "content — sending text summary",
                    tool_name,
                    getattr(self, "provider", ""),
                )
                return _multimodal_text_summary(result)
            key = (
                (getattr(self, "provider", "") or "").strip().lower(),
                (getattr(self, "model", "") or "").strip(),
            )
            no_list = getattr(self, "_no_list_tool_content_models", None)
            if no_list and key in no_list:
                if tool_name in {"pcb_render_board", "pcb_observe_board_region"}:
                    return self._visual_tool_result_error(
                        tool_name,
                        "visual_tool_result_unsupported",
                        "The active provider rejected image content in tool results earlier in this session.",
                    )
                logger.debug(
                    "Tool %s: model %s/%s known to reject list-type tool "
                    "content this session — sending text summary",
                    tool_name,
                    key[0],
                    key[1],
                )
                return _multimodal_text_summary(result)
            return content

        summary = _multimodal_text_summary(result)
        if tool_name in {"pcb_render_board", "pcb_observe_board_region"}:
            return self._visual_tool_result_error(
                tool_name,
                "visual_input_unsupported",
                "The active model does not support image input; board pixels were not downgraded to text.",
            )
        if tool_name == "computer_use":
            return json.dumps(
                {
                    "error": (
                        "computer_use returned screenshot/image content, but the active "
                        "model/provider does not support image input. Switch to a "
                        "vision-capable model for desktop computer use, or use browser "
                        "tools for browser tasks."
                    ),
                    "text_summary": summary,
                }
            )

        logger.warning(
            "Tool %s returned image content for non-vision model %s/%s; "
            "falling back to text summary",
            tool_name,
            self.provider,
            self.model,
        )
        return summary

    @staticmethod
    def _visual_tool_result_error(tool_name: str, code: str, message: str) -> str:
        return json.dumps(
            {
                "tool": tool_name,
                "success": False,
                "ok": False,
                "error_code": code,
                "error": message,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _try_strip_image_parts_from_tool_messages(
        self,
        api_messages: list,
        *,
        remember_model: bool = True,
    ) -> bool:
        """Downgrade list-type tool messages to text summaries in-place.

        Recovery path for providers that reject list-type tool message content
        (e.g. Xiaomi MiMo's 400 "text is not set"; see issue #27344).  Walks
        ``api_messages`` for any ``role: "tool"`` message whose ``content`` is
        a list containing image parts, replaces the content with the existing
        text part(s) (or a minimal placeholder if none survive), and by default
        records the active (provider, model) in
        ``self._no_list_tool_content_models`` so subsequent
        ``_tool_result_content_for_active_model`` calls in this session
        preemptively downgrade screenshots without a round-trip.

        413 payload-size recovery passes ``remember_model=False`` because that
        error means this request body was too large, not that the provider/model
        rejects list-type tool content in general.

        Returns True when at least one tool message was downgraded — the
        caller (the 400 recovery branch in ``agent.conversation_loop``) uses
        this to decide whether to retry the API call with the modified
        history or surface the original error.
        """
        if not isinstance(api_messages, list):
            return False

        # Board image tools promise that the provider sees receipt-bound pixels
        # or receives a hard capability error. Retrying after deleting those
        # pixels would turn a visual observation into a false success.
        if any(
            isinstance(msg, dict)
            and msg.get("role") == "tool"
            and msg.get("name", msg.get("tool_name"))
            in {"pcb_render_board", "pcb_observe_board_region"}
            and self._content_has_image_parts(msg.get("content"))
            for msg in api_messages
        ):
            return False

        if remember_model:
            # Record (provider, model) so we don't relearn this lesson.
            key = (
                (getattr(self, "provider", "") or "").strip().lower(),
                (getattr(self, "model", "") or "").strip(),
            )
            if not hasattr(self, "_no_list_tool_content_models"):
                self._no_list_tool_content_models = set()
            if key[1]:  # only record when we actually have a model id
                self._no_list_tool_content_models.add(key)

        changed = False
        for msg in api_messages:
            if not isinstance(msg, dict) or msg.get("role") != "tool":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue

            # Salvage any text parts so the model still sees some signal.
            text_parts: list[str] = []
            had_image = False
            for part in content:
                if not isinstance(part, dict):
                    if isinstance(part, str) and part.strip():
                        text_parts.append(part.strip())
                    continue
                ptype = part.get("type")
                if ptype == "image_url" or ptype == "input_image":
                    had_image = True
                    continue
                if ptype in {"text", "input_text"}:
                    text = str(part.get("text") or "").strip()
                    if text:
                        text_parts.append(text)

            if not had_image:
                # List-type content but no image parts — leave alone (some
                # providers reject ANY list content, but stripping a
                # text-only list doesn't reduce ambiguity; let the caller
                # surface the original error if this turns out to be the
                # case).
                continue

            if text_parts:
                msg["content"] = "\n\n".join(text_parts)
            else:
                msg["content"] = (
                    "[image content removed — provider does not accept "
                    "list-type tool message content]"
                )
            changed = True

        return changed

    def _anthropic_preserve_dots(self) -> bool:
        """True when using an anthropic-compatible endpoint that preserves dots in model names.
        Alibaba/DashScope keeps dots (e.g. qwen3.5-plus).
        MiniMax keeps dots (e.g. MiniMax-M2.7).
        Xiaomi MiMo keeps dots (e.g. mimo-v2.5, mimo-v2.5-pro).
        OpenCode Go/Zen keeps dots for non-Claude models (e.g. minimax-m2.5-free).
        ZAI/Zhipu keeps dots (e.g. glm-4.7, glm-5.1).
        AWS Bedrock uses dotted inference-profile IDs
        (e.g. ``global.anthropic.claude-opus-4-7``,
        ``us.anthropic.claude-sonnet-4-5-20250929-v1:0``) and rejects
        the hyphenated form with
        ``HTTP 400 The provided model identifier is invalid``.
        Regression for #11976; mirrors the opencode-go fix for #5211
        (commit f77be22c), which extended this same allowlist."""
        if (getattr(self, "provider", "") or "").lower() in {
            "alibaba",
            "minimax",
            "minimax-cn",
            "opencode-go",
            "opencode-zen",
            "zai",
            "bedrock",
            "xiaomi",
            "vertex",
        }:
            return True
        base = (getattr(self, "base_url", "") or "").lower()
        host = base_url_hostname(base)
        return (
            "dashscope" in host
            or base_url_host_matches(base, "aliyuncs.com")
            or "minimax" in host
            or (base_url_host_matches(base, "opencode.ai") and "/zen/" in base)
            or base_url_host_matches(base, "bigmodel.cn")
            or base_url_host_matches(base, "xiaomimimo.com")
            # Vertex AI OpenAI-compat endpoint — Gemini model ids keep dots
            # (e.g. google/gemini-3.5-flash); the hyphenated form is wrong.
            or base_url_host_matches(base, "aiplatform.googleapis.com")
            # AWS Bedrock runtime endpoints — defense-in-depth when
            # ``provider`` is unset but ``base_url`` still names Bedrock.
            or host.startswith("bedrock-runtime.")
        )

    def _is_qwen_portal(self) -> bool:
        """Return True when the base URL targets Qwen Portal."""
        return base_url_host_matches(self._base_url_lower, "portal.qwen.ai")

    def _qwen_prepare_chat_messages(self, api_messages: list) -> list:
        prepared = copy.deepcopy(api_messages)
        if not prepared:
            return prepared

        for msg in prepared:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = [{"type": "text", "text": content}]
            elif isinstance(content, list):
                # Normalize: convert bare strings to text dicts, keep dicts as-is.
                # deepcopy already created independent copies, no need for dict().
                normalized_parts = []
                for part in content:
                    if isinstance(part, str):
                        normalized_parts.append({"type": "text", "text": part})
                    elif isinstance(part, dict):
                        normalized_parts.append(part)
                if normalized_parts:
                    msg["content"] = normalized_parts

        # Inject cache_control on the last part of the system message.
        for msg in prepared:
            if isinstance(msg, dict) and msg.get("role") == "system":
                content = msg.get("content")
                if (
                    isinstance(content, list)
                    and content
                    and isinstance(content[-1], dict)
                ):
                    content[-1]["cache_control"] = {"type": "ephemeral"}
                break

        return prepared

    def _qwen_prepare_chat_messages_inplace(self, messages: list) -> None:
        """In-place variant — mutates an already-copied message list."""
        if not messages:
            return

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = [{"type": "text", "text": content}]
            elif isinstance(content, list):
                normalized_parts = []
                for part in content:
                    if isinstance(part, str):
                        normalized_parts.append({"type": "text", "text": part})
                    elif isinstance(part, dict):
                        normalized_parts.append(part)
                if normalized_parts:
                    msg["content"] = normalized_parts

        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "system":
                content = msg.get("content")
                if (
                    isinstance(content, list)
                    and content
                    and isinstance(content[-1], dict)
                ):
                    content[-1]["cache_control"] = {"type": "ephemeral"}
                break
