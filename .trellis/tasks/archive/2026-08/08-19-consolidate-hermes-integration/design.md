# Design — Consolidate Hermes UI integration

## Context

`pcbdraft` is the product. Its interactive terminal (bare `pcbdraft`) is currently implemented on the vendored Hermes classic `prompt_toolkit` runtime (`vendor/hermes` + `src/pcbdraft/hermes/bridge.py`). The old PCBDraft Textual app under `src/pcbdraft/interfaces/tui/` remains in the tree but is no longer the default launch path (`src/pcbdraft/interfaces/cli.py:260-267`). The vendored Hermes tree is a trimmed Python runtime only; there is no `ui-tui`/`tui_gateway` frontend in the checkout, and the classic CLI is the delivered terminal. The user wants one coherent CLI, a single project repository, and no catch-all `hermes` bridge folder.

## Goals

- Keep `pcbdraft` as the single user-facing name and CLI entry.
- Deliver the classic interactive terminal as the only interactive frontend; remove the old Textual frontend from the product graph.
- Keep all PCB projects under the repository resolved by `src/pcbdraft/core/repository.py` (`~/PCBDraft` default, persisted pointer + `.pcbdraft-repository.json` marker, `<repo>/projects/`).
- Put PCBDraft-owned Hermes integration where the behavior belongs instead of a generic `src/pcbdraft/hermes` bridge.

## Non-Goals

- Restoring the separate modern Ink `ui-tui`/`tui_gateway` stack.
- Changing PCB domain/KiCad generation/validation/release semantics.

## Architecture

### Runtime layering after this task

```
CLI entry: src/pcbdraft/interfaces/cli.py
    │
    ├─ repository authority: src/pcbdraft/core/repository.py
    ├─ project + conversation authority: src/pcbdraft/services/application.py
    ├─ permission / capability authority: src/pcbdraft/agent/{permissions,capability_registry,tooling}
    └─ interactive terminal runtime: vendored Hermes classic CLI (vendor/hermes/cli.py + hermes_cli/*, run_agent.py, agent/*, tools/*)
        │
        └─ PCBDraft integration (distributed, not a monolithic bridge):
            ├─ paths/home:        src/pcbdraft/core/hermes_paths.py (HERMES_HOME, vendor dir)
            ├─ model → Hermes config: src/pcbdraft/model/hermes_config.py (PCBDraft ModelConfig → Hermes config.yaml)
            ├─ persona:           src/pcbdraft/agent/persona.py (PCB_SOUL_MD)
            ├─ tool registration: src/pcbdraft/agent/hermes_tools.py (PCB macros + domain routers → Hermes registry)
            ├─ slash dispatch:    src/pcbdraft/interfaces/hermes_cli.py (PCBDraft slash handlers: /new, /projects, /project, /open)
            └─ debug trace:       src/pcbdraft/core/debug_trace.py + src/pcbdraft/interfaces/hermes_plugin.py (observer hooks)
```

The Hermes Python runtime stays vendored under `vendor/hermes` as a flat top-level layout (required by its own imports: `import cli`, `import run_agent`, `agent/*`, `tools/*`, `hermes_cli/*`). The PCBDraft process inserts that directory at `sys.path[0]` before any Hermes import. The vendored tree is not modified by hand per `.trellis/spec/backend/directory-structure.md`.

### Why the runtime stays in vendor/hermes

- Upstream Hermes expects top-level modules (`cli.py`, `run_agent.py`, `agent`, `tools`, `hermes_cli`, …). Moving them under `src/pcbdraft/...` would break absolute imports (`from agent...`, `from tools.registry import registry`, etc.) and require forking upstream files.
- The vendored trim already drops browser, voice, messaging gateways, kanban, web dist, locales extras, etc. Remaining scope is Python agent + CLI.
- Keeping it in `vendor/hermes` preserves the ability to re-trim from upstream with `git diff` anchored at that directory.

