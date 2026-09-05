from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from PIL import Image

from pcbdraft.agent.tool_bindings import (
    _handler,
    _set_service,
    set_current_project_id,
)
from pcbdraft.agent.tool_dispatch_helpers import make_tool_result_message
from pcbdraft.agent.tooling import DEFAULT_PCB_TOOL_REGISTRY
from pcbdraft.core.io import atomic_write_json
from pcbdraft.model.codex_responses_adapter import (
    _chat_messages_to_responses_input,
)


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), (10, 20, 30)).save(output, format="PNG")
    return output.getvalue()


class _VisualPCBService:
    def __init__(
        self,
        root: Path,
        *,
        image: bytes | None = None,
        image_relative: str | None = None,
        source_design_revision: int = 7,
    ) -> None:
        self.root = root
        self.project_id = "visual-board"
        self.content_hash = "a" * 64
        self.run_id = "20260905T120000Z-1234abcd"
        self.image = _png_bytes() if image is None else image
        self.image_relative = image_relative or (
            f"previews/{self.run_id}/board-top.png"
        )
        self.source_design_revision = source_design_revision
        self._rendered = False
        self._prepare_artifacts()

    def _prepare_artifacts(self) -> None:
        preview_root = self.root / "previews" / self.run_id
        preview_root.mkdir(parents=True)
        board_svg = preview_root / "board.svg"
        board_svg.write_text("<svg/>", encoding="utf-8")
        board_png = self.root / self.image_relative
        board_png.parent.mkdir(parents=True, exist_ok=True)
        board_png.write_bytes(self.image)
        atomic_write_json(
            preview_root / "receipt.json",
            {
                "schema": "pcbdraft-preview-bundle",
                "version": 1,
                "created_at": "2026-09-05T12:00:00Z",
                "renders": ["render_board"],
                "design_content_hash": self.content_hash,
                "files": {
                    "board_svg": {
                        "path": "board.svg",
                        "bytes": board_svg.stat().st_size,
                        "sha256": hashlib.sha256(board_svg.read_bytes()).hexdigest(),
                    },
                    "board_render": {
                        "path": "board-top.png",
                        "bytes": len(self.image),
                        "sha256": hashlib.sha256(self.image).hexdigest(),
                    },
                },
                "tool_runs": [],
            },
        )

    def project_root(self, project_id: str) -> Path:
        if project_id != self.project_id:
            raise AssertionError(project_id)
        return self.root

    def inspect_engineering_stage(self, project_id: str) -> dict[str, Any]:
        view = self.open_project(project_id)
        return {
            "project_id": project_id,
            "live_revision": view["state"]["revision"],
            "design_revision": view["state"]["design_revision"],
            "evidence_source": "validation-run:none",
            "stage": "routing",
            "release_gate_passed": False,
            "blockers": [],
        }

    def _view(self, *, rendered: bool) -> dict[str, Any]:
        revision = 12 if rendered else 11
        view: dict[str, Any] = {
            "project": {
                "id": self.project_id,
                "name": "Visual Board",
                "status": "generated",
                "design_revision": 7,
            },
            "state": {"revision": revision, "design_revision": 7},
            "design": {
                "root": str(self.root / "design"),
                "content_hash": self.content_hash,
                "files": {},
            },
            "artifacts": {},
            "conversation": {},
            "events": [],
        }
        if rendered:
            view["tool_result"] = {
                "run_id": self.run_id,
                "render": "render_board",
                "root": f"previews/{self.run_id}",
                "receipt": f"previews/{self.run_id}/receipt.json",
                "design_content_hash": self.content_hash,
                "source_revision": 11,
                "source_design_revision": self.source_design_revision,
                "revision": 12,
                "files": {
                    "board_svg": f"previews/{self.run_id}/board.svg",
                    "board_render": self.image_relative,
                },
            }
        return view

    def open_project(self, project_id: str) -> dict[str, Any]:
        if project_id != self.project_id:
            raise AssertionError(project_id)
        return copy.deepcopy(self._view(rendered=self._rendered))

    def execute_pcb_tool(
        self,
        project_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float,
        expected_revision: int,
    ) -> dict[str, Any]:
        del arguments, timeout
        if (project_id, tool_name, expected_revision) != (
            self.project_id,
            "render_board",
            11,
        ):
            raise AssertionError((project_id, tool_name, expected_revision))
        self._rendered = True
        return copy.deepcopy(self._view(rendered=True))


