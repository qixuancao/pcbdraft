from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from copperwright.api import serve
from copperwright.benchmark import bundled_requirements_path
from copperwright.errors import CopperWrightError, ValidationError
from copperwright.external_evidence import MAX_ARTIFACTS, record_external_evidence
from copperwright.managed import generate_managed_project, open_managed_project
from copperwright.project import TREE_MEMBER_LIMIT, validate_agent_tree
from copperwright.release import build_manufacturing_release


class HostileInputSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="copperwright-security-")
        cls.root = Path(cls.temporary.name)
        cls.project = generate_managed_project(
            bundled_requirements_path(), cls.root / "project"
        ).project

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_managed_manifest_path_escape_and_hardlink_are_rejected(self) -> None:
        escaped = self.root / "escaped"
        shutil.copytree(self.project.root, escaped)
        manifest_path = escaped / "project.copperwright.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"]["ir"] = "../outside.pcbir.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "unsafe file reference"):
            open_managed_project(escaped)

        linked = self.root / "linked"
        shutil.copytree(self.project.root, linked)
        ir = linked / "design.pcbir.json"
        outside = self.root / "outside-hardlink.pcbir.json"
        os.link(ir, outside)
        with self.assertRaisesRegex(ValidationError, "unsafe"):
            open_managed_project(linked)

    def test_external_evidence_rejects_nonfinite_metadata_bad_time_and_flood(
        self,
    ) -> None:
        artifact = self.root / "review.txt"
        artifact.write_text("review\n", encoding="utf-8")
        common = {
            "project_value": self.project,
            "level": "L6",
            "outcome": "pass",
            "actor": "Reviewer",
            "role": "engineer",
            "performed_at": "2026-08-12T00:00:00Z",
            "statement": "External test-only statement.",
            "artifacts": [artifact],
        }
        with self.assertRaisesRegex(ValidationError, "non-finite"):
            record_external_evidence(
                **common,
                metadata={
                    "review_scope": "test",
                    "reviewer_qualification": "test",
                    "score": math.nan,
                },
            )
        with self.assertRaisesRegex(ValidationError, "UTC ISO-8601"):
            record_external_evidence(
                **(common | {"performed_at": "yesterday"}),
                metadata={
                    "review_scope": "test",
                    "reviewer_qualification": "test",
                },
            )
        with self.assertRaisesRegex(ValidationError, "1-64"):
            record_external_evidence(
                **(common | {"artifacts": [artifact] * (MAX_ARTIFACTS + 1)}),
                metadata={
                    "review_scope": "test",
                    "reviewer_qualification": "test",
                },
            )
        nested: dict[str, object] = {}
        cursor = nested
        for _index in range(70):
            child: dict[str, object] = {}
            cursor["child"] = child
            cursor = child
        nested["review_scope"] = "test"
        nested["reviewer_qualification"] = "test"
        with self.assertRaisesRegex(ValidationError, "nesting"):
            record_external_evidence(**common, metadata=nested)
        with self.assertRaisesRegex(ValidationError, "L4, L6, or L7"):
            record_external_evidence(
                **(common | {"level": "L5"}),
                metadata={
                    "review_scope": "test",
                    "reviewer_qualification": "test",
                },
            )

    def test_release_refuses_symlink_output(self) -> None:
        link = self.root / "release-link"
        target = self.root / "release-target"
        target.mkdir()
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValidationError, "unsafe"):
            build_manufacturing_release(self.project, link)

    def test_api_oversize_line_is_rejected_without_dispatch(self) -> None:
        from io import StringIO

        source = StringIO("x" * (4 * 1024 * 1024 + 1) + "\n")
        destination = StringIO()
        with patch("copperwright.api.handle_request") as handler:
            self.assertEqual(serve(source, destination), 0)
        handler.assert_not_called()
        response = json.loads(destination.getvalue())
        self.assertEqual(response["error"]["code"], -32600)

    def test_project_tree_member_limit_is_enforced(self) -> None:
        root = self.root / "too-many"
        root.mkdir()
        for index in range(TREE_MEMBER_LIMIT + 1):
            (root / f"f{index:05d}").touch()
        with self.assertRaisesRegex(ValidationError, "member traversal limit"):
            validate_agent_tree(root)

    def test_missing_system_pcbnew_python_fails_closed(self) -> None:
        with self.assertRaisesRegex(CopperWrightError, "unavailable"):
            generate_managed_project(
                bundled_requirements_path(),
                self.root / "missing-python",
                system_python=self.root / "does-not-exist",
            )
        self.assertFalse((self.root / "missing-python").exists())


if __name__ == "__main__":
    unittest.main()
