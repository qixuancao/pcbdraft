# Implementation Plan

## 1. Lock the flat protocol

- Define the complete concrete tool-name/schema matrix and effect/risk policy.
- Replace macro/router descriptor tests with one canonical flat-registry
  contract test covering Hermes and MCP exports.
- Add a schema snapshot/fingerprint so accidental tool authority drift fails
  deterministically.

## 2. Version semantic and native-intent state

- Extend the semantic operations needed for power domains, interfaces,
  footprint assignment, outline, placement, routes, and vias.
- Add backward-compatible IR/managed-manifest loading for existing projects.
- Add round-trip, strict-validation, stable-hash, conflict, and v1-compatibility
  tests before wiring model tools.

## 3. Build the atomic semantic-to-KiCad transaction

- Add one ApplicationService write primitive for a single typed operation.
- Stage semantic mutation and complete native materialization in a sibling
  directory; validate parity and publish with the existing lock/revision model.
- Persist bounded receipts and fact-only diffs.
- Test stale revisions, semantic failure, materialization failure,
  publication failure, rollback, idempotency, and concurrent calls.

## 4. Expose project, inspection, library, and semantic tools

- Register concrete project/read/library tools.
- Register component, block, net, pin, power-domain, interface, constraint,
  footprint, and board-rule write tools over the atomic service primitive.
- Ensure every successful write returns synchronized hashes/revision and every
  failure returns no authoritative mutation.

## 5. Implement native board tools

- Add installed-footprint search/description.
- Make materialization honor explicit outline and footprint pose state.
- Refactor routing to target one selected net while seeding all retained routes
  as obstacles; persist segments/vias only after successful materialization.
- Implement unroute and explicit add/remove via operations.
- Verify native inspection parity and preserve unrelated geometry in focused
  small-board fixtures.

## 6. Split checks, renders, and exports

- Extract individual semantic, connectivity, ERC, and DRC runners and receipts.
- Make candidate readiness a derived evidence view.
- Split schematic/board/3D rendering and Gerber/drill/BOM/pick-place/STEP
  exports into individual service methods and flat tools.
- Verify each output against its source revision/hash.

## 7. Switch the Hermes product path

- Register only flat PCB tools in the PCBDraft toolset.
- Route all handlers through ``PCBToolGateway`` with the requested CLI
  permission mode.
- Remove the default ``hermes-cli`` general-purpose tool bundle from the PCB
  agent surface, retaining only explicitly approved non-mutating helpers if
  required by tests.
- Remove routers and phase-sized macros from new Hermes schemas and ensure one
  result returns to the model after each operation.

## 8. Compatibility cleanup and documentation

- Keep old durable records readable but fail closed on attempted replay.
- Remove dead default-path policy/macro/router code after consumer searches.
- Update persona, README, architecture, roadmap, help, and changelog.
- Record the flat tool inventory and synchronization contract in project spec.

## Validation Plan

During normal iteration, follow the repository's ~90-second fast-validation
budget:

1. ``git diff --check``.
2. ``uv run ruff check`` and ``uv run ruff format --check`` on changed files.
3. Focused unit modules for the layer changed in that iteration.
4. Focused end-to-end Hermes tool-registration and one-operation transaction
   tests using fakes.

Before final integration, run focused cross-layer cases for:

- create empty synchronized project;
- add/remove/connect/disconnect component topology;
- footprint assignment and move;
- selected-net route/unroute and via add/remove;
- independent ERC/DRC and exports;
- permission modes and stale revisions;
- rollback under injected failures;
- opening an existing pre-change project.

Full ``scripts/test.sh``, real-KiCad acceptance, TUI E2E, Python matrix,
dependency audit, and release checks remain for CI/integration unless the user
explicitly requests a release gate.

## Risky Files and Rollback Points

- ``domain/ir.py`` / ``domain/operations.py``: version and hash compatibility.
- ``services/managed.py`` / ``services/application.py``: atomic publication and
  project revision authority.
- ``kicad/pcb.py`` / ``kicad/routing.py``: preserved geometry and selected-net
  routing.
- ``agent/tooling.py`` / ``agent/hermes_tools.py``: model authority surface.
- ``verification/validation.py``: evidence/readiness compatibility.

Commit implementation in the same milestone order so each compatibility or
geometry layer can be reverted independently without discarding unrelated
work.
