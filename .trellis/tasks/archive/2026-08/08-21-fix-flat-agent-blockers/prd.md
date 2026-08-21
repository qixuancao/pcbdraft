# Fix flat PCB agent blockers

## Goal

Make a fresh flat-toolbox PCB task stay bound to the project the user selected
or created, and let the model complete a simple low-voltage prototype using
explicit, individually named tools without guessing hidden identifiers,
borrowing evidence from older projects, batching unobserved actions, or being
blocked between installed KiCad facts and PCBDraft's canonical part contract.

The motivating acceptance case is a new project containing a green 5 mm LED
and a 330 ohm 0805 resistor in the topology
`3V3 -> R1 -> D1 -> GND`, followed by explicit placement, routing, ERC, and DRC.

## Background and Confirmed Defects

### D1. Cross-project context is visible and switchable

- `pcb_list_projects` enumerates every project in the persistent repository.
- `pcb_open_project` replaces the module-level, process-scoped
  `_current_project_id` even after the model created a new project
  (`src/pcbdraft/agent/hermes_tools.py`).
- In the observed run, the model opened
  `led-5mm-led-330-0805-2-43cb9f7f`, created the new
  `project-f2564c65`, then opened the old project again and included its
  `pd_3v3` failure in the new-project summary.
- The project directories were not physically mixed. The defect is authority
  and conversational evidence leakage across project boundaries.

### D2. Canonical parts are required but not discoverable or creatable

- `pcb_add_component` requires an existing canonical `part_id`; a KiCad symbol
  such as `Device:LED` and a footprint such as `LED_THT:LED_D5.0mm` are not part
  identities.
- The model-facing toolbox has symbol/footprint search and description but no
  canonical `pcb_search_parts`, `pcb_describe_part`, or project-local part
  registration action.
- The failed transaction recorded
  `part.unknown: canonical part does not exist: led_green_5mm_part`.
- The new project catalog was not empty: it retained 16 bundled parts, including
  a green 0603 LED and several 0603 resistors. It contained neither the requested
  green 5 mm LED nor a 330 ohm 0805 resistor. The model's final factual summary
  was therefore inaccurate even though its conclusion that no usable exact part
  existed was correct.

### D3. Installed-library reads have an unnecessary project precondition

- The first symbol and footprint searches failed because no project was current.
- Installed KiCad symbol/footprint facts are machine-local read-only facts, but
  the Hermes handler routes them through the same current-project requirement as
  design reads.

### D4. One model response can batch several PCB calls

- The first model decision emitted `pcb_list_projects` plus four library searches
  before observing any result. The searches then failed because no project had
  been opened or created.
- Setting provider parallelism off does not by itself enforce the product rule
  that one PCB action returns one result to the model before it chooses the next
  action.

### D5. Provenance identifiers are writable but not discoverable

- `pcb_add_block` failed three times while the model guessed provenance values:
  one value violated identifier syntax and two referenced nonexistent IDs.
- The fourth call succeeded only after using an empty provenance list. A model
  should not be asked to supply opaque identifiers that no factual tool exposes.

### D6. Legacy power-domain binding compares semantic roles as identity

- The retained legacy circuit plan placed component `c1`, pin `1` on the
  `pd_3v3` net with role `source`, while the power-domain source used the same
  component and pin with role `vcc`.
- `validate_circuit_plan` indexes the full `Endpoint`, including `role`, so it
  reported that the source was not on the assigned net even though the physical
  component/pin matched (`src/pcbdraft/agent/plan.py:1151-1161`).
- The error is preserved in an existing project and must remain diagnosable.

## Requirements

- R1: Bind each live Hermes PCB task to one current project. Model-originated
  operations must not read from or switch to another project unless the user has
  explicitly authorized a project switch.
- R2: Keep projects durably separate. No fix may copy old conversation,
  validation, release, transaction, or design evidence into a new project.
- R3: Add flat, factual canonical-part discovery sufficient for the model to
  obtain valid `part_id` values before `pcb_add_component`.
- R4: Add one explicit, typed, atomic project-local part registration action for
  a locally installed symbol/footprint combination that has no suitable
  canonical catalog entry. It must validate symbol pins, footprint pads, pin/pad
  mapping, identity, and electrical types needed to generate a usable KiCad
  prototype.
- R5: Make installed symbol and footprint search/description usable without a
  current project. These reads must not establish or change the project cursor.
