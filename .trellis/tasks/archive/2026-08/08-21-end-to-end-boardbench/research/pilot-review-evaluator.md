# Research: BoardBench pilot evaluator audit

- Query: Independently audit all 20 private draft cases for evaluator validity, physical plausibility, fair component alternatives, unambiguous endpoint matching, correct rating semantics, reasonable manufacturing/assembly limits, useful ERC/DRC-blind review rubrics, and satisfiable contracts.
- Scope: mixed (static repository inspection, sanitized private-corpus inspection, installed KiCad 10.0.5 libraries, and primary technical sources)
- Date: 2026-08-22

## Findings

### Result

The static audit result is **0 pass, 3 fail, and 17 needs-change**.

- **Fail** means the current hidden contract conflicts with product/footprint validation, so no conforming design can pass all intended gates.
- **Needs-change** means a physically plausible solution exists, but the current automatic predicates or human rubric can accept a materially wrong solution, or the exact-part policy needs an explicit fairness decision.
- No direct contract impossibility was found in the 17 needs-change cases. All reviewed numeric endpoint references resolve uniquely and agree with the catalogued device pinouts.

This audit did not run the PCB-generating model and did not generate or modify any candidate board.

### Per-case decision

| case_id | decision | concise finding |
|---|---|---|
| `mcu-01-attiny-updi` | NEEDS CHANGE | The programming signal is not required to be distinct from either rail, so an unusable rail short can satisfy the machine predicates. Add signal-to-rail inequality and an explicit close-decoupler review item. |
| `mcu-02-attiny-led` | NEEDS CHANGE | Programming, LED-drive, and resistor-junction nets can collapse in invalid combinations; a series resistor can be bypassed. Add net inequalities and operating LED/resistor stress checks. |
| `mcu-03-attiny-i2c-host` | NEEDS CHANGE | The two bus signals may be shorted together, and either can be tied to ground, while the pull-up bridge checks still pass. Require mutual bus/rail separation. |
| `mcu-04-attiny-uart` | NEEDS CHANGE | Transmit, receive, and programming signals can be shorted together or tied to a rail. Require signal-to-signal and signal-to-rail separation. |
| `sensor-01-tmp102-i2c` | FAIL | The required 0.20 mm board clearance exceeds the exact SOT-563 footprint's 0.15 mm minimum pad gap, which product validation rejects. Lower the envelope to 0.15 mm or choose a compatible package; also separate both bus signals from each other and ground. |
| `sensor-02-bme280-i2c` | NEEDS CHANGE | Bus and mode/address strap nets can collapse into electrically invalid combinations while pairwise bridge predicates pass. Add explicit bus/strap/rail inequalities; retain the vent/keepout human review. |
| `sensor-03-bme280-spi` | NEEDS CHANGE | SPI signals can collapse together or onto rails. The review rubric should also name both independent decouplers rather than relying on a generic functional judgment. |
| `sensor-04-dual-i2c` | FAIL | It contains the same unsatisfiable 0.20 mm versus 0.15 mm SOT-563 clearance conflict. Bus and address-strap separation also needs strengthening. |
| `power-01-ap2112-basic` | NEEDS CHANGE | A requested load capacity is encoded only as a maximum operating current, so an under-designed domain can pass. Require the intended capacity as a lower/exact bound, enforce the regulator NC pin as open, and retain thermal review. |
| `power-02-ap2112-indicator` | NEEDS CHANGE | Load-capacity direction is wrong, the indicator resistor can be bypassed, and the regulator NC pin is unenforced. Add capacity, net-separation, NC, and thermal checks. |
| `power-03-ap2112-sensor-feed` | NEEDS CHANGE | The regulator NC pin is not required open, and generic exact-part choices need a fairness decision. The deliberately unused connector endpoints are correctly constrained open. |
| `power-04-ap2112-dual-output` | NEEDS CHANGE | Load-capacity direction and the regulator NC condition are incomplete. The upper-load operating point also needs explicit ambient/copper-area thermal acceptance criteria. |
| `driver-01-single-led` | NEEDS CHANGE | The resistor can be topologically bypassed, and catalog rating thresholds do not prove actual LED current or resistor dissipation. Add junction inequalities and calculated stress bounds. |
| `driver-02-dual-led` | NEEDS CHANGE | One or both resistor branches can be bypassed or tied to a rail despite branch-level matching. Add per-branch rail/junction inequalities and calculated stress bounds. |
| `driver-03-attiny-dual-led` | NEEDS CHANGE | Drive and branch nets can collapse around the resistors. Add separation rules and make LED polarity an explicit review item. |
| `driver-04-i2c-alert-led` | FAIL | It contains the unsatisfiable SOT-563 clearance conflict; the I2C nets can also collapse and the alert resistor can be bypassed. |
| `adapter-01-uart-straight` | NEEDS CHANGE | Signal nets are only separated from each other, not from power/ground. The requested absence of active devices is not machine-enforced, and extra components are not rejected. |
| `adapter-02-uart-crossover` | NEEDS CHANGE | The two signals can still equal a power rail or ground. Require all four functional nets to remain distinct where intended. |
| `adapter-03-i2c-fanout` | NEEDS CHANGE | Either bus signal can equal ground, and “exactly one pull-up pair” is not machine-enforced because extra components are permitted. Add rail inequalities and component-cardinality/forbidden-extra coverage. |
| `adapter-04-i2c-to-spi-header` | NEEDS CHANGE | SPI signals can collapse together or onto rails. The unused auxiliary endpoints are correctly constrained open. |

