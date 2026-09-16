import threading
import unittest
from unittest.mock import patch

from pcbdraft.model import auxiliary_cancellation, auxiliary_client


class AuxiliaryCancellationCompatibilityTests(unittest.TestCase):
    def test_legacy_module_reexports_cancellation_runtime(self):
        names = (
            "AuxiliaryExplicitCancellation",
            "_aux_interrupt_protection",
            "_aux_interrupt_protected",
            "_aux_interrupt_cancel_requested",
            "aux_interrupt_protection",
            "_capture_aux_cancel_check",
            "_captured_aux_cancel_requested",
            "_AuxiliaryCancellationDecision",
            "_aux_progress",
            "_notify_aux_progress",
            "_aux_progress_active",
            "aux_progress_hook",
            "_run_protected_sync_provider_call",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(auxiliary_client, name),
                    getattr(auxiliary_cancellation, name),
                )

    def test_legacy_monkeypatch_intercepts_provider_call_site(self):
        sentinel = object()
        callback = lambda request: request
        with patch.object(
            auxiliary_client,
            "_run_protected_sync_provider_call",
            return_value=sentinel,
        ) as protected_call:
            result = auxiliary_client._relay_sync_completion(
                object(), {"model": "test"}, create=callback
            )
        self.assertIs(result, sentinel)
        protected_call.assert_called_once_with(callback, {"model": "test"})


class AuxiliaryCancellationBehaviorTests(unittest.TestCase):
    def test_interrupt_scope_is_reentrant_and_restores_state(self):
        event = threading.Event()
        self.assertFalse(auxiliary_cancellation._aux_interrupt_protected())
        with auxiliary_cancellation.aux_interrupt_protection(cancel_event=event):
            self.assertTrue(auxiliary_cancellation._aux_interrupt_protected())
            self.assertFalse(auxiliary_cancellation._aux_interrupt_cancel_requested())
            event.set()
            self.assertTrue(auxiliary_cancellation._aux_interrupt_cancel_requested())
            with auxiliary_cancellation.aux_interrupt_protection(active=False):
                self.assertFalse(auxiliary_cancellation._aux_interrupt_protected())
                self.assertIsNotNone(auxiliary_cancellation._capture_aux_cancel_check())
            self.assertTrue(auxiliary_cancellation._aux_interrupt_protected())
        self.assertFalse(auxiliary_cancellation._aux_interrupt_protected())
        self.assertIsNone(auxiliary_cancellation._capture_aux_cancel_check())

    def test_cancellation_decision_linearizes_timeout_and_cancel(self):
        cancelled = False

        def source() -> bool:
            return cancelled

        timeout_wins = auxiliary_cancellation._AuxiliaryCancellationDecision(source)
        self.assertTrue(timeout_wins.begin_timeout_cleanup())
        cancelled = True
        self.assertFalse(timeout_wins())

        cancel_wins = auxiliary_cancellation._AuxiliaryCancellationDecision(source)
        self.assertTrue(cancel_wins())
        self.assertFalse(cancel_wins.begin_timeout_cleanup())

    def test_protected_provider_call_propagates_progress_and_explicit_cancel(self):
        progress: list[str] = []
        event = threading.Event()
        with (
            auxiliary_cancellation.aux_progress_hook(lambda: progress.append("tick")),
            auxiliary_cancellation.aux_interrupt_protection(cancel_event=event),
        ):
            result = auxiliary_cancellation._run_protected_sync_provider_call(
                lambda request: (
                    auxiliary_cancellation._notify_aux_progress(),
                    request["value"],
                )[1],
                {"value": 42},
            )
        self.assertEqual(result, 42)
        self.assertEqual(progress, ["tick"])

        event.set()
        with (
            auxiliary_cancellation.aux_interrupt_protection(cancel_event=event),
            self.assertRaises(auxiliary_cancellation.AuxiliaryExplicitCancellation),
        ):
            auxiliary_cancellation._run_protected_sync_provider_call(
                lambda request: request,
                {},
            )


if __name__ == "__main__":
    unittest.main()
