# Research: Pilot R2 evaluator audit

- Query: Independently audit all 20 R2 pilot cases for evaluator validity, prompt alignment, net-partition and series-path completeness, exact-BOM fairness, rating direction, physical satisfiability, manufacturing geometry, and ERC/DRC blind-spot coverage.
- Scope: mixed (private corpus, local evaluator-v2 implementation, installed KiCad 10.0.5 libraries, and official device/manufacturer documentation)
- Date: 2026-08-22
- Reviewer role: R2 Reviewer B; fresh AI desk review, independent of R2 authoring
- Audited input: `/mnt/2T/pcbdraft-boardbench-private/v1-ai-pilot-r2/corpus.draft.json`

## Findings

### Hash and overall decision

The raw corpus SHA-256 is exactly
`4f774ea99a18755c65601f61dd270d9a1a8833b52d801d68ca8013aef149c1b6`,
matching the requested value. The production loader accepts all 20 cases under
the `ai_reviewed_pilot` cohort, and all referenced symbols, footprints, and pin
tokens resolve against the installed KiCad 10.0.5 environment.

The R2 corpus is **not ready for an AI pilot without changes**. Verdict totals:

- **PASS: 0**
- **NEEDS-CHANGE: 17**
- **FAIL: 3** (`power-01`, `power-02`, and `power-04`)

Verdict meanings used here:

- **PASS**: no material corpus/evaluator change found.
- **NEEDS-CHANGE**: a physically correct scoring solution exists, but a hidden
  predicate can reject a prompt-compliant solution, accept a materially wrong
  one, or the evidence path cannot prove the required review was performed.
- **FAIL**: the current closed contract prevents an otherwise correct solution
  from obtaining a fully passing automatic score.

### Blocking findings

#### F1 — Three AP2112 cases request a nonexistent rating fact

`power-01`, `power-02`, and `power-04` use the part-rating fact key
`max_current_a` (`corpus.draft.json:2470`, `:2802`, and `:3374`). The allowed
AP2112 catalog part instead records `max_output_current_a: 0.6`
(`src/pcbdraft/data/parts/catalog.json:214`). Evaluator v2 does not alias fact
names: a missing fact becomes `unknown`
(`src/pcbdraft/verification/boardbench_evaluator.py:1353-1375`). Ratings are a
mandatory metric and any mandatory `unknown` prevents an overall pass
(`boardbench_evaluator.py:2387-2401`, `:2441-2470`). Therefore no correct design
using the only allowed regulator can fully pass those three cases.

Required change: bind those three predicates to `max_output_current_a` and add a
focused evaluator fixture that proves the bundled AP2112 resolves to 0.6 A.

#### C1 — AP2112 load claims are not compared with regulator capacity

Even after F1 is corrected, `power-01`, `power-02`, and `power-04` independently
test (a) declared output-domain current is at least the prompt target and (b) the
part rating is at least the same target. They never test declared load/capacity
against the part's 0.6 A ceiling. A candidate claiming a 1 A output domain can
therefore satisfy both lower bounds. `power-03` has no current predicate at all,
although its prompt asks for thermal documentation at the declared load.

The direction implemented by `_inside_bound` is correct for the unary predicates
actually encoded: operating intervals must lie inside stated min/max bounds, and
a scalar capacity fact must meet a stated minimum
(`boardbench_evaluator.py:1345-1381`). The defect is the missing relation between
operating load and part rating, not a reversed comparator.

Required change: add a cross-bound relation, or at minimum an operating-current
maximum of 0.6 A to every AP2112 output domain while retaining the requested
minimums for `power-01`, `power-02`, and `power-04`.

#### C2 — Exact-BOM mechanics work, but the no-extra-parts rule is not in the prompts

