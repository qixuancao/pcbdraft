from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.core.errors import PCBDraftError
from pcbdraft.core.hermes_paths import install_vendor_path
from pcbdraft.interfaces.cli import main
from pcbdraft.interfaces.hermes_cli import launch_cli
from pcbdraft.model.hermes_config import write_hermes_config
from pcbdraft.model.providers import (
    HermesIntentProvider,
    ProviderContext,
    resolve_provider,
)
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
    def test_registry_is_exactly_hermes_canonical_picker_identities(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {"PCBDRAFT_HERMES_HOME": str(Path(temporary) / "product")},
                clear=False,
            ),
        ):
            activate_provider_runtime()
            from hermes_cli.models import CANONICAL_PROVIDERS

            self.assertEqual(
                provider_identities(),
                tuple(entry.slug for entry in CANONICAL_PROVIDERS),
            )

    def test_product_home_ignores_standalone_hermes_home_and_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            standalone = root / "home" / ".hermes"
            standalone.mkdir(parents=True)
            sentinel = standalone / "sentinel"
            sentinel.write_bytes(b"standalone-state")
            product = root / "pcbdraft-hermes"
            with patch.dict(
                os.environ,
                {
                    "HOME": str(root / "home"),
                    "XDG_CONFIG_HOME": str(root / "xdg"),
                    "HERMES_HOME": str(standalone),
                    "PCBDRAFT_HERMES_HOME": str(product),
                },
                clear=False,
            ):
                activate_provider_runtime()
                write_hermes_config()
                status_value = connection_status(verify=False)
                self.assertFalse(status_value.configured)
                self.assertEqual(status_value.state, "unconfigured")
                self.assertEqual(os.environ["HERMES_HOME"], str(product))
                self.assertEqual(
                    os.environ["HERMES_SHARED_AUTH_DIR"], str(product / "shared")
                )
                self.assertEqual(os.environ["HERMES_HOME_MODE"], "0700")
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
                {"PCBDRAFT_HERMES_HOME": str(Path(temporary) / "product")},
                clear=False,
            ),
        ):
            activate_provider_runtime()

            def select(_args=None, *, args=None) -> None:
                del _args, args
                from hermes_cli.config import read_raw_config, save_config

                config = read_raw_config()
                config["model"] = {
                    "provider": "custom",
                    "default": "board-model",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "api_key": secret,
                }
                save_config(config, strip_defaults=False)

            with patch("hermes_cli.main.select_provider_and_model", side_effect=select):
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
                    "PCBDRAFT_HERMES_HOME": str(Path(temporary) / "product"),
                    "HERMES_SHARED_AUTH_DIR": str(Path(temporary) / "standalone"),
                },
                clear=False,
            ),
        ):
            activate_provider_runtime()
            write_hermes_config()
            product = Path(os.environ["HERMES_HOME"])
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
                "hermes_cli.main.select_provider_and_model", side_effect=partial_write
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
                {"PCBDRAFT_HERMES_HOME": str(Path(temporary) / "product")},
                clear=False,
            ),
        ):
            activate_provider_runtime()
            write_hermes_config()
            product = Path(os.environ["HERMES_HOME"])

            def auth_then_cancel(_args=None, *, args=None) -> None:
                del _args, args
                (product / "auth.json").write_text(
                    '{"access_token":"partial-token"}\n', encoding="utf-8"
                )

            with patch(
                "hermes_cli.main.select_provider_and_model",
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
                {"PCBDRAFT_HERMES_HOME": str(Path(temporary) / "product")},
                clear=False,
            ),
        ):
            root = Path(temporary)
            activate_provider_runtime()
            write_hermes_config()
            external = root / "external-auth"
            external.mkdir()
            (Path(os.environ["HERMES_HOME"]) / "auth").symlink_to(
                external, target_is_directory=True
            )
            with (
                patch("hermes_cli.main.select_provider_and_model") as wizard,
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
                {"PCBDRAFT_HERMES_HOME": str(Path(temporary) / "product")},
                clear=False,
            ),
        ):
            activate_provider_runtime()
            write_hermes_config()
            from hermes_cli.config import read_raw_config, save_config

            config = read_raw_config()
            config["model"] = {"provider": "zai", "default": "glm-4.5"}
            save_config(config, strip_defaults=False)
            for error, expected in cases:
                with (
                    self.subTest(expected=expected),
                    patch(
                        "hermes_cli.runtime_provider.resolve_runtime_provider",
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

    def test_connect_timeout_covers_vendor_flows_that_ignore_args(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {"PCBDRAFT_HERMES_HOME": str(Path(temporary) / "product")},
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
                    "hermes_cli.main.select_provider_and_model",
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
                {"PCBDRAFT_HERMES_HOME": str(Path(temporary) / "product")},
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
                "hermes_cli.main.select_provider_and_model",
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
                {"PCBDRAFT_HERMES_HOME": str(Path(temporary) / "product")},
                clear=False,
            ),
            patch(
                "hermes_cli.main.select_provider_and_model",
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
                {"PCBDRAFT_HERMES_HOME": str(Path(temporary) / "product")},
                clear=False,
            ),
        ):
            activate_provider_runtime()
            from hermes_cli import auth, model_setup_flows

            observed: dict[str, object] = {}

            def select(_args=None, *, args=None) -> None:
                del _args
                observed["timeout"] = args.timeout
                observed["force"] = args.force
                observed["state"] = auth.get_provider_auth_state("minimax-oauth")
                observed["choice"] = model_setup_flows._prompt_auth_credentials_choice(
                    "MiniMax"
                )
                from hermes_cli.config import read_raw_config, save_config

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
                patch("hermes_cli.main.select_provider_and_model", side_effect=select),
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

    def test_picker_uses_hermes_groups_and_saved_custom_rows(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {"PCBDRAFT_HERMES_HOME": str(Path(temporary) / "product")},
                clear=False,
            ),
        ):
            activate_provider_runtime()
            write_hermes_config()
            from hermes_cli.config import read_raw_config, save_config

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
                patch("hermes_cli.auth.resolve_provider", return_value=None),
                patch(
                    "hermes_cli.main._prompt_provider_choice",
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
            patch("pcbdraft.interfaces.hermes_cli.activate"),
            patch(
                "pcbdraft.interfaces.hermes_cli.connection_status", return_value=missing
            ),
            patch("pcbdraft.interfaces.hermes_cli.connect", return_value=cancelled),
            patch("sys.stdin.isatty", return_value=True),
        ):
            self.assertEqual(launch_cli([]), 1)


