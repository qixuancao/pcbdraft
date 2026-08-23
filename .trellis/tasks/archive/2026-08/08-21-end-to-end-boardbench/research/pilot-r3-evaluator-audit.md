# Research: BoardBench R3 evaluator and evidence audit

- Query: Independently audit all 20 R3 AI-pilot cases for prompt/contract/scorer/review alignment, exact-BOM and net-topology bypasses, rating direction, manufacturing and assembly decidability, and the source binding/completion semantics of formal review artifact v2 versus the private R3 AI review draft.
- Scope: mixed (private corpus, production verifier/evidence code, focused read-only probes, and primary component references)
- Date: 2026-08-22
- Reviewer: Codex `gpt-5.6-sol`, reasoning effort `max`; independent evaluator/evidence reviewer B, not an R3 author.
- Corpus: `/mnt/2T/pcbdraft-boardbench-private/v1-ai-pilot-r3/corpus.draft.json`
- Start raw SHA-256: `1bffb09b6cbb24f40526a1ed919ab1f401bdb76320ec1a323bbe505cb91bfe3d`
- End raw SHA-256: `1bffb09b6cbb24f40526a1ed919ab1f401bdb76320ec1a323bbe505cb91bfe3d`
- Method: field-by-field inspection of every case's `prompt`, component slots, net rules, support and forbidden requirements, rating bounds, manufacturing constraints, review rubric, and assembly constraints; production-code tracing; bundled-part fact comparison; and small in-memory negative probes. No production or private-corpus bytes were changed.
- AI-only caveat: this is a static AI desk audit, not independent human-electronics approval, fabrication approval, a safety assessment, or physical evidence. No candidate board, campaign, freeze, or model call was created.

## Findings

### Fixed decision rule and gate result

`PASS` requires the prompt, reference, automatic scorer, manual-review obligations, and evidence chain all to agree without a material false-positive or false-negative route. `NEEDS_CHANGE` means the intended circuit remains satisfiable but a disclosure, scoring, or evidence defect can misclassify it. `FAIL` is reserved for an unsatisfiable reference or a wrong fact/direction that prevents a correct design from obtaining complete passing evidence.

Result: **0 PASS / 20 NEEDS_CHANGE / 0 FAIL**. The R3 electrical references are materially improved and I found no case-local fatal pin, rating-key, rating-direction, net-partition, or exact-BOM cardinality error. Nevertheless, every case inherits prompt/scoring gaps and formal-review evidence bypasses, so none meets the fixed PASS threshold. I do **not** recommend entering a real-model smoke until the blocking items below are corrected and the resulting new corpus/code hashes are independently re-reviewed.

### Corpus and automatic-evaluator findings

#### C1 — Scored assembly and exact-identity requirements are not fully disclosed to the tested model (blocking; all 20)

The common prompt suffix discloses exact populated-BOM cardinality, numeric routing/manufacturing minima, and square X/Y board limits, but it does not disclose the case's complete assembly checklist (`author_corpus.py:251-258` versus `:283-288`). Those assembly items are materialized as mandatory formal-review checklist items (`src/pcbdraft/verification/boardbench.py:2150-2174`). Examples across the corpus include exact catalogued footprints, pin-one silkscreen, manual connector access, reflow-only assembly, unobstructed sensor vents, contamination keep-outs, and cable-exit clearance. A board can satisfy its prompt and automatic metrics yet fail one of these undisclosed review requirements.

The same issue exists at the part-identity boundary. Slot matching requires an exact part id, symbol, and footprint (`src/pcbdraft/verification/boardbench_evaluator.py:1156-1174`), and every R3 slot has only one alternative, while several prompts specify only electrical value/function for passives, LEDs, and connectors. The private review instructions openly document this as a version-locked bundled-catalog limitation (`REVIEW-INSTRUCTIONS.md:48-55`), but that reviewer-only document is not part of the held-out prompt. For a deliberately catalog-only benchmark this can be acceptable only if the tested-model contract explicitly states the restriction and gives a discoverable catalog route; otherwise an electrically equivalent prompt-compliant part is a false negative.

Required change: put every outcome-affecting physical/assembly requirement in the case prompt, or mark it advisory rather than pass/fail. Also disclose the bundled-catalog-only identity rule in the tested-model task/environment contract (or accept equivalent attributed alternatives).

#### C2 — The prompt-wide ban on test points is not completely scored (blocking; all 20)

