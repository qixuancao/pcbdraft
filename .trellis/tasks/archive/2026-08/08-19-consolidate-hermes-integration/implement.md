# Implement — Consolidate Hermes UI integration

## Checklist

### 1. Evidence checks (before editing)

- [ ] Re-read `src/pcbdraft/interfaces/cli.py`, `src/pcbdraft/hermes/*`, `src/pcbdraft/interfaces/tui/*`, `src/pcbdraft/core/repository.py`, `src/pcbdraft/services/application.py`, `vendor/hermes/cli.py` and `hermes_cli/main.py` hot paths.
- [ ] Run `python3 .trellis/scripts/get_context.py --mode packages` and confirm spec indexes for `backend` and `guides` are current.
- [ ] Inspect `tests/interfaces/test_tui.py` and `tests/core/test_package_structure.py` expectations about `interfaces/tui` and packaged `styles.tcss`.

### 2. New homes for PCBDraft-owned Hermes integration

- [ ] Create `src/pcbdraft/core/hermes_paths.py` — `hermes_vendor_dir()`, `hermes_home()`, `_install_vendor_path()`, handling `PCBDRAFT_HERMES_DIR`/`HERMES_HOME` and installed wheel path (`importlib.resources` / `Path(__file__).parent / "data/vendor/hermes"` fallback to `vendor/hermes` for editable installs).
- [ ] Create `src/pcbdraft/model/hermes_config.py` — `write_hermes_config(model?)` translating `ModelConfig` to `HERMES_HOME/config.yaml` (same content as before: `model.provider`, `display.interface: cli`, `platform_toolsets.cli`, `plugins.enabled: [pcbdraft-debug, pcbdraft-cli]` if the CLI plugin is added).
- [ ] Move persona: `src/pcbdraft/hermes/persona.py` → `src/pcbdraft/agent/persona.py` (keep `PCB_SOUL_MD` text ± minor edits), and provide `write_soul()` helper via `core/hermes_paths`.
- [ ] Move debug trace: `src/pcbdraft/hermes/debug_trace.py` → `src/pcbdraft/core/debug_trace.py` (no behavior change; keep `PCBDRAFT_DEBUG_TRACE` gating, rotation, redaction).
- [ ] Move debug plugin body: `src/pcbdraft/hermes/debug_plugin.py` → `src/pcbdraft/interfaces/hermes_plugin.py` (still exposes `register(ctx)` with the 8 hooks, now importing `core/debug_trace`).
- [ ] Move tool registration: `src/pcbdraft/hermes/pcb_tools.py` → `src/pcbdraft/agent/hermes_tools.py` (keep `_current_project_id`, `_service_cache`, bounded summaries, macro + router registration into `tools.registry`).
- [ ] Create `src/pcbdraft/interfaces/hermes_cli.py` — `install_debug_plugin()`, `install_cli_plugin()` (if using plugin for slash commands), `activate(*, model?)`, `launch_cli(argv?)` (formerly `launch_chat`). Ensure `activate()` is idempotent and imports `model_tools` for discovery.
- [ ] Keep a minimal re-export shim in `src/pcbdraft/hermes/__init__.py` during the transition (or delete after 1 commit and update imports). Do not re-add a catch-all `bridge.py`.

### 3. Interactive terminal slash integration (the "real bridge" fix)

