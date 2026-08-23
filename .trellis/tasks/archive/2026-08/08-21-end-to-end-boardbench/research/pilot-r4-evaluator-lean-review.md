# Research: BoardBench R4 lean evaluator review

- Query: Review the 20 R4 cases only for issues that can change real BoardBench outcomes: prompt/hidden-contract unfairness, obvious automatic-scoring false results, unsatisfiable BOM/net/rating/manufacturing rules, or requirements unavailable through the normal PCBDraft product path.
- Scope: internal; private corpus read-only plus production BoardBench evaluator and normal Hermes/PCBDraft tool path.
- Date: 2026-08-22
- Reviewer: `codex-r4-evaluator-sol-max-20260822` (`gpt-5.6-sol`, max); independent of R4 authoring.

## Findings

### Outcome

**16 PASS / 4 NEEDS_CHANGE.** I found no new case-local failure in component identity, pin mapping, injective slot assignment, exact-BOM cardinality, intended-net separation, series-path integrity, rating direction, or numeric board envelope. The four changes below are product-affordance/acceptance ambiguities that can make an automatically successful run fail the mandatory engineering review.

Recommendation: **YES for one bounded smoke, using `mcu-01-attiny-updi` only.** Treat it strictly as a product-path/automatic-evaluator smoke, not as R4-wide readiness or engineering approval. Do not choose one of the four `NEEDS_CHANGE` cases, and do not begin a wider campaign until their requirements are made executable and unambiguous.

### Blocking test-impact findings

1. **Semantic board labels are unavailable through the tested product path.** The closed flat tool registry exposes component/net/board/placement/routing/check/export operations but no board-text or silkscreen-graphics write (`src/pcbdraft/agent/tooling.py:1163`, `:1383`, `:1408`, `:1531`). Native materialization also forcibly hides reference, value, and added footprint fields (`src/pcbdraft/kicad/pcbnew_worker.py:761-798`). Consequently the tested Agent cannot add `+`/`-`, `GND`, `3V3`, `HOST`, or `BRANCH` markings. This directly affects:

   - `driver-01-single-led`: physical input-polarity indication is mandatory.
   - `adapter-02-uart-crossover`: power and ground must be clearly labelled.
   - `adapter-03-i2c-fanout`: connector labels must distinguish host and branches.

   These conditions are absent from the automatic metric set (`src/pcbdraft/verification/boardbench_evaluator.py:2385-2440`), so the automatic score can pass a board that the mandatory review must reject. Add a board-text tool, or explicitly narrow these requirements to schematic/net naming where that is the intended criterion.

2. **The `sensor-02-bme280-i2c` vent rule has no executable layer/geometry contract.** It mandates a copper-and-silkscreen exclusion around the sensor vent, but the available semantic `board_keepout` is a board-anchored rectangle used for routing/placement (`src/pcbdraft/kicad/pcb.py:479-511`; `src/pcbdraft/agent/plan.py:1345-1363`), not a component-local vent opening that may contain the sensor itself. Native board generation separately creates and fills a board-wide GND reference plane without applying those semantic keepouts (`src/pcbdraft/kicad/pcbnew_worker.py:628-663`, `:832-847`, `:904-910`). The automatic evaluator does not inspect this manual assembly condition. Define the exact layer-local exclusion geometry and support it in native generation, or narrow the text to a visually decidable top-side obstruction/contamination rule.

### Per-case disposition