Every prompt forbids test points (`author_corpus.py:251-254`), but the exact-BOM predicate iterates only `design.components` and ignores non-component copper/via/test-pad semantics (`src/pcbdraft/verification/boardbench_evaluator.py:1417-1460`). Seven cases explicitly ask the reviewer to ensure named unused active pins have no copper/test-point attachment; that does not cover a test feature attached elsewhere. The remaining 13 cases have no explicit full-board no-testpoint checklist item. Thus a via or copper feature used as an undeclared test point can evade the automatic exact-BOM metric, and its prompt requirement has no stable case-authored disposition.

Required change: add a full-board no-testpoint review item to every case and, if the managed IR/native inspection can represent it reliably, add a native-board predicate. If only populated test-point footprints are intended, narrow the prompt wording accordingly.

#### C3 — Core topology, exact-BOM cardinality, ratings, and numeric manufacturing checks are otherwise coherent

Positive evidence from all 20 cases:

- Each `forbidden_unmatched_bom_component` subject set equals the complete component-slot set. The scorer rejects unmatched trusted BOM components, returns `unknown` for unavailable/untrusted BOM classification, and permits only attributed trusted `bom=false` entries (`src/pcbdraft/verification/boardbench_evaluator.py:1417-1460`). Missing evidence therefore does not become a pass. Focused tests cover extra active parts, trusted non-BOM virtuals, provisional non-BOM entries, duplicate pull-ups, and missing-slot evidence (`tests/verification/test_boardbench_evaluator.py:1083-1276`).
- Matching is injective and dependent topology predicates are evaluated on the selected assignment (`src/pcbdraft/verification/boardbench_evaluator.py:1525-1967`). Two-terminal support parts must physically bridge the two distinct target nets (`:1233-1254`), while the generated `intended-net-partition` demands one representative from each declared functional net remain mutually distinct (`author_corpus.py:304-360`). I found no parallel-short, collapsed-rail/signal, shared-series-resistor, or component-reuse pass route in the authored cases.
- All named unused active pins are forbidden/open, and the read-only coverage probe found no active device pin omitted from the combined net/support/forbidden contract. AP2112 uses the existing `max_output_current_a` fact and the bound direction is correct; `_inside_bound` rejects a rating interval outside the authored minimum/maximum (`src/pcbdraft/verification/boardbench_evaluator.py:1345-1381`; bundled fact at `src/pcbdraft/data/parts/catalog.json:200-223`).
- R3 exposes trace width, clearance, drill, and equal X/Y board maxima in every prompt. Automatic manufacturing evaluation uses the matching semantic values (`src/pcbdraft/verification/boardbench_evaluator.py:1466-1515`), and the evaluator requires a synchronized managed project before scoring (`:2331-2337`). The four fine-pitch TMP102-containing cases use 0.15 mm clearance; the other 16 use 0.20 mm. The stated square envelopes are physically plausible for the named footprints. Qualitative placement, access, vent, polarity, thermal-copper, and silkscreen judgments correctly belong to manual review—but formal v2 currently allows those checks to be skipped, as described below.

### Formal review artifact v2 findings

These are production review-evidence defects, not case-local electrical-reference errors.

#### R1 — Review v2 lacks portable full-case/corpus/campaign source binding (blocking)

A v2 review contains `campaign_id`, `run_id`, and `source_run_sha256`, but no `source_campaign_sha256`, `source_case_sha256`, or full-corpus hash (`src/pcbdraft/verification/boardbench.py:2177-2194,2324-2344`). A run receipt itself binds the prompt hash, not the hidden reference contract (`:1397-1418,1470-1484`). `validate_review_against_case` compares only generated checklist ids/kinds/text from `review_rubric` and `assembly_constraints`; it does not bind component slots, net rules, support/forbidden requirements, ratings, or manufacturing constraints (`:2427-2446`). Scores, by contrast, carry all three source hashes and the report verifies them (`src/pcbdraft/verification/boardbench_report.py:403-415`).

The canonical importer does protect an unchanged campaign root by checking the supplied corpus against the campaign and then checking the run hash (`src/pcbdraft/verification/boardbench_evidence.py:98-104,152-170`). That is useful, but it does not make the review artifact portable or prevent replay into a separately reconstructed campaign with the same ids/run bytes. A focused negative probe cloned a case, changed only a hidden `net_rules` requirement, retained identical prompt/rubric/assembly text, and observed the same v2 review accepted by `validate_review_against_case` for both cases.

