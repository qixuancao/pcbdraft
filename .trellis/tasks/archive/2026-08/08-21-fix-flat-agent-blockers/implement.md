# Implementation Plan

## 1. Lock project authority

- Introduce a session-aware PCB project context store using trusted Hermes
  `session_id` and human selection epochs.
- Route `/new`, `/open`, `--project`, and repository switching through the
  trusted selection boundary.
- Remove model-facing project list/open exports; constrain model project creation
  to unbound sessions.
- Add cross-project denial, session reset, repository switch, and durable
  isolation tests.

## 2. Enforce one PCB action per model decision

- Register PCBDraft `tool_execution` middleware in the installed Hermes plugin.
- Execute only the first `pcb_*` call for each `(session_id, turn_id)` and return
  bounded non-mutating policy results for later calls in the same provider batch.
- Bound/clean tracker state and verify protocol-complete tool results under
  sequential, parallel-safe, and mixed batches.
- Extend debug-trace tests to distinguish dispatched, blocked, and failed calls.

## 3. Add generic KiCad part workflow

- Add strict schemas and registry handlers for `search_parts`, `describe_part`,
  and `register_kicad_part`; refresh the fixed schema fingerprint.
- Make installed symbol/footprint reads project-independent.
- Add `PartGraph` search/describe/append helpers and deterministic generic
  `PartRecord` construction with `extracted` trust and local KiCad evidence.
- Validate installed symbol pins, footprint pads, uniqueness, and complete
  pin-to-pad mappings before staging.
- Implement atomic catalog + design + native project publication,
  CAS checks, no-op/collision behavior, receipts, and post-swap rollback.

## 4. Remove undiscoverable inputs and repair compatibility validation

- Remove provenance arrays from ordinary model-facing add schemas and inject
  safe internal provenance values at the operation boundary.
- Change legacy power-domain source lookup to physical `(component, pin)`
  identity without weakening connect/disconnect role matching.
- Improve validation messages and add the retained `pd_3v3` plan as a regression
  fixture or minimal equivalent.

## 5. Make inspection and summaries factual

- Include bound project identity in every project-scoped result.
- Return actual catalog counts, stable part IDs, and retained local KiCad facts
  from part tools.
- Add regressions proving a failed part lookup cannot become a claim that the
  catalog is empty and another project's events cannot enter the summary.
- Update persona, CLI help, architecture, changelog, roadmap, and flat-toolbox
  code spec after the contracts are final.

## 6. Focused verification

Run the repository's normal fast checks during implementation:

1. `git diff --check`.
2. Ruff check/format on changed Python files.
3. Mypy on changed source or `src/pcbdraft` when the focused run is fast.
4. Focused `unittest` modules for tooling, Hermes tools/plugin, commands, parts,
   operations/plan, application transactions, and managed KiCad generation.

Required failure injection:

- cross-project model open/list/create attempts;
- second PCB call in one provider response;
- unknown/colliding part, nonexistent symbol/footprint, bad/missing pad mapping;
- stale revision/catalog hash;
- materialization, swap, state/event/receipt write failure;
- provenance guessing and legacy power-domain role mismatch.

## 7. Real CLI acceptance

- Run `pcbdraft doctor --json` and require model/KiCad/library readiness.
- Reinstall with `uv tool install --force /mnt/2T/pcbdraft`.
- Create a temporary explicit workspace; never change the user's persistent
  repository pointer.
- Drive the global `pcbdraft --workspace <temp>` terminal through a PTY.
- Seed old project A, create new project B, and request the green 5 mm LED +
  330-ohm 0805 board.
- Inspect the trace to prove one dispatched PCB action per model decision and no
  cross-project read/switch after B is bound.
- Verify D1/R1 and their generic part contracts, topology, explicit poses/routes,
  synchronized IR/KiCad hashes, receipts, and same-revision ERC/DRC evidence.
- Open/check the generated native files with KiCad CLI and report any stochastic
  model retry separately from deterministic product failures.

## Risk and Rollback Points

- Hermes middleware must always return one result per provider tool-call ID or
  the next model request becomes invalid.
- Session binding must use trusted runtime context, never a model argument.
- Part registration changes two authoritative inputs (catalog and design); both
  must roll back together.
- The CLI acceptance may exceed the normal 90-second iteration budget; this is
  explicitly authorized as the task's final acceptance, not an every-commit
  test.