| case_id | verdict | Test-impact basis |
|---|---|---|
| `mcu-01-attiny-updi` | PASS | Supply/return, UPDI, opens, local bypass, exact BOM, partition, ratings, and envelope align; suitable bounded-smoke candidate. |
| `mcu-02-attiny-led` | PASS | One unbypassed resistor/LED path, polarity, opens, UPDI, ratings, and exact BOM align. |
| `mcu-03-attiny-i2c-host` | PASS | Separate pull-ups, pin order, UPDI, unused pins, and net partition align. |
| `mcu-04-attiny-uart` | PASS | UART mapping, UPDI, opens, decoupling, and distinct-net requirements align. |
| `sensor-01-tmp102-i2c` | PASS | Pin mapping, ADD0, ALERT-open rule, pull-ups, bypass, rating, and 0.15 mm clearance align. |
| `sensor-02-bme280-i2c` | NEEDS_CHANGE | Mandatory vent copper/silkscreen exclusion is not executable or objectively bounded through the normal path. |
| `sensor-03-bme280-spi` | PASS | SPI mapping, dual-domain bypass, ratings, exact BOM, and physical-obstruction wording are satisfiable. |
| `sensor-04-dual-i2c` | PASS | Shared bus/pull-up pair, distinct addresses, three injective bypass slots, and ALERT-open rule align. |
| `power-01-ap2112-basic` | PASS | EN/NC/cap topology and 0.2–0.6 A direction align; thermal proof remains an explicitly manual candidate check. |
| `power-02-ap2112-indicator` | PASS | Regulator and indicator series path align; thermal/LED calculations are prompt-visible manual checks. |
| `power-03-ap2112-sensor-feed` | PASS | Power-only JST, open data pins, LDO topology, current range, and exact BOM align. |
| `power-04-ap2112-dual-output` | PASS | Common regulated rail and total-current semantics align; no split-output bypass found. |
| `driver-01-single-led` | NEEDS_CHANGE | Required physical polarity marking cannot be authored by the current PCB tool surface. |
| `driver-02-dual-led` | PASS | Two injective resistor/LED branches and separate junctions prevent component reuse or branch collapse. |
| `driver-03-attiny-dual-led` | PASS | Two GPIO branches, UPDI, bypass, unused pins, exact BOM, and ratings align. |
| `driver-04-i2c-alert-led` | PASS | Open-drain sink topology, polarity, pull-ups, bypass, and safe fixed series values align. |
| `adapter-01-uart-straight` | PASS | Straight-through pin mapping and net partition align; fixed footprints retain pin-one silk geometry. |
| `adapter-02-uart-crossover` | NEEDS_CHANGE | Mandatory power/ground labelling is ambiguous and cannot be physically authored by the tested path. |
| `adapter-03-i2c-fanout` | NEEDS_CHANGE | Mandatory HOST/branch connector identification cannot be physically authored by the tested path. |
| `adapter-04-i2c-to-spi-header` | PASS | SPI topology and open auxiliary data pins align; the two connector types plus retained pin-one footprint marks can distinguish the interfaces without adding BOM items. |

### Files and code patterns inspected

- `corpus.draft.json`: all 20 prompts and all component, net, support, forbidden, rating, manufacturing, rubric, and assembly fields.
- `src/pcbdraft/verification/boardbench_evaluator.py`: exact identity candidate matching, injective assignment, topology/support/rating predicates, manufacturing checks, and final automatic metric construction.
- `src/pcbdraft/agent/tooling.py`: complete normal flat PCB tool surface.
- `src/pcbdraft/kicad/pcb.py` and `src/pcbdraft/kicad/pcbnew_worker.py`: semantic keepouts and native board/silkscreen/plane materialization.
- `src/pcbdraft/data/parts/catalog.json`: every R4 part identity, symbol, footprint, trust/BOM state, pin map, rating fact, and manufacturing fact.

### Related specs

- `.trellis/spec/backend/flat-pcb-toolbox.md`: the closed model-facing tool surface and explicit native writes.
- `.trellis/spec/backend/quality-guidelines.md`: evidence-first claims and focused verification.
- `.trellis/spec/guides/cross-layer-thinking-guide.md`: prompt to IR to native-board to evaluator contract tracing.
- `.trellis/tasks/08-21-end-to-end-boardbench/prd.md` and `design.md`: separation of automatic metrics from mandatory engineering review.

## Caveats / Not Found

- This was a static evaluator/product-path review. No PCBDraft/Hermes model, campaign, candidate board, fabrication, or hardware test ran.
- Pure review-protocol, provenance, hash, freeze, and source-binding metadata were intentionally excluded by the narrowed request.
- A PASS here means no practical result-changing defect was found in the reviewed source contract; it is not electrical sign-off, layout approval, orderability evidence, or proof that a model will finish within the smoke budget.
- Automatic PASS must not be reported as full-case PASS before the mandatory manual rubric and assembly checks are completed.
