"""Offline identity and persisted-data migration contracts for the native TUI."""

from __future__ import annotations

import ast
import asyncio
import io
import json
import os
import re
import shlex
import sqlite3
import sys
import tempfile
import tomllib
import unittest
from contextlib import closing, redirect_stdout
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

TUI = Path(__file__).resolve().parents[2] / "src/pcbdraft/interfaces/tui"
EXCLUDED = frozenset(
    "main.py _parser.py _startup_fast.py _early_recovery.py _install_repair.py "  # noqa: SIM905 - owner boundary
    "relaunch.py linux_desktop_entry.py windows_ssh_runtime.py gateway.py "
    "gateway_windows.py service_manager.py container_boot.py dashboard_procs.py "
    "uninstall.py gui_uninstall.py update_cmd.py update_lock.py managed_uv.py "
    "completion.py config_defaults.py config_migrations.py profiles.py "
    "project_commands.py".split()
)


class NativeTUIIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temporary)
        self.enterContext(
            patch.dict(os.environ, {"PCBDRAFT_RUNTIME_HOME": str(self.root)})
        )
        # Any accidental HTTP or subprocess network call is a test failure.
        self.enterContext(
            patch("socket.create_connection", side_effect=AssertionError("network"))
        )

    def test_factory_soul_migrates_only_exact_factory_content(self) -> None:
        from pcbdraft.interfaces.tui import default_soul as soul

        self.assertIn("You are PCBDraft", soul.DEFAULT_SOUL_MD)
        self.assertIn("KiCad", soul.DEFAULT_SOUL_MD)
        self.assertNotIn("Nous Research", soul.DEFAULT_SOUL_MD)
        self.assertTrue(soul.is_legacy_template_soul(soul._LEGACY_FACTORY_SOUL))
        for scaffold in soul._LEGACY_TEMPLATE_SOULS:
            legacy = scaffold.replace("PCBDraft", "Hermes")
            self.assertTrue(
                soul.is_legacy_template_soul("\ufeff" + legacy.replace("\n", "\r\n"))
            )
            self.assertFalse(
                soul.is_legacy_template_soul(legacy + "\nMy custom persona")
            )
        self.assertFalse(
            soul.is_legacy_template_soul(soul._LEGACY_FACTORY_SOUL + " Be brief.")
        )

    def test_locales_have_native_brand_and_matching_placeholders(self) -> None:
        import yaml

        def flatten(value, prefix=""):
            if isinstance(value, dict):
                return {
                    name: item
                    for key, child in value.items()
                    for name, item in flatten(child, f"{prefix}.{key}").items()
                }
            return {prefix: value}

        catalogues = {}
        paths = sorted((TUI / "locales").glob("*.yaml"))
        self.assertEqual(len(paths), 17)
        for path in paths:
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?i)hermes")
            self.assertNotRegex(
                text, r"pcbdraft (?:tools|gateway|update|model|debug|kanban)\b"
            )
            catalogues[path.stem] = flatten(yaml.safe_load(text))
        for language, catalogue in catalogues.items():
            self.assertEqual(set(catalogue), set(catalogues["en"]), language)
            for key, value in catalogue.items():
                if isinstance(value, str):
                    self.assertEqual(
                        set(re.findall(r"\{[^{}]+\}", value)),
                        set(re.findall(r"\{[^{}]+\}", catalogues["en"][key])),
                        f"{language}:{key}",
                    )

    def test_owned_python_identifiers_are_native(self) -> None:
        for path in TUI.rglob("*.py"):
            if path.name in EXCLUDED:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    names.append(node.name)
                elif isinstance(node, ast.Name):
                    names.append(node.id)
                elif isinstance(node, ast.Attribute):
                    names.append(node.attr)
                elif isinstance(node, ast.arg):
                    names.append(node.arg)
                elif isinstance(node, ast.alias):
                    names.extend([node.name, node.asname or ""])
                self.assertFalse(
                    any("hermes" in name.lower() for name in names),
                    f"{path}:{node.lineno if names else 0}",
                )

    def test_session_html_is_native_escaped_and_offline(self) -> None:
        from pcbdraft.interfaces.tui.session_export_html import (
            generate_multi_session_html_export,
        )

        document = generate_multi_session_html_export(
            [
                {
                    "id": "session-1",
                    "title": "<unsafe>",
                    "messages": [
                        {"role": "user", "content": "<script>alert(1)</script>"},
                        {"role": "assistant", "content": "PCB ready"},
                    ],
                }
            ]
        )
        self.assertIn("PCBDraft", document)
        self.assertNotIn("Hermes", document)
        self.assertNotIn("fonts.googleapis.com", document)
        self.assertNotIn("<script>alert(1)</script>", document)

    def test_updates_and_unconfigured_index_are_offline(self) -> None:
        from pcbdraft.interfaces.tui import banner, plugin_index

        with patch("subprocess.run", side_effect=AssertionError("subprocess")):
            self.assertIsNone(banner.check_for_updates())
        with (
            patch.object(plugin_index, "get_index_url", return_value=None),
            patch.object(
                plugin_index, "_fetch_remote", side_effect=AssertionError("index fetch")
            ),
            patch.object(
                plugin_index, "_read_cache", side_effect=AssertionError("old cache")
            ),
        ):
            self.assertEqual(plugin_index.load_index(refresh=True), ([], "none"))

    def test_managed_bot_requires_explicit_service(self) -> None:
        from pcbdraft.interfaces.tui.telegram_managed_bot import _api_url

        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(ValueError, "TELEGRAM_ONBOARDING_URL"),
        ):
            _api_url()
        self.assertEqual(
            _api_url("https://example.test/onboarding/"),
            "https://example.test/onboarding",
        )

    def test_public_guidance_uses_real_cli_commands(self) -> None:
        from pcbdraft.interfaces.cli import build_parser
        from pcbdraft.interfaces.tui.commands import resolve_command
        from pcbdraft.interfaces.tui.setup import print_noninteractive_setup_guidance

        parser = build_parser(prog="pcbdraft")
        for argv in (["connect", "--no-browser"], ["doctor"], ["setup"]):
            self.assertEqual(parser.parse_args(argv).command, argv[0])
        for retired in ("update", "gateway", "tools", "plugins", "skills"):
            self.assertIsNone(resolve_command(retired))
        output = io.StringIO()
        with redirect_stdout(output):
            print_noninteractive_setup_guidance()
        text = output.getvalue()
        self.assertIn("pcbdraft connect --no-browser", text)
        self.assertIn("pcbdraft doctor", text)
        self.assertIn("pcbdraft --help", text)
        self.assertNotIn("--help.", text)

    def test_history_copy_preserves_legacy_and_existing_native(self) -> None:
        from prompt_toolkit.history import FileHistory

        from pcbdraft.interfaces.tui.history_migration import (
            NativeFileHistory,
            migrate_history_file,
        )

        legacy = self.root / ".hermes_history"
        native = self.root / ".pcbdraft_history"
        legacy.write_bytes(b"# old timestamp\n+old prompt\n")
        # An existing native file may have been created after a failed import.
        FileHistory(str(native)).store_string("native prompt")
        with patch("os.link", side_effect=OSError("hard links unsupported")) as link:
            self.assertTrue(migrate_history_file(native))
            link.assert_not_called()
        history = NativeFileHistory(str(native))
        self.assertEqual(
            history.load_history_strings(), ["native prompt", "old prompt"]
        )
        history.store_string("next native prompt")
        self.assertTrue(migrate_history_file(native))
        self.assertEqual(
            history.load_history_strings(),
            ["next native prompt", "native prompt", "old prompt"],
        )
        self.assertEqual(legacy.read_bytes(), b"# old timestamp\n+old prompt\n")

    def test_history_failed_publication_remains_readable_and_retries_after_append(
        self,
    ) -> None:
        from prompt_toolkit.history import FileHistory

        from pcbdraft.interfaces.tui.history_migration import (
            NativeFileHistory,
            migrate_history_file,
        )

        legacy = self.root / ".hermes_history"
        native = self.root / ".pcbdraft_history"
        legacy.write_bytes(b"# legacy\n+legacy prompt\n")
        history = NativeFileHistory(str(native))
        with (
            patch("os.replace", side_effect=PermissionError("publication failed")),
            self.assertLogs(
                "pcbdraft.interfaces.tui.history_migration", level="WARNING"
            ),
        ):
            self.assertFalse(migrate_history_file(native))
            self.assertFalse(native.exists())
            # A pre-fix process can create native history even while snapshot
            # publication fails. Its existence must not suppress legacy reads.
            FileHistory(str(native)).store_string("new prompt while pending")
            self.assertTrue(native.exists())
            self.assertTrue(history.migration_pending)
            self.assertEqual(
                history.load_history_strings(),
                ["new prompt while pending", "legacy prompt"],
            )
            restarted = NativeFileHistory(str(native))
            self.assertEqual(
                restarted.load_history_strings(),
                ["new prompt while pending", "legacy prompt"],
            )
            self.assertTrue(restarted.migration_pending)
        self.assertEqual(
            restarted.load_history_strings(),
            ["new prompt while pending", "legacy prompt"],
        )
        self.assertFalse(restarted.migration_pending)
        self.assertTrue(migrate_history_file(native))
        self.assertEqual(
            restarted.load_history_strings(),
            ["new prompt while pending", "legacy prompt"],
        )
        self.assertEqual(legacy.read_bytes(), b"# legacy\n+legacy prompt\n")

    def test_history_previous_unmarked_copy_is_not_imported_twice(self) -> None:
        from prompt_toolkit.history import FileHistory

        from pcbdraft.interfaces.tui.history_migration import NativeFileHistory

        legacy = self.root / ".hermes_history"
        native = self.root / ".pcbdraft_history"
        legacy.write_bytes(b"# previous version\n+legacy prompt\n")
        native.write_bytes(legacy.read_bytes())
        FileHistory(str(native)).store_string("newer prompt")
        history = NativeFileHistory(str(native))
        self.assertEqual(
            history.load_history_strings(), ["newer prompt", "legacy prompt"]
        )
        self.assertFalse(history.migration_pending)

    def test_history_concurrent_native_writes_share_migration_lock(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        from pcbdraft.interfaces.tui.history_migration import NativeFileHistory

        legacy = self.root / ".hermes_history"
        native = self.root / ".pcbdraft_history"
        legacy.write_bytes(b"# legacy\n+legacy prompt\n")
        start = Barrier(2)

        def write(prompt):
            start.wait(timeout=5)
            history = NativeFileHistory(str(native))
            history.store_string(prompt)
            self.assertFalse(history.migration_pending)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(write, ["first prompt", "second prompt"]))
        self.assertCountEqual(
            NativeFileHistory(str(native)).load_history_strings(),
            ["first prompt", "second prompt", "legacy prompt"],
        )

    def test_history_lock_timeout_then_second_store_flushes_both_in_order(self) -> None:
        from prompt_toolkit.history import FileHistory

        from pcbdraft.core.errors import PCBDraftError
        from pcbdraft.interfaces.tui.history_migration import NativeFileHistory

        native = self.root / ".pcbdraft_history"
        FileHistory(str(native)).store_string("existing prompt")
        history = NativeFileHistory(str(native))
        history.load_history_strings()
        self.assertFalse(history.migration_pending)
        with (
            patch(
                "pcbdraft.interfaces.tui.history_migration.ResourceLock.acquire",
                side_effect=PCBDraftError("resource is locked: timeout"),
            ),
            self.assertLogs(
                "pcbdraft.interfaces.tui.history_migration", level="WARNING"
            ),
        ):
            history.store_string("first prompt")
        self.assertTrue(history.append_pending)
        self.assertFalse(history.migration_pending)
        self.assertEqual(
            list(FileHistory(str(native)).load_history_strings()), ["existing prompt"]
        )

        history.store_string("second prompt")
        self.assertFalse(history.append_pending)
        self.assertFalse(history.migration_pending)
        self.assertEqual(
            list(reversed(list(FileHistory(str(native)).load_history_strings()))),
            ["existing prompt", "first prompt", "second prompt"],
        )
        committed = native.read_bytes()
        history.flush_pending()
        history.load_history_strings()
        self.assertEqual(native.read_bytes(), committed)

    def test_history_io_error_keeps_ordered_queue_until_retry_commits_once(
        self,
    ) -> None:
        from prompt_toolkit.history import FileHistory

        from pcbdraft.interfaces.tui.history_migration import NativeFileHistory

        native = self.root / ".pcbdraft_history"
        FileHistory(str(native)).store_string("existing prompt")
        history = NativeFileHistory(str(native))
        history.load_history_strings()
        original = native.read_bytes()
        with (
            patch("os.replace", side_effect=OSError("append publication failed")),
            self.assertLogs(
                "pcbdraft.interfaces.tui.history_migration", level="WARNING"
            ),
        ):
            history.store_string("first prompt\nsecond line")
            history.store_string("second prompt")
            self.assertTrue(history.append_pending)
            self.assertFalse(history.migration_pending)
            self.assertEqual(native.read_bytes(), original)
            self.assertEqual(
                history.load_history_strings(),
                ["second prompt", "first prompt\nsecond line", "existing prompt"],
            )
        history.flush_pending()
        self.assertFalse(history.append_pending)
        self.assertFalse(history.migration_pending)
        self.assertEqual(
            list(reversed(list(FileHistory(str(native)).load_history_strings()))),
            ["existing prompt", "first prompt\nsecond line", "second prompt"],
        )
        committed = native.read_bytes()
        history.flush_pending()
        self.assertEqual(native.read_bytes(), committed)

    def test_terminal_run_exit_flushes_last_failed_input_without_reloading_history(
        self,
    ) -> None:
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.widgets import TextArea

        from pcbdraft.core.errors import PCBDraftError
        from pcbdraft.interfaces import terminal
        from pcbdraft.interfaces.tui.app import TerminalApp
        from pcbdraft.interfaces.tui.history_migration import NativeFileHistory
        from pcbdraft.services.provider_connection import ConnectionOptions

        terminal._take_deferred_connection()
        self.addCleanup(terminal._take_deferred_connection)
        for exit_kind in ("eof", "return", "deferred-connect", "error", "relaunch"):
            with self.subTest(exit_kind=exit_kind):
                native = self.root / f"history-{exit_kind}"
                history = NativeFileHistory(str(native))
                input_area = TextArea(history=history, multiline=True)

                async def warm_cache(history=history):
                    return [item async for item in history.load()]

                self.assertEqual(asyncio.run(warm_cache()), [])
                self.assertTrue(history._loaded)
                cli = TerminalApp.__new__(TerminalApp)
                request = ConnectionOptions(no_browser=True)

                def interact(
                    instance,
                    input_area=input_area,
                    history=history,
                    exit_kind=exit_kind,
                    request=request,
                ):
                    # Replace only the interactive driver. Use a real TextArea,
                    # Buffer submission and history backend inside production
                    # TerminalApp.run(); its finally is not mocked or extracted.
                    instance._input_history = input_area.buffer.history
                    self.assertIs(instance._input_history, history)
                    with (
                        patch(
                            "pcbdraft.interfaces.tui.history_migration.ResourceLock.acquire",
                            side_effect=PCBDraftError("last append lock timeout"),
                        ),
                        self.assertLogs(
                            "pcbdraft.interfaces.tui.history_migration", level="WARNING"
                        ),
                    ):
                        input_area.buffer.text = "last prompt before exit"
                        input_area.buffer.append_to_history()
                    self.assertTrue(history.append_pending)
                    # Storage has recovered. Exit immediately: no second store,
                    # explicit helper flush, or uncached History.load follows.
                    if exit_kind == "eof":
                        raise EOFError
                    if exit_kind == "error":
                        raise RuntimeError("interactive driver failed")
                    if exit_kind == "deferred-connect":
                        terminal._defer_connection(request)
                    if exit_kind == "relaunch":
                        instance._pending_relaunch = ["pcbdraft", "--help"]

                def after_close(*_args, native=native, **_kwargs):
                    self.assertEqual(
                        list(FileHistory(str(native)).load_history_strings()),
                        ["last prompt before exit"],
                    )

                with (
                    patch.object(TerminalApp, "_run_interactive", new=interact),
                    patch.object(
                        history,
                        "load_history_strings",
                        side_effect=AssertionError("cached history reread"),
                    ),
                    patch.object(
                        history, "flush_pending", wraps=history.flush_pending
                    ) as flush,
                    patch(
                        "pcbdraft.interfaces.tui.relaunch.relaunch",
                        side_effect=after_close,
                    ) as relaunch,
                ):
                    if exit_kind in {"eof", "error"}:
                        error = EOFError if exit_kind == "eof" else RuntimeError
                        with self.assertRaises(error):
                            cli.run()
                    else:
                        cli.run()
                    # One failed submission, then exactly one exit flush.
                    self.assertEqual(flush.call_count, 2)
                    self.assertEqual(relaunch.call_count, int(exit_kind == "relaunch"))
                self.assertFalse(history.append_pending)
                after_close()
                deferred = terminal._take_deferred_connection()
                self.assertEqual(
                    deferred, request if exit_kind == "deferred-connect" else None
                )

    def test_terminal_run_bounds_final_history_flush(self) -> None:
        from threading import Event
        from time import monotonic

        from pcbdraft.interfaces.tui.app import TerminalApp
        from pcbdraft.interfaces.tui.history_migration import NativeFileHistory

        history = NativeFileHistory(str(self.root / "bounded-history"))
        cli = TerminalApp.__new__(TerminalApp)
        cli._input_history = history
        entered, release, finished = Event(), Event(), Event()

        def blocked_flush():
            entered.set()
            try:
                release.wait(5)
            finally:
                finished.set()

        with (
            patch.object(TerminalApp, "_run_interactive", return_value=None),
            patch.object(history, "flush_pending", side_effect=blocked_flush) as flush,
        ):
            try:
                started = monotonic()
                cli.run()
                elapsed = monotonic() - started
                self.assertTrue(entered.is_set())
                self.assertFalse(finished.is_set())
                self.assertLess(elapsed, 2.5)
                flush.assert_called_once_with()
            finally:
                release.set()
                self.assertTrue(finished.wait(2))

    def test_cookie_read_compatibility_native_write_and_logout(self) -> None:
        from starlette.requests import Request
        from starlette.responses import Response

        from pcbdraft.interfaces.tui.dashboard_auth import cookies

        for variant in ("", "__Host-", "__Secure-"):
            request = Request(
                {
                    "type": "http",
                    "headers": [
                        (
                            b"cookie",
                            f"{variant}hermes_session_at=old-at; {variant}hermes_session_rt=old-rt".encode(),
                        )
                    ],
                }
            )
            self.assertEqual(
                cookies.read_session_cookies(request), ("old-at", "old-rt")
            )
        response = Response()
        cookies.set_session_cookies(
            response,
            access_token="new-at",  # noqa: S106 - opaque cookie fixture
            refresh_token="new-rt",  # noqa: S106 - opaque cookie fixtures
            access_token_expires_in=60,
            use_https=True,
            prefix="/pcb",
            provider="custom",
        )
        headers = response.headers.getlist("set-cookie")
        self.assertTrue(
            all("__Secure-pcbdraft_session_" in header for header in headers)
        )
        self.assertTrue(all("hermes" not in header for header in headers))
        logout = Response()
        cookies.clear_session_cookies(logout, prefix="/pcb")
        deleted = logout.headers.getlist("set-cookie")
        self.assertEqual(len(deleted), 18)
        self.assertTrue(
            all("Max-Age=0" in header and "Path=/pcb" in header for header in deleted)
        )

    def test_new_cookie_family_never_uses_old_refresh_token(self) -> None:
        from starlette.requests import Request

        from pcbdraft.interfaces.tui.dashboard_auth.cookies import read_session_cookies

        request = Request(
            {
                "type": "http",
                "headers": [
                    (
                        b"cookie",
                        b"pcbdraft_session_at=new; hermes_session_rt=old-account",
                    )
                ],
            }
        )
        self.assertEqual(read_session_cookies(request), ("new", None))

    def test_provider_hint_backfill_keeps_legacy_session_on_next_request(self) -> None:
        from starlette.requests import Request
        from starlette.responses import Response

        from pcbdraft.interfaces.tui.dashboard_auth import cookies, middleware

        session = SimpleNamespace(provider="custom")
        provider = SimpleNamespace(verify_session=Mock(return_value=session))

        async def call_next(request):
            self.assertIs(request.state.session, session)
            return Response("authenticated")

        for variant in ("", "__Host-", "__Secure-"):
            with self.subTest(variant=variant):
                jar = SimpleCookie()
                jar[f"{variant}hermes_session_at"] = "legacy-at"
                jar[f"{variant}hermes_session_rt"] = "legacy-rt"

                def request(jar=jar):
                    return Request(
                        {
                            "type": "http",
                            "scheme": "https",
                            "path": "/private",
                            "server": ("example.test", 443),
                            "app": SimpleNamespace(
                                state=SimpleNamespace(auth_required=True)
                            ),
                            "headers": [
                                (b"cookie", jar.output(header="", sep=";").encode())
                            ],
                        }
                    )

                with (
                    patch.object(middleware, "_path_is_public", return_value=False),
                    patch.object(
                        middleware,
                        "_ordered_session_providers",
                        return_value=[provider],
                    ),
                    patch.object(
                        middleware,
                        "_auto_sso_response",
                        side_effect=AssertionError("lost session"),
                    ),
                ):
                    first = asyncio.run(
                        middleware.gated_auth_middleware(request(), call_next)
                    )
                    for header in first.headers.getlist("set-cookie"):
                        jar.load(header)
                    self.assertIn("__Host-pcbdraft_session_provider", jar)
                    self.assertEqual(
                        cookies.read_session_cookies(request()),
                        ("legacy-at", "legacy-rt"),
                    )
                    self.assertEqual(cookies.read_session_provider(request()), "custom")
                    second = asyncio.run(
                        middleware.gated_auth_middleware(request(), call_next)
                    )
                    self.assertEqual(second.status_code, 200)

    def test_native_refresh_alone_does_not_reuse_legacy_access_or_provider(
        self,
    ) -> None:
        from starlette.requests import Request

        from pcbdraft.interfaces.tui.dashboard_auth import cookies

        request = Request(
            {
                "type": "http",
                "headers": [
                    (
                        b"cookie",
                        b"pcbdraft_session_rt=new; hermes_session_at=old; hermes_session_provider=old-provider",
                    )
                ],
            }
        )
        self.assertEqual(cookies.read_session_cookies(request), (None, "new"))
        self.assertIsNone(cookies.read_session_provider(request))

    def test_codex_legacy_managed_block_migrates_idempotently(self) -> None:
        from pcbdraft.interfaces.tui import codex_runtime_plugin_migration as migration

        target = self.root / "config.toml"
        target.write_text(
            'model = "user-model"\n'
            + migration.LEGACY_MIGRATION_MARKER
            + '\n[mcp_servers.hermes-tools]\ncommand = "old-python"\n'
            + migration.LEGACY_MIGRATION_END_MARKER
            + "\n[features]\nuser_feature = true\n"
        )
        first = migration.migrate({}, codex_home=self.root, discover_plugins=False)
        self.assertTrue(first.written, first.errors)
        native_text = target.read_text()
        config = tomllib.loads(native_text)
        self.assertNotIn("hermes-tools", config["mcp_servers"])
        self.assertEqual(
            config["mcp_servers"]["pcbdraft-tools"]["args"],
            ["-m", "pcbdraft.model.transports.tools_mcp_server"],
        )
        self.assertEqual(config["model"], "user-model")
        self.assertEqual(config["features"], {"user_feature": True})
        second = migration.migrate({}, codex_home=self.root, discover_plugins=False)
        self.assertTrue(second.written, second.errors)
        self.assertEqual(target.read_text(), native_text)

    def test_verify_evidence_records_native_invocation_and_effective_scope(
        self,
    ) -> None:
        from pcbdraft.agent import verify
        from pcbdraft.agent.verify.__main__ import main

        project = self.root / "project with spaces"
        project.mkdir()
        cases = (
            ([], "full", None, False, None, 120, 30),
            (
                [
                    "--phase",
                    "build",
                    "--phase",
                    "test",
                    "--timeout",
                    "4.5",
                    "--ready-timeout",
                    "9",
                    "--port",
                    "8377",
                ],
                "targeted",
                ("build", "test"),
                False,
                8377,
                4.5,
                9.0,
            ),
            (["--skip-start"], "targeted", None, True, None, 120, 30),
        )
        for flags, scope, phases, skip_start, port, timeout, ready_timeout in cases:
            with self.subTest(flags=flags):
                recipe = SimpleNamespace(port=None)
                result = SimpleNamespace(
                    ok=True,
                    readiness=None,
                    phases=[
                        SimpleNamespace(
                            phase="test",
                            command="check-project",
                            output_tail="verified",
                        )
                    ],
                    to_dict=lambda: {"ok": True},
                )
                with (
                    patch.object(
                        verify, "load_or_detect", return_value=(recipe, "saved")
                    ),
                    patch.object(verify, "run_verify", return_value=result) as run,
                    patch(
                        "pcbdraft.agent.verification_evidence.record_verify_run"
                    ) as record,
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(main([str(project), "--json", *flags]), 0)
                run.assert_called_once_with(
                    project,
                    recipe,
                    phases=phases,
                    phase_timeout=timeout,
                    ready_timeout=ready_timeout,
                    skip_start=skip_start,
                    port_override=port,
                )
                record.assert_called_once()
                evidence = record.call_args.kwargs
                expected = [
                    sys.executable,
                    "-m",
                    "pcbdraft.agent.verify",
                    str(project),
                    "--timeout",
                    str(timeout),
                    "--ready-timeout",
                    str(ready_timeout),
                ]
                for phase in phases or ():
                    expected.extend(["--phase", phase])
                if skip_start:
                    expected.append("--skip-start")
                if port is not None:
                    expected.extend(["--port", str(port)])
                expected.append("--json")
                self.assertEqual(shlex.split(evidence["command"]), expected)
                self.assertEqual(evidence["scope"], scope)
                self.assertEqual(evidence["root"], project)
                self.assertIn("[test] check-project\nverified", evidence["output"])

    def test_verify_detect_only_does_not_record_verification_evidence(self) -> None:
        from pcbdraft.agent import verify
        from pcbdraft.agent.verify.__main__ import main

        recipe = SimpleNamespace(to_dict=lambda: {"name": "detected"})
        with (
            patch.object(verify, "load_or_detect", return_value=(recipe, "saved")),
            patch.object(
                verify,
                "run_verify",
                side_effect=AssertionError("detect-only executed verification"),
            ),
            patch("pcbdraft.agent.verification_evidence.record_verify_run") as record,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main([str(self.root), "--json", "--detect-only"]), 0)
        record.assert_not_called()

    def test_codex_user_tables_and_permissions_win(self) -> None:
        from pcbdraft.interfaces.tui import codex_runtime_plugin_migration as migration

        user_text = (
            'default_permissions = ":read-only"\n'
            '[mcp_servers.pcbdraft-tools]\ncommand = "user-command"\n'
            '[plugins."example@openai-curated"]\nenabled = false\ncustom = "keep"\n'
        )
        target = self.root / "config.toml"
        target.write_text(user_text)
        with patch.object(
            migration,
            "_query_codex_plugins",
            return_value=(
                [{"name": "example", "marketplace": "openai-curated", "enabled": True}],
                None,
            ),
        ):
            report = migration.migrate({}, codex_home=self.root)
        self.assertTrue(report.written, report.errors)
        self.assertEqual(tomllib.loads(target.read_text()), tomllib.loads(user_text))
        self.assertEqual(report.migrated_plugins, [])
        self.assertEqual(report.migrated, [])
        self.assertIsNone(report.wrote_permissions_default)

    def test_codex_incomplete_managed_marker_never_overwrites(self) -> None:
        from pcbdraft.interfaces.tui import codex_runtime_plugin_migration as migration

        text = (
            migration.LEGACY_MIGRATION_MARKER
            + '\n[mcp_servers.user]\ncommand = "mine"\n'
        )
        target = self.root / "config.toml"
        target.write_text(text)
        report = migration.migrate({}, codex_home=self.root, discover_plugins=False)
        self.assertFalse(report.written)
        self.assertTrue(report.errors)
        self.assertEqual(target.read_text(), text)

    def test_metrics_v1_and_v2_migrate_counts_without_rewriting_outbox(self) -> None:
        import jsonschema

        from pcbdraft.interfaces.tui.observability.shared_metrics import (
            SharedMetricsStore,
        )
        from pcbdraft.interfaces.tui.observability.shared_metrics_contract import (
            client_resource,
        )

        for version in ("1", "2"):
            with self.subTest(version=version):
                database = self.root / f"metrics-{version}.sqlite3"
                outbox = self.root / f"outbox-{version}"
                store = SharedMetricsStore(database, outbox)
                resource = client_resource(
                    "0.1.0",
                    os_name="linux",
                    architecture="x86_64",
                    install_method="pip",
                )
                store.record_counter(
                    "pcbdraft.model_route.count",
                    {"model": "test", "provider": "local"},
                    resource,
                )
                with closing(sqlite3.connect(database)) as connection, connection:
                    connection.execute(
                        "ALTER TABLE counter_aggregates RENAME COLUMN pcbdraft_version TO hermes_version"
                    )
                    connection.execute(
                        "UPDATE counter_aggregates SET metric_name = 'hermes.model_route.count', value = 7, packaged_value = 2"
                    )
                    connection.execute(
                        "UPDATE telemetry_state SET value = ? WHERE key = 'schema_version'",
                        (version,),
                    )
                    connection.execute(
                        "INSERT INTO telemetry_state VALUES ('install_id', '00000000-0000-4000-8000-000000000001')"
                    )
                    payload = (
                        '{"schema_version":"hermes.shared_metrics.v2","unchanged":true}'
                    )
                    connection.execute(
                        "INSERT INTO package_outbox VALUES ('old-package', '2099-01-01', '2099-01-02', ?, '2099-01-01', NULL)",
                        (payload,),
                    )
                    if version == "1":
                        connection.executescript(
                            "ALTER TABLE counter_aggregates RENAME TO source_rows;"
                            "CREATE TABLE counter_aggregates AS SELECT period_start, metric_name, hermes_version, dimensions_json, value, packaged_value FROM source_rows;"
                            "DROP TABLE source_rows;"
                        )
                    else:
                        # A mixed-name v2 store can contain both rows. Migration
                        # must merge counters and already-packaged deltas.
                        connection.execute(
                            "INSERT INTO counter_aggregates SELECT period_start, "
                            "'pcbdraft.model_route.count', hermes_version, os_family, "
                            "architecture, install_method, dimensions_json, 3, 1 "
                            "FROM counter_aggregates"
                        )
                migrated = SharedMetricsStore(database, outbox)
                row = migrated.counter_snapshot()[0]
                total, packaged = (7, 2) if version == "1" else (10, 3)
                self.assertEqual(
                    (row["value"], row["packaged_value"]), (total, packaged)
                )
                self.assertEqual(row["metric_name"], "pcbdraft.model_route.count")
                paths = migrated.create_and_export_package()
                packages = [json.loads(path.read_text()) for path in paths]
                self.assertIn(json.loads(payload), packages)
                new = next(
                    p
                    for p in packages
                    if p["schema_version"] == "pcbdraft.shared_metrics.v2"
                )
                self.assertEqual(new["metrics"][0]["value"], total - packaged)
                self.assertEqual(
                    new["install_id"], "00000000-0000-4000-8000-000000000001"
                )
                schema = json.loads(
                    (
                        TUI
                        / "observability/schemas/pcbdraft.shared_metrics.v2.schema.json"
                    ).read_text()
                )
                jsonschema.Draft202012Validator(schema).validate(new)
                with closing(sqlite3.connect(database)) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT value FROM telemetry_state WHERE key = 'schema_version'"
                        ).fetchone()[0],
                        "3",
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT payload_json FROM package_outbox WHERE package_id = 'old-package'"
                        ).fetchone()[0],
                        payload,
                    )
                self.assertEqual(
                    SharedMetricsStore(database, outbox).counter_snapshot()[0][
                        "packaged_value"
                    ],
                    total,
                )


if __name__ == "__main__":
    unittest.main()