Required change: add and validate source campaign, full case, and preferably corpus hashes in review v2 (or bump to v3); make checklist ids case-scoped/content-scoped as defense in depth.

#### R2 — Applicable review obligations can be disposed as `not_applicable` and still pass (blocking)

A completed passing review requires functional and orderable `pass`, but rejects only checklist disposition `fail`; it accepts `not_applicable` for any case-authored rubric or assembly item (`src/pcbdraft/verification/boardbench.py:2253-2265,2293-2306`). The unit test explicitly codifies a passing review with an applicable checklist item marked `not_applicable` (`tests/verification/test_boardbench.py:721-757`). This permits a reviewer artifact to skip thermal, polarity, non-bypass, exact-BOM, vent, connector, and assembly obligations while still recording a pass.

Whole-review `not_applicable` also requires a reason and all items marked N/A, but import does not verify that the terminal run actually lacks an inspectable candidate (`src/pcbdraft/verification/boardbench.py:2266-2288`; `boardbench_evidence.py:152-170`).

Required change: a passing review must require `pass` for every source-authored applicable item. N/A needs case-authored applicability metadata or a source-derived run-state rule, not reviewer prose alone; whole-review N/A should be restricted to terminal states/artifact inventories that genuinely cannot be inspected.

#### R3 — `orderable_state=pass` is an unsupported assertion (blocking for orderability claims)

Formal v2 accepts the orderable state as an enum plus a free-form reviewer string; it has no required orderability evidence field/source (`src/pcbdraft/verification/boardbench.py:2181-2217`). Selection then treats that bare value as sufficient (`src/pcbdraft/verification/boardbench_report.py:474-493`). The bundled catalog explicitly says stock is not asserted, and the audited active parts have `stock_status: not_checked` (`src/pcbdraft/data/parts/catalog.json:5-6,27-28,55-58,186-189,214-217`). An arbitrary nonempty checklist note does not establish lifecycle, availability, source, or an orderable BOM.

Required change: bind orderability to attributed catalog/procurement evidence with an as-of timestamp, or rename the field to the narrower claim actually supported (for example, `catalog_resolved_state`).

#### R4 — Final failure classification can bypass engineer confirmation (blocking for final reports)

`pass_without_schematic_change` does not require `final_failure`, even if automatic scoring failed (`src/pcbdraft/verification/boardbench.py:2307-2318`). Report aggregation then falls back to the automatic score's `failure_suggestion` (`src/pcbdraft/verification/boardbench_report.py:617-637`), and the seal gate rejects only values still labeled unclassified (`:824-856`). This conflicts with the task design requirement that every failed run have an engineer-confirmed primary stage and cause/owner (`.trellis/tasks/08-21-end-to-end-boardbench/design.md:270-273`).

Required change: evidence validation/report sealing must require a reviewer-supplied final classification whenever automatic or engineering evidence indicates failure, and must distinguish its provenance from an automatic suggestion.

### Private `ai-review.draft.json` findings (separate from formal per-run review v2)

#### D1 — Pending-template source binding is internally correct

The private packet is honestly labeled `pcbdraft-boardbench-corpus-ai-review-draft` v1, `pending_r3_ai_re_review`, with human-approval flags false and a full-corpus hash (`author_corpus.py:2056-2165`). Read-only recomputation found:

- exactly two reviewer templates and the exact 20-case set;
- per reviewer, 154 rubric items and 60 assembly items;
- every case-scoped item id, source text, and text SHA-256 exactly matches the current corpus; and
- the packet's corpus hash equals the audited raw corpus hash.

This is stronger preflight corpus binding than formal review v2's portable binding. It remains only a pending template.

#### D2 — There is no valid completed-state or import/freeze protocol for that draft (blocking before smoke)

The instructions require both AI reviewers to fill every field/item, preserve disagreements, and resolve them before an operator may freeze (`REVIEW-INSTRUCTIONS.md:18-27,57-63`). However, `validate_review_packet` accepts only the pending status and specifically requires every item disposition/evidence field to remain null (`author_corpus.py:2168-2223`). There is no completed schema, closed disposition/decision vocabulary, reviewer-independence validator, disagreement-resolution record, or operator gate consuming a completed packet. Completing the draft as instructed makes the only supplied validator reject it.

