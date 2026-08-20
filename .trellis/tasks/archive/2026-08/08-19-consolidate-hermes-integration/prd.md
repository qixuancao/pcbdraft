# Consolidate Hermes UI integration

## Goal

Make PCBDraft a coherent Hermes-based application instead of a PCBDraft
application with a loosely attached Hermes bridge. The default `pcbdraft`
launch must expose one deliberate user experience, keep all PCBDraft projects
under the configured PCBDraft repository, and place PCBDraft-specific Hermes
integration in the package layer that owns the behavior.

## Confirmed Facts

- `src/pcbdraft/interfaces/cli.py:265-267` sends a bare `pcbdraft` launch to
  `pcbdraft.hermes.bridge.launch_chat`.
- `src/pcbdraft/hermes/bridge.py:226-245` launches `hermes_cli.main` and
  therefore currently exposes Hermes' classic `prompt_toolkit` CLI.
- `src/pcbdraft/hermes/bridge.py:136-142` writes `display.interface: cli` and
  enables the Hermes CLI toolsets.
- `src/pcbdraft/interfaces/tui/` contains the old PCBDraft-owned Textual UI.
  Its `run_tui_command` entry point is only referenced by TUI tests; the normal
  CLI no longer calls it.
- `vendor/hermes` contains the trimmed Python Hermes runtime, but no
  `ui-tui/`, `tui_gateway/`, Node workspace, `package.json`, or TypeScript
  frontend. Hermes' own Python launcher explicitly expects `PROJECT_ROOT/ui-tui`
  for `--tui` when no prebuilt bundle is present.
- PCBDraft already has a repository authority in
  `src/pcbdraft/core/repository.py`. The configured pointer is stored in the
  user config directory, the default is `~/PCBDraft`, and normal project data
  is created under `<repository>/projects/`.
- `ApplicationService` resolves that repository for normal launches and creates
  project records below `projects/`; an explicit workspace is reserved for
  automation/tests.
- Hermes state/config currently lives separately under the PCBDraft config
  directory's `hermes/` child, and the Hermes bridge keeps a process-global
  current project id/service cache. Hermes' native `/new` and session commands
  are not automatically the same thing as PCBDraft project creation/opening.
- The existing worktree has unrelated and prior uncommitted Hermes migration
  changes. This task must preserve them unless a touched path is part of the
  integration redesign.

## Requirements

### R1. One default user experience

Remove the old PCBDraft Textual frontend from the product launch path and make
the selected Hermes surface the only default interactive frontend. The final
surface must have reliable startup, exit, interruption, slash-command, and
error behavior.

### R2. Full Hermes integration, not a cosmetic wrapper

PCBDraft-specific model configuration, persona, PCB tool registration, runtime
startup, project context, and debug tracing must have clear ownership under
`src/pcbdraft/agent/`, `src/pcbdraft/core/`, `src/pcbdraft/services/`,
`src/pcbdraft/interfaces/`, or `src/pcbdraft/model/`. A generic `hermes` bridge
package must not remain the catch-all owner for unrelated behavior.

### R3. Single PCBDraft project repository

All PCBDraft project records, KiCad designs, events, jobs, validation evidence,
previews, releases, and related durable project artifacts must remain below the
configured repository, normally `<repository>/projects/`. Starting PCBDraft
from another shell directory or through Hermes must not create a second project
store based on the current working directory.

### R4. Project-aware interactive commands

The chosen interactive frontend must provide a coherent mapping for viewing or
changing the repository, listing projects, creating a project, opening a project,
and starting a conversation for the selected project. `/new`, `/projects`, and
`/project` must not silently become an unrelated Hermes session operation or a
normal PCB request. Empty repositories and invalid repository paths must show an
actionable result.

### R5. One authoritative backend

Hermes tools and UI actions must use `ApplicationService`, the repository
authority, permission/revision checks, and the existing PCB capability registry.
They must not write raw KiCad files, maintain a second project database, or
duplicate project lifecycle decisions in the frontend adapter.

### R7. Prune unused Hermes commands