- [ ] Implement PCBDraft slash handlers for `/new`, `/projects`, `/project`, `/open` (and merged `/help`) that call `ApplicationService` methods directly and render actionable output (success + next step, empty-state hint, validation error).
- [ ] Preferred path: register them as a Hermes plugin discovered under `HERMES_HOME/plugins/pcbdraft-cli/` via `hermes_cli/plugins.py` (`ctx.register_command` / `COMMAND_REGISTRY` extension). Fallback if the vendored plugin API lacks command registration: monkey-patch `HermesCLI.process_command` in `interfaces/hermes_cli` to intercept the four names before delegating to Hermes.
- [ ] Ensure `/new [name]` creates `projects/<slug>-<rand>/` under the current repository, sets `_current_project_id`, and the PCB tools use it. Bare `/new` shows usage instead of silently creating `new-*` or being treated as a PCB request. Invalid names → `ValidationError` message, no project created.
- [ ] Ensure `/project [dir]` with no arg shows `current_repository()`; with arg calls `configure_repository(dir)` and rebinds service roots; `HERMES_HOME` stays under the config dir, not inside the PCB repository.
- [ ] Verify that normal PCB natural-language requests still flow through Hermes agent → `pcb_plan_request` et al., not through slash handlers.

### 4. Remove the old Textual frontend from the product graph

- [ ] Delete `src/pcbdraft/interfaces/tui/` (`app.py`, `controller.py`, `commands.py`, `widgets.py`, `theme.py`, `projection.py`, `review.py`, `session.py`, `styles.tcss`).
- [ ] Update `src/pcbdraft/interfaces/cli.py` to import `launch_cli` from `interfaces/hermes_cli` instead of `hermes/bridge.launch_chat`; remove any `run_tui_command` fallback.
- [ ] Remove `textual` from `pyproject.toml:dependencies` (keep only if a dev extra needs it). Run `uv lock` after.
- [ ] Update `src/pcbdraft/_compat.py` MOVED_MODULES: drop `pcbdraft.tui*` aliases (or keep as deprecated shims if external usage exists — check `git log --all --grep=tui` and `rg pcbdraft\.tui`).
- [ ] Rewrite or delete `tests/interfaces/test_tui.py` to cover the new `interfaces/hermes_cli` + slash handlers and the repository invariants (do not keep stale Textual screenshot tests).
- [ ] Update `tests/core/test_package_structure.py` expectations: allowed `interfaces` set no longer contains `tui/`; stylesheet test now checks `interfaces/cli` + `interfaces/hermes_cli` or is updated to not require `styles.tcss`.

### 5. Make the runtime installable outside the checkout

- [ ] Add `MANIFEST.in` line: `recursive-include vendor/hermes *.py *.yaml *.json *.md *.txt` (and any needed assets).
- [ ] Add wheel data mapping so `vendor/hermes` is present in `sdist`/`wheel`: either copy into `src/pcbdraft/data/vendor/hermes` at build time or configure `tool.setuptools.package-data` / `data-files` and update `core/hermes_paths.hermes_vendor_dir()` to locate the installed path via `importlib.resources.files("pcbdraft") / "data/vendor/hermes"` before falling back to the source checkout path. Keep `vendor/hermes` as the git-tracked source of truth.
- [ ] Ensure `HERMES_HOME` default remains `provider_config_path().parent / "hermes"`; do not place it inside `<repo>/projects`.
- [ ] Verify `uv build && uv tool install dist/*.whl` succeeds and `pcbdraft --help` / `pcbdraft repository --json` work without env overrides; `python -c "from pcbdraft.core.hermes_paths import hermes_vendor_dir; print(hermes_vendor_dir())"` prints an existing directory in both editable and installed modes.

### 6. Prune unused Hermes commands

- [ ] Create `src/pcbdraft/interfaces/commands.py` owning the PCBDraft slash command set: the keep-list (Hermes built-ins reused) + PCBDraft-owned handlers for `/projects`, `/project`, `/open`, `/connect`, `/review`, `/confirm`, `/discard`, `/logs`, `/validate`, `/release`.
- [ ] In `interfaces/hermes_cli.activate()`, apply the whitelist to `hermes_cli.commands` (`COMMAND_REGISTRY`, `COMMANDS`, `COMMANDS_BY_CATEGORY`, `COMMAND_LOOKUP`, `SUBCOMMANDS`) after import and before `hermes_cli.main.main()`. Do not edit `vendor/hermes/hermes_cli/commands.py`.
- [ ] Route PCBDraft-owned commands to `ApplicationService` / `current_repository()` handlers (same backend as `/new`). Verify `/validate`, `/release`, `/review`, `/confirm`, `/discard`, `/logs` reuse the existing PCB capability boundary, not re-implemented logic.
- [ ] Add a regression test asserting the surfaced command set equals the whitelist (no gateway/voice/kanban/cron/skills/billing commands leak into help/autocomplete), using a hermetic `HERMES_HOME`.
- [ ] Confirm normal PCB natural-language requests still reach the Hermes agent loop and PCB tools, not the pruned command table.

