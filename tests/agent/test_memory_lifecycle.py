"""Focused tests for the AIAgent external-memory lifecycle mixin."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, call, patch

from pcbdraft.agent import loop
from pcbdraft.agent.loop import AIAgent
from pcbdraft.agent.memory_lifecycle import MemoryLifecycleMixin


class MemoryLifecycleCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)
        self.agent.session_id = "session-1"
        self.agent._memory_provider_shutdown = False
        self.agent._memory_manager = MagicMock()
        self.agent.context_compressor = MagicMock()

    def test_agent_inherits_extracted_methods_without_wrappers(self) -> None:
        self.assertTrue(issubclass(AIAgent, MemoryLifecycleMixin))
        for name in (
            "shutdown_memory_provider",
            "commit_memory_session",
            "_sync_external_memory_for_turn",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(AIAgent, name), getattr(MemoryLifecycleMixin, name)
                )

    def test_legacy_logger_patch_path_handles_shutdown_failure(self) -> None:
        error = RuntimeError("offline")
        messages = [{"role": "user", "content": "hello"}]
        self.agent._memory_manager.on_session_end.side_effect = error

        with patch.object(loop.logger, "warning") as warning:
            self.agent.shutdown_memory_provider(messages)
            self.agent.shutdown_memory_provider(messages)

        self.agent._memory_manager.on_session_end.assert_called_once_with(messages)
        self.agent._memory_manager.shutdown_all.assert_called_once_with()
        self.agent.context_compressor.on_session_end.assert_called_once_with(
            "session-1", messages
        )
        warning.assert_called_once_with(
            "Memory provider on_session_end failed during shutdown: %s",
            error,
            exc_info=True,
        )


class MemoryLifecycleBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AIAgent)
        self.agent.session_id = "session-1"
        self.agent._memory_provider_shutdown = False
        self.agent._memory_manager = MagicMock()
        self.agent.context_compressor = MagicMock()

    def test_commit_flushes_memory_and_context_without_shutdown(self) -> None:
        messages = [{"role": "assistant", "content": "done"}]

        self.agent.commit_memory_session(messages)

        self.agent._memory_manager.on_session_end.assert_called_once_with(messages)
        self.agent._memory_manager.shutdown_all.assert_not_called()
        self.agent.context_compressor.on_session_end.assert_called_once_with(
            "session-1", messages
        )
        self.assertFalse(self.agent._memory_provider_shutdown)

    def test_completed_turn_sync_uses_legacy_dependency_patch_paths(self) -> None:
        messages = [{"role": "user", "content": "remember this"}]
        with (
            patch.object(
                loop,
                "_summarize_user_message_for_log",
                side_effect=["user text", "assistant text"],
            ) as summarize,
            patch.object(loop, "is_trivial_prompt", return_value=False) as trivial,
        ):
            self.agent._sync_external_memory_for_turn(
                original_user_message=[{"type": "text", "text": "user text"}],
                final_response="assistant text",
                interrupted=False,
                messages=messages,
            )

        self.assertEqual(
            summarize.call_args_list,
            [
                call([{"type": "text", "text": "user text"}], sep="\n"),
                call("assistant text", sep="\n"),
            ],
        )
        self.agent._memory_manager.sync_all.assert_called_once_with(
            "user text",
            "assistant text",
            session_id="session-1",
            messages=messages,
        )
        trivial.assert_called_once_with("user text")
        self.agent._memory_manager.queue_prefetch_all.assert_called_once_with(
            "user text", session_id="session-1"
        )

    def test_trivial_turn_is_synced_without_prefetch(self) -> None:
        with (
            patch.object(
                loop,
                "_summarize_user_message_for_log",
                side_effect=["thanks", "you are welcome"],
            ),
            patch.object(loop, "is_trivial_prompt", return_value=True),
        ):
            self.agent._sync_external_memory_for_turn(
                original_user_message="thanks",
                final_response="you are welcome",
                interrupted=False,
            )

        self.agent._memory_manager.sync_all.assert_called_once_with(
            "thanks", "you are welcome", session_id="session-1"
        )
        self.agent._memory_manager.queue_prefetch_all.assert_not_called()

    def test_interrupted_turn_skips_summarize_sync_and_prefetch(self) -> None:
        with patch.object(loop, "_summarize_user_message_for_log") as summarize:
            self.agent._sync_external_memory_for_turn(
                original_user_message="user",
                final_response="partial",
                interrupted=True,
            )

        summarize.assert_not_called()
        self.agent._memory_manager.sync_all.assert_not_called()
        self.agent._memory_manager.queue_prefetch_all.assert_not_called()

    def test_provider_sync_failure_remains_best_effort(self) -> None:
        self.agent._memory_manager.sync_all.side_effect = RuntimeError("offline")
        with (
            patch.object(
                loop,
                "_summarize_user_message_for_log",
                side_effect=["user", "assistant"],
            ),
            patch.object(loop, "is_trivial_prompt") as trivial,
        ):
            self.agent._sync_external_memory_for_turn(
                original_user_message="user",
                final_response="assistant",
                interrupted=False,
            )

        trivial.assert_not_called()
        self.agent._memory_manager.queue_prefetch_all.assert_not_called()


if __name__ == "__main__":
    unittest.main()
