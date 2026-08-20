# Directory Structure

> How backend code is organized in this project.

---

## Overview

PCBDraft is a Python 3.11+ package installed via a `src/` layout (see
`pyproject.toml`: `package-dir = {"" = "src"}`). All application code lives in
`src/pcbdraft/`; the tests live in `tests/` and mirror the package layout one
directory at a time (`tests/domain/test_parts.py` ↔ `src/pcbdraft/domain/parts.py`).

The package is layered by dependency responsibility, documented in
`docs/PROJECT_STRUCTURE.md`:

- `domain/` — pure business logic (IR, parts, blocks, scope, requirements). No
  I/O, no agent knowledge; operates on in-memory dataclasses.
- `core/` — foundational infrastructure: error types, atomic I/O, locking,
  repository/run locations, process and platform helpers.
- `agent/` — the agent loop: orchestrator, runtime, tools, permissions, plan,
  repair, review, policy, capabilities.
- `model/` — LLM provider integration: API clients, config, retry, tool calls.
- `kicad/` — KiCad integration: PCB/Schematic generation, placement, routing,
  pcbnew worker subprocess, sync, previews.
- `services/` — orchestration across the layers: application, transactions,
  patching, jobs, doctor, managed processes.
- `verification/` — gates, evidence, benchmark, release, validation.
- `interfaces/` — user-facing entry points: `cli.py` (subcommands), the
  interactive Hermes terminal startup (`hermes_cli.py`), the pruned
  slash-command surface (`commands.py`), and the debug plugin body
  (`hermes_plugin.py`). Thin; no business logic.
- Hermes integration is distributed by responsibility: paths/home in
  `core/hermes_paths.py`, provider/auth onboarding and sanitized status in
  `services/provider_connection.py`, Hermes-owned config defaults in
  `model/hermes_config.py`, persona in `agent/persona.py`, PCB tool registration in
  `agent/hermes_tools.py`, debug trace in `core/debug_trace.py` — no
  catch-all `hermes/` bridge package.
- `data/` — bundled, immutable JSON catalogs (`parts/catalog.json`,
  `blocks/catalog.json`, `benchmark/*.json`). Read via
  `pcbdraft.core.resources.data_path()`; CC0-1.0 licensed.

There is no web API layer in `src/pcbdraft`; the only public entry point is the
`pcbdraft` console script (`interfaces/cli.py:main`).

---

## Directory Layout

```
src/pcbdraft/
├── agent/          # agent loop: orchestrator, runtime, tools, permissions, plan, repair, review
├── core/           # errors, io (atomic), locking, project, repository, runs, resources, process
├── data/           # bundled JSON catalogs (immutable, CC0-1.0)
├── domain/         # pure business logic: parts, blocks, ir, operations, scope, requirements
├── interfaces/     # cli.py + hermes_cli.py + commands.py + hermes_plugin.py (interactive Hermes terminal)
├── kicad/          # pcb, schematic, placement, routing, pcbnew_worker, sync, runtime
├── model/          # LLM providers: Hermes adapter, API contracts, retry, tool calls
├── services/       # application, transactions, patching, jobs, doctor, managed
└── verification/   # gates, evidence, benchmark, release, report, validation

tests/
├── agent/ core/ domain/ interfaces/ kicad/ model/ services/ verification/
├── support/        # factories (design_factory.py, requirements_factory.py)
├── fakes/          # executable fakes, e.g. kicad-cli
├── fixtures/       # static JSON fixtures, e.g. attiny_sensor_controller.json
└── integration/

scripts/            # bash/python tooling: test.sh, release-check.sh, benchmark.sh, deploy.sh
vendor/hermes/      # vendored upstream Hermes Python runtime (do not modify by hand);
                    # shipped into the wheel as pcbdraft/data/vendor/hermes by setup.py build_py
docs/               # ARCHITECTURE.md, DEVELOPMENT.md, PROJECT_STRUCTURE.md
```

---

## Module Organization

- One module per cohesive concept; modules stay small and flat (no deep
  sub-package nesting).
- `domain/` modules import only `pcbdraft.domain.*` and
  `pcbdraft.core.errors` (shared `ValidationError`); no `agent`, `model`,
  `kicad`, or `services` imports. They are pure and testable in isolation
  (`tests/domain/` has no I/O).
- `agent/` and `services/` are allowed to import `core` and `domain`, and
  `kicad/`/`model/` sit behind adapter ports (`agent/ports.py`).
- New features follow `docs/PROJECT_STRUCTURE.md` "Adding a module": pick the
  owning layer, add a sibling test directory, and update the module map.
- Static data catalogs go under `src/pcbdraft/data/<domain>/catalog.json` and
  are loaded with bounded reads, never written at runtime.

---

## Naming Conventions

- Package: lowercase single word `pcbdraft`; modules snake_case
  (`part_resolver.py`, not `PartResolver.py`).
- Layer directories are short lowercase nouns (`agent`, `domain`, `kicad`).
- Tests mirror the module name: `src/pcbdraft/domain/parts.py` →
  `tests/domain/test_parts.py`; test classes end in `Tests` and use
  `unittest.TestCase`.
- Catalog JSON files are `catalog.json` under a domain folder
  (`data/parts/catalog.json`, `data/blocks/catalog.json`).
- Run artifacts live under the repository root / per-run directories managed by
  `core/runs.py` and `core/repository.py`, never inside `src/`.

---

## Examples

- Layering reference: `src/pcbdraft/domain/parts.py` (pure dataclasses +
  `from_dict`/`to_dict`) → `src/pcbdraft/agent/part_resolver.py` (consumes the
  domain through the agent layer).
- CLI entry point with error mapping: `src/pcbdraft/interfaces/cli.py`.
- Well-organized test module mirroring the package:
  `tests/domain/test_operations.py` and `tests/kicad/test_routing.py`.
- Structural invariants are enforced by tests:
  `tests/core/test_package_structure.py`.
