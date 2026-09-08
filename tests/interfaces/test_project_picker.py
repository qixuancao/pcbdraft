"""Focused state, rendering, and controller tests for the PCB project picker."""

from __future__ import annotations

import queue
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pcbdraft.core.errors import ValidationError
from pcbdraft.interfaces.tui.app import TerminalApp
from pcbdraft.interfaces.tui.project_picker import (
    ProjectPickerState,
    _display_width,
    project_picker_query,
    render_project_picker,
)


def _project(index: int, *, name: str | None = None) -> dict:
    return {
        "id": f"board-{index:02d}-{index:08x}",
        "name": name or f"Board {index:02d}",
        "status": "draft" if index % 2 else "validated",
        "created_at": f"2026-08-{index + 1:02d}T08:00:00Z",
        "updated_at": f"2026-09-{index + 1:02d}T09:30:00Z",
    }


def _controller() -> TerminalApp:
    controller = TerminalApp.__new__(TerminalApp)
    controller._agent_running = False
    controller._command_running = False
    controller._pending_input = queue.Queue()
    controller._pending_resume_sessions = None
    controller._project_picker_state = None
    controller._app = SimpleNamespace(
        current_buffer=SimpleNamespace(text="", cursor_position=0)
    )
    controller._capture_modal_input_snapshot = Mock()
    controller._restore_modal_input_snapshot = Mock()
    controller._invalidate = Mock()
    controller.conversation_history = [{"role": "user", "content": "keep me"}]
    return controller


class ProjectPickerDispatchTests(unittest.TestCase):
    def test_pure_command_query_rules(self) -> None:
        self.assertEqual(project_picker_query("resume", " power "), "power")
        self.assertEqual(project_picker_query("projects", " sensor "), "sensor")
        self.assertEqual(project_picker_query("open", ""), "")
        self.assertIsNone(project_picker_query("open", "board-1"))
        self.assertIsNone(project_picker_query("new", "board"))

    def test_registered_commands_and_projects_alias_parse_for_picker(self) -> None:
        controller = _controller()

        self.assertEqual(
            controller._project_picker_query_from_input("/resume power"), "power"
        )
        self.assertEqual(
            controller._project_picker_query_from_input("/projects sensor"), "sensor"
        )
        self.assertEqual(
            controller._project_picker_query_from_input("/pr sensor"), "sensor"
        )
        self.assertEqual(controller._project_picker_query_from_input("/open"), "")
        self.assertIsNone(controller._project_picker_query_from_input("/open board-1"))
        self.assertTrue(
            controller._should_handle_project_command_inline("/open board-1")
        )
        self.assertFalse(
            controller._should_handle_project_command_inline("/resume", has_images=True)
        )

    def test_projects_alias_dispatches_to_the_projects_handler(self) -> None:
        controller = _controller()
        controller._process_builtin_command = Mock(
            side_effect=AssertionError("/pr must not reach legacy built-in dispatch")
        )
        projects_handler = Mock(return_value="matching projects")

        with (
            patch(
                "pcbdraft.interfaces.tui.project_commands.HANDLERS",
                {"projects": projects_handler},
            ),
            patch(
                "pcbdraft.agent.tool_bindings.get_current_project_id",
                return_value="board-current",
            ),
            patch("pcbdraft.interfaces.tui.app._cprint"),
        ):
            self.assertTrue(controller.process_command("/pr sensor"))

        projects_handler.assert_called_once_with("sensor")
        controller._process_builtin_command.assert_not_called()