- R6: Enforce at most one model-originated PCB tool call per decision boundary.
  A second PCB call may be selected only after the first result has been returned
  to the model. This restriction does not prohibit deterministic bounded work
  internal to one concrete tool, such as routing one selected net.
- R7: Remove opaque provenance guessing from ordinary flat writes. Either derive
  operation provenance internally or expose a factual typed read before an ID is
  accepted; unknown IDs must still fail closed.
- R8: Treat physical endpoint identity as component plus pin for power-domain
  source binding, while retaining role as semantic metadata. Validation failures
  must identify an actual missing endpoint, wrong domain, or conflicting role
  instead of the current ambiguous message.
- R9: All part, semantic, and native writes retain the existing atomic
  stage/materialize/CAS/swap/rollback contract. Failed attempts leave no partial
  component or KiCad mutation.
- R10: Model summaries must distinguish persisted facts from inference. Catalog
  counts, available part identities, current project ID, and the origin project
  of any cited error must be explicit and correct.
- R11: Preserve readability and inspection of existing projects and legacy
  failure evidence.
- R12: The final quality gate must include a real black-box Hermes CLI run from
  the UV-installed package, not only direct service calls or mocked unit tests.
  Run it against an explicit isolated `--workspace` repository so acceptance can
  create both an old and a new project without modifying the user's normal
  repository pointer or retained projects.

## Acceptance Criteria

- [x] After a user creates project B, model-originated tools cannot open or quote
  project A without an explicit user-authorized switch; project B remains the
  active project through the turn.
- [x] A fresh Hermes session can search installed symbols and footprints before
  opening or creating a project, and those calls do not set a current project.
- [x] Canonical part search/description returns stable IDs and the typed contract
  needed by `pcb_add_component`.
- [x] When no exact part exists, the model can explicitly register project-local
  generic contracts for a green 5 mm LED and a 330 ohm 0805 resistor from local
  KiCad symbol/footprint facts.
- [x] The motivating LED project can add D1 and R1, create/connect its nets,
  explicitly place and route them, and run standalone ERC and DRC in KiCad.
- [x] Every model response contains at most one executable `pcb_*` call; the next
  call is produced only after the prior result is present in model context.
- [x] Adding a normal block/component does not require the model to guess a
  provenance ID. Deliberately supplied unknown provenance still fails closed.
- [x] A power-domain source referring to the same component/pin as an assigned
  net is recognized regardless of its descriptive role label, with focused
  compatibility coverage for the retained `pd_3v3` plan.
- [x] A failed part registration or component addition leaves design hash,
  revision, catalog, schematic, and PCB unchanged and retains a factual failed
  receipt.
- [x] Inspection and final responses identify the current project and never call
  a non-empty catalog empty; focused regression tests reproduce the observed
  trace failures.
- [x] After `uv tool install --force <checkout>`, the global `pcbdraft` command is
  driven through a PTY with `--workspace <isolated-repository>`: create a seeded
  old project, request the complete new LED design, and observe the agent finish
  without reading/switching to the old project.
- [x] The CLI acceptance inspects the debug trace to prove one `pcb_*` call per
  model decision, checks the final semantic/KiCad hashes and transaction
  receipts, opens the generated project records, and verifies standalone ERC and
  DRC evidence for the same design revision.

## Key Decisions

- K1: Once a Hermes session creates or opens a project, model-originated PCB
  actions are locked to that project. Listing or switching to another project
  requires explicit authority derived from the current user request; a model's
  own stated intention is insufficient. This preserves deliberate user-driven
  project switching without allowing autonomous cross-project inspection.
- K2: The MVP optimizes for a valid, normally usable KiCad prototype rather than
  a richer component catalog. When no existing part matches, the model may
  explicitly register a generic project-local part from installed KiCad symbol
  and footprint facts, then connect, place, route, render, and check it normally.

## Evidence

- Debug trace: `/home/qixuancao/.config/pcbdraft/debug/agent-trace.jsonl`, observed
  session `20260820_181917_5a5e51`, especially tool-end sequence 54-98.
- New project: `/home/qixuancao/.local/share/pcbdraft/application/projects/project-f2564c65`.
- Old project: `/home/qixuancao/.local/share/pcbdraft/application/projects/led-5mm-led-330-0805-2-43cb9f7f`.
- Flat Hermes cursor and dispatch: `src/pcbdraft/agent/hermes_tools.py`.
- Canonical part validation: `src/pcbdraft/domain/parts.py` and
  `src/pcbdraft/services/application.py`.
- Legacy power-domain validation: `src/pcbdraft/agent/plan.py:1151-1161`.