Every case's `forbidden_unmatched_bom_component` subject set equals its complete
slot set. Slot assignment is injective, and evaluator v2 rejects every unmatched
trusted BOM component while permitting trusted `bom: false` ERC artifacts such
as `PWR_FLAG` (`boardbench_evaluator.py:1417-1460`). This correctly prevents one
physical resistor from satisfying two slots and prevents duplicate populated
pull-ups.

However, all 20 natural-language prompts permit at least some reasonable extra
physical component. None says “use exactly this BOM and no other populated
parts.” For example, the straight-through adapter forbids additional *active*
devices, not passives; the I2C fanout requires exactly one pull-up pair, not an
otherwise exact BOM. A benign test point, bulk capacitor, optional protection
part, or mounting-related BOM item therefore makes `support_circuits` fail even
when the board satisfies the prompt. The R2 methodology's bundled-part-only
disclosure is not shown to the tested model: the BoardBench design explicitly
sends the prompt only (`.trellis/tasks/08-21-end-to-end-boardbench/design.md:14-15`,
`:182-186`). A hidden scope note does not repair prompt/score alignment.

Required change: either state the exact populated-BOM/no-extras constraint in
each prompt, or limit exact cardinality to the components whose count is actually
specified and permit declared nonfunctional extras. The exact catalog identity
can remain a clearly labeled narrow-pilot limitation, but it should not silently
change the natural-language task.

#### C3 — Quantitative board envelopes are mostly hidden and are orientation-sensitive

All 20 cases have mandatory numeric maximum width and height. Only `mcu-01`
states its numeric 35 mm × 25 mm limit. `mcu-02` and `adapter-03` merely say
“compact”; the other 17 prompts give no size. A prompt-compliant, manufacturable
board can therefore fail DRC scoring solely because it exceeds an undisclosed
case-author preference.

Evaluator v2 also compares width and height independently
(`boardbench_evaluator.py:1489-1492`). Thus a 25 mm × 35 mm board fails a 35 mm ×
25 mm envelope even though it fits after a 90-degree rotation. This affects even
the explicitly dimensioned `mcu-01` case.

Required change: state each scored envelope in its prompt (or make it non-scoring)
and evaluate the unordered pair of board dimensions unless orientation itself is
a stated requirement.

#### C4 — Declared net partitions are complete, but unused active pins remain unconstrained

For every case, the authored `intended-net-partition` contains one representative
of every declared functional net, and all representatives are mutually distinct.
Every declared endpoint resolves. All required pull-up, strap, and decoupling
resistors/capacitors bridge the intended two distinct nets. Every specified LED
series path has separate before/after nets; a direct copper bypass collapses those
nets and fails the partition. No declared signal, rail, return, strap junction,
or series junction is missing.

The reference matcher nevertheless permits arbitrary connections on active-device
pins that appear in no predicate. These connections can evade ERC/DRC and still
pass automatic reference topology:

- `mcu-01`: ATtiny pins 2, 3, 4, 5, and 7;
- `mcu-02`: ATtiny pins 3, 4, 5, and 7;
- `mcu-03` and `mcu-04`: ATtiny pins 2, 3, and 7;
- `driver-03`: ATtiny pins 4, 5, and 7;
- `sensor-01`: TMP102 ALERT pin 3;
- `sensor-04`: TMP102 ALERT pin 3.

For example, an unused bidirectional GPIO tied to a rail need not be an ERC or
DRC error, but may become a firmware-dependent short. Required change: explicitly
state and enforce the intended treatment of unused pins, or add a mandatory
per-run review decision for unintended connections. Do not silently add an
“all unused pins must float” hidden rule unless that restriction is also made
part of the task.

#### C5 — Rubric and assembly text is good, but evaluator evidence cannot prove it was checked

The case text broadly covers the right ERC/DRC blind spots: local decoupler
placement and loop length, resistor non-bypass, LED polarity/current/power,
connector pin-one and cable exit, TMP102/BME280 package orientation, BME280 vent
and contamination control, AP2112 NC, ambient, copper area, dissipation, and
junction-temperature margin. These obligations are appropriate and make the
physical tasks satisfiable rather than relying on ERC/DRC alone.