class ProjectPickerStateAndRenderTests(unittest.TestCase):
    def test_viewport_scrolls_between_top_and_bottom_for_more_than_ten(self) -> None:
        state = ProjectPickerState.create([_project(index) for index in range(15)])

        self.assertEqual(state.viewport(40, lines_per_project=2), (0, 10))
        state.move(11)
        self.assertEqual(state.viewport(40, lines_per_project=2), (2, 10))
        state.move(100)
        self.assertEqual(state.selected_index, 14)
        self.assertEqual(state.viewport(40, lines_per_project=2), (5, 10))
        state.move(-100)
        self.assertEqual(state.selected_index, 0)
        self.assertEqual(state.viewport(40, lines_per_project=2), (0, 10))

    def test_narrow_chinese_render_has_consistent_width_and_bounded_height(
        self,
    ) -> None:
        rows = 16
        state = ProjectPickerState.create(
            [
                _project(
                    index,
                    name="超长中文电源控制板项目名称"
                    if index == 0
                    else f"传感器板{index}",
                )
                for index in range(12)
            ],
            query="中文筛选条件",
            current_project_id=_project(0)["id"],
        )

        fragments = render_project_picker(
            state, terminal_columns=26, terminal_rows=rows
        )
        rendered = "".join(text for _style, text in fragments)
        lines = rendered.splitlines()

        self.assertTrue(lines)
        self.assertEqual({_display_width(line) for line in lines}, {26})
        self.assertLessEqual(len(lines), rows - 3)
        self.assertTrue(lines[0].startswith("╭"))
        self.assertTrue(lines[-1].startswith("╰"))
        self.assertIn("…", rendered)

    def test_empty_matches_render_and_enter_selection_are_safe(self) -> None:
        state = ProjectPickerState.create([], query="missing")

        rendered = "".join(
            text
            for _style, text in render_project_picker(
                state, terminal_columns=50, terminal_rows=20
            )
        )
        self.assertIn("0 matches", rendered)
        self.assertIn("No matching projects", rendered)
        self.assertIsNone(state.selected_project())