The vendored Hermes CLI exposes ~50 built-in slash commands that are irrelevant
to a PCB design product (messaging/gateway, voice, pets, kanban, cron, skills
hub, billing, checkpoints, delegation, etc.). The PCBDraft CLI must surface only
the commands it actually uses, in help, autocomplete, and dispatch. This pruning
must not be implemented by editing `vendor/hermes` by hand; it is applied by the
PCBDraft integration layer at startup.

### R6. Installable and testable runtime

The selected Hermes UI/runtime assets and PCBDraft integration must be available
from the supported installation path, not only from this checkout's current
working directory. Tests must cover the launch route, repository invariants,
project command behavior, and the actual integration boundary that can run in
the local test environment.

## Acceptance Criteria

- [ ] A bare `pcbdraft` launch selects the approved Hermes interactive surface;
  the old PCBDraft Textual app is no longer the product frontend.
- [ ] The selected surface starts without relying on the shell's current
  directory and without creating a Hermes-specific alternative PCBDraft project
  directory.
- [ ] Repository selection is persisted once and is visible at startup; all
  normal `/new` project creation and subsequent engineering artifacts resolve
  below `<repository>/projects/`.
- [ ] `/projects`, `/project`, `/new`, and project opening operate on PCBDraft
  projects with visible success, empty-state, validation-error, and cancellation
  behavior.
- [ ] A selected project remains the project context for Hermes PCB tool calls;
  tools cannot silently switch to a different current-working-directory store.
- [ ] PCBDraft-specific Hermes integration is moved to responsibility-appropriate
  package locations, with no catch-all bridge module or stale old-TUI path left
  in the default dependency graph.
- [ ] Packaging/install verification proves every required runtime asset is
  present and launchable outside the source checkout.
- [ ] Focused tests and static checks pass for the changed paths, and any
  unavailable full Hermes/Node UI validation is explicitly reported.

## Product Decisions (Confirmed)

- The bare `pcbdraft` command is PCBDraft's CLI. Its interactive terminal is
  implemented on the vendored Hermes classic `prompt_toolkit` runtime; the
  product name remains PCBDraft's CLI, not a separate "Hermes mode".
- `/new` is the PCB project creation command. It creates a new project/session
  inside the configured PCBDraft repository (`<repository>/projects/<id>/`) and
  becomes the current project context. It is not a separate Hermes-session
  concept.
- `/projects`, `/project`, and `/open` operate on the same repository. Empty
  repositories show an actionable empty state; invalid repository paths show a
  validation error without creating a second store elsewhere.
- The CLI surfaces only PCBDraft-needed commands. Irrelevant Hermes built-ins
  are pruned (not shown in help/autocomplete, not dispatchable).

### PCBDraft command set (keep)

Kept from Hermes built-ins (as-is or lightly aliased): `/new`, `/open`,
`/status`, `/model`, `/goal`, `/stop`, `/retry`, `/undo`, `/quit`, `/help`,
`/clear`.

Added/owned by PCBDraft as slash handlers: `/projects`, `/project`, `/connect`,
`/review`, `/confirm`, `/discard`, `/logs`, `/validate`, `/release`.

Pruned (removed from help/autocomplete/dispatch): gateway/messaging and
platform commands, voice/wake, pets, kanban, cron, curator, skills hub and
bundles, blueprint/suggestions, memory/journey/refine, background/queue/steer,
delegation/agents, worktree/branch/checkpoints/snapshot/rollback/backup,
browser/image/paste/copy, subscription/topup/billing, insights/debug/update,
egress/context/reasoning/fast/yolo/approvals/skin/indicator/statusbar/battery/
timestamps/diff/verbose/focus/config/personality/profile/codex-runtime/reload*.

## Out Of Scope

- Restoring Hermes' separate modern Ink `ui-tui` / `tui_gateway` stack.

- Rewriting the PCB domain, KiCad generation, validation, or release engines.
- Adding a browser UI or a second chat frontend.
- Changing provider credentials or model protocol semantics unrelated to the
  Hermes launch/configuration boundary.

## Notes

- The prior project-repository requirement is recorded in the existing behavior
  and task history: projects should live in one user-selected repository,
  `/projects` must work even for an empty repository, and `/new` must provide a
  visible creation flow.
- Complex task artifacts `design.md` and `implement.md` are required before
  `task.py start`.