Production evidence does not consume those obligations. `review_rubric` and
`assembly_constraints` are parsed and serialized on `BoardBenchCase`
(`src/pcbdraft/verification/boardbench.py:815-828`, `:840-906`, `:972-1068`),
but no production evaluator or review artifact stores per-item dispositions.
Review templates contain only global states and free-form findings
(`src/pcbdraft/verification/boardbench_evidence.py:105-139`;
`src/pcbdraft/verification/boardbench.py:2080-2197`). A
`pass_without_schematic_change` may have zero findings, and a passing outcome is
required to set `functional_correctness=pass` but is not required to set
`orderable_state=pass`. The seal gate checks only that reviews are present and
not `not_reviewed`; it does not require rubric coverage or an orderable pass
(`src/pcbdraft/verification/boardbench_report.py:811-848`).

Required change: preserve the current run/campaign source binding, carry stable
rubric and assembly item IDs into the review template, require a disposition and
evidence/note for every applicable item, require passing outcomes to be
orderable, and make seal validation fail closed on missing decisions.

### Twenty-case verdict table

Global tags in the table: **BOM** = C2; **ENV** = C3; **REV** = C5; **PIN** = C4;
**LOAD** = C1; **FACT** = F1. All cases have BOM and REV. All cases have the
orientation form of ENV; 19 also have an undisclosed numeric envelope.