### Cross-case evaluator validity

1. **Component slots are deterministic but overly exact.** All 107 component slots have exactly one alternative. Matching requires exact `part_id`, symbol, and allowed footprint (`src/pcbdraft/verification/boardbench_evaluator.py:1155`). Exact named semiconductors are reasonable; the natural-language cases do not uniquely imply one manufacturer/MPN for generic passives, connectors, or green LEDs. The pilot is defensible only if the bundled-catalog-only scope is explicit and independently accepted. Otherwise add vetted, attributed equivalents rather than arbitrary IDs.

2. **Endpoint matching is unambiguous in this corpus.** Resolution prefers exact pin numbers and falls back to case-insensitive names/functions only when no number matches (`src/pcbdraft/verification/boardbench_evaluator.py:1198`). Every private endpoint uses a numeric token, each resolves to one catalog pin, and checked mappings agree with the official device documents and installed KiCad libraries. No endpoint ambiguity was found.

3. **Net predicates do not establish a full intended partition.** `same_net` checks only equality and `different_net` only pairwise uniqueness within the listed endpoints (`src/pcbdraft/verification/boardbench_evaluator.py:1224`, `src/pcbdraft/verification/boardbench_evaluator.py:1284`). Most cases omit inequalities between signal groups and rails. Consequently, ERC/DRC-clean but functionally shorted interfaces can auto-pass. Add explicit `different_net`/`forbidden_connection` coverage for every intended independent net.

4. **Support predicates prove only a two-terminal bridge.** Decoupling, pull-up, protection, reset, and boot support checks verify that an exact two-pin part spans two different nets; they do not verify placement, parasitics, uniqueness, or that the path is not bypassed elsewhere (`src/pcbdraft/verification/boardbench_evaluator.py:1232`, `src/pcbdraft/verification/boardbench_evaluator.py:1303`). Pair support such as power/debug/interface reduces to same-net endpoint pairs. Placement and cardinality therefore remain human-review obligations unless the contract is extended.

5. **Operating-current bounds use declared metadata, not demonstrated capacity.** Operating voltage comes from a `PowerDomain` interval, while current is the single declared `max_current_a` value and power is `max_v * max_current_a` (`src/pcbdraft/verification/boardbench_evaluator.py:1255`; semantic field definition at `src/pcbdraft/domain/ir.py:559`). A maximum-only current bound accepts a smaller declared capacity, the reverse of “design for up to N A.” Cases that specify load capacity need a minimum/exact capacity predicate plus regulator, copper, connector, and thermal capability evidence.

