from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASH_INSTALLER = ROOT / "scripts" / "install.sh"
POWERSHELL_INSTALLER = ROOT / "scripts" / "install.ps1"
COMMIT = "a" * 40


class InstallerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.name == "nt":
            self.skipTest("Bash installer behavior is covered on Unix runners")
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is unavailable on this runner")
        self.bash = bash
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.fake_bin = self.root / "bin"
        self.home.mkdir()
        self.fake_bin.mkdir()
        self.log = self.root / "mutations.log"
        self.os_release = self.root / "os-release"
        self.os_release.write_text(
            'ID=ubuntu\nPRETTY_NAME="Ubuntu test fixture"\n', encoding="utf-8"
        )
        for command in ("dirname", "grep", "head", "mktemp", "rm", "sed"):
            executable = shutil.which(command)
            if executable is None:
                self.skipTest(f"{command} is unavailable on this runner")
            (self.fake_bin / command).symlink_to(executable)
        self._write_executable(
            self.fake_bin / "curl",
            'printf \'curl %s\\n\' "$*" >> "$PCBDRAFT_TEST_LOG"\nexit 97',
        )

    def _write_executable(self, path: Path, body: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def _environment(self, **overrides: str) -> dict[str, str]:
        environment = {
            "HOME": str(self.home),
            "PATH": str(self.fake_bin),
            "TMPDIR": str(self.root),
            "PCBDRAFT_INSTALL_TESTING": "1",
            "PCBDRAFT_TEST_SYSTEM": "Linux",
            "PCBDRAFT_TEST_OS_RELEASE_FILE": str(self.os_release),
            "PCBDRAFT_TEST_LOG": str(self.log),
        }
        environment.update(overrides)
        return environment

    def _run(
        self, *arguments: str, **environment: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.bash, str(BASH_INSTALLER), *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=self._environment(**environment),
        )

    def _fake_kicad(self, version: str = "10.0.5") -> Path:
        return self._write_executable(
            self.root / "tools" / "kicad-cli", f"printf '%s\\n' '{version}'"
        )

    def _fake_pcbdraft(
        self,
        *,
        ready: bool = True,
        libraries_ready: bool = True,
        setup_exit: int = 0,
    ) -> Path:
        ok = "true" if ready else "false"
        libraries = "true" if libraries_ready else "false"
        body = (
            'case "$1" in\n'
            "  --version) printf '%s\\n' 'pcbdraft 0.1.0' ;;\n"
            f"  doctor) printf '%s\\n' "
            f'\'{{"runtime":{{"commit":"{COMMIT}"}},'
            f'"library_data":{{"symbols":{{"available":{libraries}}}}},'
            f'"library_tables":{{}},"model_available":false,"ok":{ok}}}\' ;;\n'
            "  setup)\n"
            "    printf 'setup\\n' >> \"$PCBDRAFT_TEST_LOG\"\n"
            f"    exit {setup_exit}\n"
            "    ;;\n"
            "  *) exit 64 ;;\n"
            "esac"
        )
        return self._write_executable(
            self.root / "tools" / "pcbdraft",
            body,
        )

    def _fake_uv(self) -> Path:
        return self._write_executable(
            self.home / ".local" / "bin" / "uv",
            """
if [ "$1 $2 $3" = 'tool install --help' ]; then
  printf '%s\\n' '--build-constraints'
elif [ "$1 $2" = 'tool dir' ] && [ "$3" = '--bin' ]; then
  printf '%s\\n' "$HOME/.local/bin"
else
  printf 'uv %s\\n' "$*" >> "$PCBDRAFT_TEST_LOG"
  exit 96
fi
""",
        )

    def _fake_package_manager(self, name: str, *, exit_code: int = 95) -> Path:
        return self._write_executable(
            self.fake_bin / name,
            f'printf \'{name} %s\\n\' "$*" >> "$PCBDRAFT_TEST_LOG"\nexit {exit_code}',
        )

    def test_ready_check_is_a_non_mutating_noop(self) -> None:
        self._fake_uv()
        kicad = self._fake_kicad()
        pcbdraft = self._fake_pcbdraft()

        result = self._run(
            "--check",
            "--ref",
            COMMIT,
            KICAD_CLI=str(kicad),
            PCBDRAFT_CLI=str(pcbdraft),
        )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("PCBDraft installation plan", output)
        for phase in ("preflight", "prerequisites", "pcbdraft", "setup", "verify"):
            self.assertIn(phase, output)
        self.assertIn(f"Launch now: {pcbdraft}", output)
        self.assertIn("PCBDraft version: 0.1.0", output)
        self.assertIn("KiCad version: 10.0.5", output)
        self.assertIn("Core runtime: ready", output)
        self.assertIn(
            f"Model connection: not configured (next optional step: {pcbdraft} connect)",
            output,
        )
        self.assertFalse(
            self.log.exists(), self.log.read_text() if self.log.exists() else ""
        )

    def test_missing_dependencies_check_returns_actions_required_without_mutation(
        self,
    ) -> None:
        self._fake_package_manager("sudo")
        self._fake_package_manager("apt-get")

        result = self._run("--check", "--ref", COMMIT, PCBDRAFT_TEST_KICAD_MISSING="1")

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 10, output)
        self.assertRegex(output, r"install uv|安装 uv")
        self.assertRegex(output, r"install or upgrade.*KiCad|安装或升级.*KiCad")
        self.assertRegex(output, r"sudo|administrator|管理员")
        self.assertFalse(
            self.log.exists(), self.log.read_text() if self.log.exists() else ""
        )

    def test_incompatible_kicad_is_a_visible_upgrade_action(self) -> None:
        kicad = self._fake_kicad("9.0.4")
        pcbdraft = self._fake_pcbdraft()
        self._fake_package_manager("sudo")
        self._fake_package_manager("apt-get")

        result = self._run(
            "--check",
            "--ref",
            COMMIT,
            KICAD_CLI=str(kicad),
            PCBDRAFT_CLI=str(pcbdraft),
        )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 10, output)
        self.assertIn("9.0.4", output)
        self.assertRegex(output, r"upgrade|升级")
        self.assertFalse(
            self.log.exists(), self.log.read_text() if self.log.exists() else ""
        )

    def test_missing_stock_libraries_plan_a_package_repair(self) -> None:
        kicad = self._fake_kicad()
        pcbdraft = self._fake_pcbdraft(ready=False, libraries_ready=False)
        self._fake_package_manager("sudo")
        self._fake_package_manager("apt-get")

        result = self._run(
            "--check",
            "--ref",
            COMMIT,
            KICAD_CLI=str(kicad),
            PCBDRAFT_CLI=str(pcbdraft),
        )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 10, output)
        self.assertRegex(output, r"install or upgrade.*KiCad|安装或升级.*KiCad")
        self.assertFalse(
            self.log.exists(), self.log.read_text() if self.log.exists() else ""
        )

    def test_missing_package_manager_fails_during_preflight(self) -> None:
        self._fake_package_manager("sudo")

        result = self._run("--check", "--ref", COMMIT, PCBDRAFT_TEST_KICAD_MISSING="1")

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, output)
        self.assertIn("[preflight]", output)
        self.assertIn("apt-get", output)
        self.assertFalse(
            self.log.exists(), self.log.read_text() if self.log.exists() else ""
        )

    def test_package_failure_names_prerequisites_phase(self) -> None:
        pcbdraft = self._fake_pcbdraft()
        self._fake_package_manager("sudo", exit_code=41)
        self._fake_package_manager("apt-get")
        self._fake_package_manager("add-apt-repository")

        result = self._run(
            "--yes",
            "--ref",
            COMMIT,
            PCBDRAFT_CLI=str(pcbdraft),
            PCBDRAFT_TEST_KICAD_MISSING="1",
        )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, output)
        self.assertIn("[prerequisites]", output)
        self.assertIn("sudo add-apt-repository", self.log.read_text(encoding="utf-8"))

    def test_setup_failure_names_setup_phase_and_is_safe_to_rerun(self) -> None:
        kicad = self._fake_kicad()
        pcbdraft = self._fake_pcbdraft(ready=False, setup_exit=42)

        result = self._run(
            "--yes",
            "--ref",
            COMMIT,
            KICAD_CLI=str(kicad),
            PCBDRAFT_CLI=str(pcbdraft),
        )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, output)
        self.assertIn("[setup]", output)
        self.assertEqual(self.log.read_text(encoding="utf-8"), "setup\n")

    def test_ready_run_prints_absolute_launch_and_durable_path_help(self) -> None:
        kicad = self._fake_kicad()
        pcbdraft = self._fake_pcbdraft()

        result = self._run(
            "--ref",
            COMMIT,
            KICAD_CLI=str(kicad),
            PCBDRAFT_CLI=str(pcbdraft),
        )

        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn('"ok":true', output)
        self.assertIn(f"Launch now: {pcbdraft}", output)
        self.assertRegex(output, r"Add the command directory to PATH: export PATH=")
        self.assertIn("[verify]", output)

    def test_apt_installs_stock_libraries_even_without_recommends(self) -> None:
        installer = BASH_INSTALLER.read_text(encoding="utf-8")
        for package in ("kicad-symbols", "kicad-footprints", "kicad-templates"):
            self.assertIn(package, installer)

    def test_supported_unix_package_manager_branches_plan_without_mutation(
        self,
    ) -> None:
        cases = (
            ("ubuntu", "apt-get", "sudo + apt"),
            ("linuxmint", "apt-get", "sudo + apt"),
            ("debian", "apt-get", "sudo + apt"),
            ("fedora", "dnf", "sudo + dnf"),
            ("arch", "pacman", "sudo + pacman"),
        )
        self._fake_package_manager("sudo")
        for distribution, manager, expected in cases:
            with self.subTest(distribution=distribution):
                self.os_release.write_text(
                    f'ID={distribution}\nPRETTY_NAME="{distribution} fixture"\n',
                    encoding="utf-8",
                )
                self._fake_package_manager(manager)
                result = self._run(
                    "--check",
                    "--ref",
                    COMMIT,
                    PCBDRAFT_TEST_KICAD_MISSING="1",
                )
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 10, output)
                self.assertIn(expected, output)
                self.assertFalse(
                    self.log.exists(),
                    self.log.read_text(encoding="utf-8") if self.log.exists() else "",
                )

        self._fake_package_manager("brew")
        result = self._run(
            "--check",
            "--ref",
            COMMIT,
            PCBDRAFT_TEST_SYSTEM="Darwin",
            PCBDRAFT_TEST_KICAD_MISSING="1",
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 10, output)
        self.assertIn("Homebrew", output)
        self.assertFalse(
            self.log.exists(), self.log.read_text() if self.log.exists() else ""
        )


