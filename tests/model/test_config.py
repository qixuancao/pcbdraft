from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pcbdraft.core.errors import ValidationError
from pcbdraft.model.api import OpenAICompatibleSettings
from pcbdraft.model.config import (
    connect_provider,
    load_model_config,
    select_model,
)


class ModelConfigTests(unittest.TestCase):
    def test_connect_and_select_round_trip_private_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pcbdraft" / "config.toml"
            config = connect_provider("deepseek", api_key="sk-test", path=path)

            self.assertEqual(config.active_provider, "deepseek")
            self.assertEqual(config.active_model, "deepseek-v4-pro")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn("sk-test", repr(config))

            switched = select_model("deepseek", "deepseek-v4-flash", path=path)
            self.assertEqual(switched.active_model, "deepseek-v4-flash")
            with patch.dict(os.environ, {"PCBDRAFT_CONFIG": str(path)}):
                settings = OpenAICompatibleSettings.from_config()
            assert settings is not None
            self.assertEqual(settings.provider_id, "deepseek")
            self.assertEqual(settings.model, "deepseek-v4-flash")
            self.assertEqual(settings.api_key, "sk-test")

    def test_custom_provider_is_available_to_the_model_picker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            config = connect_provider(
                "lab-provider",
                api_key="local-secret",
                base_url="http://127.0.0.1:8080/v1",
                model="board-model",
                name="Lab provider",
                path=path,
            )
            choices = config.choices("board")
            self.assertEqual(len(choices), 1)
            self.assertEqual(choices[0].label, "Lab provider / board-model")

    def test_missing_config_is_auto_generated_with_private_template(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pcbdraft" / "config.toml"
            self.assertFalse(path.exists())
            config = load_model_config(path)
            self.assertTrue(path.exists())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIsNone(config.active)
            self.assertEqual(len(config.providers), 0)
            self.assertIn("version = 1", path.read_text(encoding="utf-8"))
            self.assertIn("/connect", path.read_text(encoding="utf-8"))
            reloaded = load_model_config(path)
            self.assertEqual(reloaded.path, path)
            self.assertIsNone(reloaded.active)

    def test_config_with_secret_must_not_be_group_or_world_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                """version = 1
active_provider = "deepseek"
active_model = "deepseek-v4-pro"

[providers.deepseek]
name = "DeepSeek"
base_url = "https://api.deepseek.com"
api_key = "sk-test"
models = ["deepseek-v4-pro"]
""",
                encoding="utf-8",
            )
            path.chmod(0o644)
            with self.assertRaisesRegex(ValidationError, "chmod 600"):
                load_model_config(path)


if __name__ == "__main__":
    unittest.main()