class PCBVisualToolFeedbackTests(unittest.TestCase):
    def tearDown(self) -> None:
        _set_service(None)
        set_current_project_id(None)

    def _call(self, service: _VisualPCBService) -> str | dict[str, Any]:
        _set_service(service)
        set_current_project_id(service.project_id)
        spec = DEFAULT_PCB_TOOL_REGISTRY.resolve("render_board")
        with patch("pcbdraft.agent.tool_bindings._permission_mode", "workspace"):
            return _handler(spec)({}, session_id="visual-session")

    def test_render_board_pixels_reach_responses_provider_with_revision_binding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _VisualPCBService(Path(temporary))

            result = self._call(service)

            self.assertIsInstance(result, dict)
            assert isinstance(result, dict)
            self.assertTrue(result["_multimodal"])
            summary = json.loads(result["content"][0]["text"])
            self.assertEqual(summary["tool"], "pcb_render_board")
            self.assertEqual(summary["project_id"], service.project_id)
            self.assertEqual(summary["revision"], 12)
            self.assertEqual(summary["design_revision"], 7)
            self.assertEqual(summary["source_revision"], 11)
            self.assertEqual(summary["design_content_hash"], service.content_hash)

            image_url = result["content"][1]["image_url"]["url"]
            self.assertTrue(image_url.startswith("data:image/png;base64,"))
            self.assertEqual(
                base64.b64decode(image_url.partition(",")[2], validate=True),
                service.image,
            )

            history = make_tool_result_message(
                "pcb_render_board", result["content"], "call-render"
            )
            wire = _chat_messages_to_responses_input([history])
            self.assertEqual(wire[0]["type"], "function_call_output")
            self.assertEqual(wire[0]["call_id"], "call-render")
            self.assertEqual(wire[0]["output"][1]["type"], "input_image")
            self.assertEqual(wire[0]["output"][1]["image_url"], image_url)

    def test_render_board_rejects_corrupt_stale_and_escaping_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = (
                (
                    "corrupt",
                    _VisualPCBService(root / "corrupt", image=b"not-a-png"),
                    "valid PNG",
                ),
                (
                    "stale",
                    _VisualPCBService(root / "stale", source_design_revision=6),
                    "stale",
                ),
                (
                    "escaping",
                    _VisualPCBService(
                        root / "escaping",
                        image_relative="../outside.png",
                    ),
                    "preview bundle",
                ),
            )
            for label, service, expected_error in cases:
                with self.subTest(label=label):
                    result = self._call(service)
                    self.assertIsInstance(result, str)
                    payload = json.loads(result)
                    self.assertFalse(payload["success"])
                    self.assertIn(expected_error, payload["error"])

    def test_render_board_rejects_symlinked_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _VisualPCBService(Path(temporary))
            preview_parent = service.root / "previews"
            run_directory = preview_parent / service.run_id
            real_directory = preview_parent / "real-render-bundle"
            run_directory.rename(real_directory)
            try:
                run_directory.symlink_to(real_directory.name, target_is_directory=True)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")

            result = self._call(service)

            self.assertIsInstance(result, str)
            payload = json.loads(result)
            self.assertFalse(payload["success"])
            self.assertIn("path is unsafe", payload["error"])


class PCBVisualProviderCapabilityTests(unittest.TestCase):
    @staticmethod
    def _agent() -> Any:
        from pcbdraft.agent.loop import AIAgent

        agent = object.__new__(AIAgent)
        agent.provider = "test-provider"
        agent.model = "test-model"
        agent._no_list_tool_content_models = set()
        return agent

    @staticmethod
    def _result() -> dict[str, Any]:
        return {
            "_multimodal": True,
            "content": [
                {"type": "text", "text": '{"tool":"pcb_render_board"}'},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                },
            ],
            "text_summary": "board render",
        }

    def test_render_board_fails_explicitly_for_non_visual_model(self) -> None:
        agent = self._agent()
        with patch.object(agent, "_model_supports_vision", return_value=False):
            content = agent._tool_result_content_for_active_model(
                "pcb_render_board", self._result()
            )

        payload = json.loads(content)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error_code"], "visual_input_unsupported")

    def test_render_board_fails_when_provider_rejects_visual_tool_messages(
        self,
    ) -> None:
        agent = self._agent()
        with (
            patch.object(agent, "_model_supports_vision", return_value=True),
            patch.object(
                agent,
                "_provider_supports_vision_tool_messages",
                return_value=False,
            ),
        ):
            content = agent._tool_result_content_for_active_model(
                "pcb_render_board", self._result()
            )

        payload = json.loads(content)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error_code"], "visual_tool_result_unsupported")

    def test_provider_rejection_recovery_does_not_strip_board_pixels(self) -> None:
        agent = self._agent()
        messages = [
            make_tool_result_message(
                "pcb_render_board", self._result()["content"], "call-render"
            )
        ]
        before = copy.deepcopy(messages)

        changed = agent._try_strip_image_parts_from_tool_messages(messages)

        self.assertFalse(changed)
        self.assertEqual(messages, before)


if __name__ == "__main__":
    unittest.main()