| # | Case | Verdict | Case-specific result |
|---:|---|---|---|
| 1 | `mcu-01-attiny-updi` | **NEEDS-CHANGE** | VCC/GND/UPDI and the 100 nF bridge are correct; 35 × 25 mm is physically feasible and stated, but ENV rotation, PIN, BOM, and REV remain. |
| 2 | `mcu-02-attiny-led` | **NEEDS-CHANGE** | PA6-to-1 kΩ-to-LED partition is complete and non-bypass; 35 × 25 mm is feasible, but its number is hidden; PIN/BOM/ENV/REV. |
| 3 | `mcu-03-attiny-i2c-host` | **NEEDS-CHANGE** | PA1/SDA, PA2/SCL, separate pull-ups, decoupling, and GND/VTREF/UPDI are correct; 40 × 25 mm fits; PIN/BOM/ENV/REV. |
| 4 | `mcu-04-attiny-uart` | **NEEDS-CHANGE** | PA1/TX, PA2/RX, UPDI, supply, and decoupling are correct; 40 × 30 mm fits; PIN/BOM/ENV/REV. |
| 5 | `sensor-01-tmp102-i2c` | **NEEDS-CHANGE** | TMP102 pin map, ADD0-low, separate pull-ups, and 100 nF decoupling are plausible; 30 × 20 mm fits; ALERT is unconstrained; PIN/BOM/ENV/REV. |
| 6 | `sensor-02-bme280-i2c` | **NEEDS-CHANGE** | VDD/VDDIO, two capacitors, direct CSB-to-VDDIO, and resistor-strapped SDO-low are correct; 30 × 22 mm fits; BOM/ENV/REV. |
| 7 | `sensor-03-bme280-spi` | **NEEDS-CHANGE** | Complete MOSI/MISO/SCK/CS mapping, both supplies/grounds, and separate decouplers are correct; 32 × 24 mm fits; BOM/ENV/REV. |
| 8 | `sensor-04-dual-i2c` | **NEEDS-CHANGE** | One pull-up pair, three local capacitors, direct BME280 CSB, TMP102 0x48/BME280 0x77 straps, and net separation are correct; 40 × 28 mm fits; TMP ALERT is unconstrained; PIN/BOM/ENV/REV. |
| 9 | `power-01-ap2112-basic` | **FAIL** | VIN/EN, VOUT, NC-open, and both 1 µF bridges are correct; 0.46 W worst-case rubric and 35 × 25 mm envelope are plausible. FACT makes ratings permanently unknown; LOAD/BOM/ENV/REV also remain. |
| 10 | `power-02-ap2112-indicator` | **FAIL** | Regulated-rail LED series path and 0.345 W thermal obligation are correct; 38 × 25 mm fits. FACT is fatal; LOAD/BOM/ENV/REV also remain. |
| 11 | `power-03-ap2112-sensor-feed` | **NEEDS-CHANGE** | JST power-only topology, open SDA/SCL, NC-open, and capacitor placement obligations are correct; 35 × 25 mm fits. No current/rating relation is scored; LOAD/BOM/ENV/REV. |
| 12 | `power-04-ap2112-dual-output` | **FAIL** | Both outputs share the regulated rail; 42 × 28 mm fits. The 0.575 W worst-case point is thermally marginal but not impossible with justified copper/layout. FACT is fatal; LOAD/BOM/ENV/REV remain. |
| 13 | `driver-01-single-led` | **NEEDS-CHANGE** | VIN-to-1 kΩ-to-LED-to-GND direction and non-bypass partition are correct; 25 × 18 mm fits; BOM/ENV/REV. |
| 14 | `driver-02-dual-led` | **NEEDS-CHANGE** | Two injectively distinct resistors and separate branch junctions correctly reject shared/bypassed limiting; 30 × 20 mm fits; BOM/ENV/REV. |
| 15 | `driver-03-attiny-dual-led` | **NEEDS-CHANGE** | Separate PA6/PA7 resistor/LED branches, UPDI, and decoupling are correct; 35 × 25 mm fits; PIN/BOM/ENV/REV. |
| 16 | `driver-04-i2c-alert-led` | **NEEDS-CHANGE** | ALERT correctly sinks current through 1 kΩ from an LED anode at 3V3; expected current is below the TMP102's documented sink test point; 35 × 25 mm fits; BOM/ENV/REV. |
| 17 | `adapter-01-uart-straight` | **NEEDS-CHANGE** | Four straight-through nets and TX/RX separation are correct; 35 × 20 mm fits. Exact BOM is broader than “no active devices”; BOM/ENV/REV. |
| 18 | `adapter-02-uart-crossover` | **NEEDS-CHANGE** | TX/RX cross once, remain distinct, and power/ground pass straight through; 35 × 20 mm fits; BOM/ENV/REV. |
| 19 | `adapter-03-i2c-fanout` | **NEEDS-CHANGE** | All three connectors share four correctly separated nets and exactly one injective pull-up pair; 45 × 30 mm fits, but the numeric meaning of “compact” is hidden; BOM/ENV/REV. |
| 20 | `adapter-04-i2c-to-spi-header` | **NEEDS-CHANGE** | BME280 SPI topology is complete, auxiliary JST data pins are forbidden/open, and both supplies are decoupled; 42 × 30 mm fits; BOM/ENV/REV. |

### Physical plausibility and geometry

No impossible component geometry was found. Using the installed official KiCad
10.0.5 footprint courtyard extents, a deterministic rectangular packing check
with 1 mm board-edge margin found a non-overlapping component placement inside
every hidden envelope. This is a feasibility lower bound, not a routed-board or
cable-access proof. The textual assembly rubrics supply the necessary review for
JST side-entry clearance, BME280 vent access, fine-pitch reflow, polarity, and
pin-one marking.

The most constrained electrical/thermal cases remain feasible:

- ATtiny402 pin functions used by the corpus match the official eight-pin
  mapping: PA0/UPDI, PA1/SDA/TX, PA2/SCL/RX, PA6, and PA7. A 3.3 V or 5 V task is
  inside the device's documented supply range.
- TMP102's ADD0-low address and open-drain ALERT usage are valid. With 3.3 V,
  a green LED, and 1 kΩ, `driver-04` draws only about 1.2 mA at the catalogued
  typical 2.1 V forward drop, below the datasheet's 3 mA VOL test condition.