### Why PCBDraft-owned code leaves src/pcbdraft/hermes

`src/pcbdraft/hermes/bridge.py` currently owns vendor-path insertion, `HERMES_HOME` selection, Hermes config generation, persona seeding, PCB tool registration, debug plugin install, and `launch_chat`. That mixes `core` (paths), `model` (config translation), `agent` (persona/tools), and `interfaces` (launch) responsibilities into one catch-all. Splitting it matches `docs/PROJECT_STRUCTURE.md` layering and the `.trellis/spec` quality rules (layer purity, no new broad `except Exception` without justification, mypy-clean public APIs).

New homes:

| Current | New home | Owning layer |
|---|---|---|
| `hermes/bridge.hermes_vendor_dir`, `hermes_home`, `_install_vendor_path` | `src/pcbdraft/core/hermes_paths.py` | `core` (path resolution, env override `PCBDRAFT_HERMES_DIR` / `HERMES_HOME`) |
| `hermes/bridge.write_hermes_config` | `src/pcbdraft/model/hermes_config.py` | `model` (translate `ModelConfig` → Hermes `config.yaml`, include `platform_toolsets`, enable `pcbdraft-debug` plugin) |
| `hermes/persona.PCB_SOUL_MD`, `write_soul` | `src/pcbdraft/agent/persona.py` + `core/hermes_paths` helper | `agent` (persona text) + `core` (write `SOUL.md` under `HERMES_HOME`) |
| `hermes/pcb_tools.register_all_pcb_tools` and helpers | `src/pcbdraft/agent/hermes_tools.py` | `agent` (tool boundary, uses `ApplicationService`, `PCBToolRegistry`, `capability_registry`, bounded summaries) |
| `hermes/debug_trace` | `src/pcbdraft/core/debug_trace.py` | `core` (append-only JSONL writer, `PCBDRAFT_DEBUG_TRACE` gating, rotation) |
| `hermes/debug_plugin` | `src/pcbdraft/interfaces/hermes_plugin.py` | `interfaces` (Hermes plugin hook wiring, delegates to `core/debug_trace`) |
| `hermes/bridge.install_debug_plugin`, `activate`, `launch_chat` | `src/pcbdraft/interfaces/hermes_cli.py` | `interfaces` (orchestrates vendor path + config + persona + tools + plugin, then calls `hermes_cli.main.main`) |

`src/pcbdraft/hermes/__init__.py` and the old `bridge.py` are removed after the split. Compatibility re-exports remain temporarily in `src/pcbdraft/_compat.py` if needed for external imports, but internal imports must use canonical paths.

### Installable runtime

Problem: `vendor/hermes` lives outside `src/` and `pyproject.toml` currently packages only `pcbdraft*` (`package-dir = {"" = "src"}`). The built wheel `dist/pcbdraft-1.1.0.dev0-py3-none-any.whl` therefore contains no vendored runtime; an installed `pcbdraft` cannot find `vendor/hermes/cli.py` via `here.parents[3] / "vendor/hermes"` after `pipx`/`uv tool install`.

Solution without forking upstream imports:

- Keep the source-of-truth in `vendor/hermes` (git-tracked, diffable).
- At build/sdist time, copy/sync the trimmed runtime into the wheel as data attached to the `pcbdraft` distribution: `src/pcbdraft/data/vendor/hermes/` (or `src/pcbdraft/hermes_runtime/`). Alternatively, add a `package-dir` mapping that installs the vendored tree as top-level data alongside `pcbdraft` without claiming it as a Python package.
- `core/hermes_paths.hermes_vendor_dir()` resolves in priority: `PCBDRAFT_HERMES_DIR` override → installed data path (`importlib.resources` / `Path(__file__).parent / "data/vendor/hermes"`) → source checkout `vendor/hermes` relative to the file for editable installs (`uv sync --extra dev`). Resolution failure raises `RuntimeError` with actionable message.
- `MANIFEST.in` adds `recursive-include vendor/hermes *.py *.yaml *.json *.md` so `sdist` remains complete.
- `pyproject.toml` adds package data for the vendored runtime so the wheel contains it. No change to `hermes-agent`'s own `setup.py` guard (it blocks `pip wheel` for that project; we are building `pcbdraft`).