Nor is the private packet a formal v2 review: `BoardBenchReview.from_dict` accepts only the closed per-run v2 fields (`src/pcbdraft/verification/boardbench.py:2346-2424`), while the private packet is corpus-level, pre-run, two-reviewer evidence. A direct parse probe rejected it as the expected schema/field mismatch. The mismatch is not itself wrong—these artifacts serve different stages—but the undocumented transition makes a completed private draft impossible to validate and unsafe to treat as a formal review pass.

Required change: define a separate completed corpus-review artifact version and validator, bound to the full corpus hash, with closed per-item dispositions, reviewer identity/model/effort/independence, start/end times, explicit disagreement resolution, and a fail-closed pilot-freeze gate. State explicitly that it cannot substitute for the per-run formal review artifact. Do not mutate the pending template in place without a version/status transition.

### Per-case audit

All rows cover `cases[id=<case_id>].{prompt,component_slots,net_rules,support_requirements,forbidden_conditions,rating_bounds,manufacturing_constraints,review_rubric,assembly_constraints}`. “Common blockers” means C1, C2, R1-R4, and D2 above; it is not shorthand for an uninspected case.

| Case ID | Case-local finding | Fixed verdict |
|---|---|---|
| `mcu-01-attiny-updi` | Supply/return, local decoupling, VTREF/GND/UPDI, and all five unused GPIO opens are fully represented; the intended partition prevents rail/debug collapse. No wrong rating direction. Hidden catalog/assembly criteria and the prompt-wide testpoint gap remain, plus common evidence blockers. | **NEEDS_CHANGE** |
| `mcu-02-attiny-led` | The PA6–resistor–LED path uses distinct junction nets, correct LED direction, one physical two-pin resistor, and explicit unused-pin opens; a parallel short or shared slot cannot satisfy the contract. Hidden assembly/testpoint requirements and common evidence blockers remain. | **NEEDS_CHANGE** |
| `mcu-03-attiny-i2c-host` | PA1/PA2 map to SDA/SCL, the two 4.7 kΩ pull-ups are injective, UPDI is complete, and PA6/PA7/PA3 are open. No net-folding route found. Hidden assembly/testpoint requirements and common evidence blockers remain. | **NEEDS_CHANGE** |
| `mcu-04-attiny-uart` | PA1/PA2 map to TX/RX, UPDI and decoupling are present, and remaining GPIOs are explicitly open; signal/rail partition is complete. Hidden assembly/testpoint requirements and common evidence blockers remain. | **NEEDS_CHANGE** |
| `sensor-01-tmp102-i2c` | TMP102 pin mapping, ADD0-low, ALERT-open, separate pull-ups, supply range, 0.15 mm clearance, and local bypass are coherent. Reflow, pin-one, and cable-exit requirements are scored but not fully prompted; common blockers remain. | **NEEDS_CHANGE** |
| `sensor-02-bme280-i2c` | CSB is directly on VDDIO, SDO is pulled low, both supply pins are decoupled, and I²C rails/signals remain distinct. Vent/contamination/reflow/cable requirements are hidden from the prompt; common blockers remain. | **NEEDS_CHANGE** |
| `sensor-03-bme280-spi` | MOSI/MISO/SCK/CS map to the correct BME280 pins, both supplies are represented and decoupled, and the partition prevents signal collapse. Vent/reflow/pin-one obligations are hidden; common blockers remain. | **NEEDS_CHANGE** |
| `sensor-04-dual-i2c` | Both sensors share exactly one SDA/SCL pull-up pair; TMP102 ADD0/ALERT and BME280 CSB/SDO straps are correct; three named bypass capacitors remain distinct. Reflow/vent/pin-one obligations are hidden; common blockers remain. | **NEEDS_CHANGE** |
| `power-01-ap2112-basic` | VIN/GND/EN/NC/VOUT mapping, two 1 µF capacitors, `max_output_current_a`, 200–600 mA declared-load interval, and prompt/rubric thermal point align. Thermal acceptance is appropriately manual but can be skipped through v2 N/A; common blockers remain. | **NEEDS_CHANGE** |
| `power-02-ap2112-indicator` | Regulator topology and 150–600 mA load interval are coherent; the post-regulator LED branch has the correct distinct series path and stress rubric. Manual thermal/polarity checks can be bypassed and hidden assembly/testpoint/common blockers remain. | **NEEDS_CHANGE** |
| `power-03-ap2112-sensor-feed` | AP2112 pins/caps and 100–600 mA interval are correct; auxiliary SDA/SCL connector pins are explicitly forbidden/open. Thermal/orderability evidence and hidden assembly/testpoint/common blockers remain. | **NEEDS_CHANGE** |
| `power-04-ap2112-dual-output` | Both output connectors are constrained to the same regulated rail; total declared load is bounded 150–600 mA, with correct NC/EN/capacitor rules. Thermal/orderability evidence and hidden assembly/testpoint/common blockers remain. | **NEEDS_CHANGE** |
| `driver-01-single-led` | VIN must pass through the exact 1 kΩ resistor before the LED anode, the cathode returns to ground, and partition/support rules reject a bypass. Hidden catalog/assembly and full-board testpoint requirements plus common evidence blockers remain. | **NEEDS_CHANGE** |
| `driver-02-dual-led` | Two injective resistor/LED branches and two distinct series junctions prevent a shared-resistor or collapsed-branch pass. Hidden catalog/assembly and full-board testpoint requirements plus common evidence blockers remain. | **NEEDS_CHANGE** |
| `driver-03-attiny-dual-led` | PA6 and PA7 drive separate injective resistor/LED paths; MCU power/UPDI/decoupling and three unused GPIO opens are complete. Hidden assembly/testpoint requirements and common evidence blockers remain. | **NEEDS_CHANGE** |
| `driver-04-i2c-alert-led` | TMP102 ALERT drives the LED cathode through the resistor while the anode is on 3.3 V, matching open-drain sink behavior; address, I²C pull-ups, and decoupling are correct. Hidden assembly/testpoint/common evidence blockers remain. | **NEEDS_CHANGE** |
| `adapter-01-uart-straight` | Both four-pin headers are mapped straight with TX and RX kept distinct; exact two-connector BOM and board envelope are satisfiable. Catalog footprint/pin-one/manual-access criteria are hidden, and common testpoint/evidence blockers remain. | **NEEDS_CHANGE** |
| `adapter-02-uart-crossover` | TX/RX cross exactly once while ground and 3.3 V remain straight and all four nets distinct; no collapse bypass found. Catalog footprint/pin-one/manual-access criteria are hidden, and common testpoint/evidence blockers remain. | **NEEDS_CHANGE** |
| `adapter-03-i2c-fanout` | Three connectors share the intended four buses and exactly one injective pull-up pair; the partition rejects rail/signal collapse and duplicate populated pull-ups fail exact BOM. Hidden assembly/testpoint and common evidence blockers remain. | **NEEDS_CHANGE** |
| `adapter-04-i2c-to-spi-header` | BME280 SPI mapping is complete; auxiliary JST data pads are explicitly open while only power is shared; both supply bypass capacitors are distinct. Reflow/vent/pin-label requirements are hidden, and common testpoint/evidence blockers remain. | **NEEDS_CHANGE** |