class HermesIntentProviderTests(unittest.TestCase):
    def test_interpret_uses_selected_hermes_provider_and_safe_artifacts(self) -> None:
        install_vendor_path()
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
            provider = HermesIntentProvider("openai-codex", "gpt-5-codex")
            with patch(
                "agent.auxiliary_client.call_llm", return_value=response
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
            "hermes-config",
        )
        with patch(
            "pcbdraft.services.provider_connection.connection_status",
            return_value=selected,
        ):
            provider = resolve_provider("auto")
        self.assertIsInstance(provider, HermesIntentProvider)
        assert provider is not None
        self.assertEqual(provider.provider_id, "anthropic")

    def test_provider_failure_receipt_is_classified_and_secret_free(self) -> None:
        install_vendor_path()

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
            provider = HermesIntentProvider("zai", "glm-test")
            with (
                patch(
                    "agent.auxiliary_client.call_llm",
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
        install_vendor_path()
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
                "agent.auxiliary_client.call_llm", return_value=response
            ) as call:
                for index, provider_id in enumerate(providers):
                    with self.subTest(provider=provider_id):
                        provider = HermesIntentProvider(provider_id, "board-model")
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


class HermesVendorContractTests(unittest.TestCase):
    def test_provider_adapter_contract_is_present(self) -> None:
        install_vendor_path()
        from agent.auxiliary_client import call_llm, extract_content_or_reasoning
        from hermes_cli.main import select_provider_and_model
        from hermes_cli.models import CANONICAL_PROVIDERS
        from hermes_cli.runtime_provider import resolve_runtime_provider

        self.assertTrue(CANONICAL_PROVIDERS)
        self.assertTrue(callable(select_provider_and_model))
        self.assertTrue(callable(resolve_runtime_provider))
        self.assertTrue(callable(call_llm))
        self.assertTrue(callable(extract_content_or_reasoning))

    def test_required_auth_and_transport_classes_remain_available(self) -> None:
        install_vendor_path()
        from agent.auxiliary_client import (
            AnthropicAuxiliaryClient,
            BedrockAuxiliaryClient,
            CodexAuxiliaryClient,
        )
        from agent.copilot_acp_client import CopilotACPClient
        from hermes_cli.auth import PROVIDER_REGISTRY, ZAI_ENDPOINTS

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
        install_vendor_path()
        from hermes_cli import auth

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
        install_vendor_path()
        from agent import anthropic_adapter, bedrock_adapter, vertex_adapter
        from agent import auxiliary_client as auxiliary
        from hermes_cli import auth

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
                "agent.copilot_acp_client.CopilotACPClient",
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