- BME280 I2C mode correctly holds CSB at VDDIO and straps SDO; both supply rails
  and grounds are represented. The dual-sensor case uses distinct addresses.
- AP2112's 600 mA rating and 1 µF stability capacitors support the requested
  loads. At the corpus worst corner, `power-04` dissipates
  `(5.5 - 3.2) × 0.25 = 0.575 W`. Applying the datasheet's nominal SOT-25
  `theta_JA = 184 °C/W` to 40 °C gives about 145.8 °C, only about 4.2 °C below
  150 °C. That does not prove impossibility, because thermal resistance is
  board-dependent; it does justify retaining a strict copper-area and junction
  review rather than an automatic pass.

### Independent source checks

- [KiCad 10 Getting Started — ERC](https://docs.kicad.org/10.0/en/getting_started_in_kicad/getting_started_in_kicad.html): ERC checks common connectivity/electrical-rule problems but does not establish that a circuit works.
- [KiCad 10 PCB Editor — DRC](https://docs.kicad.org/10.0/en/pcbnew/pcbnew.pdf): DRC evaluates configured board rules and connectivity; it does not establish polarity, thermal margin, sensor airflow, assembly access, or functional intent.
- [KiCad official symbol libraries](https://gitlab.com/kicad/libraries/kicad-symbols) and [footprint libraries](https://gitlab.com/kicad/libraries/kicad-footprints): independent basis for the installed symbol/footprint resolution check.
- [Microchip ATtiny202/204/402/404/406 datasheet](https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATtiny202-402-DataSheet-DS40002318A.pdf): supply range and eight-pin signal/UPDI mapping.
- [TI TMP102 datasheet](https://www.ti.com/lit/ds/symlink/tmp102.pdf): 1.4–3.6 V supply, SOT-563 pin map, ADD0 addressing, open-drain ALERT, and sink test conditions.
- [Bosch BME280 datasheet](https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bme280-ds002.pdf): VDD/VDDIO ranges, I2C/SPI pin mapping, CSB/SDO mode selection, 100 nF supply capacitors, and pull-up guidance.
- [Bosch BME280 handling, soldering, and mounting guidance](https://www.bosch-sensortec.com/media/boschsensortec/downloads/handling_soldering_mounting_instructions/bst-bme280-hs006.pdf): reflow, vent, contamination, and placement constraints.
- [Diodes Incorporated AP2112 product page](https://www.diodes.com/part/view/AP2112/) and [datasheet](https://www.diodes.com/assets/Datasheets/AP2112.pdf): 600 mA rating, SOT-25 pinout, 1 µF capacitor requirement, and thermal data.
- [JST SH series datasheet](https://www.jst-mfg.com/product/pdf/eng/eSH.pdf): current/voltage rating and side-entry connector dimensions.
- [Lite-On LTST-C190KGKT datasheet](https://optoelectronics.liteon.com/upload/download/DS22-2000-229/LTST-C190KGKT.pdf), [Yageo RC-series datasheet](https://www.yageo.com/upload/media/product/productsearch/datasheet/rchip/PYu-RC_Group_51_RoHS_L_13.pdf), and [Murata GRM product records](https://www.murata.com/en-global/products/productdetail?partno=GRM188R71C104KA01%23): LED polarity/current, resistor power, and capacitor voltage/value checks.

## Files Found

- `/mnt/2T/pcbdraft-boardbench-private/v1-ai-pilot-r2/corpus.draft.json` — the 20-case R2 private pilot corpus audited at the confirmed raw hash.
- `/mnt/2T/pcbdraft-boardbench-private/v1-ai-pilot-r2/REVIEW-INSTRUCTIONS.md` — R2 process instructions; read only after the independent technical checks and not treated as correctness evidence.
- `/mnt/2T/pcbdraft-boardbench-private/v1-ai-pilot-r2/remediation.draft.json` — R1 remediation claims; comparison material only, not accepted without rechecking.
- `src/pcbdraft/verification/boardbench.py` — strict corpus and review artifact schemas.
- `src/pcbdraft/verification/boardbench_evaluator.py` — evaluator-v2 slot matching, topology, support, rating, exact-BOM, and manufacturing semantics.
- `src/pcbdraft/verification/boardbench_evidence.py` — review-template creation and review import path.
- `src/pcbdraft/verification/boardbench_report.py` — campaign aggregation and sealing gates.
- `src/pcbdraft/data/parts/catalog.json` — bundled part identities, pin mappings, ratings, footprints, and source locators.
- `/usr/share/kicad/symbols/` and `/usr/share/kicad/footprints/` — installed KiCad 10.0.5 libraries used for resolution and courtyard checks.

## Code Patterns

- `src/pcbdraft/verification/boardbench_evaluator.py:1199-1254` — endpoint resolution, same-net matching, and a real two-pin support bridge between distinct nets.
- `src/pcbdraft/verification/boardbench_evaluator.py:1285-1301` — required/forbidden/same/different-net predicate semantics.
- `src/pcbdraft/verification/boardbench_evaluator.py:1345-1381` — unary rating bound direction and fail-closed missing-fact handling.
- `src/pcbdraft/verification/boardbench_evaluator.py:1417-1460` — exact-BOM enforcement and trusted non-BOM exception.
- `src/pcbdraft/verification/boardbench_evaluator.py:1466-1515` — manufacturing-envelope evaluation.
- `src/pcbdraft/verification/boardbench_evaluator.py:2387-2470` — mandatory metric combination and overall pass/fail/unknown state.
- `src/pcbdraft/verification/boardbench.py:2080-2197` — review schema lacks per-rubric/assembly decisions and does not require `orderable_state=pass` for a passing outcome.
- `src/pcbdraft/verification/boardbench_evidence.py:105-159` — review templates/import bind to a run but do not materialize or validate case rubric coverage.
- `src/pcbdraft/verification/boardbench_report.py:811-874` — sealing checks review presence, not case-rubric completion.

## Related Specs

- `.trellis/tasks/08-21-end-to-end-boardbench/prd.md:38-55` — natural-language-only task input and evaluator obligations.
- `.trellis/tasks/08-21-end-to-end-boardbench/prd.md:84-110` — automatic metrics and structured engineering review requirements.
- `.trellis/tasks/08-21-end-to-end-boardbench/prd.md:186-192` — ERC/DRC and AI review must not be represented as electrical, thermal, manufacturing, or engineering approval.
- `.trellis/tasks/08-21-end-to-end-boardbench/design.md:5-21` — prompt-only model boundary and separation of automatic, engineering, and physical evidence.
- `.trellis/tasks/08-21-end-to-end-boardbench/design.md:105-130` — corpus contract, AI-pilot boundary, injective alternatives, and unknown-on-missing-fact rule.
- `.trellis/spec/backend/quality-guidelines.md:22-29` — unavailable human/physical evidence must never be fabricated.

## Caveats / Not Found

- No PCB-generating model was run. No campaign, model request, or paid call was
  started, and the corpus was not exposed through the tested Agent path.
- No candidate board, routed copper, Gerber set, assembly, fabrication, or bench
  measurement exists. The geometry result proves only a component-courtyard
  placement lower bound; it is not manufacturing or thermal approval.
- This is AI-only desk review, not independent qualified-human engineering
  approval. It cannot satisfy the sealed holdout gate, and the cohort must remain
  `ai_reviewed_pilot`.
- The R1 findings and R2 remediation record were not used as premises. They were
  inspected only after the independent corpus/code/datasheet checks to identify
  disagreements.
- No code, corpus, review template, remediation record, or Git state was modified.
