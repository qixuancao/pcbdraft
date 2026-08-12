# Specification traceability

Source specification: `/mnt/2T/ai_agent_pcb_design_analysis.md`, read in full on
2026-08-12.  Status values are `implemented`, `in progress`, `external gate`, and
`not applicable`; an item is not marked implemented until its cited automated check passes.

| ID | Requirement | Implementation evidence | Verification evidence | Status |
|---|---|---|---|---|
| R01 | Model-independent semantic circuit/PCB IR | `pcb_agent.ir` | `tests/test_ir.py` | implemented |
| R02 | Explicit requirements, intent, constraints, risk and provenance | `Design`, `Requirement`, `Constraint`, `Provenance` | `test_ir` round-trip/graph checks | implemented |
| R03 | Deterministic serialization, comparison and content identity | `Design.canonical_bytes/content_hash` | deterministic reordered-input test | implemented |
| R04 | Honest rejection of unsupported high-risk designs | `pcb_agent.scope` | `tests/test_scope.py` | implemented |
| R05 | Canonical part identity and symbol/footprint/pad graph | `pcb_agent.parts`, seed CC0 catalog | `tests/test_parts.py` real KiCad library resolution | implemented |
| R06 | Ratings, lifecycle, sourcing, BOM and manufacturability contracts | `PartRecord` contracts | part/rating validation tests | implemented |
| R07 | Evidence and trust states distinguish extraction/rules/human/production | `PartRecord.trust`, per-record evidence | catalog schema tests | implemented |
| R08 | Semantic transactional edits are primary; preview, commit, undo, recovery | `pcb_agent.operations`, `pcb_agent.transactions` | `test_operations`, `test_transactions` | implemented |
| R09 | Preconditions, idempotency and conflict detection | typed operation expectations + base/staged/source hashes | stale-field/base/drift/idempotency tests | implemented |
| R10 | High-level stable CLI/Python/agent API | planned CLI and JSON-RPC surface | CLI/RPC compatibility tests | in progress |
| R11 | Structured requirements to schematic/PCB IR | planned deterministic requirements compiler | generation E2E | in progress |
| R12 | Versioned, verified reusable functional blocks | planned block registry | block contract fixtures | in progress |
| R13 | Constraints and PCB synchronization | planned compiler/sync state | real KiCad round-trip E2E | in progress |
| R14 | Deterministic placement solver | planned bounded objective solver | geometry/property tests | in progress |
| R15 | Constraint-aware routing integration | planned bounded grid router + external adapter | routed real-board DRC | in progress |
| R16 | L0 syntax/file validity | validation pipeline | malformed/real KiCad tests | in progress |
| R17 | L1 component/pin/footprint/connectivity validity | part graph implemented; unified gate pending | part and sync tests | in progress |
| R18 | L2 ERC/DRC with raw evidence | existing `gates.py` | existing fake + real smoke | implemented |
| R19 | L3 deterministic interface/functional rules | planned rule registry | injected-fault tests | in progress |
| R20 | L4 BOM/lifecycle/DFM/manufacturing checks | part contracts implemented; release gate pending | manufacturing E2E | in progress |
| R21 | L5 simulation/SI/PI/thermal/EMI integration with honest availability | planned adapters/status model | adapter tests | in progress |
| R22 | L6 human review is never fabricated | planned sign-off gate | release refusal tests | in progress |
| R23 | L7 physical build/test feedback is never fabricated | planned physical-evidence importer | release refusal/import tests | in progress |
| R24 | Explicit completed/N-A/unavailable/heuristic/human-required states | planned validation evidence model | state coverage tests | in progress |
| R25 | BOM, Gerber, drill, position, render and release evidence bundle | planned manufacturing backend | real `kicad-cli` E2E | in progress |
| R26 | Bidirectional KiCad synchronization and semantic diff | planned KiCad adapters | import/export/check E2E | in progress |
| R27 | Snapshots, receipts, rollback and crash recovery | durable semantic journal, backup, undo/recovery; legacy patch receipts | apply/undo/partial-state recovery tests | implemented |
| R28 | Concurrency safety | `ResourceLock` + pre/post hash conflict checks | lock contention and drift tests | implemented |
| R29 | Bounded execution and hostile-project handling | existing process/tree bounds; new paths pending | security suite | in progress |
| R30 | Independent license-clear error-injection corpus | planned CC0 corpus | corpus license/integrity test | in progress |
| R31 | Detection, FP, repair, regression, repeatability, latency metrics | planned benchmark runner | reproducible benchmark artifact | in progress |
| R32 | Model-output consistency measurement | planned optional bounded reviewer repetitions | live result or explicit unavailable state | in progress |
| R33 | Low-voltage 2-4 layer MCU/sensor/control acceptance example | planned ATtiny402/TMP102 board | real ERC/DRC/manufacturing E2E | in progress |
| R34 | Documentation, deploy/test/benchmark scripts, CI and packaging | existing MVP docs/scripts; release work pending | clean build/install checks | in progress |
| R35 | Physical manufacture and engineering sign-off | evidence import only; requires people/fabrication | L6/L7 remain explicit gates | external gate |

This table is updated at every verified milestone.  External gates are product behavior,
not unfinished software: the runtime must preserve and report them rather than manufacture
evidence.
