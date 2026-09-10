"""Offline regression coverage for the native lifecycle ownership boundary."""

from __future__ import annotations

import argparse
import ast
import contextlib
import copy
import importlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft.core.errors import PCBDraftError

MODULES = (
    "main",
    "_parser",
    "_startup_fast",
    "_early_recovery",
    "_install_repair",
    "relaunch",
    "linux_desktop_entry",
    "windows_ssh_runtime",
    "gateway",
    "gateway_windows",
    "service_manager",
    "container_boot",
    "dashboard_procs",
    "uninstall",
    "gui_uninstall",
    "update_cmd",
    "update_lock",
    "managed_uv",
    "completion",
    "config_defaults",
    "config_migrations",
    "profiles",
)


class NativeLifecycleMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pcbdraft-lifecycle-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "HOME": str(self.root),
                    "PCBDRAFT_CONFIG": str(self.root / "native" / "config.json"),
                },
                clear=True,
            )
        )
        for target in (
            "subprocess.run",
            "subprocess.Popen",
            "os.execvp",
            "os.kill",
            "socket.create_connection",
            "socket.socket.connect",
        ):
            mocked = self.stack.enter_context(
                patch(
                    target,
                    side_effect=AssertionError(f"unexpected external action: {target}"),
                )
            )
            self.addCleanup(mocked.assert_not_called)

    def module(self, name):
        return importlib.import_module("pcbdraft.interfaces.tui." + name)

    def test_owned_modules_import_without_install_network_or_service_actions(self):
        for name in MODULES:
            with self.subTest(module=name):
                self.module(name)
        self.assertFalse((self.root / "native").exists())

    def test_importing_main_does_not_consume_public_arguments(self):
        argv = ["pcbdraft", "--profile", "foreign", "--version"]
        with (
            patch("sys.argv", argv),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            importlib.reload(self.module("main"))
        self.assertEqual(argv, ["pcbdraft", "--profile", "foreign", "--version"])
        self.assertEqual(output.getvalue(), "")
        self.assertFalse((self.root / "native").exists())

    def test_model_flow_import_contract_is_preserved(self):
        flow_module = importlib.import_module("pcbdraft.model.model_setup_flows")
        tree = ast.parse(Path(flow_module.__file__).read_text(encoding="utf-8"))
        main = self.module("main")
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == main.__name__:
                for alias in node.names:
                    with self.subTest(helper=alias.name):
                        self.assertIsNotNone(getattr(main, alias.name))

    def test_owned_sources_have_no_old_namespace_or_missing_messaging_import(self):
        for name in MODULES:
            with self.subTest(module=name):
                source = Path(self.module(name).__file__).read_text(encoding="utf-8")
                self.assertNotIn("hermes", source.lower())
                self.assertNotIn("services.messaging", source)

    def test_all_retired_operations_fail_before_touching_old_data(self):
        standalone = self.root / ".hermes"
        standalone.mkdir()
        sentinel = standalone / "gateway.pid"
        sentinel.write_text("12345\n", encoding="utf-8")
        native = self.root / "native" / "runtime"
        native.mkdir(parents=True)
        marker = native / ".update-incomplete"
        marker.write_text("old recovery data", encoding="utf-8")
        operations = {
            "main": (
                "cmd_update",
                "cmd_uninstall",
                "cmd_gateway",
                "cmd_gui",
                "_run_install_with_heartbeat",
            ),
            "update_cmd": ("cmd_update", "_cmd_update_impl", "_cmd_update_check"),
            "uninstall": (
                "run_uninstall",
                "remove_wrapper_script",
                "uninstall_gateway_service",
            ),
            "gui_uninstall": ("uninstall_gui",),
            "gateway": (
                "systemd_start",
                "systemd_stop",
                "launchd_uninstall",
                "kill_gateway_processes",
            ),
            "gateway_windows": ("install", "uninstall", "restart", "stop"),
            "container_boot": ("reconcile_profile_gateways",),
            "dashboard_procs": (
                "_kill_stale_dashboard_processes",
                "_reap_orphaned_desktop_local_serves",
            ),
            "windows_ssh_runtime": (
                "spawn_backend",
                "terminate_owned",
                "remove_artifact",
            ),
            "linux_desktop_entry": ("install_desktop_entry",),
            "managed_uv": ("repair_vulnerable_runtime", "update_managed_uv"),
            "_install_repair": ("run_core_install",),
            "profiles": (
                "delete_profile",
                "remove_wrapper_script",
                "_stop_gateway_process",
                "_cleanup_gateway_service",
            ),
        }
        for name, functions in operations.items():
            for function in functions:
                with (
                    self.subTest(module=name, function=function),
                    self.assertRaisesRegex(PCBDraftError, "Unsupported lifecycle"),
                ):
                    getattr(self.module(name), function)(standalone)
        self.assertFalse(self.module("_early_recovery").recover_if_needed())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "12345\n")
        self.assertEqual(marker.read_text(encoding="utf-8"), "old recovery data")

    def test_gateway_queries_do_not_claim_foreign_processes(self):
        gateway = self.module("gateway")
        self.assertEqual(gateway.find_gateway_pids(), [])
        self.assertEqual(gateway.get_gateway_runtime_snapshot().manager, "unsupported")
        self.assertFalse(gateway.get_gateway_runtime_snapshot().running)
        self.assertTrue(gateway.get_service_name().startswith("pcbdraft-"))
        managers = self.module("service_manager")
        self.assertEqual(managers.detect_service_manager(), "none")
        for cls in (
            managers.S6ServiceManager,
            managers.SystemdServiceManager,
            managers.LaunchdServiceManager,
            managers.WindowsServiceManager,
        ):
            manager = cls()
            self.assertFalse(manager.supports_runtime_registration())
            with self.assertRaises(PCBDraftError):
                manager.stop("hermes-gateway")

    def test_public_parser_is_the_only_command_tree(self):
        parser, subcommands, chat = self.module("_parser").build_top_level_parser()
        self.assertIsNone(chat)
        self.assertEqual(
            set(subcommands.choices),
            {"connect", "doctor", "setup", "repository", "trace", "gui"},
        )
        for command in ("gateway", "desktop", "update", "uninstall", "serve", "chat"):
            with (
                self.subTest(command=command),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as failure:
                    parser.parse_args([command])
                self.assertEqual(failure.exception.code, 2)
        with patch("pcbdraft.interfaces.cli.main", return_value=17) as public:
            self.assertEqual(self.module("main").main(["doctor"]), 17)
            public.assert_called_once_with(["doctor"])

    def test_relaunch_uses_native_module_and_root_flags(self):
        relaunch = self.module("relaunch")
        with patch.object(relaunch, "resolve_pcbdraft_bin", return_value=None):
            argv = relaunch.build_relaunch_argv(
                ["gui"],
                original_argv=[
                    "--workspace",
                    "/a path",
                    "--approval-mode=workspace",
                    "--project",
                    "board",
                ],
            )
        self.assertEqual(argv[1:3], ["-m", "pcbdraft"])
        self.assertEqual(
            argv[3:],
            [
                "--workspace",
                "/a path",
                "--approval-mode=workspace",
                "--project",
                "board",
                "gui",
            ],
        )
        for args in (["desktop"], ["serve"], ["chat"], ["--resume", "old-session"]):
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                relaunch.build_relaunch_argv(args, preserve_inherited=False)
        self.assertEqual(
            relaunch._extract_inherited_flags(["connect", "--timeout", "5"]), []
        )

    def test_relaunch_does_not_reexecute_arbitrary_argv_zero(self):
        relaunch = self.module("relaunch")
        foreign = self.root / "hermes"
        foreign.write_text("#!/bin/sh\n", encoding="utf-8")
        foreign.chmod(0o700)
        with (
            patch("sys.argv", [str(foreign)]),
            patch.object(relaunch.shutil, "which", return_value=None),
        ):
            self.assertIsNone(relaunch.resolve_pcbdraft_bin())

    def test_startup_default_uses_core_platform_home_not_standalone(self):
        from pcbdraft.core import platform_paths

        fast = self.module("_startup_fast")
        (self.root / ".hermes").mkdir()
        (self.root / ".hermes" / ".install_method").write_text(
            "foreign", encoding="utf-8"
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(self.root / ".hermes")}):
            self.assertEqual(
                Path(fast._resolved_home()),
                platform_paths.pcbdraft_config_dir() / "runtime",
            )
            self.assertIsNone(fast.read_install_method())
        from pcbdraft.core.resources import PACKAGE_ROOT

        self.assertEqual(Path(fast.project_root_str()), PACKAGE_ROOT)
        with patch.dict(
            os.environ, {"PCBDRAFT_RUNTIME_HOME": str(self.root / "explicit")}
        ):
            self.assertEqual(Path(fast._resolved_home()), self.root / "explicit")

    def test_fast_home_matches_platform_defaults(self):
        from pcbdraft.core import platform_paths

        for system, suffix in (
            ("Linux", ".config/pcbdraft/runtime"),
            ("Darwin", "Library/Application Support/pcbdraft/runtime"),
            ("Windows", "AppData/Roaming/pcbdraft/runtime"),
        ):
            with (
                self.subTest(system=system),
                patch.dict(os.environ, {"HOME": str(self.root)}, clear=True),
                patch.object(platform_paths.platform, "system", return_value=system),
            ):
                self.assertEqual(
                    Path(self.module("_startup_fast")._resolved_home()),
                    self.root / suffix,
                )

    def test_uv_lookup_never_bootstraps_or_uses_foreign_home(self):
        uv = self.module("managed_uv")
        self.assertIsNone(uv.ensure_uv())
        self.assertEqual(
            uv.managed_uv_path().parent, self.root / "native" / "runtime" / "bin"
        )
        self.assertFalse(uv.managed_uv_path().parent.exists())
        self.assertIn(".pcbdraft-runtime", uv.managed_python_install_dir().parts)

    def test_old_update_markers_are_inert_even_when_explicitly_supplied(self):
        lock = self.module("update_lock")
        old = self.root / ".hermes-update-in-progress"
        old.write_text("not a live pid", encoding="utf-8")
        self.assertIsNone(lock.read_live_update(path=old))
        self.assertFalse(lock.UpdateLock(path=old).acquire())
        self.assertEqual(old.read_text(encoding="utf-8"), "not a live pid")
        self.assertEqual(lock.update_marker_path().name, ".pcbdraft-update-in-progress")

    def test_native_profile_metadata_is_readable_without_adopting_services(self):
        profiles = self.module("profiles")
        native = self.root / "native" / "runtime"
        work = native / "profiles" / "work"
        work.mkdir(parents=True)
        (work / "config.yaml").write_text(
            "model:\n  provider: openai-api\n  default: test-model\n", encoding="utf-8"
        )
        (work / "profile.yaml").write_text(
            "description: Existing work\n", encoding="utf-8"
        )
        (work / "gateway.pid").write_text("12345", encoding="utf-8")
        info = next(
            profile for profile in profiles.list_profiles() if profile.name == "work"
        )
        self.assertEqual(info.description, "Existing work")
        self.assertEqual(info.model, "test-model")
        self.assertFalse(info.gateway_running)
        self.assertEqual(profiles.resolve_profile_env("work"), str(work))
        (native / "profiles" / "foreign").symlink_to(self.root)
        self.assertFalse(profiles.profile_exists("foreign"))
        with self.assertRaises(ValueError):
            profiles.get_profile_dir("../../.hermes")

    def test_completions_only_advertise_real_commands(self):
        completion = self.module("completion")
        stale = argparse.ArgumentParser()
        stale.add_subparsers().add_parser("gateway")
        for generate in (
            completion.generate_bash,
            completion.generate_zsh,
            completion.generate_fish,
        ):
            script = generate(stale)
            self.assertIn("pcbdraft", script)
            self.assertIn("connect", script)
            for obsolete in ("hermes", "gateway", "desktop", "uninstall", "--profile"):
                self.assertNotIn(obsolete, script)

    def test_existing_reserved_profile_names_remain_readable(self):
        profiles = self.module("profiles")
        native = self.root / "native" / "runtime"
        names = ("pcbdraft", "python", "python3", "bin", "profiles", "test")
        for name in names:
            path = native / "profiles" / name
            path.mkdir(parents=True)
            (path / "config.yaml").write_text(
                f"model:\n  default: {name}-model\n  provider: custom\n",
                encoding="utf-8",
            )
        listed = {item.name: item for item in profiles.list_profiles()}
        for name in names:
            with self.subTest(name=name):
                path = native / "profiles" / name
                before = (path / "config.yaml").read_bytes()
                profiles.validate_profile_name(name)
                with self.assertRaisesRegex(ValueError, "reserved profile name"):
                    profiles.validate_new_profile_name(name)
                self.assertTrue(profiles.profile_exists(name))
                self.assertEqual(profiles.get_profile_dir(name), path)
                self.assertEqual(profiles.resolve_profile_env(name), str(path))
                self.assertEqual(listed[name].path, path)
                self.assertEqual(listed[name].model, f"{name}-model")
                self.assertEqual(listed[name].provider, "custom")
                (native / "active_profile").write_text(name, encoding="utf-8")
                self.assertEqual(profiles.get_active_profile(), name)
                with patch.dict(os.environ, {"PCBDRAFT_RUNTIME_HOME": str(path)}):
                    self.assertEqual(profiles.get_active_profile_name(), name)
                self.assertEqual((path / "config.yaml").read_bytes(), before)
                with self.assertRaises(PCBDraftError):
                    profiles.create_profile(name)
        for invalid in ("../python", "bin/child", "", "bad name", "a" * 65):
            with self.subTest(invalid=invalid):
                self.assertFalse(profiles.profile_exists(invalid))
                with self.assertRaises(ValueError):
                    profiles.resolve_profile_env(invalid)

    def test_profile_model_reader_and_list_share_stored_config_values(self):
        profiles = self.module("profiles")
        native = self.root / "native" / "runtime"
        cases = (
            ("scalar", "model: legacy-model\n", ("legacy-model", None)),
            (
                "mapping",
                "model:\n  default: preferred\n  model: fallback\n  provider: custom\n",
                ("preferred", "custom"),
            ),
            (
                "fallback",
                "model:\n  model: fallback\n  provider: openai-api\n",
                ("fallback", "openai-api"),
            ),
            ("missing", None, (None, None)),
            ("malformed", "model: [\n", (None, None)),
            ("invalid", "model: [unexpected]\n", (None, None)),
        )
        for name, text, expected in cases:
            path = native / "profiles" / name
            path.mkdir(parents=True)
            if text is not None:
                (path / "config.yaml").write_text(text, encoding="utf-8")
            with self.subTest(name=name):
                self.assertEqual(profiles._read_config_model(path), expected)
        with patch.object(
            profiles, "_read_config_model", wraps=profiles._read_config_model
        ) as read_model:
            listed = {item.name: item for item in profiles.list_profiles()}
        for name, text, expected in cases:
            with self.subTest(name=name):
                path = native / "profiles" / name
                read_model.assert_any_call(path)
                self.assertEqual((listed[name].model, listed[name].provider), expected)
                if text is None:
                    self.assertFalse((path / "config.yaml").exists())
                else:
                    self.assertEqual(
                        (path / "config.yaml").read_text(encoding="utf-8"), text
                    )

    def test_profile_describer_receives_existing_profile_model_and_provider(self):
        describer = self.module("profile_describer")
        path = self.root / "native" / "runtime" / "profiles" / "python"
        path.mkdir(parents=True)
        (path / "config.yaml").write_text(
            "model:\n  default: board-model\n  provider: custom\n", encoding="utf-8"
        )
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"description": "Reviews circuit plans."}'
                    )
                )
            ]
        )
        with patch(
            "pcbdraft.model.auxiliary_client.call_llm", return_value=response
        ) as call_llm:
            outcome = describer.describe_profile("python")
        self.assertTrue(outcome.ok, outcome.reason)
        prompt = call_llm.call_args.kwargs["messages"][1]["content"]
        self.assertIn("Default model: board-model", prompt)
        self.assertIn("Provider: custom", prompt)
        self.assertNotIn("(unset)", prompt)

    def test_lazy_main_exports_resolve_actual_native_modules(self):
        main = self.module("main")
        for module, names in main._LAZY_COMMAND_EXPORTS.items():
            for name in names:
                self.assertIs(
                    getattr(main, name), getattr(importlib.import_module(module), name)
                )
        self.assertTrue(callable(main._model_flow_nous))

    def test_custom_provider_templates_remain_references(self):
        main = self.module("main")
        loaded = {
            "name": "Local",
            "base_url": "https://example.invalid/v1",
            "api_key": "expanded-secret",
            "model": "m",
        }
        raw = {
            "providers": {
                "local": {
                    "name": "Local",
                    "base_url": "${LOCAL_URL}",
                    "api_key": "${LOCAL_KEY}",
                    "model": "m",
                }
            }
        }
        with (
            patch("pcbdraft.model.configuration.read_raw_config", return_value=raw),
            patch(
                "pcbdraft.model.configuration.get_compatible_custom_providers",
                return_value=[loaded],
            ),
        ):
            info = next(iter(main._named_custom_provider_map({}).values()))
        self.assertEqual(
            main._custom_provider_api_key_config_value(info, "expanded-secret"),
            "${LOCAL_KEY}",
        )
        self.assertEqual(
            main._custom_provider_base_url_config_value(info, loaded["base_url"]),
            "${LOCAL_URL}",
        )

    def test_auxiliary_routing_preserves_nonrouting_and_main_config(self):
        main = self.module("main")
        config = {
            "model": {"default": "main-model"},
            "auxiliary": {"vision": {"timeout": 123}},
            "delegation": {"max_spawn_depth": 2},
        }
        with (
            patch(
                "pcbdraft.model.configuration.load_config",
                side_effect=lambda: copy.deepcopy(config),
            ),
            patch("pcbdraft.model.configuration.save_config") as save,
        ):
            main._save_aux_choice("vision", provider="native", model="vision-model")
            changed = save.call_args.args[0]
            self.assertEqual(changed["model"], config["model"])
            self.assertEqual(changed["auxiliary"]["vision"]["timeout"], 123)
            main._save_aux_choice("delegation", provider="auto")
            self.assertEqual(save.call_args.args[0]["delegation"]["provider"], "")
            self.assertEqual(save.call_args.args[0]["delegation"]["max_spawn_depth"], 2)

    def test_connection_picker_dispatch_and_cancel_remain_available(self):
        main = self.module("main")
        config = {"model": {"default": "before", "provider": "nous"}}
        with (
            patch("pcbdraft.model.configuration.load_config", return_value=config),
            patch("pcbdraft.model.configuration.get_env_value", return_value=""),
            patch(
                "pcbdraft.model.configuration.get_compatible_custom_providers",
                return_value=[],
            ),
            patch(
                "pcbdraft.model.provider_config.resolve_provider_full",
                return_value=SimpleNamespace(id="nous", source="builtin"),
            ),
            patch(
                "pcbdraft.model.catalog.group_providers",
                return_value=[{"kind": "leaf", "slug": "nous"}],
            ),
            patch.object(main, "_named_custom_provider_map", return_value={}),
            patch.object(main, "_prompt_provider_choice", side_effect=[0, None]),
            patch.object(main, "_model_flow_nous") as flow,
            patch.object(main, "_clear_stale_openai_base_url"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            args = SimpleNamespace(no_browser=True)
            main.select_provider_and_model(args)
            flow.assert_called_once_with(config, "before", args=args)
            main.select_provider_and_model(args)
            self.assertEqual(flow.call_count, 1)

    def test_public_connection_handoff_still_persists_native_config(self):
        from pcbdraft.services.provider_connection import connect

        def select(*, args=None):
            from pcbdraft.model.configuration import read_raw_config, save_config

            config = read_raw_config()
            config["model"] = {
                "provider": "custom",
                "default": "board-model",
                "base_url": "http://127.0.0.1:11434/v1",
                "api_key": "local-test-key",
            }
            save_config(config, strip_defaults=False)

        with patch.object(
            self.module("main"), "select_provider_and_model", side_effect=select
        ):
            result = connect()
        self.assertEqual(result.outcome, "changed")
        self.assertTrue(result.usable)
        self.assertEqual(result.model, "board-model")
        self.assertTrue((self.root / "native" / "runtime" / "config.yaml").is_file())
        self.assertNotIn("local-test-key", repr(result))

    def test_public_connection_cancellation_retains_existing_data(self):
        from pcbdraft.services.provider_connection import connect

        root = self.root / "native" / "runtime"
        root.mkdir(parents=True)
        env = root / ".env"
        env.write_text("LOCAL_KEY=previous-value\n", encoding="utf-8")

        def cancel(*, args=None):
            env.write_text("LOCAL_KEY=partial-write\n", encoding="utf-8")
            raise KeyboardInterrupt

        with patch.object(
            self.module("main"), "select_provider_and_model", side_effect=cancel
        ):
            result = connect()
        self.assertEqual(result.outcome, "cancelled")
        self.assertEqual(env.read_text(encoding="utf-8"), "LOCAL_KEY=previous-value\n")


if __name__ == "__main__":
    unittest.main()
