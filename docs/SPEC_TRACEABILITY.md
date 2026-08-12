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
| R10 | High-level stable CLI/Python/agent API | `pcb_agent.cli`, Python modules, versioned JSON-RPC in `pcb_agent.api` | `tests/test_api.py`, CLI E2E | implemented |
| R11 | Structured requirements to schematic/PCB IR | strict `RequirementsSpec` + deterministic compiler | `tests/test_requirements.py` | implemented |
| R12 | Versioned, verified reusable functional blocks | CC0 block catalog + `BlockRegistry` implementations | block evidence/part/instantiation tests | implemented |
| R13 | Constraints and PCB synchronization | compiler constraints, managed manifest, `pcb_agent.sync` | real `pcbnew` pose import/apply/undo E2E | implemented |
| R14 | Deterministic placement solver | bounded objective solver in `pcb_agent.placement` | `tests/test_placement.py`, generated-board reproducibility | implemented |
| R15 | Constraint-aware routing integration | bounded grid router, fine-pitch neckdown, layers/vias, receipts | `tests/test_routing.py`, routed real-board DRC=0 | implemented |
| R16 | L0 syntax/file validity | `pcb_agent.validation` manifest/IR/KiCad parse checks | `tests/test_managed_pipeline.py` with real KiCad | implemented |
| R17 | L1 component/pin/footprint/connectivity validity | unified part contracts and schematic/PCB parity gate | part tests plus real parity E2E | implemented |
| R18 | L2 ERC/DRC with raw evidence | existing `gates.py` | existing fake + real smoke | implemented |
| R19 | L3 deterministic interface/functional rules | shared `pcb_agent.semantic_rules` plus exact pad/footprint/route checks | 78-case injected-fault corpus and managed validation E2E | implemented |
| R20 | L4 BOM/lifecycle/DFM/manufacturing checks | catalog lifecycle, BOM cross-checks, KiCad DRC and declared fabrication contracts | real manufacturing-release E2E | implemented |
| R21 | L5 simulation/SI/PI/thermal/EMI integration with honest availability | deterministic DC adapter, heuristic power budget, explicit unavailable/N-A adapters | validation state assertions | implemented |
| R22 | L6 human review is never fabricated | attributed external-evidence importer and production gate | missing/imported/tampered evidence tests | implemented |
| R23 | L7 physical build/test feedback is never fabricated | attributed, hash-checked physical evidence importer | missing/imported/tampered evidence tests | implemented |
| R24 | Explicit completed/N-A/unavailable/heuristic/human-required states | `CheckResult`/`LevelResult` evidence model | L0-L7 managed-pipeline tests | implemented |
| R25 | BOM, Gerber, drill, position, IPC-D356, PDF/SVG/render/STEP and release bundle | `pcb_agent.release` with cross-checks and deterministic ZIP | real `kicad-cli` manufacturing E2E | implemented |
| R26 | Bidirectional KiCad synchronization and semantic diff | recognized native pose import; unsupported native drift rejection; regeneration | real `pcbnew` preview/apply/validate/undo E2E | implemented |
| R27 | Snapshots, receipts, rollback and crash recovery | durable semantic journal, backup, undo/recovery; legacy patch receipts | apply/undo/partial-state recovery tests | implemented |
| R28 | Concurrency safety | `ResourceLock` + pre/post hash conflict checks | lock contention and drift tests | implemented |
| R29 | Bounded execution and hostile-project handling | bounded process/JSON/tree/model paths, create-only outputs, link checks, locks | process, project, transaction, integration, benchmark security tests | implemented |
| R30 | Independent license-clear error-injection corpus | 78-case CC0 corpus and independent methodology record | `tests/test_benchmark.py` license/balance/integrity checks | implemented |
| R31 | Detection, FP, repair, regression, repeatability, latency metrics | `pcb_agent.benchmark` and CLI/API runner | full-corpus metric assertions; final artifact pending | implemented |
| R32 | Model-output consistency measurement | blinded, isolated, bounded optional Codex repetitions | 2-run live smoke passed; final persisted run pending | in progress |
| R33 | Low-voltage 2-4 layer MCU/sensor/control acceptance example | planned ATtiny402/TMP102 board | real ERC/DRC/manufacturing E2E | in progress |
| R34 | Documentation, deploy/test/benchmark scripts, CI and packaging | existing MVP docs/scripts; release work pending | clean build/install checks | in progress |
| R35 | Physical manufacture and engineering sign-off | evidence import only; requires people/fabrication | L6/L7 remain explicit gates | external gate |

This table is updated at every verified milestone.  External gates are product behavior,
not unfinished software: the runtime must preserve and report them rather than manufacture
evidence.
