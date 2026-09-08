"""Offline behavior for the optional PCBDraft plugin index."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.interfaces.tui import plugin_index

_INDEX_URL = "https://plugins.example.test/pcbdraft.json"
_INDEX_TEXT = """{
  "plugins": [
    {
      "name": "pcb-review",
      "description": "Review a PCB project",
      "repo": "example/pcb-review",
      "ref": "0123456789012345678901234567890123456789"
    }
  ]
}"""


class _Response:
    text = _INDEX_TEXT

    @staticmethod
    def raise_for_status() -> None:
        return None


class PluginIndexTests(unittest.TestCase):
    def test_unconfigured_index_is_empty_without_cache_or_network(self) -> None:
        with (
            patch("pcbdraft.model.configuration.load_config_readonly", return_value={}),
            patch.object(plugin_index, "_read_cache") as read_cache,
            patch.object(plugin_index, "_fetch_remote") as fetch_remote,
        ):
            entries, source = plugin_index.load_index(refresh=True)

        self.assertEqual(entries, [])
        self.assertEqual(source, "none")
        read_cache.assert_not_called()
        fetch_remote.assert_not_called()

    def test_configured_index_fetches_and_uses_its_cache_offline(self) -> None:
        config = {"plugins": {"index_url": f"  {_INDEX_URL}  "}}
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch(
                    "pcbdraft.model.configuration.load_config_readonly",
                    return_value=config,
                ),
                patch.object(
                    plugin_index, "get_runtime_home", return_value=Path(temporary)
                ),
                patch("httpx.get", return_value=_Response()) as get,
            ):
                entries, source = plugin_index.load_index(refresh=True)
                cached_entries, cached_source = plugin_index.load_index(
                    refresh=True, offline=True
                )

            with (
                patch(
                    "pcbdraft.model.configuration.load_config_readonly",
                    return_value=config,
                ),
                patch.object(
                    plugin_index, "get_runtime_home", return_value=Path(temporary)
                ),
                patch("httpx.get", side_effect=OSError("offline")),
            ):
                fallback_entries, fallback_source = plugin_index.load_index(
                    refresh=True
                )

            self.assertEqual([entry.name for entry in entries], ["pcb-review"])
            self.assertEqual(source, "remote")
            self.assertEqual([entry.name for entry in cached_entries], ["pcb-review"])
            self.assertEqual(cached_source, "cache")
            self.assertEqual([entry.name for entry in fallback_entries], ["pcb-review"])
            self.assertEqual(fallback_source, "cache")
            get.assert_called_once_with(
                _INDEX_URL,
                timeout=plugin_index._FETCH_TIMEOUT,
                follow_redirects=True,
            )

    def test_cache_is_isolated_by_configured_url(self) -> None:
        first_config = {"plugins": {"index_url": _INDEX_URL}}
        second_config = {
            "plugins": {"index_url": "https://other.example.test/index.json"}
        }
        with tempfile.TemporaryDirectory() as temporary:
            runtime_home = Path(temporary)
            with (
                patch(
                    "pcbdraft.model.configuration.load_config_readonly",
                    return_value=first_config,
                ),
                patch.object(
                    plugin_index, "get_runtime_home", return_value=runtime_home
                ),
                patch("httpx.get", return_value=_Response()),
            ):
                plugin_index.load_index(refresh=True)

            with (
                patch(
                    "pcbdraft.model.configuration.load_config_readonly",
                    return_value=second_config,
                ),
                patch.object(
                    plugin_index, "get_runtime_home", return_value=runtime_home
                ),
                patch("httpx.get", side_effect=OSError("offline")),
            ):
                entries, source = plugin_index.load_index(refresh=True)

        self.assertEqual(entries, [])
        self.assertEqual(source, "none")


if __name__ == "__main__":
    unittest.main()
