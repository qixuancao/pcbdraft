"""Focused tests for AIAgent interruption, steering, and redirects."""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from pcbdraft.agent import loop
from pcbdraft.agent.loop import AIAgent
from pcbdraft.agent.turn_control import TurnControlMixin


class TurnControlCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)

    def test_agent_inherits_all_extracted_methods(self) -> None:
        self.assertTrue(issubclass(AIAgent, TurnControlMixin))
        for name in (
            "interrupt",
            "hard_interrupt",
            "clear_interrupt",
            "steer",
            "redirect",
            "_has_pending_redirect",
            "_drain_pending_redirect",
            "_drain_pending_steer",
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(AIAgent, name), getattr(TurnControlMixin, name))

    def test_hard_interrupt_keeps_legacy_aiagent_patch_path(self) -> None:
        with patch.object(AIAgent, "interrupt") as interrupt:
            self.agent.hard_interrupt("stop now")
        interrupt.assert_called_once_with(self.agent, "stop now", hard_cancel=True)

    def test_loop_print_and_thread_interrupt_hooks_remain_dynamic(self) -> None:
        self.agent._pending_redirect_lock = None
        self.agent._pending_redirect = None
        self.agent._hard_interrupt_requested = threading.Event()
        self.agent.api_mode = "chat_completions"
        self.agent._execution_thread_id = 11
        self.agent._tool_worker_threads = set()
        self.agent._tool_worker_threads_lock = threading.Lock()
        self.agent._active_children = set()
        self.agent._active_children_lock = threading.Lock()
        self.agent.quiet_mode = False

        with (
            patch.object(loop, "_set_interrupt") as set_interrupt,
            patch.object(loop, "print", create=True) as print_notice,
        ):
            self.agent.interrupt("updated request")

        set_interrupt.assert_called_once_with(True, 11)
        print_notice.assert_called_once()
        self.assertIn("updated request", print_notice.call_args.args[0])

    def test_clear_interrupt_uses_legacy_loop_event_factory(self) -> None:
        self.agent._pending_redirect_lock = None
        self.agent._pending_redirect = None
        self.agent._interrupt_requested = True
        self.agent._interrupt_message = "stop"
        self.agent._execution_thread_id = None
        self.agent._tool_worker_threads = None
        self.agent._tool_worker_threads_lock = None
        self.agent._pending_steer_lock = None
        fake_event = MagicMock()

        with patch.object(loop.threading, "Event", return_value=fake_event) as factory:
            self.assertTrue(self.agent.clear_interrupt())

        factory.assert_called_once_with()
        fake_event.clear.assert_called_once_with()


