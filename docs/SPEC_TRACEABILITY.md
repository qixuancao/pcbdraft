# CopperWright specification traceability

Source: `/mnt/2T/ai_agent_pcb_design_analysis.md`, read in full on 2026-08-12.
`implemented` means local code and an automated verification path exist. `external
gate` means the runtime behavior is complete but truthful evidence can only come
from a person, supplier, fabrication, lab, or physical board.

This table is the historical engineering-runtime baseline. R01–R44 do not claim
that conversational onboarding, a browser application, persistent user sessions,
or a multi-profile end-user workflow are complete. Those product-level criteria
are tracked separately in [`PRODUCT_ACCEPTANCE.md`](PRODUCT_ACCEPTANCE.md); the
historical report and its artifact hashes remain unchanged.

| ID | Analysis / completion requirement | Implementation | Verification | Status |
|---|---|---|---|---|
| R01 | Model-independent semantic circuit/PCB IR | `pcb_agent.ir` | `tests/test_ir.py` strict round trip | implemented |
| R02 | Requirements, typed interfaces, power domains, modules, budgets, intent, constraints, risk, verification, provenance | IR records and compiler output | IR/requirements tests and checked-in `design.pcbir.json` | implemented |
| R03 | Readable, versionable, comparable, compilable deterministic identity | canonical JSON and `Design.content_hash()` | reordered-input byte/hash equality | implemented |
| R04 | Explicit bounded low-voltage scope; reject unsupported/high-risk work | `pcb_agent.scope`, profile declaration | scope/requirements/API tests | implemented |
| R05 | Canonical part identity graph | `pcb_agent.parts`, CC0 part catalog | `tests/test_parts.py` | implemented |
| R06 | Manufacturer/MPN/package, symbol, footprint, exact pin/pad map | `PartRecord`, `PinDefinition` | real KiCad library resolution and native parity | implemented |
| R07 | Ratings, lifecycle/source, sourcing, BOM, manufacturing, models | part contracts | part/requirements/validation/release tests | implemented |
| R08 | Separate extracted/rule/human/production trust and provenance | trust enums and per-record evidence | catalog schema/trust tests | implemented |
| R09 | Verified reusable blocks | CC0 block catalog + `BlockRegistry` builders | metadata/implementation parts/ports/test equality | implemented |
| R10 | Semantic edits are primary, not raw KiCad text | `operations.py`, `transactions.py` | operation and transaction suites | implemented |
| R11 | Snapshot, preconditions, semantic diff, validate, commit/rollback | change set and transaction journal | preview/apply/undo/recovery E2E | implemented |
| R12 | Idempotency and concurrent conflict protection | base/field/source/staged hashes + `ResourceLock` | drift, idempotency, contention tests | implemented |
| R13 | High-level stable CLI/Python/agent API | `cli.py`, modules, JSON-RPC API 1.0 | CLI integration and `tests/test_api.py` | implemented |
| R14 | Strict requirements to semantic schematic/PCB design | `RequirementsSpec`, `compile_requirements` | compiler tests and acceptance fixture | implemented |
| R15 | Functional grouping and constraint generation | compiler constraints and block ports | semantic/requirements tests | implemented |
| R16 | Dedicated deterministic placement optimizer | `placement.py` | overlap/near/group/fixed tests | implemented |
| R17 | Dedicated bounded multilayer router | `routing.py`, fine-pitch escapes | route/via/keepout/bound tests | implemented |
| R18 | Native KiCad compilation through proven interfaces | `kicad_schematic.py`, isolated `pcbnew_worker.py` | real reproducible KiCad generation | implemented |
| R19 | KiCad round trip without post-generation loss of control | managed snapshots + `sync.py` pose import | real move/preview/regenerate/validate/undo | implemented |
| R20 | Reject unsupported native topology/part/rule drift | snapshot comparison and semantic import allow-list | sync and manifest drift tests | implemented |
| R21 | L0 file/syntax validity | validation manifest/IR/KiCad parse checks | real managed validation | implemented |
| R22 | L1 symbol/pin/footprint/connectivity validity | part graph and native parity | part tests + real schematic parity | implemented |
| R23 | L2 ERC/DRC | bounded real `kicad-cli` JSON reports | real 2/4-layer ERC/DRC tests | implemented |
| R24 | L3 interface/function/intent rules | shared `semantic_rules.py` | semantic corpus and managed validation | implemented |
| R25 | L4 BOM/lifecycle/DFM/manufacturing rules | catalog contracts, KiCad DRC, BOM/position/export cross-checks | real release E2E | implemented |
| R26 | L5 SPICE/SI/PI/thermal/EMI integration states | deterministic DC/power adapter, explicit N/A/unavailable results | L5 state assertions in managed pipeline | implemented |
| R27 | L6 qualified human review remains honest | attributed external-evidence importer | missing/imported/tampered tests | implemented |
| R28 | L7 fabrication/bring-up/test feedback remains honest | L7 artifact/serial/test-plan importer | missing/imported/tampered tests | implemented |
| R29 | Completed/N-A/unavailable/heuristic/human-required states | `CheckResult`/`LevelResult` evidence model | L0–L7 state tests | implemented |
| R30 | Manufacturing release workflow | `release.py` exports and contracts | real Gerber/drill/BOM/position/PDF/SVG/PNG/STEP/IPC-D356 E2E | implemented |
| R31 | Receipts, evidence bundles, rollback/recovery | validation/release/transaction receipts and backups | transaction/integration/release tests | implemented |
| R32 | Byte-reproducible content releases | normalized content/audit split and deterministic ZIP | two-release byte/hash equality test | implemented |
| R33 | Offline release verification and tamper detection | `release-verify`, `release.verify` | exact inventory/hash/ZIP and tamper tests | implemented |
| R34 | KiCad compatibility testing | fail-closed KiCad 10 policy; exact 10.0.5 marker | compatibility/doctor/real generation tests | implemented |
| R35 | Bounded execution and hostile-project safety | process/JSON/tree/archive/model bounds and link/path checks | security/process/integration/transaction tests | implemented |
| R36 | License-clear independent fault corpus | 90-case CC0 corpus, 70 faults + 20 controls | corpus integrity/license tests | implemented |
| R37 | Detection and false-positive metrics | `benchmark.py` confusion/target metrics | persisted result: 70 TP, 0 FN, 0 FP, 20 TN | implemented |
| R38 | Repair success and introduced regressions | production typed change sets and post-repair rules | persisted result: 65/65, 0 regressions | implemented |
| R39 | Repeatability and latency | finding digests and monotonic timing | 90/90 stable; mean 0.290597 ms over 450 | implemented |
| R40 | Model consistency across repetitions | blinded optional Codex runner | 2 runs, 48/48 correct, agreement 1.0 | implemented |
| R41 | Initial low-voltage MCU/sensor/control acceptance fixture | ATtiny402/TMP102 3.3 V 2-layer project; real 4-layer test variant | checked-in example, release, real review | implemented |
| R42 | Unsupported domains fail explicitly | profile/global policy split in API/compiler | historical 0.2 SPI rejection plus current USB/buck/RS-232/high-risk rejection tests; supported SPI now passes its complete v1 chain | implemented |
| R43 | Install/deploy/test/benchmark/docs/CI packaging | `pyproject.toml`, scripts, Makefile, docs, GitHub workflow | clean wheel/sdist/install and release check | implemented |
| R44 | Open-source licensing | Apache-2.0 source, CC0 data, KiCad/dependency notice | package license/member inspection | implemented |
| R45 | Physical manufacture, qualified sign-off, live sourcing, EMC and measured L7 | evidence import/gates only | candidate remains `production=false`; no fabricated evidence | external gate |

