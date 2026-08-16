from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pcbdraft import __version__, build_identity


class RuntimeIdentityTests(unittest.TestCase):
    def test_package_and_project_versions_cannot_drift(self) -> None:
        root = Path(__file__).resolve().parents[2]
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(__version__, project["project"]["version"])
        installer = (root / "scripts" / "install.sh").read_text(encoding="utf-8")
        self.assertIn(f'readonly PCBDRAFT_EXPECTED_VERSION="{__version__}"', installer)
        windows_installer = (root / "scripts" / "install.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn(f'$ExpectedVersion = "{__version__}"', windows_installer)

    def test_unix_installer_accepts_distribution_suffixed_kicad_version(self) -> None:
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / "kicad-cli"
            fake.write_text(
                "#!/bin/sh\nprintf '%s\\n' '10.0.5-10.0.5~ubuntu26.04.1'\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            environment = dict(os.environ)
            environment.update(
                {
                    "PCBDRAFT_TEST_INSTALLER": str(root / "scripts" / "install.sh"),
                    "PCBDRAFT_TEST_KICAD": str(fake),
                }
            )
            bash = shutil.which("bash")
            if bash is None:
                self.skipTest("bash is unavailable")
            command = (
                'source "$PCBDRAFT_TEST_INSTALLER"; '
                'check_kicad_version "$PCBDRAFT_TEST_KICAD"'
            )
            result = subprocess.run(
                [bash, "-c", command],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("KiCad 10.0.5", result.stderr)

    def test_build_identity_exposes_only_bounded_commit_provenance(self) -> None:
        identity = build_identity()
        self.assertEqual(set(identity), {"version", "commit"})
        self.assertEqual(identity["version"], __version__)
        commit = identity["commit"]
        if commit is not None:
            self.assertIsNotNone(re.fullmatch(r"[0-9a-f]{40,64}", commit))

    def test_archive_install_preserves_immutable_commit_provenance(self) -> None:
        commit = "a" * 40
        metadata = SimpleNamespace(
            read_text=lambda _name: (
                '{"url":"https://github.com/qixuancao/pcbdraft/archive/'
                f'{commit}.tar.gz","archive_info":{{}}}}'
            )
        )
        with patch("pcbdraft.distribution", return_value=metadata):
            self.assertEqual(build_identity()["commit"], commit)


if __name__ == "__main__":
    unittest.main()