Verification: `uv build` → `unzip -l dist/*.whl | grep vendor/hermes` shows runtime, and `uv tool install dist/*.whl` can `pcbdraft --help` and `pcbdraft repository --json` without `PCBDRAFT_HERMES_DIR`.

### Single project repository

Ownership stays in `src/pcbdraft/core/repository.py`:

- Persisted pointer: `~/.config/pcbdraft/repository.json` (or platform config home) with `{schema, version, root, updated_at}`.
- Marker: `<repo>/.pcbdraft-repository.json` with `{schema, version, created_at}`.
- Default: `~/PCBDraft`.
- All `ApplicationService` instances created without explicit `workspace` use `current_repository()` and expose `root`, `projects_root`, `repository_source`.

Hermes's own notion of "workspace directory" (`TERMINAL_CWD`, cwd-based projects/checkpoints) must not become a second PCB project store. Enforcement:

- `ApplicationService` remains the only writer of `<repo>/projects/*`. No Hermes tool writes outside `project_root` except through `PCBToolExecutor` / `execute_capability`.
- Interactive startup does not `chdir` to the Hermes session directory. The process cwd is irrelevant; `core/repository.current_repository()` is still the source of truth.
- The Hermes home itself is placed under the PCBDraft config dir: `provider_config_path().parent / "hermes"` (e.g. `~/.config/pcbdraft/hermes/`), not under the PCB repository. Hermes state (`state.db`, `sessions/`, `config.yaml`, `SOUL.md`, `plugins/pcbdraft-debug/`) is therefore separate from PCB projects but still uses the PCBDraft config home.

### Interactive terminal behavior (now called "PCBDraft CLI")

Entry: `pcbdraft` with no subcommand → Hermes classic CLI (prompt_toolkit REPL) via `hermes_cli.main.main`. Subcommands `pcbdraft doctor|setup|repository|trace` are handled by `src/pcbdraft/interfaces/cli.py` without launching Hermes.

Startup sequence (`interfaces/hermes_cli.activate`):

1. Resolve vendored runtime dir and insert at `sys.path[0]`.
2. Install `gateway` stub (vendored trim omits messaging gateway but some `tools/*.py` import `gateway.*` lazily).
3. Resolve `HERMES_HOME` via `core/hermes_paths` (env override else `<config dir>/hermes`), `mkdir -p`.
4. Write Hermes `config.yaml` via `model/hermes_config` from `load_model_config()` (or `None` → `provider: auto`).
5. Write `SOUL.md` via `agent/persona`.
6. Register PCB tools via `agent/hermes_tools`.
7. Install debug plugin (`plugins/pcbdraft-debug/` + `__init__.py` shim) pointing at `core/debug_trace`.
8. `import model_tools` to trigger Hermes built-in tool discovery so the schema includes both Hermes tools and PCB tools.
9. `import hermes_cli.main as hermes_main` and exec with `sys.argv = ["pcbdraft", ...tokens]`.

Tool boundary:

- PCB tools execute through `PCBToolExecutor` / `execute_capability`, not by writing files directly. Tool results are bounded fact-only summaries; no `next_step` workflow directive.
- Process-scoped `_current_project_id` is kept in `agent/hermes_tools` for convenience but never trusted as durable state. Every tool that needs a project resolves `project_id or _current_project_id` and fails with a clear error if none.

Slash/interface commands in the terminal:

PCBDraft needs visible, keyboard-friendly commands mapped to `ApplicationService`:

| Slash | Action | Backend |
|---|---|---|
| `/new [name]` | Create a new PCB project in the repository and make it the current context | `ApplicationService.create_draft(name)` then `send_message` if an initial request is provided. Empty/invalid name → `ValidationError` with visible error. This is the primary "new window/session in the repository" action the user described. |
| `/projects` | List projects in `<repo>/projects/` | `ApplicationService.list_projects()`; empty → actionable hint to use `/new`. |
| `/project [directory]` | Show or switch repository | No arg → show `current_repository()`; with arg → `configure_repository(directory)` then rebind service/roots and refresh hermes context |
| `/open <id>` | Open an existing project | `ApplicationService.open_project(id)`; invalid id → `ValidationError` |
| `/help` | Show PCBDraft commands + Hermes built-ins | Merged help |

Implementation options (chosen: Hermes plugin slash handler):

- Option A: Hermes plugin (`hermes_cli/plugins.py` discovery) that registers handlers via `ctx.register_command(name, description, handler)` or the CLI command registry. Preferred because it keeps dispatch inside Hermes's own command system and appears in autocomplete/help.
- Option B: Wrapper that intercepts `sys.stdin` lines starting with `/new`, `/projects`, etc., before Hermes's `process_command` sees them. Simpler but bypasses Hermes's completion/help and risks drift.

Chosen: Option A — PCBDraft ships a Hermes plugin package (e.g., `src/pcbdraft/interfaces/hermes_plugin_cli.py` consumed via the `PCBDraft-debug` plugin install or a second `pcbdraft-cli` plugin manifest) that registers the four slash commands and delegates to `ApplicationService`. The plugin is installed alongside `pcbdraft-debug` under `HERMES_HOME/plugins/` by `activate()`.

If Option A proves incompatible with the vendored trim's plugin API, fallback is Option B with a thin `HermesCLI` monkey-patch that wraps `process_command`. The fallback is documented as a rollback point.

### Removal of the old Textual frontend

Delete:

- `src/pcbdraft/interfaces/tui/` (`app.py`, `controller.py`, `commands.py`, `widgets.py`, `theme.py`, `projection.py`, `review.py`, `session.py`, `styles.tcss`)
- Package stylesheet reference in `pyproject.toml` / `src/pcbdraft/__init__.py` if any
- Compat aliases for `pcbdraft.tui*` in `src/pcbdraft/_compat.py` (retain only if external users import them; otherwise remove and let the package-structure test catch drift)
- Tests that exclusively cover the old TUI (`tests/interfaces/test_tui.py`): remove or rewrite to cover the new Hermes CLI integration. The `test_implementation_modules_are_grouped_by_responsibility` expectation for `interfaces` now includes `cli.py`, `hermes_cli.py`, `hermes_plugin.py` instead of `tui/`.

No other layer imports `interfaces/tui`. Removal is therefore isolated.

### Command pruning

The vendored Hermes `COMMAND_REGISTRY` in `vendor/hermes/hermes_cli/commands.py`
is not edited by hand. PCBDraft prunes the surface at startup in
`interfaces/hermes_cli.activate()` by applying a whitelist to the registry and
the derived lookups (`COMMANDS`, `COMMANDS_BY_CATEGORY`, `COMMAND_LOOKUP`,
`SUBCOMMANDS`) before `hermes_cli.main.main()` runs. This is a controlled
monkey-patch owned by the integration layer, not a fork of vendor code, and is
re-applied on every launch (idempotent).

Mechanism: after `import hermes_cli.commands as _commands`, rebuild the registry
in place to contain only whitelisted `CommandDef`s plus PCBDraft-owned commands,
then call `_commands._build_command_lookup()` / rebuild the derived dicts
(`COMMANDS`, `COMMANDS_BY_CATEGORY`, `SUBCOMMANDS`, `GATEWAY_KNOWN_COMMANDS`).
The whitelist and the PCBDraft command handlers live in
`src/pcbdraft/interfaces/commands.py` (canonical owner), so future edits do not
touch vendor files. Dispatch of the four PCBDraft repo commands (`/projects`,
`/project`, `/open`) plus PCB workflow commands (`/connect`, `/review`,
`/confirm`, `/discard`, `/logs`, `/validate`, `/release`) goes through the same
handlers that call `ApplicationService`/`current_repository()`.