### Verdict table and totals

| # | Case ID | Verdict |
|---:|---|---|
| 1 | `mcu-01-attiny-updi` | **NEEDS_CHANGE** |
| 2 | `mcu-02-attiny-led` | **NEEDS_CHANGE** |
| 3 | `mcu-03-attiny-i2c-host` | **NEEDS_CHANGE** |
| 4 | `mcu-04-attiny-uart` | **NEEDS_CHANGE** |
| 5 | `sensor-01-tmp102-i2c` | **NEEDS_CHANGE** |
| 6 | `sensor-02-bme280-i2c` | **NEEDS_CHANGE** |
| 7 | `sensor-03-bme280-spi` | **NEEDS_CHANGE** |
| 8 | `sensor-04-dual-i2c` | **NEEDS_CHANGE** |
| 9 | `power-01-ap2112-basic` | **NEEDS_CHANGE** |
| 10 | `power-02-ap2112-indicator` | **NEEDS_CHANGE** |
| 11 | `power-03-ap2112-sensor-feed` | **NEEDS_CHANGE** |
| 12 | `power-04-ap2112-dual-output` | **NEEDS_CHANGE** |
| 13 | `driver-01-single-led` | **NEEDS_CHANGE** |
| 14 | `driver-02-dual-led` | **NEEDS_CHANGE** |
| 15 | `driver-03-attiny-dual-led` | **NEEDS_CHANGE** |
| 16 | `driver-04-i2c-alert-led` | **NEEDS_CHANGE** |
| 17 | `adapter-01-uart-straight` | **NEEDS_CHANGE** |
| 18 | `adapter-02-uart-crossover` | **NEEDS_CHANGE** |
| 19 | `adapter-03-i2c-fanout` | **NEEDS_CHANGE** |
| 20 | `adapter-04-i2c-to-spi-header` | **NEEDS_CHANGE** |
|  | **Total** | **PASS 0 / NEEDS_CHANGE 20 / FAIL 0** |

