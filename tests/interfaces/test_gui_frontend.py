from __future__ import annotations

import re
import unittest
from contextlib import redirect_stderr
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from pcbdraft.interfaces.cli import build_parser, main


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag
        values = dict(attrs)
        for name in ("href", "src"):
            value = values.get(name)
            if value:
                self.urls.append(value)


class GUIFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[2]
        cls.root = root
        cls.html = (root / "src/pcbdraft/web/index.html").read_text(encoding="utf-8")
        cls.script = (root / "src/pcbdraft/web/app.js").read_text(encoding="utf-8")
        cls.styles = (root / "src/pcbdraft/web/app.css").read_text(encoding="utf-8")
        cls.pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        cls.board_script = (root / "src/pcbdraft/web/board.js").read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _asset_text(root: Path, name: str) -> str:
        return (root / "src/pcbdraft/web" / name).read_text(encoding="utf-8")

    def test_gui_command_has_explicit_bounded_network_arguments(self) -> None:
        parser = build_parser(prog="pcbdraft")
        defaults = parser.parse_args(["gui"])
        self.assertEqual(defaults.command, "gui")
        self.assertEqual(defaults.host, "127.0.0.1")
        self.assertEqual(defaults.port, 9130)
        self.assertIsNone(defaults.gui_project_id)
        self.assertFalse(defaults.kicad_ipc)

        selected = parser.parse_args(
            [
                "gui",
                "--host",
                "localhost",
                "--port",
                "9131",
                "--project",
                "demo",
                "--kicad-ipc",
            ]
        )
        self.assertEqual(selected.host, "localhost")
        self.assertEqual(selected.port, 9131)
        self.assertEqual(selected.gui_project_id, "demo")
        self.assertTrue(selected.kicad_ipc)

        for invalid in ("0", "65536"):
            with (
                self.subTest(port=invalid),
                redirect_stderr(StringIO()),
                self.assertRaises(SystemExit),
            ):
                parser.parse_args(["gui", "--port", invalid])

    def test_gui_command_lazily_dispatches_to_server(self) -> None:
        with patch("pcbdraft.interfaces.gui.run_gui", return_value=23) as run_gui:
            result = main(
                [
                    "gui",
                    "--host",
                    "127.0.0.2",
                    "--port",
                    "9137",
                    "--project",
                    "demo",
                ]
            )

        self.assertEqual(result, 23)
        run_gui.assert_called_once_with(
            host="127.0.0.2",
            port=9137,
            project_id="demo",
            kicad_ipc=False,
        )

    def test_assets_and_runtime_urls_are_base_path_relative(self) -> None:
        parser = _AssetParser()
        parser.feed(self.html)
        self.assertIn("./assets/app.css", parser.urls)
        self.assertIn("./assets/app.js", parser.urls)
        self.assertTrue(all(not value.startswith("/") for value in parser.urls))
        self.assertIn('const APP_BASE = new URL(".", document.baseURI);', self.script)
        self.assertIn('const API_BASE = new URL("api/", APP_BASE);', self.script)
        self.assertNotRegex(self.script, r"(?:fetch|EventSource)\s*\(\s*[\"']/")
        self.assertNotRegex(self.html, r"(?:href|src)=[\"']/")

    def test_snapshot_busy_state_and_sse_resume_cursor_remain_independent(self) -> None:
        self.assertNotIn(
            "state.eventCursor = Math.max(state.eventCursor, next.eventSequence);",
            self.script,
        )
        self.assertNotIn("if (busy && !payload?.scene) return;", self.script)
        self.assertIn("payload?.external_change", self.script)
        self.assertIn(
            "payload?.scene ? board.setScene(payload.scene) : false", self.script
        )
        self.assertIn("new EventSource(url)", self.script)
        self.assertIn("state.eventCursor = sequence", self.script)
        self.assertIn("inspector.setScene(scene, payload?.ipc)", self.script)

    def test_snapshot_fallback_runs_only_while_sse_is_disconnected_with_backoff(
        self,
    ) -> None:
        self.assertNotIn("setInterval", self.script)
        self.assertNotIn("startSnapshotPolling", self.script)
        self.assertNotIn("SNAPSHOT_POLL_MS", self.script)
        self.assertRegex(
            self.script, re.compile(r"DISCONNECTED_POLL_MIN_MS\s*=\s*1000")
        )
        self.assertRegex(
            self.script, re.compile(r"DISCONNECTED_POLL_MAX_MS\s*=\s*30000")
        )
        self.assertIn("snapshotInFlight: null", self.script)
        self.assertIn("if (running)", self.script)
        self.assertIn("return running.promise;", self.script)
        self.assertIn("function scheduleDisconnectedPolling()", self.script)
        self.assertIn("if (state.eventStreamHealthy", self.script)
        self.assertIn("Math.min(DISCONNECTED_POLL_MAX_MS, delay * 2)", self.script)
        self.assertIn("stopDisconnectedPolling();", self.script)
        self.assertIn("refreshSnapshot({ quiet: true })", self.script)

    def test_sse_gap_and_stream_reset_force_a_complete_resnapshot(self) -> None:
        self.assertIn("sequence !== state.eventCursor + 1", self.script)
        self.assertIn('value.kind === "stream.reset_required"', self.script)
        self.assertIn("recoverEventStream();", self.script)
        self.assertIn("resetStreamCursor: true", self.script)
        self.assertIn("payload?.stream?.last_sequence", self.script)

    def test_frontend_exposes_required_board_and_turn_controls(self) -> None:
        required_ids = {
            "project-select",
            "activity-list",
            "board-svg",
            "exact-board-layer",
            "preview-precision",
            "primary-3d-view",
            "view-2d",
            "view-3d",
            "front-layer",
            "back-layer",
            "via-layer",
            "footprint-layer",
            "pad-layer",
            "finding-layer",
            "label-layer",
            "unrouted-layer",
            "exact-board-preview",
            "exact-3d-preview",
            "open-in-kicad",
            "validation-card",
            "ipc-indicator",
            "message-form",
            "message-input",
            "stop-turn",
        }
        for element_id in required_ids:
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('data-scene-layer="front"', self.html)
        self.assertIn('data-scene-layer="back"', self.html)
        self.assertIn("pointerdown", self.board_script)
        self.assertIn("wheel", self.board_script)
        self.assertIn("route-draw", self._asset_text(self.root, "workbench.css"))
        self.assertIn("via-pop", self._asset_text(self.root, "workbench.css"))
        self.assertIn("近似预览", self.html)
        self.assertIn("locateFinding", self.board_script)
        self.assertIn('url.searchParams.set("revision"', self.script)
        self.assertIn('url.searchParams.set("content_hash"', self.script)
        self.assertIn(
            '"artifacts/board-3d"', self._asset_text(self.root, "inspector.js")
        )
        self.assertIn("api.post", self._asset_text(self.root, "inspector.js"))

    def test_schematic_preview_requires_project_and_manifest_artifact(self) -> None:
        inspector = self._asset_text(self.root, "inspector.js")
        self.assertIn(
            'const hasSchematic = Boolean(projectId && schematicArtifact?.state === "ready");',
            inspector,
        )
        self.assertIn('item && state === "ready"', inspector)

    def test_preview_rendering_does_not_reload_unchanged_images(self) -> None:
        inspector = self._asset_text(self.root, "inspector.js")
        self.assertIn(
            "if (available) {\n"
            '      const boardUrl = previewUrl("board.svg", { bound: true });\n'
            "      if (elements.boardPreview.src !== boardUrl) {\n"
            "        elements.boardPreview.src = boardUrl;\n"
            "      }\n"
            "    }",
            inspector,
        )
        self.assertIn(
            "if (hasSchematic) {\n"
            '      const schematicUrl = previewUrl("schematic.svg", { bound: true });\n'
            "      if (elements.schematicPreview.src !== schematicUrl) {\n"
            "        elements.schematicPreview.src = schematicUrl;\n"
            "      }\n"
            "    }",
            inspector,
        )

    def test_rendering_uses_text_nodes_and_whitelisted_activity_fields(self) -> None:
        sources = "\n".join(
            self._asset_text(self.root, name)
            for name in ("app.js", "board.js", "conversation.js", "inspector.js")
        )
        self.assertNotIn("innerHTML", sources)
        self.assertIn("textContent", sources)
        self.assertIn(
            "MAX_ACTIVITY_ITEMS", self._asset_text(self.root, "conversation.js")
        )
        for forbidden in (
            "debug_trace",
            "tool_arguments",
            "tool_result",
            "chain_of_thought",
            "reasoning_content",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, sources)
        self.assertRegex(self.board_script, re.compile(r"MAX_SCENE_ITEMS\s*=\s*5000"))
        self.assertRegex(
            self._asset_text(self.root, "conversation.js"),
            re.compile(r"MAX_ACTIVITY_ITEMS\s*=\s*120"),
        )

    def test_web_assets_and_official_ipc_binding_are_packaged(self) -> None:
        for entry in ('"web/*.html"', '"web/*.css"', '"web/*.js"'):
            self.assertIn(entry, self.pyproject)
        self.assertIn("kicad-ipc = [", self.pyproject)
        self.assertIn('"kicad-python==0.7.1"', self.pyproject)
        manifest = (Path(__file__).resolve().parents[2] / "MANIFEST.in").read_text(
            encoding="utf-8"
        )
        self.assertIn("recursive-include src/pcbdraft/web *.html *.css *.js", manifest)

    def test_workbench_modules_are_local_relative_es_modules(self) -> None:
        required = {
            "api.js",
            "store.js",
            "i18n.js",
            "commands.js",
            "board.js",
            "projects.js",
            "inspector.js",
            "conversation.js",
            "tokens.css",
            "workbench.css",
        }
        web_root = self.root / "src/pcbdraft/web"
        self.assertTrue(all((web_root / name).is_file() for name in required))
        parser = _AssetParser()
        parser.feed(self.html)
        for url in parser.urls:
            with self.subTest(url=url):
                self.assertFalse(url.startswith("/"))
                self.assertNotRegex(url, r"^(?:https?:)?//")
        sources = "\n".join(
            self._asset_text(self.root, name)
            for name in required
            if name.endswith(".js")
        )
        self.assertNotIn("cdn", sources.casefold())
        self.assertNotRegex(sources, r"(?:fetch|EventSource)\s*\(\s*[\"']/")
        self.assertIn('from "./api.js"', self.script)
        self.assertIn('from "./board.js"', self.script)

    def test_workbench_exposes_accessible_project_canvas_inspector_and_drawer(
        self,
    ) -> None:
        required_ids = {
            "project-rail",
            "project-search",
            "project-status-filter",
            "project-list",
            "rail-resize",
            "command-button",
            "command-palette",
            "command-search",
            "board-svg",
            "fit-board",
            "zoom-in",
            "zoom-out",
            "layer-preset",
            "inspector-panel",
            "inspector-resize",
            "inspector-tabs",
            "validation-pane",
            "fabrication-pane",
            "agent-drawer",
            "conversation-tab",
            "activity-tab",
            "message-form",
            "status-bar",
        }
        for element_id in required_ids:
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('role="dialog"', self.html)
        self.assertIn('role="tablist"', self.html)
        self.assertIn('role="separator"', self.html)
        self.assertIn("aria-controls=", self.html)
        self.assertIn('type="module"', self.html)
        self.assertIn("panelResize", self.script)

    def test_workbench_preserves_safe_rendering_and_never_projects_private_fields(
        self,
    ) -> None:
        module_names = (
            "app.js",
            "api.js",
            "store.js",
            "i18n.js",
            "commands.js",
            "board.js",
            "projects.js",
            "inspector.js",
            "conversation.js",
        )
        sources = "\n".join(self._asset_text(self.root, name) for name in module_names)
        self.assertNotIn("innerHTML", sources)
        self.assertIn("textContent", sources)
        for forbidden in (
            "debug_trace",
            "tool_arguments",
            "tool_result",
            "chain_of_thought",
            "reasoning_content",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, sources)
        self.assertIn("http:", sources)
        self.assertIn("https:", sources)
        self.assertIn("noopener", sources)

    def test_i18n_command_and_preference_contracts_are_explicit(self) -> None:
        i18n = self._asset_text(self.root, "i18n.js")
        commands = self._asset_text(self.root, "commands.js")
        store = self._asset_text(self.root, "store.js")
        self.assertIn('"zh-CN"', i18n)
        self.assertIn('"en"', i18n)
        self.assertIn("assertDictionaryParity", i18n)
        self.assertIn("validateCommandDefinitions", commands)
        self.assertIn("new Set", commands)
        self.assertIn('event.key !== "Tab"', commands)
        self.assertIn("PREFERENCE_SCHEMA_VERSION", store)
        self.assertIn("ALLOWED_PREFERENCE_KEYS", store)
        for key, chinese, english in (
            ("validation.check.semantics", "语义", "Semantics"),
            ("validation.check.connectivity", "连通性", "Connectivity"),
        ):
            with self.subTest(key=key):
                self.assertIn(f'"{key}": "{chinese}"', i18n)
                self.assertIn(f'"{key}": "{english}"', i18n)
        self.assertIn('id: "layer-preset"', self.script)
        self.assertNotIn("messages", store)
        self.assertNotIn("prompt", store)
        self.assertNotIn("pathname", store)

    def test_desktop_panel_width_preferences_are_not_overridden_at_1366px(
        self,
    ) -> None:
        workbench = self._asset_text(self.root, "workbench.css")
        self.assertIn("var(--rail-width)", workbench)
        self.assertIn("var(--inspector-width)", workbench)
        self.assertNotIn("grid-template-columns: 232px", workbench)

    def test_mobile_overlays_are_transient_exclusive_and_hidden_by_default(
        self,
    ) -> None:
        workbench = self._asset_text(self.root, "workbench.css")
        self.assertIn('const MOBILE_LAYOUT_QUERY = "(max-width: 1179px)";', self.script)
        self.assertIn("function isMobileLayout()", self.script)
        self.assertIn("function currentMobileOverlay()", self.script)
        self.assertIn("function setMobileOverlay(overlay)", self.script)
        self.assertIn(
            'elements.shell.classList.toggle("mobile-rail-open", overlay === "rail");',
            self.script,
        )
        self.assertIn(
            'elements.shell.classList.toggle("mobile-inspector-open", overlay === "inspector");',
            self.script,
        )
        self.assertIn("function togglePanel(panel)", self.script)
        self.assertIn("function collapsePanel(panel)", self.script)
        self.assertIn(
            'elements.railToggle.addEventListener("click", () => togglePanel("rail"));',
            self.script,
        )
        self.assertIn(
            'elements.inspectorToggle.addEventListener("click", () => togglePanel("inspector"));',
            self.script,
        )
        self.assertIn(
            'elements.railCollapse.addEventListener("click", () => collapsePanel("rail"));',
            self.script,
        )
        self.assertIn(
            'elements.inspectorCollapse.addEventListener("click", () => collapsePanel("inspector"));',
            self.script,
        )
        self.assertIn(
            'if (isMobileLayout() && currentMobileOverlay() === "rail") setMobileOverlay(null);',
            self.script,
        )
        self.assertIn("function openInspectorTab(tab)", self.script)
        self.assertIn('openInspectorTab("validation")', self.script)
        self.assertIn('openInspectorTab("fabrication")', self.script)
        self.assertIn(
            'if (event.key === "Escape" && isMobileLayout() && currentMobileOverlay()) {',
            self.script,
        )
        self.assertGreaterEqual(self.script.count("if (isMobileLayout()) {"), 4)

        mobile_start = workbench.index("@media (max-width: 1179px) {")
        mobile_end = workbench.index("@media (max-width: 819px) {")
        mobile_rules = workbench[mobile_start:mobile_end]
        self.assertIn(
            ".project-rail { left: 8px; transform: translateX(calc(-100% - 16px)); }",
            mobile_rules,
        )
        self.assertIn(
            ".inspector-panel { right: 8px; transform: translateX(calc(100% + 16px)); }",
            mobile_rules,
        )
        self.assertIn(
            ".app-shell.mobile-rail-open .project-rail { transform: none; }",
            mobile_rules,
        )
        self.assertIn(
            ".app-shell.mobile-inspector-open .inspector-panel { transform: none; }",
            mobile_rules,
        )
        self.assertNotIn("rail-collapsed", mobile_rules)
        self.assertNotIn("inspector-collapsed", mobile_rules)
        self.assertIn("overflow-x: clip", mobile_rules)
        self.assertIn("@media (prefers-reduced-motion: reduce)", workbench)


if __name__ == "__main__":
    unittest.main()
