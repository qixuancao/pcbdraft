"""Focused checks for extracted terminal signal handling."""

import errno
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from pcbdraft.interfaces.tui.signal_handlers import (
    install_interactive_signal_handlers,
    make_interactive_signal_handler,
    suppress_closed_loop_errors,
)


class _FakeSignals:
    SIGINT = 2
    SIGTERM = 15
    SIGHUP = 1

    def __init__(self) -> None:
        self.handlers: dict[int, object] = {}

    def signal(self, signalnum: int, handler: object) -> None:
        self.handlers[signalnum] = handler


class TuiSignalHandlerTests(unittest.TestCase):
    def test_interactive_installer_registers_posix_shutdown_signals(self) -> None:
        signals = _FakeSignals()
        handler = install_interactive_signal_handlers(
            SimpleNamespace(agent=None),
            arm_exit_watchdog=Mock(),
            logger=logging.getLogger(__name__),
            signal_module=signals,
            platform="linux",
        )

        self.assertIs(signals.handlers[signals.SIGTERM], handler)
        self.assertIs(signals.handlers[signals.SIGHUP], handler)
        self.assertNotIn(signals.SIGINT, signals.handlers)

    def test_interactive_handler_interrupts_agent_then_exits_ui(self) -> None:
        agent = object()
        cli = SimpleNamespace(agent=agent, _agent_running=True)
        arm_watchdog = Mock()
        hard_interrupt = Mock()
        sleep = Mock()
        loop = Mock()
        prompt_app = SimpleNamespace(loop=loop, exit=Mock())
        handler = make_interactive_signal_handler(
            cli,
            arm_exit_watchdog=arm_watchdog,
            logger=logging.getLogger(__name__),
            environ={"PCBDRAFT_RUNTIME_SIGTERM_GRACE": "0.25"},
            sleep=sleep,
            hard_interrupt=hard_interrupt,
            get_app=lambda: prompt_app,
        )

        handler(15, None)

        arm_watchdog.assert_called_once_with()
        hard_interrupt.assert_called_once_with(agent, "received signal 15")
        sleep.assert_called_once_with(0.25)
        loop.call_soon_threadsafe.assert_called_once_with(prompt_app.exit)

    def test_loop_handler_suppresses_teardown_errors_only(self) -> None:
        loop = Mock()

        suppress_closed_loop_errors(loop, {"exception": OSError(errno.EIO, "closed")})
        loop.default_exception_handler.assert_not_called()

        context = {"exception": ValueError("unexpected")}
        suppress_closed_loop_errors(loop, context)
        loop.default_exception_handler.assert_called_once_with(context)