class PowerShellInstallerContractTests(unittest.TestCase):
    @staticmethod
    def _powershell_engines() -> list[str]:
        engines: list[str] = []
        for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
            executable = shutil.which(name)
            if executable and executable not in engines:
                engines.append(executable)
        return engines

    def test_options_phases_and_package_manager_order_match_shared_contract(
        self,
    ) -> None:
        installer = POWERSHELL_INSTALLER.read_text(encoding="utf-8")
        self.assertTrue(
            POWERSHELL_INSTALLER.read_bytes().startswith(b"\xef\xbb\xbf"),
            "Windows PowerShell 5.1 requires a BOM to decode the Chinese source",
        )
        for option in ("Ref", "Check", "Yes", "NoInstallKiCad", "NoInstallUv"):
            self.assertRegex(installer, rf"\$(?:{option})\b")
        for phase in ("preflight", "prerequisites", "pcbdraft", "setup", "verify"):
            self.assertIn(f'"{phase}"', installer)
        self.assertLess(installer.index("winget.exe"), installer.index("choco.exe"))
        self.assertRegex(
            installer, r"\bdoctor\b.*--json|@\(\s*\"doctor\",\s*\"--json\"\s*\)"
        )
        self.assertNotRegex(installer, r"setup[^\r\n]*(?:Out-Null|>\s*\$null)")
        self.assertIn("Test-DoctorHasMissingLibraryData", installer)
        self.assertIn("Test-KiCadStockLibraryData", installer)
        self.assertIn("RepairKiCad = $repairKiCad", installer)
        self.assertIn('"--force"', installer)

    def test_installer_can_be_dot_sourced_without_running_main(self) -> None:
        installer = POWERSHELL_INSTALLER.read_text(encoding="utf-8")
        self.assertIn("Invoke-PCBDraftInstaller", installer)
        self.assertRegex(
            installer, r"\$MyInvocation\.InvocationName|\$MyInvocation\.Line"
        )

        engines = self._powershell_engines()
        if not engines:
            self.skipTest("PowerShell is unavailable on this runner")
        escaped = str(POWERSHELL_INSTALLER).replace("'", "''")
        command = (
            f". '{escaped}'; "
            "if (-not (Get-Command Invoke-PCBDraftInstaller -ErrorAction SilentlyContinue)) "
            "{ exit 91 }"
        )
        for powershell in engines:
            with self.subTest(powershell=powershell):
                result = subprocess.run(
                    [powershell, "-NoProfile", "-Command", command],
                    check=False,
                    capture_output=True,
                    text=True,
                    errors="replace",
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_check_decision_tree_is_non_mutating_when_pwsh_is_available(self) -> None:
        engines = self._powershell_engines()
        if not engines:
            self.skipTest("PowerShell is unavailable on this runner")
        escaped = str(POWERSHELL_INSTALLER).replace("'", "''")
        command = f"""
. '{escaped}'
$script:FixtureActions = $true
function Test-IsAdministrator {{ return $false }}
function Get-PCBDraftInstallPlan {{
    param($RequestedRef, $AllowKiCadInstall, $AllowUvInstall)
    return [pscustomobject]@{{
        Platform = 'Windows'; Architecture = 'fixture'; ResolvedRef = '{COMMIT}'
        Uv = $null; KiCad = 'C:\\fixture\\kicad-cli.exe'; KiCadVersion = '10.0.5'
        PCBDraft = $null; Doctor = $null; PackageManager = $null
        NeedUv = $script:FixtureActions; NeedKiCad = $false
        NeedPCBDraft = $script:FixtureActions; NeedSetup = $script:FixtureActions
    }}
}}
function Confirm-InstallPlan {{ throw 'check mode requested confirmation' }}
function Invoke-InstallPlan {{ throw 'check mode attempted mutation' }}
$required = @(Invoke-PCBDraftInstaller -Check)[-1]
if ($required -ne 10) {{ exit 92 }}
$script:FixtureActions = $false
$ready = @(Invoke-PCBDraftInstaller -Check)[-1]
if ($ready -ne 0) {{ exit 93 }}
"""
        for powershell in engines:
            with self.subTest(powershell=powershell):
                result = subprocess.run(
                    [powershell, "-NoProfile", "-Command", command],
                    check=False,
                    capture_output=True,
                    text=True,
                    errors="replace",
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_dynamic_one_line_check_does_not_exit_the_calling_shell(self) -> None:
        engines = self._powershell_engines()
        if not engines:
            self.skipTest("PowerShell is unavailable on this runner")
        escaped = str(POWERSHELL_INSTALLER).replace("'", "''")
        command = f"""
$content = [IO.File]::ReadAllText('{escaped}')
$result = @(& ([scriptblock]::Create($content)) -Check -Ref '{COMMIT}')[-1]
if ($result -ne 10) {{ exit 94 }}
exit 0
"""
        for powershell in engines:
            with self.subTest(powershell=powershell):
                result = subprocess.run(
                    [powershell, "-NoProfile", "-Command", command],
                    check=False,
                    capture_output=True,
                    text=True,
                    errors="replace",
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ReadmeInstallerCommandTests(unittest.TestCase):
    def test_one_line_commands_preserve_installer_standard_input(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn('bash "$installer"', readme)
        self.assertIn("trap 'rm -f -- \"$installer\"' EXIT", readme)
        self.assertNotIn('bash -c "$install_script"', readme)
        self.assertIn("[scriptblock]::Create", readme)
        self.assertNotRegex(readme, r"curl[^\r\n]*\|\s*(?:ba)?sh")

    def test_documentation_covers_modes_prerequisites_and_next_steps(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for option in (
            "`--check`",
            "`-Check`",
            "`--yes`",
            "`-Yes`",
            "`--ref`",
            "`-Ref`",
        ):
            self.assertIn(option, readme)
        for prerequisite in ("apt", "Homebrew", "WinGet", "Chocolatey"):
            self.assertIn(prerequisite, readme)
        self.assertIn("Launch now:", readme)
        self.assertIn("pcbdraft connect", readme)


if __name__ == "__main__":
    unittest.main()
