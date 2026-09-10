from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.interfaces.cli import main
from pcbdraft.interfaces.terminal import launch_cli
from pcbdraft.model.providers import (
    NativeIntentProvider,
    ProviderContext,
    resolve_provider,
)
from pcbdraft.model.settings import write_runtime_config
from pcbdraft.services.provider_connection import (
    ConnectionOptions,
    ConnectionStatus,
    activate_provider_runtime,
    classify_provider_error,
    connect,
    connection_status,
    provider_identities,
)


class ProviderConnectionTests(unittest.TestCase):
    def test_connect_loads_process_dotenv_before_config_and_wizard(self) -> None:
        from pcbdraft.core import runtime_environment
        from pcbdraft.model import env_loader

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            process_home = root / "process"
            task_home = root / "task"
            process_home.mkdir()
            task_home.mkdir()
            (process_home / ".env").write_text(
                "OPENAI_API_KEY=process-test-key\n", encoding="utf-8"
            )
            (task_home / ".env").write_text(
                "OPENAI_API_KEY=wrong-task-key\n", encoding="utf-8"
            )
            observed: list[str] = []

            def observe(stage):
                self.assertEqual(os.environ.get("OPENAI_API_KEY"), "process-test-key")
                observed.append(stage)

            token = runtime_environment.set_runtime_home_override(task_home)
            try:
                with (
                    patch.dict(
                        os.environ,
                        {
                            "PCBDRAFT_RUNTIME_HOME": str(process_home),
                            "PCBDRAFT_HERMES_HOME": "",
                            "OPENAI_API_KEY": "",
                        },
                    ),
                    patch.object(env_loader, "_apply_external_secret_sources"),
                    patch.object(env_loader, "_apply_managed_env"),
                    patch.object(env_loader, "_reapply_terminal_config_bridge"),
                    patch.object(
                        env_loader,
                        "load_pcbdraft_dotenv",
                        wraps=env_loader.load_pcbdraft_dotenv,
                    ) as load,
                    patch(
                        "pcbdraft.model.settings.write_runtime_config",
                        side_effect=lambda: observe("config"),
                    ),
                    patch(
                        "pcbdraft.model.configuration.read_raw_config", return_value={}
                    ),
                    patch(
                        "pcbdraft.interfaces.tui.main.select_provider_and_model",
                        side_effect=lambda **_: observe("wizard"),
                    ),
                    patch(
                        "pcbdraft.services.provider_connection.connection_status",
                        return_value=ConnectionStatus(
                            False, False, None, None, None, None
                        ),
                    ),
                ):
                    result = connect()
                    self.assertEqual(result.outcome, "cancelled")
                    load.assert_called_once_with(runtime_home=process_home)
            finally:
                runtime_environment.reset_runtime_home_override(token)
            self.assertEqual(observed, ["config", "wizard"])

    def test_registry_is_exactly_pcbdraft_canonical_picker_identities(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {"PCBDRAFT_RUNTIME_HOME": str(Path(temporary) / "product")},
                clear=False,
            ),
        ):
            activate_provider_runtime()
            from pcbdraft.model.catalog import CANONICAL_PROVIDERS

            self.assertEqual(
                provider_identities(),
                tuple(entry.slug for entry in CANONICAL_PROVIDERS),
            )

    def test_product_home_ignores_standalone_runtime_home_and_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            standalone = root / "home" / ".hermes"
            standalone.mkdir(parents=True)
            sentinel = standalone / "sentinel"
            sentinel.write_bytes(b"standalone-state")
            product = root / "xdg" / "pcbdraft" / "runtime"
            with patch.dict(
                os.environ,
                {
                    "HOME": str(root / "home"),
                    "XDG_CONFIG_HOME": str(root / "xdg"),
                    "HERMES_HOME": str(standalone),
                    "PCBDRAFT_HERMES_HOME": "",
                    "PCBDRAFT_RUNTIME_HOME": "",
                    "PCBDRAFT_CONFIG": str(product.parent / "config.json"),
                },
                clear=False,
            ):
                activate_provider_runtime()
                write_runtime_config()
                status_value = connection_status(verify=False)
                self.assertFalse(status_value.configured)
                self.assertEqual(status_value.state, "unconfigured")
                self.assertEqual(os.environ["PCBDRAFT_RUNTIME_HOME"], str(product))
                self.assertEqual(
                    os.environ["PCBDRAFT_RUNTIME_SHARED_AUTH_DIR"],
                    str(product / "shared"),
                )
                self.assertEqual(os.environ["PCBDRAFT_RUNTIME_HOME_MODE"], "0700")
            self.assertEqual(sentinel.read_bytes(), b"standalone-state")
            self.assertEqual(stat.S_IMODE(product.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((product / "shared").stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((product / "config.yaml").stat().st_mode), 0o600
            )

    def test_connect_persists_wizard_selection_and_returns_safe_status(self) -> None:
        secret = "sk-" + "this-must-never-render"
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {"PCBDRAFT_RUNTIME_HOME": str(Path(temporary) / "product")},
                clear=False,
            ),
        ):
            activate_provider_runtime()

            def select(_args=None, *, args=None) -> None:
                del _args, args
                from pcbdraft.model.configuration import read_raw_config, save_config

                config = read_raw_config()
                config["model"] = {
                    "provider": "custom",
                    "default": "board-model",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "api_key": secret,
                }
                save_config(config, strip_defaults=False)

            with patch(
                "pcbdraft.interfaces.tui.main.select_provider_and_model",
                side_effect=select,
            ):
                result = connect()
            self.assertEqual(result.outcome, "changed")
            self.assertTrue(result.usable)
            self.assertEqual(result.provider, "custom")
            self.assertNotIn(secret, repr(result))
            self.assertNotIn(secret, json.dumps(result.to_dict()))
            restarted = connection_status()
            self.assertEqual(restarted.model, "board-model")

    def test_cancel_rolls_back_partial_provider_file_writes(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {
                    "PCBDRAFT_RUNTIME_HOME": str(Path(temporary) / "product"),
                    "PCBDRAFT_RUNTIME_SHARED_AUTH_DIR": str(
                        Path(temporary) / "standalone"
                    ),
                },
                clear=False,
            ),
        ):
            activate_provider_runtime()
            write_runtime_config()
            product = Path(os.environ["PCBDRAFT_RUNTIME_HOME"])
            existing = {
                product / "config.yaml": (product / "config.yaml").read_bytes(),
                product / ".env": b"API_KEY=old-secret\n",
                product / "auth.json": b'{"providers":{"old":{}}}\n',
                product / ".anthropic_oauth.json": b'{"accessToken":"old"}\n',
                product / "auth" / "google_oauth.json": b'{"token":"old"}\n',
                product / "google_token.json": b'{"token":"old"}\n',
                product / "shared" / "nous_auth.json": b'{"old":true}\n',
            }
            for path, data in existing.items():
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.write_bytes(data)
                path.chmod(0o600)

            def partial_write(_args=None, *, args=None) -> None:
                del _args, args
                (product / "config.yaml").write_text(
                    "model:\n  provider: openai-codex\n  default: gpt-secret\n",
                    encoding="utf-8",
                )
                (product / ".env").write_text("API_KEY=new-secret\n", encoding="utf-8")
                (product / "auth.json").write_text(
                    '{"access_token":"new-token"}\n', encoding="utf-8"
                )
                (product / ".anthropic_oauth.json").write_text(
                    '{"accessToken":"new-token"}\n', encoding="utf-8"
                )
                (product / "auth" / "google_oauth.json").write_text(
                    '{"token":"new-token"}\n', encoding="utf-8"
                )
                (product / "google_token.json").write_text(
                    '{"token":"new-token"}\n', encoding="utf-8"
                )
                (product / "shared" / "nous_auth.json").write_text(
                    '{"refresh_token":"new-token"}\n', encoding="utf-8"
                )
                raise KeyboardInterrupt

            with patch(
                "pcbdraft.interfaces.tui.main.select_provider_and_model",
                side_effect=partial_write,
            ):
                result = connect()
            self.assertEqual(result.outcome, "cancelled")
            self.assertEqual(result.state, "cancelled")
            for path, data in existing.items():
                self.assertEqual(path.read_bytes(), data)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_clean_return_without_config_commit_rolls_back_auth_write(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {"PCBDRAFT_RUNTIME_HOME": str(Path(temporary) / "product")},
                clear=False,
            ),
        ):
            activate_provider_runtime()
            write_runtime_config()
            product = Path(os.environ["PCBDRAFT_RUNTIME_HOME"])

            def auth_then_cancel(_args=None, *, args=None) -> None:
                del _args, args
                (product / "auth.json").write_text(
                    '{"access_token":"partial-token"}\n', encoding="utf-8"
                )

            with patch(
                "pcbdraft.interfaces.tui.main.select_provider_and_model",
                side_effect=auth_then_cancel,
            ):
                result = connect()
            self.assertEqual(result.outcome, "cancelled")
            self.assertEqual(result.state, "cancelled")
            self.assertFalse((product / "auth.json").exists())

    def test_connect_rejects_symlinked_private_auth_directory(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {"PCBDRAFT_RUNTIME_HOME": str(Path(temporary) / "product")},
                clear=False,
            ),
        ):
            root = Path(temporary)
            activate_provider_runtime()
            write_runtime_config()
            external = root / "external-auth"
            external.mkdir()
            (Path(os.environ["PCBDRAFT_RUNTIME_HOME"]) / "auth").symlink_to(
                external, target_is_directory=True
            )
            with (
                patch(
                    "pcbdraft.interfaces.tui.main.select_provider_and_model"
                ) as wizard,
                self.assertRaisesRegex(PCBDraftError, "symbolic-link directory"),
            ):
                connect()
            wizard.assert_not_called()
            self.assertEqual(list(external.iterdir()), [])

    def test_status_taxonomy_is_distinct_and_never_renders_raw_secrets(self) -> None:
        class ProviderEvidenceError(Exception):
            def __init__(
                self,
                message: str,
                *,
                code: str = "",
                status_code: int | None = None,
            ) -> None:
                super().__init__(message)
                self.code = code
                self.status_code = status_code

        cases = (
            (
                ProviderEvidenceError("refresh sk-secret", code="login_required"),
                "expired",
            ),
            (
                ProviderEvidenceError("rejected sk-secret", status_code=401),
                "invalid_credentials",
            ),
            (TimeoutError("network sk-secret"), "unreachable"),
            (
                ProviderEvidenceError("plan sk-secret", status_code=404),
                "unsupported_endpoint",
            ),
            (
                ProviderEvidenceError(
                    "subscription sk-secret",
                    code="xai_oauth_tier_denied",
                    status_code=403,
                ),
                "unsupported_endpoint",
            ),
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {"PCBDRAFT_RUNTIME_HOME": str(Path(temporary) / "product")},
                clear=False,
            ),
        ):
            activate_provider_runtime()
            write_runtime_config()
            from pcbdraft.model.configuration import read_raw_config, save_config

            config = read_raw_config()
            config["model"] = {"provider": "zai", "default": "glm-4.5"}
            save_config(config, strip_defaults=False)
            for error, expected in cases:
                with (
                    self.subTest(expected=expected),
                    patch(
                        "pcbdraft.model.runtime_provider.resolve_runtime_provider",
                        side_effect=error,
                    ),
                ):
                    status_value = connection_status()
                    self.assertEqual(status_value.state, expected)
                    self.assertFalse(status_value.usable)
                    self.assertNotIn("sk-secret", json.dumps(status_value.to_dict()))
            self.assertEqual(
                classify_provider_error(ProviderEvidenceError("opaque")),
                "unavailable",
            )

    def test_connect_timeout_covers_provider_flows_that_ignore_args(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {"PCBDRAFT_RUNTIME_HOME": str(Path(temporary) / "product")},
                clear=False,
            ),
        ):
            deadline = time.monotonic() + 1.0

            def ignored_timeout(_args=None, *, args=None) -> None:
                del _args, args
                while time.monotonic() < deadline:
                    pass

            started = time.monotonic()
            with (
                patch(
                    "pcbdraft.interfaces.tui.main.select_provider_and_model",
                    side_effect=ignored_timeout,
                ),
                self.assertRaisesRegex(PCBDraftError, "timed out"),
            ):
                connect(ConnectionOptions(timeout=0.02))
            self.assertLess(time.monotonic() - started, 0.5)

    def test_timed_connect_rejects_worker_thread_before_wizard_runs(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {"PCBDRAFT_RUNTIME_HOME": str(Path(temporary) / "product")},
                clear=False,
            ),
        ):
            wizard_called = threading.Event()
            observed: list[BaseException] = []

            def invoke() -> None:
                try:
                    connect(ConnectionOptions(timeout=0.02))
                except BaseException as exc:  # noqa: BLE001 - thread handoff
                    observed.append(exc)

            with patch(
                "pcbdraft.interfaces.tui.main.select_provider_and_model",
                side_effect=lambda *args, **kwargs: wizard_called.set(),
            ):
                worker = threading.Thread(target=invoke)
                worker.start()
                worker.join(timeout=1)

            self.assertFalse(worker.is_alive())
            self.assertFalse(wizard_called.is_set())
            self.assertEqual(len(observed), 1)
            self.assertIsInstance(observed[0], PCBDraftError)
            self.assertIn("requires the main thread", str(observed[0]))

    def test_connect_failure_uses_the_same_sanitized_taxonomy(self) -> None:
        class InvalidKeyError(Exception):
            status_code = 401

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {"PCBDRAFT_RUNTIME_HOME": str(Path(temporary) / "product")},
                clear=False,
            ),
            patch(
                "pcbdraft.interfaces.tui.main.select_provider_and_model",
                side_effect=InvalidKeyError("rejected sk-secret"),
            ),
            self.assertRaisesRegex(PCBDraftError, "rejected the credential") as raised,
        ):
            connect()
        self.assertNotIn("sk-secret", str(raised.exception))

    def test_reauthenticate_forces_cached_auth_flows_and_forwards_options(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {"PCBDRAFT_RUNTIME_HOME": str(Path(temporary) / "product")},
                clear=False,
            ),
        ):
            activate_provider_runtime()
            from pcbdraft.model import auth, model_setup_flows

            observed: dict[str, object] = {}

            def select(_args=None, *, args=None) -> None:
                del _args
                observed["timeout"] = args.timeout
                observed["force"] = args.force
                observed["state"] = auth.get_provider_auth_state("minimax-oauth")
                observed["choice"] = model_setup_flows._prompt_auth_credentials_choice(
                    "MiniMax"
                )
                from pcbdraft.model.configuration import read_raw_config, save_config

                config = read_raw_config()
                config["model"] = {
                    "provider": "custom",
                    "default": "board-model",
                    "base_url": "http://127.0.0.1:11434/v1",
                }
                save_config(config, strip_defaults=False)

            cached = object()
            with (
                patch.object(
                    auth, "get_provider_auth_state", return_value=cached
                ) as state_reader,
                patch.object(
                    model_setup_flows,
                    "_prompt_auth_credentials_choice",
                    return_value="reuse",
                ) as choice_reader,
                patch(
                    "pcbdraft.interfaces.tui.main.select_provider_and_model",
                    side_effect=select,
                ),
            ):
                result = connect(ConnectionOptions(timeout=0.5, reauthenticate=True))
                self.assertIs(auth.get_provider_auth_state, state_reader)
                self.assertIs(
                    model_setup_flows._prompt_auth_credentials_choice,
                    choice_reader,
                )
            self.assertEqual(result.outcome, "changed")
            self.assertEqual(observed["timeout"], 0.5)
            self.assertIs(observed["force"], True)
            self.assertIsNone(observed["state"])
            self.assertEqual(observed["choice"], "reauth")

    def test_picker_uses_pcbdraft_groups_and_saved_custom_rows(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {"PCBDRAFT_RUNTIME_HOME": str(Path(temporary) / "product")},
                clear=False,
            ),
        ):
            activate_provider_runtime()
            write_runtime_config()
            from pcbdraft.model.configuration import read_raw_config, save_config

            config = read_raw_config()
            config["custom_providers"] = [
                {
                    "name": "PCB Lab",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "model": "board-model",
                }
            ]
            save_config(config, strip_defaults=False)
            captured: list[str] = []

            def cancel_picker(choices, **_kwargs):
                captured.extend(str(choice) for choice in choices)
                return next(
                    index
                    for index, choice in enumerate(choices)
                    if "Leave unchanged" in str(choice)
                )

            with (
                patch("pcbdraft.model.auth.resolve_provider", return_value=None),
                patch(
                    "pcbdraft.interfaces.tui.main._prompt_provider_choice",
                    side_effect=cancel_picker,
                ),
            ):
                result = connect()
            self.assertEqual(result.outcome, "cancelled")
            rendered = "\n".join(captured)
            self.assertIn("MiniMax", rendered)
            self.assertIn("OpenAI", rendered)
            self.assertIn("GitHub Copilot", rendered)
            self.assertIn("PCB Lab (127.0.0.1:11434/v1)", rendered)

    def test_non_tty_connect_fails_without_opening_wizard(self) -> None:
        stderr = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO()),
            patch("sys.stderr", stderr),
            patch("pcbdraft.interfaces.cli.connect") as wizard,
        ):
            self.assertEqual(main(["connect"]), 1)
        wizard.assert_not_called()
        self.assertIn("interactive terminal", stderr.getvalue())

    def test_first_run_cancel_does_not_enter_repl(self) -> None:
        missing = ConnectionStatus(False, False, None, None, None, None)
        cancelled = ConnectionStatus(
            False, False, None, None, None, None, outcome="cancelled"
        )
        with (
            patch("pcbdraft.interfaces.terminal.activate"),
            patch(
                "pcbdraft.interfaces.terminal.connection_status", return_value=missing
            ),
            patch("pcbdraft.interfaces.terminal.connect", return_value=cancelled),
            patch("sys.stdin.isatty", return_value=True),
        ):
            self.assertEqual(launch_cli([]), 1)