**Real-model smoke recommendation: NO.** Correct the prompt/assembly and testpoint coverage, close the formal review v2 source/NA/orderability/failure-classification gaps, define a validated completed corpus-review protocol, regenerate a new hash-bound R3 candidate, and obtain fresh independent reviews first.

## Files found

- `/mnt/2T/pcbdraft-boardbench-private/v1-ai-pilot-r3/corpus.draft.json` — audited 20-case private R3 corpus; read only.
- `/mnt/2T/pcbdraft-boardbench-private/v1-ai-pilot-r3/author_corpus.py` — case construction, common prompt suffix, net partition, review obligations, draft packet generation/validation.
- `/mnt/2T/pcbdraft-boardbench-private/v1-ai-pilot-r3/ai-review.draft.json` — pending two-reviewer corpus-level template; read only.
- `/mnt/2T/pcbdraft-boardbench-private/v1-ai-pilot-r3/REVIEW-INSTRUCTIONS.md` — R3 review/freeze instructions and catalog-only fairness limitation.
- `src/pcbdraft/verification/boardbench.py` — formal artifact schemas including review v2 and case-checklist validation.
- `src/pcbdraft/verification/boardbench_evaluator.py` — component matching, topology/support/rating/exact-BOM/manufacturing evaluation, and score source hashes.
- `src/pcbdraft/verification/boardbench_evidence.py` — review-template creation and canonical import validation.
- `src/pcbdraft/verification/boardbench_report.py` — evidence cross-checks, selection, failure aggregation, and seal gate.
- `src/pcbdraft/data/parts/catalog.json` — bundled pin/rating/lifecycle/manufacturing facts used by the contracts.
- `tests/verification/test_boardbench.py` and `tests/verification/test_boardbench_evaluator.py` — focused formal-review and exact-BOM behavior tests.

## External references

- [Microchip ATtiny402 product page](https://www.microchip.com/en-us/product/attiny402) — family/package/product status reference used to sanity-check the bundled MCU record.
- [Texas Instruments TMP102AIDRLR product page](https://www.ti.com/product/TMP102/part-details/TMP102AIDRLR) and [TMP102 datasheet](https://www.ti.com/lit/ds/symlink/tmp102.pdf) — package, pin, supply-range, and active-product checks.
- [Bosch Sensortec BME280 datasheet](https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bme280-ds002.pdf) — pin mapping, CSB/SDO I²C strap, supply, and LGA/vent checks.
- [Diodes Incorporated AP2112 product page](https://www.diodes.com/part/view/AP2112) and [AP2112 datasheet](https://www.diodes.com/assets/Datasheets/AP2112.pdf) — 600 mA capability, pin mapping, capacitor, and supply checks.

## Related specs

- `.trellis/tasks/08-21-end-to-end-boardbench/design.md:270-304` — engineer-confirmed failure classification, physical evidence, and fail-closed sealing contract.
- `.trellis/tasks/08-21-end-to-end-boardbench/verification.md:109-143` — AI-reviewed pilot is separate, unsealed, non-human evidence and no campaign/model execution has occurred.
- `.trellis/tasks/08-21-end-to-end-boardbench/implement.md:112-129` — intended review/correction/reporting completeness and source-hash validation milestone.
- `.trellis/spec/backend/quality.md` — fail-closed validation and evidence-backed quality expectations.

## Caveats / Not Found

- No fatal case-local electrical inconsistency was found; specifically, no nonexistent AP2112 fact key, reversed rating bound, wrong TMP102/BME280/AP2112 pin direction, incomplete intended-net partition, shared series slot, or infeasible square envelope was found.
- No candidate project was generated, so placement, solderability, sensor airflow, thermal spreading, and physical connector access were not—and cannot be—established by this desk audit.
- Online product state can change. The catalog itself records dated lifecycle evidence and does not assert stock; an eventual orderability decision needs fresh attributed evidence.
- The mutation and schema probes were in-memory/read-only and demonstrate validator acceptance/rejection behavior, not a fabricated review pass. The corpus and private review packet remained unchanged.