class TurnControlBehaviorTests(unittest.TestCase):
    def _agent(self) -> AIAgent:
        agent = object.__new__(AIAgent)
        agent._interrupt_requested = False
        agent._interrupt_message = None
        agent._hard_interrupt_requested = threading.Event()
        agent._pending_redirect = None
        agent._pending_redirect_lock = threading.RLock()
        agent._pending_steer = None
        agent._pending_steer_lock = threading.Lock()
        agent._interrupt_thread_signal_pending = False
        agent._execution_thread_id = 101
        agent._tool_worker_threads = {201, 202}
        agent._tool_worker_threads_lock = threading.Lock()
        agent._active_children = set()
        agent._active_children_lock = threading.Lock()
        agent._active_request_abort = MagicMock()
        agent._executing_tools = False
        agent._model_request_active = threading.Event()
        agent.api_mode = "chat_completions"
        agent.quiet_mode = True
        return agent

    def test_interrupt_sets_flags_aborts_request_and_propagates_to_workers(
        self,
    ) -> None:
        agent = self._agent()
        child = SimpleNamespace(interrupt=MagicMock())
        agent._active_children = [child]

        with patch.object(loop, "_set_interrupt") as set_interrupt:
            agent.interrupt("new input")

        self.assertTrue(agent._interrupt_requested)
        self.assertEqual(agent._interrupt_message, "new input")
        self.assertFalse(agent._interrupt_thread_signal_pending)
        agent._active_request_abort.assert_called_once_with("interrupt_abort")
        self.assertCountEqual(
            set_interrupt.call_args_list,
            [call(True, 101), call(True, 201), call(True, 202)],
        )
        child.interrupt.assert_called_once_with("new input")

    def test_hard_interrupt_sets_event_and_uses_child_compatibility_helper(
        self,
    ) -> None:
        agent = self._agent()
        child = object()
        agent._active_children.add(child)

        with (
            patch.object(loop, "_set_interrupt"),
            patch.object(loop, "request_hard_interrupt") as child_hard_interrupt,
        ):
            agent.interrupt("stop", hard_cancel=True)

        self.assertTrue(agent._hard_interrupt_requested.is_set())
        child_hard_interrupt.assert_called_once_with(child, "stop")

    def test_clear_interrupt_preserves_redirect_and_clears_steer_and_signals(
        self,
    ) -> None:
        agent = self._agent()
        agent._interrupt_requested = True
        agent._interrupt_message = "correction"
        agent._pending_redirect = "redirect text"
        agent._pending_steer = "stale steer"
        agent._hard_interrupt_requested.set()

        with patch.object(loop, "_set_interrupt") as set_interrupt:
            self.assertTrue(agent.clear_interrupt(preserve_redirect=True))

        self.assertFalse(agent._interrupt_requested)
        self.assertIsNone(agent._interrupt_message)
        self.assertEqual(agent._pending_redirect, "redirect text")
        self.assertIsNone(agent._pending_steer)
        self.assertFalse(agent._hard_interrupt_requested.is_set())
        self.assertCountEqual(
            set_interrupt.call_args_list,
            [call(False, 101), call(False, 201), call(False, 202)],
        )

        agent._pending_redirect = None
        agent._interrupt_requested = True
        self.assertFalse(agent.clear_interrupt(preserve_redirect=True))
        self.assertTrue(agent._interrupt_requested)

    def test_steer_concatenates_and_drains_under_lock(self) -> None:
        agent = self._agent()

        self.assertFalse(agent.steer("   "))
        self.assertTrue(agent.steer(" first "))
        self.assertTrue(agent.steer("second"))
        self.assertEqual(agent._drain_pending_steer(), "first\nsecond")
        self.assertIsNone(agent._drain_pending_steer())

    def test_redirect_interrupts_only_active_model_request_and_drains(self) -> None:
        agent = self._agent()
        self.assertFalse(agent.redirect("correction"))

        agent._model_request_active.set()
        with patch.object(loop, "_set_interrupt") as set_interrupt:
            self.assertTrue(agent.redirect(" first correction "))
            self.assertTrue(agent.redirect("second correction"))

        self.assertTrue(agent._interrupt_requested)
        self.assertIsNone(agent._interrupt_message)
        self.assertTrue(agent._has_pending_redirect())
        self.assertEqual(
            agent._drain_pending_redirect(),
            "first correction\n\n[Additional user correction]\nsecond correction",
        )
        self.assertFalse(agent._has_pending_redirect())
        self.assertEqual(set_interrupt.call_count, 2)
        self.assertEqual(
            agent._active_request_abort.call_args_list,
            [call("redirect_abort"), call("redirect_abort")],
        )

    def test_redirect_during_tool_execution_degrades_to_steer(self) -> None:
        agent = self._agent()
        agent._executing_tools = True

        self.assertTrue(agent.redirect("guide the next step"))

        self.assertEqual(agent._drain_pending_steer(), "guide the next step")
        self.assertFalse(agent._interrupt_requested)
        agent._active_request_abort.assert_not_called()

    def test_codex_redirect_uses_native_turn_steer(self) -> None:
        agent = self._agent()
        agent.api_mode = "codex_app_server"
        agent._codex_session = SimpleNamespace(
            request_steer=MagicMock(return_value=True)
        )

        self.assertTrue(agent.redirect("native correction"))

        agent._codex_session.request_steer.assert_called_once_with("native correction")
        agent._active_request_abort.assert_not_called()


if __name__ == "__main__":
    unittest.main()