class ProjectPickerControllerTests(unittest.TestCase):
    def test_open_reads_once_and_filtering_reuses_the_snapshot(self) -> None:
        controller = _controller()
        all_projects = [_project(0), _project(1), _project(2)]
        initial_matches = [all_projects[1]]
        filtered_matches = [all_projects[2]]

        with (
            patch(
                "pcbdraft.interfaces.tui.project_commands.list_projects",
                return_value=all_projects,
            ) as list_projects,
            patch(
                "pcbdraft.interfaces.tui.project_commands.matching_projects",
                side_effect=[initial_matches, filtered_matches],
            ) as matching_projects,
            patch(
                "pcbdraft.agent.tool_bindings.get_current_project_id",
                return_value=all_projects[0]["id"],
            ),
        ):
            self.assertTrue(controller._open_project_picker("board 1"))
            controller._update_project_picker_query("board 2")
            controller._update_project_picker_query("board 2")

        list_projects.assert_called_once_with()
        self.assertEqual(matching_projects.call_count, 2)
        self.assertIs(
            matching_projects.call_args_list[1].args[1],
            controller._project_picker_state.all_projects,
        )
        self.assertEqual(controller._project_picker_state.projects, filtered_matches)
        self.assertEqual(controller._app.current_buffer.text, "board 1")

    def test_no_matches_enter_and_cancel_preserve_project_and_history(self) -> None:
        controller = _controller()
        history = list(controller.conversation_history)
        controller._project_picker_state = ProjectPickerState.create([], query="none")

        with patch(
            "pcbdraft.interfaces.tui.project_commands.handle_open"
        ) as handle_open:
            self.assertFalse(controller._handle_project_picker_selection())

        handle_open.assert_not_called()
        self.assertIsNotNone(controller._project_picker_state)
        self.assertEqual(controller.conversation_history, history)

        controller._close_project_picker()
        self.assertIsNone(controller._project_picker_state)
        self.assertEqual(controller.conversation_history, history)
        controller._restore_modal_input_snapshot.assert_called_once_with()

    def test_failed_open_keeps_picker_project_and_history(self) -> None:
        controller = _controller()
        selected = _project(1)
        controller._project_picker_state = ProjectPickerState.create([selected])
        history = list(controller.conversation_history)
        controller._close_project_picker = Mock()

        with (
            patch(
                "pcbdraft.agent.tool_bindings.get_current_project_id",
                return_value=_project(0)["id"],
            ),
            patch(
                "pcbdraft.interfaces.tui.project_commands.handle_open",
                side_effect=ValidationError("project not found"),
            ),
            patch(
                "pcbdraft.agent.tool_bindings.set_current_project_id"
            ) as set_current_project_id,
            patch("pcbdraft.interfaces.tui.app._cprint") as rendered,
        ):
            self.assertFalse(controller._handle_project_picker_selection())

        set_current_project_id.assert_not_called()
        controller._close_project_picker.assert_not_called()
        self.assertEqual(controller.conversation_history, history)
        self.assertEqual(controller._project_picker_state.selected_project(), selected)
        self.assertIn("project not found", str(rendered.call_args.args[0]))

    def test_selection_rotates_only_when_project_id_changes(self) -> None:
        selected = _project(1)
        for previous_id, expected_rotations in (
            (_project(0)["id"], 1),
            (selected["id"], 0),
        ):
            with self.subTest(previous_id=previous_id):
                controller = _controller()
                controller._project_picker_state = ProjectPickerState.create([selected])
                controller._close_project_picker = Mock()
                with (
                    patch(
                        "pcbdraft.agent.tool_bindings.get_current_project_id",
                        side_effect=(previous_id, selected["id"]),
                    ),
                    patch(
                        "pcbdraft.interfaces.tui.project_commands.handle_open",
                        return_value="opened",
                    ),
                    patch(
                        "pcbdraft.interfaces.terminal._rotate_project_conversation"
                    ) as rotate,
                    patch("pcbdraft.interfaces.tui.app._cprint"),
                ):
                    self.assertTrue(controller._handle_project_picker_selection())

                self.assertEqual(rotate.call_count, expected_rotations)
                controller._close_project_picker.assert_called_once_with()

    def test_active_or_pending_work_blocks_open_and_confirmation(self) -> None:
        selected = _project(0)
        for busy_kind in ("active", "pending"):
            with self.subTest(busy_kind=busy_kind):
                controller = _controller()
                if busy_kind == "active":
                    controller._agent_running = True
                else:
                    controller._pending_input.put("queued request")
                controller._project_picker_state = ProjectPickerState.create([selected])

                with (
                    patch(
                        "pcbdraft.interfaces.tui.project_commands.list_projects"
                    ) as list_projects,
                    patch(
                        "pcbdraft.interfaces.tui.project_commands.handle_open"
                    ) as handle_open,
                    patch("pcbdraft.interfaces.tui.app._cprint"),
                ):
                    self.assertFalse(controller._open_project_picker())
                    self.assertFalse(controller._handle_project_picker_selection())

                list_projects.assert_not_called()
                handle_open.assert_not_called()

    def test_rotate_failure_rolls_back_trusted_project_and_keeps_picker(self) -> None:
        controller = _controller()
        previous_id = _project(0)["id"]
        selected = _project(1)
        controller._project_picker_state = ProjectPickerState.create([selected])
        controller._close_project_picker = Mock()

        with (
            patch(
                "pcbdraft.agent.tool_bindings.get_current_project_id",
                side_effect=(previous_id, selected["id"]),
            ),
            patch(
                "pcbdraft.agent.tool_bindings.set_current_project_id"
            ) as set_current_project_id,
            patch(
                "pcbdraft.interfaces.tui.project_commands.handle_open",
                return_value="opened",
            ),
            patch(
                "pcbdraft.interfaces.terminal._rotate_project_conversation",
                side_effect=RuntimeError("rotation failed"),
            ),
            patch("pcbdraft.interfaces.tui.app._cprint") as rendered,
        ):
            self.assertFalse(controller._handle_project_picker_selection())

        set_current_project_id.assert_called_once_with(previous_id)
        controller._close_project_picker.assert_not_called()
        self.assertIsNotNone(controller._project_picker_state)
        self.assertIn("rotation failed", str(rendered.call_args.args[0]))


if __name__ == "__main__":
    unittest.main()
