# Installer Contract

> Executable contract for the Bash and PowerShell one-command installers.

## Scenario: Bootstrap and repair the local PCBDraft runtime

### 1. Scope / Trigger

- Applies to `scripts/install.sh`, `scripts/install.ps1`, their README one-line
  entry points, and installer CI/tests.
- Use this contract whenever detection, package-manager selection, provenance,
  KiCad repair, setup, verification, or installer output changes.
- The installer is a thin bootstrapper. Python `setup` and `doctor --json`
  remain authoritative for KiCad runtime/data/table readiness.

### 2. Signatures

```text
install.sh [--check] [--yes] [--ref COMMIT_SHA]
           [--no-install-kicad] [--no-install-uv]

install.ps1 [-Check] [-Yes] [-Ref COMMIT_SHA]
            [-NoInstallKiCad] [-NoInstallUv]
```

- `COMMIT_SHA` is exactly 40 lowercase hexadecimal characters.
- `--check` / `-Check` performs no mutation.
- Normal mode has one installer approval boundary; `--yes` / `-Yes` skips only
  that prompt, never sudo, UAC, or package-manager policy.

### 3. Contracts

The visible phase order is `preflight`, `prerequisites`, `pcbdraft`, `setup`,
then `verify`.

Preflight resolves public `main` to a full commit unless a ref is supplied,
then reports the target and planned actions. Existing uv and compatible stable
KiCad `>=10.0.0,<10.1.0` are reused. PCBDraft is reused only when both
`pcbdraft --version` and `doctor --json.runtime.commit` match the installer.

Supported KiCad package paths are:

| Platform | Package path |
|----------|--------------|
| Ubuntu/Linux Mint | stable KiCad 10 PPA plus apt |
| Debian | apt |
| Fedora | stable KiCad COPR plus dnf |
| Arch Linux | pacman |
| macOS | existing Homebrew `kicad` cask |
| Windows | WinGet `KiCad.KiCad`, then existing Chocolatey `kicad` |

On apt with `--no-install-recommends`, request `kicad-symbols`,
`kicad-footprints`, and `kicad-templates` explicitly. `kicad-libraries` is a
meta-package whose recommendations are not sufficient under that flag.

After package work, invoke the absolute PCBDraft executable. `setup` may create
missing user library tables but must not overwrite existing tables. Final
success requires `doctor --json` to exit zero and contain `"ok": true`.
Missing model authentication is reported separately and does not fail core
installation.

Environment inputs:

- `PCBDRAFT_INSTALL_REF`: same validation as the ref option.
- `KICAD_CLI`, `PCBDRAFT_CLI`: explicit executable discovery overrides.
- Installer test hooks beginning `PCBDRAFT_TEST_` are test-only and must never
  weaken production checks.

### 4. Validation & Error Matrix

| Condition | Result |
|-----------|--------|
| Check mode and complete runtime ready | exit/return 0, no mutation |
| Check mode and supported actions required | exit/return 10, no mutation |
| Invalid ref, unsupported OS/distro, or missing required package manager | failure 1 in `preflight` |
| Package-manager/uv failure | failure 1 in `prerequisites` or `pcbdraft` |
| Missing/invalid stock data or user library-table initialization failure | failure 1 in `setup`/`verify` with safe-rerun guidance |
| `doctor` exits nonzero or JSON `ok` is not true | failure 1 in `verify` |
| Model is unconfigured while core is ready | success; print optional `connect` step |

Compatible KiCad with missing symbols, footprints, or templates is a KiCad
package repair, not only a `setup` retry. Windows repair uses package-manager
force/reinstall semantics; apt installs the concrete stock-data packages.

### 5. Good/Base/Bad Cases

- Good: fresh supported host -> one approved package plan -> exact PCBDraft
  commit -> setup -> ready doctor -> absolute launch command.
- Base: matching uv, KiCad, stock data, library tables, and PCBDraft commit ->
  no approval/elevation/reinstall -> final doctor rechecked -> success.
- Bad: `kicad-cli` exists but stock directories are absent -> never claim ready
  and never loop on setup alone; plan package repair or fail with the matching
  no-install/manual recovery message.

### 6. Tests Required

- `tests/core/test_installers.py`: check/no-op, exit 10, provenance reuse,
  missing/old KiCad, missing stock libraries, missing package manager, phase
  failures, absolute path summary, all supported Unix branches, PowerShell
  dot-source/dynamic invocation, Windows repair, and UTF-8 BOM.
- Parse Bash on Linux/macOS and PowerShell with both Windows PowerShell 5.1 and
  PowerShell 7 in platform CI.
- Keep real package-manager mutation out of decision tests. Use disposable
  non-root Ubuntu 24.04/26.04 acceptance for the apt full flow and native CI for
  macOS/Windows KiCad boundaries.
- Fast local gate: syntax, changed-file lint/format/type check, focused unit
  modules, workflow YAML parse, and `git diff --check`.

### 7. Wrong vs Correct

#### Wrong

```bash
sudo apt-get install --yes --no-install-recommends kicad kicad-libraries
pcbdraft setup >/dev/null
echo "ready"
```

This omits recommended stock-data packages, hides setup evidence, and asserts
success without checking the authoritative diagnosis.

#### Correct

```bash
sudo apt-get install --yes --no-install-recommends \
  kicad kicad-libraries kicad-symbols kicad-footprints kicad-templates
"$pcbdraft_bin" setup
report=$("$pcbdraft_bin" doctor --json)
# Require a successful command and JSON `ok: true` before printing ready.
```