## CopperWright 1.0 product extension

The v1 application work did not redefine or overwrite the historical runtime
results. It added the following traced surfaces on top of them:

| Product area | Current implementation | Verification |
|---|---|---|
| Shared application authority | `application.py`, `jobs.py`, versioned private project format | application, restart, migration, concurrency tests |
| Natural-language providers | `providers.py`: authenticated Codex, OpenAI-compatible, builtin offline | strict-schema, real Codex, local endpoint, redaction tests |
| Terminal/browser clients | `chat.py`, `webapp.py`, packaged `web/` assets | clean-install chat E2E and real clean-HOME Firefox E2E |
| Semantic conversational modification | application-owned staged generate/validate/apply/discard/undo path | both product E2Es verify distinct hashes and exact undo restoration |
| Product profiles | I2C/TMP102, SPI/BME280, UART/AP2112K LDO | three committed native projects with real ERC/DRC and previews |
| Product release | candidate export and offline verification from both clients | terminal/browser E2E and runtime release hard gate |
| Unsupported scope | other board envelopes, USB 2.0, buck, RS-232 voltage levels, and high-risk domains | provider/profile/scope tests and browser unsupported state |

The complete application requirement matrix is
[`PRODUCT_ACCEPTANCE.md`](PRODUCT_ACCEPTANCE.md). Exact new evidence is summarized
in [`PRODUCT_REPORT_ZH.md`](PRODUCT_REPORT_ZH.md); the older
[`FINAL_REPORT_ZH.md`](FINAL_REPORT_ZH.md) remains the R01–R44 historical record.

## Persisted acceptance evidence

- Managed example: `examples/attiny_sensor_controller/project/`
- Design content hash:
  `4e47cdfcea912f74c1e5ae4beded97f6fec8411b9e0fbbcc12ee4a6ff61eb1d2`
- Candidate release: `artifacts/acceptance/release/`
- Release manifest hash:
  `64b779d1ea1ff744307a2faf803ea0c42b9b9710504b63b93a1742bf4f0cd778`
- Release ZIP hash:
  `d70f582c8f428df45e2c6a6aa56dc8ffeac368185849329b3d15f17df3c88d98`
- Real final Codex review: `artifacts/acceptance/review/20260812T154551Z-8f8b4876/`
- Benchmark: `artifacts/benchmark/benchmark-20260812.json`
- Final Chinese completion report: `docs/FINAL_REPORT_ZH.md`

## Reproducible verification entry points

```bash
scripts/test.sh
scripts/benchmark.sh
scripts/smoke.sh
scripts/compatibility.sh
scripts/release-check.sh
copperwright release-verify artifacts/acceptance/release --json
```

R26, R27, R28, and R45 intentionally do not equate ERC/DRC or a model response
with physical correctness. The software for recording and gating those states is
implemented; the missing engineering/physical facts are external by definition.
