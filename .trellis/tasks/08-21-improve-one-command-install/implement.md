# Implementation plan

## 1. Freeze the shared behavior

- Add focused installer fixtures/harnesses that describe the shared five phases,
  option parity, exit semantics, and no-mutation preflight contract.
- Cover compatible/no-op, missing uv, missing KiCad, incompatible KiCad, missing
  package manager, package failure, setup failure, PATH absence, and successful
  verification before changing installer behavior.

## 2. Refine the Bash installer

- Refactor `scripts/install.sh` into loadable detection, planning, execution, and
  summary functions while preserving the guarded `main` entry point.
- Add `--check` and `--yes`, one approval point, phase-aware errors, target
  provenance reuse, visible setup output, and final diagnosis.
- Make Ubuntu the strict Linux path; preserve and test existing Debian, Fedora,
  Arch, Linux Mint, and macOS selection branches.
- Ensure the documented one-line Bash invocation leaves stdin attached for sudo
  and the installer approval prompt.

## 3. Bring PowerShell to contract parity

- Refactor `scripts/install.ps1` so functions can be dot-sourced for tests without
  running the installer.
- Add `-Check` and `-Yes`, one approval point, named phases, structured failure
  context, installed-provenance reuse, visible setup, and the same final facts.
- Preserve WinGet-first and existing-Chocolatey fallback behavior; fail before
  mutation when neither is present.

## 4. Align runtime output and documentation

- Reuse `pcbdraft setup`/`doctor` as the final authority; make only the smallest
  CLI output adjustment needed to distinguish core installation readiness from
  model connection.
- Replace README multi-line temporary-file recipes with one copyable command per
  shell and document `--check`, unattended mode, package-manager prerequisites,
  rerun behavior, and the immediate absolute launch path.
- Update development/release notes and the relevant installation contract spec.

## 5. Validation

- Run Bash syntax validation and PowerShell parser validation.
- Run only the focused installer, identity, doctor, and KiCad runtime tests during
  iteration, within the repository's fast-check budget.
- Run Ruff/format/mypy only for changed Python test/runtime files and
  `git diff --check`.
- Run disposable non-root Ubuntu 24.04 and 26.04 Docker acceptance with no host
  HOME/config mounts, Docker socket, privileged mode, host network, or published
  ports. Exercise the current checkout through a local immutable package fixture
  and retain the final setup/doctor evidence.
- Launch that gate against a separate rootless daemon with every persistent
  Docker/build/test path under a validated task-specific temporary directory on
  `/mnt/2T`. Do not alter or use the shared `/var/lib/docker` data root. Install
  no host package from the Agent session: ask the user to install the small
  `uidmap` prerequisite, verify it afterward, and otherwise leave local full-flow
  acceptance to Ubuntu CI.
- Let the existing platform CI provide macOS/Windows native KiCad confirmation
  and the remote archive-by-commit integration check. Do not attempt to claim
  macOS/Windows fidelity through Linux containers.

## Expected files

- `scripts/install.sh`
- `scripts/install.ps1`
- `README.md`
- `.github/workflows/platform.yml`
- focused installer tests under `tests/`
- `src/pcbdraft/interfaces/cli.py` or doctor output tests only if the existing
  final report cannot express the agreed readiness distinction
- installation/release documentation and Trellis spec touched by the contract

## Rollback points

- Keep Bash and PowerShell changes independently reviewable inside the work
  commit so one platform implementation can be reverted without changing Python
  runtime semantics.
- Do not delete the existing installer flags or supported distro branches.
- If provenance-based no-op detection is unreliable, retain safe constrained
  reinstall behavior and report it explicitly rather than skipping an uncertain
  update.

## Execution evidence

- Ubuntu 24.04 and Ubuntu 26.04 were installed from clean containers through an
  isolated rootless Docker daemon whose data root lived under `/mnt/2T`.
- Both platforms installed KiCad 10.0.5, PCBDraft at immutable commit
  `572b099e4b34996b9fd90fc2fddb3719842d1a20`, initialized both KiCad library
  tables, and finished with `pcbdraft doctor --json` reporting `ok: true`.
- A second `--check` returned zero on both platforms and reused uv, KiCad, and
  the exact PCBDraft commit without mutation.
- The first Ubuntu 24.04 pass exposed that `kicad-libraries` only recommends the
  stock symbol, footprint, and template packages. Because the installer uses
  `--no-install-recommends`, it now requests `kicad-symbols`,
  `kicad-footprints`, and `kicad-templates` explicitly and plans a repair when
  an existing KiCad CLI lacks that data.
- The isolated containers, daemon, images, runtime state, and task-specific
  `/mnt/2T` data were removed after acceptance. The shared Docker daemon and its
  existing containers were not used or changed.