6. **Part-rating predicates do not calculate stress.** A part-rating bound merely checks an attributed catalog fact against a threshold (`src/pcbdraft/verification/boardbench_evaluator.py:1326`). It does not compare that rating with actual branch voltage/current/power. LED and resistor cases therefore need operating calculations or explicit human calculations in addition to catalog thresholds.

7. **Three contracts are impossible under the product's own validation.** The exact TMP102 catalog part records a 0.15 mm minimum pad gap (`src/pcbdraft/data/parts/catalog.json:40`), while the three affected cases demand at least 0.20 mm board clearance. Product graph validation emits an error whenever board clearance exceeds a part's minimum pad gap (`src/pcbdraft/domain/parts.py:679`). The repository's regression corpus explicitly treats 0.20 mm on this footprint as `part.footprint_clearance_incompatible` (`src/pcbdraft/data/benchmark/error_corpus.json:70`). The official KiCad SOT-563 geometry is consistent with the 0.15 mm gap. This must be corrected before any pilot run.

8. **The general fabrication envelope is otherwise reasonable.** A 0.20 mm track/space target and 0.30 mm finished drill are conservative relative to a mainstream low-cost manufacturer's published capabilities. The board-size envelopes are not intrinsically restrictive for the specified circuits. The automatic manufacturing evaluator checks declared/routed minima and board dimensions only (`src/pcbdraft/verification/boardbench_evaluator.py:1416`); assembly strings are schema-validated prose rather than executable checks (`src/pcbdraft/verification/boardbench.py:807`). Fine-pitch reflow, sensor vent clearance, connector access, courtyard overlap, polarity, and thermal layout remain review items.

9. **The AP2112 operating points are electrically plausible but need thermal qualification.** Manufacturer limits support the stated low-voltage rails and 1 uF ceramic input/output capacitors. At the largest reviewed 5 V to 3.3 V, 0.25 A point, inferred dissipation is about 0.425 W; applying the datasheet's SOT25 thermal resistance of 184 °C/W gives an approximately 78 °C junction rise under the datasheet test conditions. That is not an unconditional failure, but it requires explicit ambient, copper-area, and junction-temperature acceptance rather than a generic “thermal reasonable” rubric.

### ERC/DRC-blind review coverage

The human rubric is necessary because KiCad ERC/DRC validates electrical-rule compatibility, connectivity, geometry, and constraints—not application intent. Each review should explicitly check net independence, series-component non-bypass, polarity/orientation, physical decoupler proximity, connector usability, and thermal/assembly intent.

Specific rubric gaps found:

- `mcu-01-attiny-updi`: name decoupler proximity.
- `sensor-03-bme280-spi`: name both separate decouplers.
- `power-02-ap2112-indicator`, `power-03-ap2112-sensor-feed`, and `power-04-ap2112-dual-output`: name the regulator NC-open condition; the loaded cases should name thermal evidence.
- `driver-03-attiny-dual-led`: name LED polarity.
- `adapter-01-uart-straight`: name the absence of active devices and unexpected extras.
- Across all interface/driver cases: name every intended distinct net and every required unbypassed series element. Vague “functional/correct” wording should not be the sole defense against an ERC/DRC-clean topology error.

### Files found

- `.trellis/tasks/08-21-end-to-end-boardbench/prd.md` — BoardBench outcomes, evidence boundaries, and holdout requirements.
- `.trellis/tasks/08-21-end-to-end-boardbench/design.md` — artifact architecture and evaluator design decisions.
- `.trellis/tasks/08-21-end-to-end-boardbench/implement.md` — milestone plan; independent per-case review is still open at lines 164–177.
- `.trellis/tasks/08-21-end-to-end-boardbench/verification.md` — software verification and explicit AI-pilot/non-human boundary at lines 109–144.
- `src/pcbdraft/verification/boardbench.py` — closed corpus/predicate schemas.
- `src/pcbdraft/verification/boardbench_evaluator.py` — slot assignment, topology/support/rating/manufacturing semantics.
- `src/pcbdraft/domain/ir.py` — semantic power-domain fields.
- `src/pcbdraft/domain/parts.py` — product graph and footprint-clearance compatibility validation.
- `src/pcbdraft/data/parts/catalog.json` — attributed part pin, rating, footprint, and manufacturing facts.
- `src/pcbdraft/data/benchmark/error_corpus.json` — regression evidence that the SOT-563 clearance combination is invalid.
- `/mnt/2T/pcbdraft-boardbench-private/v1-draft/corpus.draft.json` — private draft audited at SHA-256 `b6eb79f3e3cab5d9f935531004854369f6cbc278e09f0749b6d62a12706c8067`; prompt and reference-contract contents are intentionally not reproduced here.
- Installed KiCad 10.0.5 symbol/footprint libraries — independent local pin and land-pattern cross-check.

