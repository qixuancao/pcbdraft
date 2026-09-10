from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar
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
    image = Image.new("RGB", (4, 3))
    image.putdata(
        [
            (x * 40, y * 60, (x + y) * 20)
            for y in range(image.height)
            for x in range(image.width)
        ]
    )
    image.save(output, format="PNG")
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
            render_result = {
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
            view["state"]["last_preview"] = copy.deepcopy(render_result)
            view["tool_result"] = render_result
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
        del timeout
        if tool_name == "render_board":
            if (project_id, expected_revision) != (self.project_id, 11):
                raise AssertionError((project_id, tool_name, expected_revision))
            self._rendered = True
            return copy.deepcopy(self._view(rendered=True))
        if tool_name == "observe_board_region":
            if project_id != self.project_id:
                raise AssertionError((project_id, tool_name, expected_revision))
            view = self._view(rendered=self._rendered)
            view["tool_result"] = {
                "operation": "observe_board_region",
                **arguments,
            }
            return copy.deepcopy(view)
        raise AssertionError((project_id, tool_name, expected_revision))


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

    def _observe(
        self,
        service: _VisualPCBService,
        *,
        source_image_sha256: str | None = None,
        x_px: int = 1,
        y_px: int = 0,
        width_px: int = 2,
        height_px: int = 2,
    ) -> str | dict[str, Any]:
        _set_service(service)
        set_current_project_id(service.project_id)
        spec = DEFAULT_PCB_TOOL_REGISTRY.resolve("observe_board_region")
        arguments = {
            "source_image_sha256": source_image_sha256
            or hashlib.sha256(service.image).hexdigest(),
            "x_px": x_px,
            "y_px": y_px,
            "width_px": width_px,
            "height_px": height_px,
        }
        with patch("pcbdraft.agent.tool_bindings._permission_mode", "workspace"):
            return _handler(spec)(arguments, session_id="visual-session")

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
            self.assertEqual(summary["image_scope"], "live_tool_result_only")
            self.assertTrue(summary["rerender_after_resume"])

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

    def test_observe_board_region_returns_exact_current_png_crop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _VisualPCBService(Path(temporary))
            render = self._call(service)
            self.assertIsInstance(render, dict)

            result = self._observe(service)

            self.assertIsInstance(result, dict)
            assert isinstance(result, dict)
            summary = json.loads(result["content"][0]["text"])
            self.assertEqual(summary["tool"], "pcb_observe_board_region")
            self.assertEqual(summary["project_id"], service.project_id)
            self.assertEqual(summary["revision"], 12)
            self.assertEqual(summary["design_revision"], 7)
            self.assertEqual(summary["design_content_hash"], service.content_hash)
            self.assertEqual(summary["source_image_width"], 4)
            self.assertEqual(summary["source_image_height"], 3)
            self.assertEqual(
                summary["source_image_kind"], "current-pcb-render-board-png"
            )
            self.assertEqual(summary["source_render_run_id"], service.run_id)
            self.assertEqual(
                summary["crop_box_px"],
                {"left": 1, "top": 0, "right": 3, "bottom": 2},
            )
            self.assertEqual(summary["coordinate_system"]["origin"], "top-left")
            self.assertEqual(
                summary["coordinate_system"]["bounds"], "right-bottom-exclusive"
            )
            self.assertFalse(summary["pixel_to_board_mm_calibrated"])
            self.assertFalse(summary["adds_detail"])
            self.assertEqual(summary["image_width"], 2)
            self.assertEqual(summary["image_height"], 2)
            self.assertEqual(
                summary["source_image_sha256"],
                hashlib.sha256(service.image).hexdigest(),
            )
            self.assertEqual(summary["image_scope"], "live_tool_result_only")
            self.assertTrue(summary["rerender_after_resume"])

            image_url = result["content"][1]["image_url"]["url"]
            crop_bytes = base64.b64decode(image_url.partition(",")[2], validate=True)
            with Image.open(io.BytesIO(service.image)) as source:
                expected_pixels = source.crop((1, 0, 3, 2)).tobytes()
            with Image.open(io.BytesIO(crop_bytes)) as crop:
                self.assertEqual(crop.size, (2, 2))
                self.assertEqual(crop.tobytes(), expected_pixels)

            history = make_tool_result_message(
                "pcb_observe_board_region", result["content"], "call-region"
            )
            wire = _chat_messages_to_responses_input([history])
            self.assertEqual(wire[0]["output"][1]["type"], "input_image")
            self.assertEqual(wire[0]["output"][1]["image_url"], image_url)

    def test_observe_board_region_rejects_missing_hash_and_invalid_regions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = _VisualPCBService(Path(temporary))

            missing = self._observe(service)
            self.assertIsInstance(missing, str)
            self.assertIn("pcb_render_board", json.loads(missing)["error"])

            self.assertIsInstance(self._call(service), dict)
            cases = (
                ("hash", {"source_image_sha256": "b" * 64}, "image hash"),
                ("bounds", {"x_px": 3, "width_px": 2}, "bounds"),
                ("zero", {"width_px": 0}, "strict schema"),
            )
            for label, overrides, expected_error in cases:
                with self.subTest(label=label):
                    result = self._observe(service, **overrides)
                    self.assertIsInstance(result, str)
                    self.assertIn(expected_error, json.loads(result)["error"])

    def test_observe_board_region_rejects_stale_or_corrupt_current_render(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            stale_service = _VisualPCBService(root / "stale")
            self.assertIsInstance(self._call(stale_service), dict)
            original_open = stale_service.open_project

            def stale_open(project_id: str) -> dict[str, Any]:
                view = original_open(project_id)
                view["state"]["revision"] = 13
                return view

            with patch.object(stale_service, "open_project", side_effect=stale_open):
                stale = self._observe(stale_service)
            self.assertIsInstance(stale, str)
            self.assertIn("stale", json.loads(stale)["error"])
            self.assertIn("pcb_render_board", json.loads(stale)["error"])

            corrupt_service = _VisualPCBService(root / "corrupt")
            self.assertIsInstance(self._call(corrupt_service), dict)
            board_png = (
                corrupt_service.root
                / "previews"
                / corrupt_service.run_id
                / "board-top.png"
            )
            board_png.write_bytes(b"not-a-png")
            corrupt = self._observe(corrupt_service)
            self.assertIsInstance(corrupt, str)
            self.assertIn("pcb_render_board", json.loads(corrupt)["error"])


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
        for tool_name in ("pcb_render_board", "pcb_observe_board_region"):
            with (
                self.subTest(tool_name=tool_name),
                patch.object(agent, "_model_supports_vision", return_value=False),
            ):
                content = agent._tool_result_content_for_active_model(
                    tool_name, self._result()
                )

                payload = json.loads(content)
                self.assertFalse(payload["success"])
                self.assertEqual(payload["error_code"], "visual_input_unsupported")

    def test_render_board_fails_when_provider_rejects_visual_tool_messages(
        self,
    ) -> None:
        agent = self._agent()
        for tool_name in ("pcb_render_board", "pcb_observe_board_region"):
            with (
                self.subTest(tool_name=tool_name),
                patch.object(agent, "_model_supports_vision", return_value=True),
                patch.object(
                    agent,
                    "_provider_supports_vision_tool_messages",
                    return_value=False,
                ),
            ):
                content = agent._tool_result_content_for_active_model(
                    tool_name, self._result()
                )

                payload = json.loads(content)
                self.assertFalse(payload["success"])
                self.assertEqual(
                    payload["error_code"], "visual_tool_result_unsupported"
                )

    def test_provider_rejection_recovery_does_not_strip_board_pixels(self) -> None:
        agent = self._agent()
        for tool_name in ("pcb_render_board", "pcb_observe_board_region"):
            with self.subTest(tool_name=tool_name):
                messages = [
                    make_tool_result_message(
                        tool_name, self._result()["content"], "call-render"
                    )
                ]
                before = copy.deepcopy(messages)

                changed = agent._try_strip_image_parts_from_tool_messages(messages)

                self.assertFalse(changed)
                self.assertEqual(messages, before)


class PCBVisualWorkflowTests(unittest.TestCase):
    def test_workflow_guidance_is_conditional_on_board_render_tool(self) -> None:
        from pcbdraft.agent.prompt_builder import pcb_visual_workflow_guidance

        guidance = pcb_visual_workflow_guidance({"pcb_render_board"})

        self.assertIn("visual, layout, or silkscreen", guidance)
        self.assertIn("latest revision", guidance)
        self.assertIn("purely textual or electrical", guidance)
        self.assertEqual(pcb_visual_workflow_guidance({"pcb_run_drc"}), "")

    def test_workflow_guidance_sees_render_behind_progressive_discovery(
        self,
    ) -> None:
        from pcbdraft.agent.system_prompt import (
            _pcb_visual_workflow_guidance_for_agent,
        )

        class _Agent:
            valid_tool_names: ClassVar[set[str]] = {
                "tool_search",
                "tool_describe",
                "tool_call",
            }
            enabled_toolsets: ClassVar[list[str]] = ["pcbdraft"]
            disabled_toolsets: ClassVar[list[str]] = ["browser"]
            quiet_mode = True

        class _Runtime:
            call_kwargs: dict[str, Any] | None = None
            tool_name = "pcb_render_board"

            def get_tool_definitions(self, **kwargs: Any) -> list[dict[str, Any]]:
                self.call_kwargs = kwargs
                return [
                    {
                        "type": "function",
                        "function": {"name": self.tool_name},
                    }
                ]

        runtime = _Runtime()
        with patch("pcbdraft.agent.system_prompt._ra", return_value=runtime):
            guidance = _pcb_visual_workflow_guidance_for_agent(_Agent())

        self.assertIn("pcb_render_board", guidance)
        self.assertEqual(
            runtime.call_kwargs,
            {
                "enabled_toolsets": ["pcbdraft"],
                "disabled_toolsets": ["browser"],
                "quiet_mode": True,
                "skip_tool_search_assembly": True,
            },
        )
        runtime.tool_name = "read_file"
        with patch("pcbdraft.agent.system_prompt._ra", return_value=runtime):
            self.assertEqual(_pcb_visual_workflow_guidance_for_agent(_Agent()), "")

    def test_retiring_board_render_keeps_summary_and_non_pcb_images(self) -> None:
        from pcbdraft.agent.tool_dispatch_helpers import (
            _retire_pcb_render_board_images,
        )

        messages = [
            make_tool_result_message(
                "pcb_render_board",
                [
                    {"type": "text", "text": '{"revision":12}'},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
                "call-render",
            ),
            make_tool_result_message(
                "computer_use",
                [
                    {"type": "text", "text": "desktop observation"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,BBBB"},
                    },
                ],
                "call-computer",
            ),
        ]

        retired = _retire_pcb_render_board_images(messages)

        self.assertEqual(retired, 1)
        board_content = messages[0]["content"]
        self.assertEqual(board_content[0]["text"], '{"revision":12}')
        self.assertIn("pixels omitted", board_content[1]["text"])
        self.assertIn("pcb_render_board", board_content[1]["text"])
        self.assertEqual(messages[1]["content"][1]["type"], "image_url")

    def test_each_later_pcb_result_retires_previous_board_pixels(self) -> None:
        from pcbdraft.agent.tool_executor import _append_tool_result_message

        messages: list[dict[str, Any]] = []
        first_render = make_tool_result_message(
            "pcb_render_board",
            PCBVisualProviderCapabilityTests._result()["content"],
            "r1",
        )
        second_render = make_tool_result_message(
            "pcb_observe_board_region",
            PCBVisualProviderCapabilityTests._result()["content"],
            "r2",
        )

        _append_tool_result_message(messages, first_render)
        _append_tool_result_message(messages, second_render)
        self.assertEqual(
            sum(
                part.get("type") == "image_url"
                for message in messages
                for part in message["content"]
                if isinstance(part, dict)
            ),
            1,
        )

        _append_tool_result_message(
            messages,
            make_tool_result_message("pcb_run_drc", '{"success":true}', "drc"),
        )
        self.assertFalse(
            any(
                part.get("type") == "image_url"
                for message in messages
                if isinstance(message.get("content"), list)
                for part in message["content"]
                if isinstance(part, dict)
            )
        )

    def test_render_board_contract_describes_live_revision_bound_pixels(self) -> None:
        spec = DEFAULT_PCB_TOOL_REGISTRY.resolve("render_board")

        self.assertIn("PNG", spec.description)
        self.assertIn("project/revision", spec.description)

    def test_region_observation_is_discoverable_with_existing_object_queries(
        self,
    ) -> None:
        specs = DEFAULT_PCB_TOOL_REGISTRY.projected_specs("routing", project_bound=True)
        names = {spec.external_name for spec in specs}

        self.assertTrue(
            {
                "pcb_observe_board_region",
                "pcb_inspect_component",
                "pcb_inspect_net",
                "pcb_inspect_board",
            }.issubset(names)
        )
        spec = DEFAULT_PCB_TOOL_REGISTRY.resolve("observe_board_region")
        self.assertTrue(spec.annotations.read_only)
        self.assertIn("top-left pixel", spec.description)
        self.assertIn("do not map to board millimetres", spec.description)
        schema = spec.input_schema["properties"]
        self.assertEqual(schema["source_image_sha256"]["pattern"], "^[0-9a-f]{64}$")
        self.assertEqual(schema["width_px"]["minimum"], 1)


if __name__ == "__main__":
    unittest.main()