Fallback if registry rebuild proves too invasive against the vendored trim:
register PCBDraft commands via the Hermes plugin command API and hide built-ins
by overriding the help/completion consumers. Either way, vendor `commands.py` is
not hand-edited.

## Data flow

```
User types: /new my-board
  → Hermes CLI prompt_toolkit loop
    → PCBDraft slash handler (plugin)
      → ApplicationService.create_draft("my-board")
        → <repo>/projects/my-board-xxxx/{project.json, conversation.json, ...}
      → update _current_project_id + Hermes session metadata
      → render success panel with project id + next step (describe the board)

User types: describe a board in natural language
  → Hermes agent loop (run_agent.py / conversation_loop)
    → model selects pcb_* tool (e.g. pcb_plan_request with message)
      → agent/hermes_tools._execute_tool
        → PCBToolExecutor → ApplicationService → repository/projects/<id>/*
      → bounded fact-only summary returned to model
    → model decides next tool (pcb_generate_candidate, pcb_validate, …)

User types: /projects
  → plugin handler → ApplicationService.list_projects() → render table (empty hint if none)

User types: /project /path/to/repo
  → plugin handler → configure_repository(path) → rebind ApplicationService roots
```

## Contracts

- `core/repository.ProjectRepository` unchanged: `{root: Path, source: str, configured_now: bool}`.
- `services/application.ApplicationService` interface unchanged except optional `set_repository` rebind used by `/project`.
- `agent/hermes_tools.register_all_pcb_tools` signature unchanged externally; internal `_service_cache` still allows tests to pin an isolated service.
- Hermes plugin install is idempotent: `manifest.yaml` + `__init__.py` shim only written when content differs.
- `model/hermes_config.write_hermes_config` still writes `HERMES_HOME/config.yaml` with `platform_toolsets.cli = [hermes-cli, pcbdraft]` and `plugins.enabled = [pcbdraft-debug]` (plus PCBDraft CLI plugin if added).

## Compatibility and migration

- Old TUI commands (`/new`, `/projects`, `/open`, `/project`, `/help`) keep their names and semantics; users do not need to learn a `Hermes`-prefixed namespace.
- `PCBDRAFT_HERMES_DIR` and `HERMES_HOME` env overrides remain honored for testing/advanced users.
- `config.toml` (`provider_config_path()`) location unchanged; `hermes/config.yaml` is a derived file and can be regenerated.
- Existing PCB projects under the previous repository path remain valid; no data migration outside re-resolving the repository pointer.

## Trade-offs

- Vendoring the full Hermes Python runtime bloats the wheel (~900 files) but gives deterministic imports and avoids adding `hermes-agent` as an external pip dependency that currently blocks wheel builds. Alternatives (subprocess-isolated hermes, lazy download) add network/runtime fragility.
- Distributing PCBDraft slash commands as a Hermes plugin ties us to Hermes's plugin API. Wrapping `process_command` is less coupled but duplicates command discovery (completion, help, `COMMAND_REGISTRY`). Plugin is preferred; monkey-patch is the documented fallback.
- Removing Textual (`textual>=8.2.8`) from `dependencies` shrinks the install but is a breaking change for any external code that imports `pcbdraft.interfaces.tui`. The package-structure test will now expect the slimmer `interfaces` set.

## Rollback considerations

- Keep the Textual tui branch/tag available; rollback is `git revert` the removal commit plus re-adding `textual` to `pyproject.toml`.
- Keep the old `src/pcbdraft/hermes` bridge commit reachable until the new homes are verified in CI and with `uv tool install`.

## Open implementation details resolved after user confirmation

- `/new` is the PCB project creation entry inside the repository (confirmed). It also establishes the Hermes session/project context; there is no separate "Hermes-only new session" that competes with it.
