from __future__ import annotations

import inspect
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.agent.extensions import manager as extension_manager
from pcbdraft.model import auxiliary_client, auxiliary_task_config, configuration


class AuxiliaryTaskConfigContractTests(unittest.TestCase):
    def setUp(self) -> None:
        auxiliary_client._reset_aux_semaphores()

    def tearDown(self) -> None:
        auxiliary_client._reset_aux_semaphores()

    def test_extracted_module_has_no_reverse_import(self):
        source = Path(auxiliary_task_config.__file__).read_text(encoding="utf-8")

        self.assertNotIn("import auxiliary_client", source)
        self.assertNotIn("from pcbdraft.model.auxiliary_client", source)

    def test_legacy_symbols_keep_identity_and_host_authority_boundary(self):
        function_names = (
            "_get_auxiliary_task_config",
            "_get_task_timeout",
            "_effective_aux_timeout",
            "_get_task_extra_body",
            "_get_task_max_concurrency",
            "_acquire_sync_aux_semaphore",
            "_acquire_async_aux_semaphore",
            "_reset_aux_semaphores",
        )
        for name in function_names:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(auxiliary_client, name),
                    getattr(auxiliary_task_config, name),
                )

        self.assertIs(
            auxiliary_client._aux_sync_semaphores,
            auxiliary_task_config._aux_sync_semaphores,
        )
        self.assertIs(
            auxiliary_client._aux_async_semaphores,
            auxiliary_task_config._aux_async_semaphores,
        )
        self.assertIs(
            auxiliary_client._aux_sem_lock,
            auxiliary_task_config._aux_sem_lock,
        )
        self.assertIs(
            inspect.getmodule(auxiliary_client._get_cached_client),
            auxiliary_client,
        )
        self.assertIs(
            inspect.getmodule(auxiliary_client.resolve_provider_client),
            auxiliary_client,
        )

    def test_task_config_layers_plugin_defaults_under_user_config(self):
        with (
            patch.object(
                configuration,
                "load_config_readonly",
                return_value={
                    "auxiliary": {
                        "plugin_task": {
                            "timeout": 45,
                            "extra_body": {"user": True},
                        }
                    }
                },
            ),
            patch.object(
                extension_manager,
                "get_plugin_auxiliary_tasks",
                return_value=[
                    {
                        "key": "plugin_task",
                        "defaults": {"timeout": 30, "max_concurrency": 2},
                    }
                ],
            ),
        ):
            result = auxiliary_client._get_auxiliary_task_config("plugin_task")

        self.assertEqual(
            result,
            {
                "timeout": 45,
                "max_concurrency": 2,
                "extra_body": {"user": True},
            },
        )

    def test_timeout_helpers_use_legacy_hooks_and_compression_floor(self):
        with patch.object(
            auxiliary_client,
            "_get_auxiliary_task_config",
            return_value={"timeout": "17.5"},
        ) as get_config:
            self.assertEqual(auxiliary_client._get_task_timeout("title"), 17.5)
        get_config.assert_called_once_with("title")

        with (
            patch.object(
                auxiliary_client,
                "_get_task_timeout",
                return_value=120.0,
            ) as get_timeout,
            patch.object(
                auxiliary_client,
                "_COMPRESSION_TIMEOUT_FLOOR_SECONDS",
                240.0,
            ),
        ):
            self.assertEqual(
                auxiliary_client._effective_aux_timeout("compression", None),
                240.0,
            )
            self.assertEqual(
                auxiliary_client._effective_aux_timeout("compression", 20.0),
                20.0,
            )
        get_timeout.assert_called_once_with("compression")

    def test_extra_body_adds_reasoning_without_mutating_config(self):
        configured_body = {"metadata": {"source": "test"}}
        with patch.object(
            auxiliary_client,
            "_get_auxiliary_task_config",
            return_value={
                "extra_body": configured_body,
                "reasoning_effort": "high",
            },
        ):
            result = auxiliary_client._get_task_extra_body("compression")

        self.assertEqual(
            result,
            {
                "metadata": {"source": "test"},
                "reasoning": {"enabled": True, "effort": "high"},
            },
        )
        self.assertIsNot(result, configured_body)
        self.assertNotIn("reasoning", configured_body)

    def test_max_concurrency_normalizes_values_and_excludes_vision(self):
        with patch.object(
            auxiliary_client,
            "_get_auxiliary_task_config",
            return_value={"max_concurrency": "3"},
        ) as get_config:
            self.assertEqual(
                auxiliary_client._get_task_max_concurrency("compression"),
                3,
            )
            self.assertIsNone(auxiliary_client._get_task_max_concurrency("vision"))

        get_config.assert_called_once_with("compression")

    def test_sync_semaphore_is_cached_and_rebuilt_when_limit_changes(self):
        with patch.object(
            auxiliary_client,
            "_get_task_max_concurrency",
            return_value=2,
        ):
            first = auxiliary_client._acquire_sync_aux_semaphore("compression")
            second = auxiliary_client._acquire_sync_aux_semaphore("compression")

        self.assertIs(first, second)
        self.assertIsInstance(first, threading.BoundedSemaphore)

        with patch.object(
            auxiliary_client,
            "_get_task_max_concurrency",
            return_value=3,
        ):
            rebuilt = auxiliary_client._acquire_sync_aux_semaphore("compression")

        self.assertIsNot(rebuilt, first)
        self.assertEqual(
            auxiliary_client._aux_sync_semaphores["compression"],
            (3, rebuilt),
        )


class AuxiliaryTaskConfigAsyncContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        auxiliary_client._reset_aux_semaphores()

    async def asyncTearDown(self) -> None:
        auxiliary_client._reset_aux_semaphores()

    async def test_async_semaphore_is_cached_per_running_loop(self):
        with patch.object(
            auxiliary_client,
            "_get_task_max_concurrency",
            return_value=2,
        ):
            first = auxiliary_client._acquire_async_aux_semaphore("title")
            second = auxiliary_client._acquire_async_aux_semaphore("title")

        self.assertIs(first, second)
        await first.acquire()
        await first.acquire()
        self.assertTrue(first.locked())
        first.release()
        first.release()


if __name__ == "__main__":
    unittest.main()