class ProviderEnvironmentInitializationTests(unittest.TestCase):
    def setUp(self) -> None:
        from pcbdraft.core import runtime_environment
        from pcbdraft.interfaces.tui import managed_scope
        from pcbdraft.model import env_loader
        from pcbdraft.services import provider_connection

        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.root / "runtime"
        self.home.mkdir()
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "PCBDRAFT_RUNTIME_HOME": str(self.home),
                    "PCBDRAFT_HERMES_HOME": "",
                    "PCBDRAFT_RUNTIME_MANAGED_DIR": str(self.root / "managed-absent"),
                    "PYTHON_DOTENV_DISABLED": "0",
                    "HOME": str(self.root / "user"),
                    "USERPROFILE": str(self.root / "user"),
                    "XDG_CONFIG_HOME": str(self.root / "config"),
                    "REVIEW_PROVIDER": "",
                    "REVIEW_KEY": "",
                },
            )
        )
        self.enterContext(
            patch.object(provider_connection, "_provider_environment_state", None)
        )
        token = runtime_environment.set_runtime_home_override(None)
        self.addCleanup(runtime_environment.reset_runtime_home_override, token)
        self.managed_default = self.root / "managed-default"
        self.enterContext(
            patch.object(managed_scope, "_DEFAULT_MANAGED_DIR", self.managed_default)
        )
        # Exercise the real dotenv loader and its external-source memo, with
        # only the actual service boundary stubbed out for offline execution.
        source = SimpleNamespace(
            applied=(), result=SimpleNamespace(error=None, warnings=())
        )
        report = SimpleNamespace(sources=[source], applied_any=False, conflicts=())
        self.service = self.enterContext(
            patch(
                "pcbdraft.agent.secret_sources.registry.apply_all", return_value=report
            )
        )
        self.load = self.enterContext(
            patch.object(
                env_loader,
                "load_pcbdraft_dotenv",
                wraps=env_loader.load_pcbdraft_dotenv,
            )
        )

    def write_config(
        self, home: Path | None = None, *, revision: int = 1, secrets: bool = True
    ) -> None:
        home = home or self.home
        document = {
            "model": {
                "provider": "${REVIEW_PROVIDER}",
                "default": "board-review-model",
                "api_key": "${REVIEW_KEY}",
                "base_url": "http://127.0.0.1:11434/v1",
            }
        }
        if secrets:
            document["secrets"] = {"review": {"enabled": True, "revision": revision}}
        (home / "config.yaml").write_text(json.dumps(document), encoding="utf-8")

    def write_env(
        self,
        home: Path | None = None,
        *,
        provider: str = "custom",
        key: str | None = "review-key-one",
    ) -> None:
        home = home or self.home
        text = f"REVIEW_PROVIDER={provider}\n"
        if key is not None:
            text += f"REVIEW_KEY={key}\n"
        (home / ".env").write_text(text, encoding="utf-8")

    def test_cold_status_loads_provider_and_key_references_in_fresh_process(
        self,
    ) -> None:
        self.write_config(secrets=False)
        self.write_env()
        code = (
            "import json; "
            "from pcbdraft.services.provider_connection import connection_status; "
            "status = connection_status(verify=False); "
            "from pcbdraft.model.configuration import load_config_readonly; "
            "assert load_config_readonly()['model']['api_key'] == 'review-key-one'; "
            "print(json.dumps(status.to_dict()))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=dict(os.environ),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        status_value = json.loads(result.stdout)
        self.assertEqual(status_value["provider"], "custom")
        self.assertTrue(status_value["configured"])
        self.assertTrue(status_value["usable"])
        self.assertNotIn("review-key-one", result.stdout + result.stderr)

    def test_repeated_status_and_picker_reuse_loader_and_secret_service(self) -> None:
        from pcbdraft.model.configuration import load_config_readonly

        self.write_config()
        self.write_env()
        with patch(
            "pcbdraft.model.runtime_provider.resolve_runtime_provider",
            side_effect=lambda **kw: {
                "provider": kw["requested"],
                "source": "environment",
            },
        ) as resolve:
            first = connection_status()
            second = connection_status()
        provider_identities()
        self.assertEqual(first.provider, "custom")
        self.assertEqual(first, second)
        self.assertTrue(first.usable)
        self.assertEqual(load_config_readonly()["model"]["api_key"], "review-key-one")
        resolve.assert_called_with(
            requested="custom", target_model="board-review-model"
        )
        self.load.assert_called_once_with(runtime_home=self.home)
        self.assertEqual(self.service.call_count, 1)

    def test_env_atomic_replacement_and_config_edit_refresh_once_each(self) -> None:
        from pcbdraft.model.configuration import load_config_readonly

        self.write_config()
        self.write_env()
        connection_status(verify=False)
        previous = (self.home / ".env").stat()
        replacement = self.home / "replacement.env"
        replacement.write_text(
            "REVIEW_PROVIDER=openai\nREVIEW_KEY=review-key-two\n", encoding="utf-8"
        )
        os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        replacement.replace(self.home / ".env")
        self.assertEqual(connection_status(verify=False).provider, "openai")
        connection_status(verify=False)
        self.assertEqual(load_config_readonly()["model"]["api_key"], "review-key-two")
        self.assertEqual(self.load.call_count, 2)
        self.assertEqual(self.service.call_count, 2)
        self.write_config(revision=2)
        connection_status(verify=False)
        connection_status(verify=False)
        self.assertEqual(self.load.call_count, 3)
        self.assertEqual(self.service.call_count, 3)
        self.assertEqual(self.service.call_args.args[0]["review"]["revision"], 2)

    def test_home_switch_and_removed_key_cannot_reuse_previous_profile_values(
        self,
    ) -> None:
        from pcbdraft.core import runtime_environment

        self.write_config()
        self.write_env()
        other = self.root / "profiles" / "other"
        other.mkdir(parents=True)
        self.write_config(other)
        self.write_env(other, provider="zai", key=None)
        token = runtime_environment.set_runtime_home_override(other)
        try:
            # Task-local overrides do not change the process-home environment
            # used by startup status or its configuration expansion.
            self.assertEqual(connection_status(verify=False).provider, "custom")
            self.assertEqual(runtime_environment.get_runtime_home(), other)
            os.environ["PCBDRAFT_RUNTIME_HOME"] = str(other)
            self.assertEqual(connection_status(verify=False).provider, "zai")
            self.assertEqual(os.environ.get("REVIEW_KEY"), "")
            os.environ["PCBDRAFT_RUNTIME_HOME"] = str(self.home)
            self.assertEqual(connection_status(verify=False).provider, "custom")
            self.assertEqual(os.environ.get("REVIEW_KEY"), "review-key-one")
            self.write_env(key=None)
            connection_status(verify=False)
            self.assertEqual(os.environ.get("REVIEW_KEY"), "")
            self.assertEqual(self.load.call_count, 4)
            self.assertEqual(
                [call.kwargs["runtime_home"] for call in self.load.call_args_list],
                [self.home, other, self.home, self.home],
            )
        finally:
            runtime_environment.reset_runtime_home_override(token)

    def test_environment_change_invalidates_cache_and_failed_load_is_retryable(
        self,
    ) -> None:
        self.write_config()
        self.write_env()
        connection_status(verify=False)
        os.environ["REVIEW_PROVIDER"] = "stale-export"
        self.assertEqual(connection_status(verify=False).provider, "custom")
        self.assertEqual(self.load.call_count, 2)
        self.write_env(provider="zai")
        original_loader = self.load._mock_wraps

        def fail(**_kwargs):
            os.environ["REVIEW_KEY"] = "partial-load"
            raise RuntimeError("simulated loading failure")

        self.load.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, "simulated loading failure"):
            connection_status(verify=False)
        self.assertNotEqual(os.environ.get("REVIEW_KEY"), "partial-load")
        self.load.side_effect = original_loader
        self.assertEqual(connection_status(verify=False).provider, "zai")
        self.assertEqual(os.environ.get("REVIEW_KEY"), "review-key-one")
        self.assertEqual(self.load.call_count, 4)

    def test_edit_during_load_is_not_cached_as_already_applied(self) -> None:
        self.write_config()
        self.write_env()
        original_loader = self.load._mock_wraps

        def edit_after_read(**kwargs):
            result = original_loader(**kwargs)
            self.write_env(provider="zai", key="edited-during-load")
            return result

        self.load.side_effect = edit_after_read
        self.assertEqual(connection_status(verify=False).provider, "custom")
        self.load.side_effect = original_loader
        self.assertEqual(connection_status(verify=False).provider, "zai")
        self.assertEqual(os.environ.get("REVIEW_KEY"), "edited-during-load")
        connection_status(verify=False)
        self.assertEqual(self.load.call_count, 2)
        self.assertEqual(self.service.call_count, 2)

    def test_real_managed_loader_refreshes_rotation_and_directory_switch_once(
        self,
    ) -> None:
        from pcbdraft.model import env_loader
        from pcbdraft.model.configuration import load_config_readonly

        self.write_config()
        self.write_env(key="home-fallback")
        managed_a = self.root / "managed-a"
        managed_b = self.root / "managed-b"
        for directory, value in ((managed_a, "managed-a"), (managed_b, "managed-c")):
            directory.mkdir()
            (directory / ".env").write_text(f"REVIEW_KEY={value}\n", encoding="utf-8")
        os.environ["PCBDRAFT_RUNTIME_MANAGED_DIR"] = str(managed_a)
        # The original loader's final value equals the existing input, so it
        # does not appear in the write delta. It remains a cache dependency.
        os.environ["REVIEW_KEY"] = "managed-a"

        def snapshot():
            return [
                (path.stat().st_ino, path.stat().st_mtime_ns, path.stat().st_mode)
                for directory in (managed_a, managed_b)
                for path in (directory, directory / ".env")
            ]

        with patch.object(env_loader, "atomic_replace") as normalization_write:
            for expected_calls, value in (
                (1, "managed-a"),
                (2, "managed-b"),
                (3, "managed-c"),
            ):
                if expected_calls == 2:
                    (managed_a / ".env").write_text(
                        "REVIEW_KEY=managed-b\n", encoding="utf-8"
                    )
                elif expected_calls == 3:
                    os.environ["PCBDRAFT_RUNTIME_MANAGED_DIR"] = str(managed_b)
                before = snapshot()
                connection_status(verify=False)
                connection_status(verify=False)
                self.assertEqual(os.environ["REVIEW_KEY"], value)
                self.assertEqual(load_config_readonly()["model"]["api_key"], value)
                self.assertEqual(self.load.call_count, expected_calls)
                self.assertEqual(self.service.call_count, expected_calls)
                self.assertEqual(snapshot(), before)
            normalization_write.assert_not_called()
        self.assertEqual((managed_a / ".env").read_text(), "REVIEW_KEY=managed-b\n")
        self.assertEqual((managed_b / ".env").read_text(), "REVIEW_KEY=managed-c\n")

    def test_managed_default_selector_tracks_pytest_presence_not_only_env_delta(
        self,
    ) -> None:
        self.write_config()
        self.write_env(key="home-fallback")
        self.managed_default.mkdir()
        (self.managed_default / ".env").write_text(
            "REVIEW_KEY=managed-default\n", encoding="utf-8"
        )
        os.environ.pop("PCBDRAFT_RUNTIME_MANAGED_DIR", None)
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        for count, expected in (
            (1, "managed-default"),
            (2, "home-fallback"),
            (3, "managed-default"),
        ):
            if count == 2:
                os.environ["PYTEST_CURRENT_TEST"] = ""
            elif count == 3:
                os.environ.pop("PYTEST_CURRENT_TEST")
            connection_status(verify=False)
            connection_status(verify=False)
            self.assertEqual(os.environ["REVIEW_KEY"], expected)
            self.assertEqual(self.load.call_count, count)

    def test_managed_terminal_config_edit_refreshes_real_loader_bridge(self) -> None:
        self.write_config()
        self.write_env()
        self.managed_default.mkdir()
        os.environ["PCBDRAFT_RUNTIME_MANAGED_DIR"] = str(self.managed_default)
        for count, timeout in ((1, 17), (2, 29)):
            (self.managed_default / "config.yaml").write_text(
                f"terminal:\n  timeout: {timeout}\n", encoding="utf-8"
            )
            connection_status(verify=False)
            connection_status(verify=False)
            self.assertEqual(os.environ["TERMINAL_TIMEOUT"], str(timeout))
            self.assertEqual(self.load.call_count, count)

    def test_dotenv_disable_and_interpolation_inputs_invalidate_without_file_edits(
        self,
    ) -> None:
        self.write_config()
        (self.home / ".env").write_text(
            "REVIEW_PROVIDER=custom\nREVIEW_KEY=${REVIEW_INPUT}\n", encoding="utf-8"
        )
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"
        os.environ["REVIEW_INPUT"] = "interpolated-a"
        for _ in range(2):
            self.assertFalse(connection_status(verify=False).configured)
        self.assertEqual(self.load.call_count, 1)
        os.environ["PYTHON_DOTENV_DISABLED"] = "0"
        for _ in range(2):
            self.assertEqual(connection_status(verify=False).provider, "custom")
        self.assertEqual(os.environ["REVIEW_KEY"], "interpolated-a")
        self.assertEqual(self.load.call_count, 2)
        os.environ["REVIEW_INPUT"] = "interpolated-b"
        connection_status(verify=False)
        connection_status(verify=False)
        self.assertEqual(os.environ["REVIEW_KEY"], "interpolated-b")
        self.assertEqual(self.load.call_count, 3)

    def test_op_bootstrap_input_change_refreshes_without_file_edits(self) -> None:
        self.write_config()
        self.write_env()
        (self.home / ".op.env").write_text(
            "OP_SERVICE_ACCOUNT_TOKEN=file-token\n", encoding="utf-8"
        )
        os.environ["OP_SERVICE_ACCOUNT_TOKEN"] = "shell-" + "token"
        connection_status(verify=False)
        connection_status(verify=False)
        self.assertEqual(os.environ["OP_SERVICE_ACCOUNT_TOKEN"], "shell-token")
        self.assertEqual(self.load.call_count, 1)
        os.environ.pop("OP_SERVICE_ACCOUNT_TOKEN")
        connection_status(verify=False)
        connection_status(verify=False)
        self.assertEqual(os.environ["OP_SERVICE_ACCOUNT_TOKEN"], "file-token")
        self.assertEqual(self.load.call_count, 2)


