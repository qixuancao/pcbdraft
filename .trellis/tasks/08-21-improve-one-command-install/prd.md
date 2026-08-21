# Improve one-command installation

## Goal

Turn the existing Linux, macOS, and Windows PCBDraft installers into a
predictable, genuinely low-friction first-user experience: one visible command
should detect what is already ready, install only missing supported
prerequisites, prepare KiCad safely, verify the result, and tell the user exactly
how to launch PCBDraft or recover from a failed step.

This task improves the existing installation path; it does not replace KiCad or
change PCBDraft's runtime architecture.

## Background and confirmed facts

- The repository already has `scripts/install.sh` for Linux/macOS and
  `scripts/install.ps1` for Windows. They detect or install uv, detect or install
  KiCad, install PCBDraft with `uv tool install`, and run `pcbdraft setup`.
- The README calls this one-command installation, but presents a multi-line
  download/run/delete sequence. The installer also suppresses setup output and
  does not end with a concise human-readable readiness summary.
- PCBDraft requires more than the KiCad desktop application: it needs a
  compatible host-visible `kicad-cli`, the `pcbnew` Python binding, symbol and
  footprint data, and valid user library tables.
- `pcbdraft setup` already initializes only missing stock library tables and
  refuses to overwrite invalid existing user files. `pcbdraft doctor` already
  exposes the factual runtime state.
- The current installers preserve important earlier decisions: PCBDraft itself
  is installed per-user; system privileges are requested only for a missing
  KiCad package; source installation resolves to a full commit SHA and uses
  locked runtime/build constraints; existing KiCad configuration is not
  replaced.
- Installer CI currently parses Bash/PowerShell syntax and exercises the Python
  runtime-discovery layer, but does not test the installer decision tree with
  fake package managers or verify restart/retry behavior.
- The official KiCad channels differ by platform. Ubuntu uses the KiCad PPA,
  Fedora has distribution/COPR packages, Debian and Arch use distribution
  packages, and macOS/Windows use desktop installers or package-manager
  wrappers. KiCad recommends Flatpak for many other Linux distributions, but a
  sandboxed desktop install is not automatically a valid PCBDraft runtime
  because PCBDraft needs host-visible CLI and Python bindings.
- There are currently no repository release tags. The default installer first
  downloads the bootstrap script from `main`, then pins the actual package and
  constraints to the resolved `main` commit.

## Requirements

- R1: Provide one copyable command for Linux, macOS, and Windows. The command may
  download a temporary installer, but the user should not have to manually
  create, run, and remove a file.
- R2: Start with a non-destructive preflight that reports the detected platform,
  KiCad state, uv state, intended package-manager action, install target, and
  whether administrator permission will be requested.
- R3: Make repeated execution idempotent and resumable. Compatible uv, Python,
  KiCad, and library state must be reused. A PCBDraft install with the same
  verified immutable commit is reused; an install with different or unavailable
  provenance may be replaced through the same constrained installation path.
- R4: Keep PCBDraft installation user-scoped and preserve immutable-commit plus
  locked-constraint installation. Do not silently switch to an unpinned package
  or mutable dependency set.
- R5: Treat KiCad as a platform prerequisite with an explicit strategy. Ubuntu
  uses apt plus the stable KiCad PPA; macOS uses the Homebrew KiCad cask; Windows
  prefers WinGet and falls back to an existing Chocolatey installation. Check
  the candidate package/version before mutation where the package manager allows
  it; after installation, verify `kicad-cli`, stable 10.0.x compatibility,
  `pcbnew`, stock symbols/footprints/templates, and library tables.
- R6: Never overwrite an existing KiCad library table or user configuration.
  An invalid existing file must produce a specific recovery message and path.
- R7: Run `pcbdraft setup` and a final readiness check without hiding their
  result. The success screen must show the installed executable, PCBDraft
  version/commit, KiCad version, core readiness, and the exact launch command.
- R8: Distinguish installation readiness from model connection. A missing model
  login must not be reported as a broken KiCad/PCBDraft install; it should be
  presented as the next optional interactive step: `pcbdraft connect`.
- R9: On failure, identify the failed phase and preserve enough state for a
  normal rerun to continue. Do not leave a temporary PATH assumption as the only
  way to invoke the freshly installed executable.
- R10: Add a non-mutating `--check`/preflight mode and an explicit unattended
  mode such as `--yes`, so users and CI can see or approve system package
  changes without ambiguous prompts.
- R11: Add focused installer tests for detection, no-op reinstall, missing uv,
  missing/unsupported KiCad, package-manager failure, setup failure, PATH
  handling, and successful final diagnosis. Keep the existing real KiCad
  platform jobs as the native integration boundary.
- R12: Keep help, errors, and README commands concise in Chinese while retaining
  technically precise command names and paths.

## Acceptance criteria

- [ ] A new user can paste one documented command and reach a final screen that
  either says the core runtime is ready or names one concrete unresolved action.
- [ ] The same stage names, approval semantics, success summary, and failure
  vocabulary are used by the Bash and PowerShell installers.
- [ ] A user with compatible KiCad and uv already installed completes without
  sudo/admin prompts and without reinstalling either dependency.
