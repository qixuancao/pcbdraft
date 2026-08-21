# One-command installer design

## Summary

Retain the two native installer implementations—Bash for Linux/macOS and
PowerShell for Windows—but make them implement one shared observable contract.
The installer remains a thin bootstrapper around platform package managers,
`uv tool install`, `pcbdraft setup`, and `pcbdraft doctor`; it does not duplicate
KiCad runtime repair logic already owned by Python services.

## Supported platform baseline

| Platform | KiCad installation mechanism | Exact acceptance |
| --- | --- | --- |
| Ubuntu | apt plus stable KiCad 10 PPA | Ubuntu 24.04 CI and Ubuntu 26.04 local |
| macOS | existing Homebrew, `brew install --cask kicad` | macOS 15 CI |
| Windows | WinGet, then existing Chocolatey as fallback | Windows 2025 CI |

Existing Debian, Fedora, Arch, and Linux Mint branches stay available and gain
mocked decision-path coverage. They are not promoted to native acceptance
platforms in this task. Flatpak is not selected because the current PCBDraft
runtime requires host-visible `kicad-cli`, stock data, and `pcbnew` bindings.

## Shared installer contract

Both installers expose the same conceptual options:

- normal mode: inspect, show the plan, obtain one approval before system package
  changes, execute, and verify;
- `--check` / `-Check`: non-mutating preflight;
- `--yes` / `-Yes`: approve supported package-manager changes for automation;
- retained ref and skip-install options for reproducible/debug use.

`--check` returns success only when the complete PCBDraft core runtime is already
ready, a distinct nonzero code when supported actions would be required, and a
normal failure code for an unsupported or unsafe state. Human output remains the
primary contract; scripts must not require `jq` or another new bootstrap tool.

## Phase model

The same named phases appear on every platform:

1. `preflight` — platform, privilege, network tool, package manager, uv, KiCad,
   current PCBDraft, and install-target discovery;
2. `prerequisites` — install only missing/unsupported uv or KiCad after approval;
3. `pcbdraft` — resolve one immutable repository commit, validate constraints,
   and install/update the user-scoped uv tool only when the installed provenance
   differs;
4. `setup` — invoke the newly installed executable by absolute path and let
   `pcbdraft setup` initialize only missing KiCad library tables;
5. `verify` — run the final factual diagnosis and print a compact result.

Each phase updates a single current-phase label. Bash error traps and PowerShell
exception handling include that label, the failing operation, and the safe rerun
command. No durable installer state file is needed: restartability is derived
from the same factual probes on every run.

## Preflight and approval

Preflight never invokes a mutating package-manager command. It prints:

- detected OS/distribution and architecture;
- compatible or missing uv;
- KiCad executable/version and required action;
- selected package manager and whether admin elevation will occur;
- PCBDraft user installation target and resolved source policy;
- the exact action list or the one blocking prerequisite.

Normal interactive mode asks once before the first system mutation. If no system
mutation is required, no approval prompt or elevation occurs. `--yes` skips this
single product prompt but does not bypass sudo/UAC policy. Missing Homebrew or
missing WinGet/Chocolatey is a preflight blocker, not an invitation to install a
new package manager.

## Idempotency and provenance

Existing compatible uv and KiCad installations are reused. The installer probes
the installed PCBDraft `doctor --json` identity to compare its immutable commit
with the resolved target. A matching install is reused; a different or
unidentifiable install is replaced through the existing constrained
`uv tool install` path.

The bootstrap script may still be fetched from the public `main` URL, but it
must resolve the package, runtime constraints, and build constraints to the same
full commit SHA before installation. This task does not invent a release channel
in a repository that currently has no release tags.

## KiCad and setup boundary

Platform scripts own only package-manager selection and installation. Python
runtime code remains authoritative for:

- locating `kicad-cli`, `pcbnew`, symbols, footprints, templates, and user paths;
- checking the supported stable KiCad 10.0 range;
- creating absent stock library tables without replacing user files;
- producing the final doctor/setup report.

After package installation, the script calls the absolute PCBDraft executable so
the current shell does not depend on a refreshed PATH. The final summary reports
PCBDraft version/commit, KiCad version, core readiness, executable path, one
durable PATH command when needed, and `pcbdraft connect` only as a separate next
step when no model is configured.

## Testability

Refactor each installer so its detection and planning functions can be loaded by
a test harness without executing `main`. Focused tests provide fake executable
directories, HOME/config roots, OS-release data, package-manager output, and
network responses. They assert commands selected and mutations requested; they
never run real sudo, UAC, apt, brew, WinGet, or Chocolatey.

The platform workflow retains syntax parsing and native KiCad generation, and
adds installer decision-path cases on the corresponding runner. Ubuntu remains
the exact Linux end-to-end environment. No new Pester dependency is required;
PowerShell behavior can be driven from the existing Python unittest harness or a
small checked-in PowerShell harness.

Ubuntu full-flow acceptance uses disposable `ubuntu:24.04` and `ubuntu:26.04`
containers with a non-root test account and container-local sudo. The run uses
the default bridge network only for apt/HTTPS downloads, publishes no ports,
mounts no Docker socket or host HOME/config directory, does not use host PID/IPC
or `--privileged`, and applies bounded CPU/memory limits. A checkout may be
mounted read-only only when needed to exercise the script under test; all HOME,
KiCad, uv, and PCBDraft writes remain in the container layer or an explicitly
temporary container volume.

The local pre-push container gate must exercise the current checkout rather than
silently downloading the older public `main`. It combines the real installer
phase logic with a locally built immutable package/archive fixture, then runs
the installed `setup` and `doctor`. Once the commit exists on the remote, CI can
exercise the normal GitHub archive-by-SHA path. Test containers/images are given
task-specific labels and only those exact temporary resources may be removed.
Existing unrelated containers and Docker caches are never pruned.

The machine's shared daemon uses `/var/lib/docker` on the system disk, so it is
not used for this gate. Start a separate rootless daemon on a task-specific
`mktemp` directory below `/mnt/2T/pcbdraft-install-tests/`; point its data root,
exec root, BuildKit state/cache, logs, container HOME, and all test artifacts at
that directory. A small socket/pid under `/run/user/<uid>` is acceptable runtime
metadata, not persisted image data. Do not edit `/etc/docker/daemon.json`, stop
or restart the shared daemon, switch its context, or run broad Docker cleanup.

Rootless operation requires `newuidmap` and `newgidmap`. The current host has
valid `/etc/subuid` and `/etc/subgid` ranges but not those binaries, so local
acceptance has one explicit host prerequisite: the Ubuntu `uidmap` package. It
is installed only by the user because it requires sudo/root authentication. The
Agent only verifies the binaries after the user reports completion. If the
prerequisite remains absent, skip local full Docker acceptance and rely on the
ephemeral Ubuntu CI runner; never fall back to placing test images in the shared
system-disk daemon.

## Compatibility and rollback

- Existing `--ref`, `--no-install-kicad`, and `--no-install-uv` behavior remains.
- Existing compatible installations and all user KiCad files are preserved.
- A failed uv tool update may be rerun; KiCad installation is delegated to the
  platform package manager's normal transactional/retry behavior.
- No task code edits generated PCB projects, model credentials, or repository
  settings.
- Container acceptance has no writable host application/config mount; failure
  is rolled back by removing only the exact task-labelled disposable container
  and temporary image/volume.