### External references

- [Microchip ATtiny402 product documentation](https://www.microchip.com/en-us/product/attiny402) and [official pinout](https://onlinedocs.microchip.com/oxy/GUID-5A56DB3A-31E1-4F46-984F-39186535C84E-en-US-7/GUID-3B4CD538-EDA0-4831-BCB6-1CED0C439DB2.html) — supply range and pin functions.
- [Texas Instruments TMP102 datasheet](https://www.ti.com/lit/ds/symlink/tmp102.pdf) — supply range, DRL package, I2C/address/alert pin functions.
- [Bosch BME280 datasheet](https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bme280-ds002.pdf) — VDD/VDDIO limits, I2C/SPI modes, pin functions, and sensor handling context.
- [Diodes Incorporated AP2112 datasheet](https://www.diodes.com/datasheet/download/AP2112.pdf) — input/output limits, fixed-output accuracy, NC pin, capacitor recommendations, current rating, and SOT25 thermal data.
- [KiCad 10 schematic/ERC documentation](https://docs.kicad.org/10.0/en/eeschema/eeschema.html) and [PCB/DRC documentation](https://docs.kicad.org/10.0/en/pcbnew/pcbnew.pdf) — scope of native rule checking.
- [Official KiCad symbol library](https://gitlab.com/kicad/libraries/kicad-symbols), [footprint library](https://gitlab.com/kicad/libraries/kicad-footprints), and [SOT-563 footprint](https://gitlab.com/kicad/libraries/kicad-footprints/-/blob/master/Package_TO_SOT_SMD.pretty/SOT-563.kicad_mod) — library provenance and pad geometry.
- [JLCPCB published capabilities](https://jlcpcb.com/capabilities/Capabilities) — fabrication capability comparison for trace/space and drilled vias.
- [JST SH series specification](https://www.jst-mfg.com/product/pdf/eng/eSH.pdf) and [Samtec TSW series documentation](https://www.samtec.com/products/tsw-104-07-g-s) — connector family/footprint plausibility.

### Related specs

- `.trellis/spec/backend/flat-pcb-toolbox.md` — semantic-edit and native-evidence boundaries.
- `.trellis/spec/backend/quality-guidelines.md` — validation and truthful evidence expectations.
- `.trellis/spec/backend/error-handling.md` — fail-closed handling of unsupported or unknown states.
- `.trellis/spec/backend/directory-structure.md` — verification module ownership.
- `.trellis/spec/guides/index.md` — project-level workflow and review guidance.

## Caveats / Not Found

- **AI-only/non-human caveat:** this is an independent AI desk audit for the separately labelled pilot, not a review by a qualified independent human electronics engineer. It must not populate or stand in for the human `engineer-review` record, satisfy the Milestone 6 human-review gate, seal a baseline, authorize publication, or establish manufacturing/hardware safety.
- No tested PCB model was run. No candidate schematic/layout, Gerbers, ERC/DRC output from a candidate, BOM procurement evidence, assembly result, or bench measurement was available; physical conclusions are contract-level plausibility findings only.
- Exact component availability, pricing, lifecycle, counterfeit risk, high-ambient derating, assembly yield, and supplier-specific design-rule exceptions were not requalified. Manufacturer and KiCad sources establish technical plausibility, not procurement readiness.
- The private draft is still unfrozen. Any byte change invalidates this source-bound audit unless the new corpus is re-reviewed.
