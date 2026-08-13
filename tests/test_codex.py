from __future__ import annotations

import unittest
from pathlib import Path

from copperwright.codex import build_codex_argv, patch_schema


class CodexArgvTests(unittest.TestCase):
    def test_pinned_model_reasoning_tier_and_disabled_features(self) -> None:
        argv = build_codex_argv(
            executable="/opt/bin/codex",
            project=Path("/project"),
            schema_path=Path("/run/schema.json"),
            last_message_path=Path("/run/final.json"),
        )
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-5.6-sol")
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("--ignore-rules", argv)
        self.assertIn("--strict-config", argv)
        self.assertEqual(argv[argv.index("--enable") + 1], "use_legacy_landlock")
        configs = [
            argv[index + 1]
            for index, value in enumerate(argv[:-1])
            if value == "--config"
        ]
        self.assertIn('model_reasoning_effort="max"', configs)
        self.assertIn('service_tier="default"', configs)
        self.assertIn("features.fast_mode=false", configs)
        self.assertIn("features.hooks=false", configs)
        self.assertIn("agents.enabled=false", configs)
        self.assertIn("features.multi_agent=false", configs)
        self.assertIn("features.multi_agent_v2=false", configs)
        self.assertIn("features.apps=false", configs)
        self.assertIn("features.browser_use=false", configs)
        self.assertIn("features.computer_use=false", configs)
        self.assertIn("features.image_generation=false", configs)
        self.assertIn("features.plugins=false", configs)
        self.assertIn("features.skill_search=false", configs)
        self.assertIn("features.tool_suggest=false", configs)
        self.assertIn("tools.web_search=false", configs)
        self.assertIn('approval_policy="never"', configs)
        self.assertIn("allow_login_shell=false", configs)
        self.assertEqual(argv[-1], "-")

    def test_prompt_is_not_an_argv_field(self) -> None:
        argv = build_codex_argv(
            executable="codex",
            project=Path("/project"),
            schema_path=Path("/run/schema.json"),
            last_message_path=Path("/run/final.json"),
        )
        self.assertNotIn("secret prompt", argv)
        self.assertEqual(argv.count("-"), 1)

    def test_patch_schema_operation_has_explicit_type_for_codex(self) -> None:
        operation = patch_schema()["properties"]["operations"]["items"]["properties"][
            "op"
        ]
        self.assertEqual(operation, {"type": "string", "enum": ["replace_text"]})


if __name__ == "__main__":
    unittest.main()