### 7. Docs and spec sync

- [ ] Update `docs/PROJECT_STRUCTURE.md` and `README.md` (the `pcbdraft/interfaces/` row and the `/new` description) to match the new layout (no `tui/`, new `hermes_cli`/`hermes_plugin` owners, pruned command set).
- [ ] Update `.trellis/spec/backend/directory-structure.md` (interface list, vendor note) and any guide that mentions Textual TUI as the primary frontend.
- [ ] Keep this design's rollback note: tag the last Textual commit and keep `textual` pin in git history for revert.

## Validation

Run only the focused checks for changed paths before proposing the change (AGENTS.md fast budget ~90s):

```bash
git diff --check
uv run ruff check src/pcbdraft/core/hermes_paths.py src/pcbdraft/model/hermes_config.py src/pcbdraft/agent/persona.py src/pcbdraft/agent/hermes_tools.py src/pcbdraft/interfaces/hermes_cli.py src/pcbdraft/interfaces/hermes_plugin.py src/pcbdraft/core/debug_trace.py
uv run ruff format --check src/pcbdraft/core/hermes_paths.py src/pcbdraft/model/hermes_config.py src/pcbdraft/agent/persona.py src/pcbdraft/agent/hermes_tools.py src/pcbdraft/interfaces/hermes_cli.py src/pcbdraft/core/debug_trace.py
uv run mypy src/pcbdraft/core/hermes_paths.py src/pcbdraft/model/hermes_config.py src/pcbdraft/agent/hermes_tools.py src/pcbdraft/interfaces/hermes_cli.py
uv run coverage run -m unittest discover -s tests -v -k "test_package_structure or test_hermes or test_cli"
uv build && python3 -c "import zipfile, pathlib; print([n for n in zipfile.ZipFile(sorted(pathlib.Path('dist').glob('*.whl'))[-1]).namelist() if 'vendor/hermes' in n][:20])"
```

Full gates (`scripts/test.sh`, `scripts/release-check.sh`) are for the branch/CI, not every local commit.

## Rollback points

- Before deleting `interfaces/tui/`: tag or keep the commit that last passed `tests/interfaces/test_tui.py` so revert is one `git revert`.
- Before editing `pyproject.toml`/`uv.lock` (`textual` removal, packaging): keep the lock diff isolated in its own commit.
- Before changing `MANIFEST.in`/`hermes_vendor_dir`: verify both `uv sync --extra dev` (editable) and `uv build` + `uv tool install` paths in the same branch.
- If Hermes plugin command registration fails against the vendored trim, fall back to `HermesCLI.process_command` wrapping and document the deviation in `design.md`.

## Risks

- Vendored Hermes internal APIs (`tools.registry`, `hermes_cli.plugins`, `model_tools` discovery, `hermes_cli.commands` registry) are not versioned semver; a future re-trim may change the import path (`gateway` stub, `hermes_constants.get_hermes_home`) or the command registry shape. Mitigate by keeping the trim diff minimal and covering `hermes_vendor_dir`, `activate`, and the command whitelist with a small integration test.
- Packaging the vendored tree doubles sdist size; keep the trim (no `locales`, `web_dist`, `tui_dist`, `optional-skills`, etc.) and gate `MANIFEST.in` with `prune vendor/hermes/.venv vendor/hermes/.git`.
