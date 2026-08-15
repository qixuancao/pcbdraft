from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

from pcbdraft import __version__, build_identity


class RuntimeIdentityTests(unittest.TestCase):
    def test_package_and_project_versions_cannot_drift(self) -> None:
        root = Path(__file__).resolve().parents[2]
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(__version__, project["project"]["version"])
        installer = (root / "scripts" / "install.sh").read_text(encoding="utf-8")
        self.assertIn(f'readonly PCBDRAFT_EXPECTED_VERSION="{__version__}"', installer)

    def test_build_identity_exposes_only_bounded_commit_provenance(self) -> None:
        identity = build_identity()
        self.assertEqual(set(identity), {"version", "commit"})
        self.assertEqual(identity["version"], __version__)
        commit = identity["commit"]
        if commit is not None:
            self.assertIsNotNone(re.fullmatch(r"[0-9a-f]{40,64}", commit))


if __name__ == "__main__":
    unittest.main()