- [ ] A missing uv is installed per-user and used immediately even before the
  parent shell's PATH is refreshed.
- [ ] On Ubuntu, macOS, and Windows with the selected package-manager baseline, a
  missing KiCad is installed only after the proposed system change is
  visible/approved, then all required CLI, Python, and library capabilities are
  verified.
- [ ] On a supported package-manager path, an older/incompatible KiCad becomes a
  visible upgrade action and PCBDraft is installed only after the resulting
  runtime passes verification.
- [ ] An unsupported distro or missing required package manager fails before any
  PCBDraft mutation and prints one exact prerequisite or manual KiCad route.
- [ ] Re-running after an interrupted or failed phase safely reuses completed
  phases and reaches the same final state.
- [ ] The installed command can be invoked immediately by absolute detected path;
  the installer also gives one durable PATH fix when needed.
- [ ] `pcbdraft setup` and the final diagnosis agree that KiCad core, data, and
  library tables are ready; model authentication is reported separately.
- [ ] Existing KiCad user files remain byte-for-byte unchanged unless the target
  table was absent and initialized from the installed stock template.
- [ ] Installation still records an immutable source commit and uses the
  repository's locked runtime/build constraints.
- [ ] Focused Bash and PowerShell decision-path tests run in CI without actually
  invoking host package managers; existing native KiCad jobs remain green.
- [ ] Ubuntu 24.04 and 26.04 installer acceptance runs as a non-root test user in
  disposable Docker containers and reaches ready `setup`/`doctor` evidence
  without mounting or changing the host's HOME, KiCad configuration, uv tools,
  or project repository.
- [ ] Ubuntu acceptance uses a separate rootless Docker daemon whose data root,
  container layers, images, BuildKit cache, logs, and test HOME are all under a
  task-specific temporary directory on `/mnt/2T`; it does not write image data
  to the shared `/var/lib/docker`, change the global Docker configuration, or
  restart/interfere with the existing daemon and containers.

## Out of scope

- A graphical installer, desktop launcher, or package-manager-native PCBDraft
  package.
- Bundling or redistributing KiCad inside PCBDraft.
- Using Flatpak as a PCBDraft backend until host CLI/Python integration has an
  explicit supported contract.
- Automatically creating model-service accounts or completing OAuth/API-key
  authentication inside the installer.
- Changing the PCB project repository, generated KiCad format, or Agent tools.
- Designing a full release/tag/channel system.
- Relocating or pruning the machine's existing shared Docker data root.

## Key decisions

- The first implementation covers all three advertised OS families rather than
  shipping an Ubuntu-only improvement.
- Ubuntu 24.04/26.04 is the strict Linux native acceptance baseline. Existing
  Debian, Fedora, Arch, and Linux Mint branches remain supported code paths and
  receive focused decision-path tests, but this task does not add four more
  native CI operating systems.
- The installer does not bootstrap a general-purpose system package manager.
  Ubuntu supplies apt; macOS must already have Homebrew; Windows must already
  have WinGet or Chocolatey. A missing package manager fails during preflight
  before PCBDraft mutation and prints one concrete prerequisite action.
- The three platform changes remain one task because they share one observable
  installation contract, one README entry point, and one cross-platform CI
  gate; splitting them would duplicate the shared semantics and integration
  review.
- Ubuntu destructive-path acceptance is isolated in Docker. macOS and Windows
  native validation remains on their hosted OS runners because Linux containers
  cannot faithfully validate Homebrew application bundles, WinGet/Chocolatey,
  UAC, Windows paths, or the platform KiCad Python runtime.
- Local Ubuntu acceptance uses an isolated rootless daemon on `/mnt/2T`, not the
  existing rootful daemon whose `/var/lib/docker` data root is on the system
  disk. Only its Unix socket/runtime metadata may live temporarily under the
  per-user runtime directory.

## Test-environment responsibility

- The host has rootless Docker and valid subordinate UID/GID ranges, but lacks
  the `newuidmap`/`newgidmap` binaries. Starting the isolated rootless daemon
  requires Ubuntu's optional `uidmap` package (about 138 KB installed, plus any
  missing standard dependency).
- The user owns every host action that requires sudo/root authentication,
  including installing `uidmap`. The Agent must never attempt, queue, or work
  around a root-password prompt. It may only verify the prerequisite afterward.
- Membership in the `docker` group grants access to the existing shared daemon;
  it does not satisfy the rootless UID/GID mapping prerequisite and is not a
  reason to place task images in the shared system-disk data root.

## Research references

- Existing implementation: `scripts/install.sh`, `scripts/install.ps1`,
  `src/pcbdraft/services/doctor.py`, `src/pcbdraft/kicad/runtime.py`.
- Existing validation: `.github/workflows/platform.yml`,
  `tests/core/test_identity.py`, `tests/services/test_doctor.py`, and
  `tests/kicad/test_runtime.py`.
- Prior installation work retained the per-user, immutable-commit, constrained,
  non-destructive design; see local Trellis memory sessions
  `019fffd2-ad4a-7613-9c59-464bdf40342e` and
  `01a00290-e730-70c1-a934-e8326526b085`.