class NativeIntentProviderTests(unittest.TestCase):
    def test_interpret_uses_selected_pcbdraft_provider_and_safe_artifacts(self) -> None:
        value = {
            "request_summary": "sensor board",
            "design_name": "sensor",
            "layers": 2,
            "board": {"width_mm": 40, "height_mm": 30},
            "assumptions": [],
            "requested_parts": ["TMP102"],
            "functions": ["temperature sensing"],
            "power": {
                "nominal_v": 3.3,
                "max_voltage_v": 3.3,
                "max_current_a": 0.2,
                "max_power_w": 0.66,
            },
            "missing_fields": [],
        }
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=json.dumps(value),
                        reasoning=None,
                        reasoning_content=None,
                        reasoning_details=None,
                    ),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = NativeIntentProvider("openai-codex", "gpt-5-codex")
            with patch(
                "pcbdraft.model.auxiliary_client.call_llm", return_value=response
            ) as call:
                result = provider.interpret(
                    ProviderContext("make a sensor", "sensor", {}),
                    project_dir=root,
                    run_dir=root / "run",
                    timeout=30,
                )
            self.assertEqual(result["design_name"], "sensor")
            self.assertEqual(call.call_args.kwargs["provider"], "openai-codex")
            self.assertEqual(call.call_args.kwargs["model"], "gpt-5-codex")
            receipt = (root / "run" / "intent.receipt.json").read_text()
            self.assertNotIn("api_key", receipt)
            self.assertFalse((root / "run" / "prompt.json").exists())

    def test_application_resolution_uses_same_selected_identity(self) -> None:
        selected = ConnectionStatus(
            True,
            True,
            "anthropic",
            "claude-sonnet-4",
            "api_key",
            "runtime-config",
        )
        with patch(
            "pcbdraft.services.provider_connection.connection_status",
            return_value=selected,
        ):
            provider = resolve_provider("auto")
        self.assertIsInstance(provider, NativeIntentProvider)
        assert provider is not None
        self.assertEqual(provider.provider_id, "anthropic")

    def test_provider_failure_receipt_is_classified_and_secret_free(self) -> None:

        class InvalidKeyError(Exception):
            status_code = 401

        schema = {
            "type": "object",
            "properties": {"ok": {"const": True}},
            "required": ["ok"],
            "additionalProperties": False,
        }
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            provider = NativeIntentProvider("zai", "glm-test")
            with (
                patch(
                    "pcbdraft.model.auxiliary_client.call_llm",
                    side_effect=InvalidKeyError("rejected sk-secret"),
                ),
                self.assertRaisesRegex(
                    PCBDraftError, "provider rejected the credential"
                ) as raised,
            ):
                provider._structured(
                    "Return ok",
                    "provider_contract",
                    schema,
                    30,
                    run_dir=run_dir,
                    artifact_prefix="provider",
                )
            receipt = json.loads(
                (run_dir / "provider.receipt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["failure_category"], "invalid_credentials")
            self.assertFalse(receipt["completed"])
            self.assertNotIn("sk-secret", json.dumps(receipt))
            self.assertNotIn("sk-secret", str(raised.exception))

    def test_representative_provider_classes_share_the_normalized_call_boundary(
        self,
    ) -> None:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content='{"ok":true}',
                        reasoning=None,
                        reasoning_content=None,
                        reasoning_details=None,
                    ),
                )
            ]
        )
        providers = (
            "zai",
            "minimax",
            "minimax-oauth",
            "openai-codex",
            "anthropic",
            "bedrock",
            "vertex",
            "azure-foundry",
            "copilot-acp",
            "openrouter",
            "lmstudio",
            "custom",
        )
        schema = {
            "type": "object",
            "properties": {"ok": {"const": True}},
            "required": ["ok"],
            "additionalProperties": False,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch(
                "pcbdraft.model.auxiliary_client.call_llm", return_value=response
            ) as call:
                for index, provider_id in enumerate(providers):
                    with self.subTest(provider=provider_id):
                        provider = NativeIntentProvider(provider_id, "board-model")
                        value = provider._structured(
                            "Return ok",
                            "provider_contract",
                            schema,
                            30,
                            run_dir=root / str(index),
                            artifact_prefix="provider",
                        )
                        self.assertEqual(value, {"ok": True})
                        self.assertEqual(call.call_args.kwargs["provider"], provider_id)


class PCBDraftProviderContractTests(unittest.TestCase):
    def test_provider_adapter_contract_is_present(self) -> None:
        from pcbdraft.interfaces.tui.main import select_provider_and_model
        from pcbdraft.model.auxiliary_client import (
            call_llm,
            extract_content_or_reasoning,
        )
        from pcbdraft.model.catalog import CANONICAL_PROVIDERS
        from pcbdraft.model.runtime_provider import resolve_runtime_provider

        self.assertTrue(CANONICAL_PROVIDERS)
        self.assertTrue(callable(select_provider_and_model))
        self.assertTrue(callable(resolve_runtime_provider))
        self.assertTrue(callable(call_llm))
        self.assertTrue(callable(extract_content_or_reasoning))

    def test_required_auth_and_transport_classes_remain_available(self) -> None:
        from pcbdraft.model.auth import PROVIDER_REGISTRY, ZAI_ENDPOINTS
        from pcbdraft.model.auxiliary_client import (
            AnthropicAuxiliaryClient,
            BedrockAuxiliaryClient,
            CodexAuxiliaryClient,
        )
        from pcbdraft.model.copilot_acp_client import CopilotACPClient

        expected_auth_types = {
            "zai": "api_key",
            "minimax": "api_key",
            "minimax-oauth": "oauth_minimax",
            "openai-codex": "oauth_external",
            "copilot-acp": "external_process",
            "bedrock": "aws_sdk",
            "vertex": "vertex",
            "azure-foundry": "api_key",
        }
        self.assertEqual(
            {
                provider: PROVIDER_REGISTRY[provider].auth_type
                for provider in expected_auth_types
            },
            expected_auth_types,
        )
        self.assertEqual(len(ZAI_ENDPOINTS), 4)
        self.assertTrue(callable(AnthropicAuxiliaryClient))
        self.assertTrue(callable(BedrockAuxiliaryClient))
        self.assertTrue(callable(CodexAuxiliaryClient))
        self.assertTrue(callable(CopilotACPClient))

    def test_glm_and_minimax_endpoint_and_refresh_contracts(self) -> None:
        from pcbdraft.model import auth

        self.assertEqual(
            [entry[0] for entry in auth.ZAI_ENDPOINTS],
            ["global", "cn", "coding-global", "coding-cn"],
        )
        self.assertEqual(
            ["/coding/" in entry[1] for entry in auth.ZAI_ENDPOINTS],
            [False, False, True, True],
        )
        self.assertIn(
            "api.minimax.io", auth.PROVIDER_REGISTRY["minimax"].inference_base_url
        )
        self.assertIn(
            "api.minimaxi.com", auth.PROVIDER_REGISTRY["minimax-cn"].inference_base_url
        )
        oauth = auth.PROVIDER_REGISTRY["minimax-oauth"]
        self.assertIn("api.minimax.io", oauth.inference_base_url)
        self.assertIn("api.minimaxi.com", oauth.extra["cn_inference_base_url"])

        expired = {
            "access_token": "expired-token",
            "refresh_token": "refresh-token",
            "expires_at": "2000-01-01T00:00:00+00:00",
        }
        refreshed = {**expired, "access_token": "fresh-token"}
        with (
            patch.object(auth, "get_provider_auth_state", return_value=expired),
            patch.object(
                auth, "_refresh_minimax_oauth_state", return_value=refreshed
            ) as refresh,
        ):
            token_provider = auth.build_minimax_oauth_token_provider()
            self.assertEqual(token_provider(), "fresh-token")
        refresh.assert_called_once_with(expired)

    def test_cloud_and_external_transport_routing_is_preserved(self) -> None:
        import pcbdraft.model.auxiliary_client as auxiliary
        from pcbdraft.agent import vertex_adapter
        from pcbdraft.model import anthropic_adapter, auth, bedrock_adapter

        bedrock_client = object()
        with (
            patch.object(bedrock_adapter, "has_aws_credentials", return_value=True),
            patch.object(
                bedrock_adapter, "resolve_bedrock_region", return_value="us-test-1"
            ),
            patch.object(
                bedrock_adapter, "is_anthropic_bedrock_model", return_value=False
            ),
            patch.object(anthropic_adapter, "build_anthropic_bedrock_client"),
            patch.object(
                auxiliary, "BedrockAuxiliaryClient", return_value=bedrock_client
            ) as bedrock_builder,
        ):
            resolved, model = auxiliary.resolve_provider_client(
                "bedrock", "meta.llama-test"
            )
        self.assertIs(resolved, bedrock_client)
        self.assertEqual(model, "meta.llama-test")
        bedrock_builder.assert_called_once_with("us-test-1", "meta.llama-test")

        vertex_client = object()
        with (
            patch.object(vertex_adapter, "has_vertex_credentials", return_value=True),
            patch.object(
                vertex_adapter,
                "get_vertex_config",
                return_value=("vertex-token", "https://vertex.test/v1"),
            ),
            patch("openai.OpenAI", return_value=vertex_client) as vertex_builder,
        ):
            resolved, model = auxiliary.resolve_provider_client(
                "vertex", "google/gemini-test"
            )
        self.assertIs(resolved, vertex_client)
        self.assertEqual(model, "google/gemini-test")
        vertex_builder.assert_called_once_with(
            api_key="vertex-token", base_url="https://vertex.test/v1"
        )

        azure_client = object()
        with patch.object(
            auxiliary,
            "_try_azure_foundry",
            return_value=(azure_client, "deployment-test"),
        ) as azure_builder:
            resolved, model = auxiliary.resolve_provider_client(
                "azure-foundry", "deployment-test"
            )
        self.assertIs(resolved, azure_client)
        self.assertEqual(model, "deployment-test")
        azure_builder.assert_called_once()

        acp_client = object()
        with (
            patch.object(
                auth,
                "resolve_external_process_provider_credentials",
                return_value={
                    "api_key": "process-token",
                    "base_url": "acp+stdio://copilot",
                    "command": "copilot",
                    "args": ["--acp", "--stdio"],
                },
            ),
            patch(
                "pcbdraft.model.copilot_acp_client.CopilotACPClient",
                return_value=acp_client,
            ) as acp_builder,
        ):
            resolved, model = auxiliary.resolve_provider_client(
                "copilot-acp", "copilot-model"
            )
        self.assertIs(resolved, acp_client)
        self.assertEqual(model, "copilot-model")
        acp_builder.assert_called_once_with(
            api_key="process-token",
            base_url="acp+stdio://copilot",
            command="copilot",
            args=["--acp", "--stdio"],
        )


if __name__ == "__main__":
    unittest.main()
