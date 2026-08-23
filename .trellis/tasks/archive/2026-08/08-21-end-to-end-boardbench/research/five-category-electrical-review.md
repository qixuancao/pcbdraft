# Research: Five-category electrical review

- Query: Lean Codex review of five generated BoardBench outputs for schematic/electrical correctness and order-blocking defects.
- Scope: mixed (generated project evidence plus official component datasheets)
- Date: 2026-08-22

## Findings

### Review convention

- A **change** is one distinct schematic, electrical-constraint, or validation-record correction required for electrical release. Placement, routing, and silkscreen work is listed separately and is not included in that count.
- Time estimates assume an experienced PCB engineer, include schematic/IR correction and electrical revalidation, and exclude PCB placement/routing, sourcing, fabrication, and bench testing.
- No verdict below is a claim of manufacturability or physical validation.

### Results

| Board | Functional verdict | Required non-layout changes | Engineer time | Cause tags | Final-response accuracy |
|---|---|---:|---:|---|---|
| ATtiny MCU | **Needs changes** | **1** | **10–20 min** | model reasoning; component knowledge; layout/router | Honest that DRC blocked release, but electrically inaccurate about the debug-header pinout |
| TMP102 sensor | **Incomplete** | **2 validation annotations; 0 functional rewires** | **5–10 min** | infrastructure timeout; layout/router | No final response because the run timed out; therefore no false completion claim |
| AP2112 power | **Needs changes** | **5** | **15–30 min** | model reasoning; component knowledge; compiler/materialization | Honest that validation failed, but inaccurate about no-connect handling and too confident in the thermal evidence |
| Dual LED driver | **Pass** | **0** | **5–10 min** | none observed | Accurate |
| UART adapter | **Needs changes** | **0 schematic changes; layout-only blocker** | **0–5 min** | layout/router | Accurate; it explicitly reported the DRC blocker and did not claim orderability |

#### 1. MCU — `mcu-01-attiny-updi-run-1`

- The ATtiny402 supply and UPDI device pins are correct: VDD is pin 1, GND is pin 8, and PA0/RESET/UPDI is pin 6. The generated native netlist connects those pins accordingly.
- The three-pin debug header is wrong relative to the task's connector contract. Generated J2 is **pin 1 = +3V3, pin 2 = UPDI, pin 3 = GND**; the reference requires **pin 1 = UPDI, pin 2 = VTREF/VCC, pin 3 = GND**. This is a real cable/interface error even though the three required signals are present.
- **Required change count: 1** — swap J2 pins 1 and 2 at the schematic/net level.
- **Order-blocking layout work, excluded from the count/time:** remove the reported GND-to-+3V3 copper short and resolve the remaining clearance, solder-mask, edge, and courtyard violations.
- **ERC/DRC blind spot:** ERC cannot infer the semantic pin order of a passive connector, so it passes the incorrect header assignment. DRC detects the physical short but not the connector contract error.
- The final response correctly said the board was not ready because DRC failed, but its statement that J2 pin 1 was VTREF did not match the generated project.

#### 2. Sensor — `sensor-01-tmp102-i2c-run-1`

- The retained native schematic is electrically coherent: TMP102 V+ and the two pull-up tops connect to 3V3; GND and ADD0 connect to ground; SDA and SCL each reach the connector through their own 4.7 kΩ pull-up; ALERT is intentionally open.
- The automatic topology failure for SDA/SCL/VCC is not a real defect: it compared resistor pin numbers literally even though the two resistor terminals are electrically interchangeable.
- ERC reports the 3V3 and GND power-input pins as not driven because the partial project lacks source/PWR_FLAG annotations. Under the benchmark's “ERC must pass” release rule, **2 validation-only changes** are needed: add one explicit source/PWR_FLAG marker to 3V3 and one to GND. No functional rewiring is required.
- **Order-blocking layout work, excluded from the count/time:** finish the incomplete C1, SDA, and SCL routing and clear the courtyard violation, then rerun DRC. The process was killed at the 900-second wall timeout before completion.
- **ERC/DRC blind spot:** no additional functional schematic defect was found. Physical sensor orientation, connector pin-one usability, and cable exit direction still require human/mechanical review.
- There was no final model response, so this is an incomplete run rather than a false claim of completion.

#### 3. Power — `power-03-ap2112-sensor-feed-run-1`

- The main regulator circuit is correct: VIN and EN are tied to VIN_5V, the input and output capacitors return to GND, and VOUT feeds the 3V3 connector pin.
- **Required change count: 5:**

  1. Remove the one-pin `SDA_NC` net/label from J2.3 and leave the pin as a real no-connect.
  2. Remove the one-pin `SCL_NC` net/label from J2.4 and leave the pin as a real no-connect.
  3. Remove the `U1_NC4` label/net from AP2112 pin 4 and retain a true no-connect marker.
  4. Correct the declared fixed-output operating range from 3.0–3.3 V to the reference-accepted 3.2–3.4 V range, or record an equivalent truthful 3.3 V tolerance contract.
  5. Replace or substantiate the unsupported 100 °C/W thermal assumption and recompute the junction estimate. The official SOT-25 no-heatsink value is 184 °C/W; at the recorded 0.23 W and 40 °C ambient, that conservative estimate is about 82 °C and remains acceptable.

- The three malformed “NC nets” are caught by ERC as dangling/isolated labels or a no-connect conflict. Connectivity DRC otherwise passes.
- **ERC/DRC blind spots:** neither checker validates the declared output-voltage contract nor the provenance/appropriateness of the thermal resistance assumption.
- The final response correctly withheld completion, but it incorrectly said the connector pins used explicit no-connect markers and overstated the support for the thermal result.

#### 4. Driver — `driver-02-dual-led-run-1`

- The native schematic has two independent branches from VIN: each uses its own 1 kΩ resistor and LED, with both LED cathodes returning to GND. The two branch junctions remain separate and LED polarity is correct.
- **Required change count: 0.** ERC and DRC both report no violations. The evaluator's “unknown” rating result reflects an inability to derive the operating point automatically, not an observed circuit fault; manual checking at the declared 4.75–5.25 V extremes confirms large resistor/LED margin.
- **ERC/DRC blind spot:** the tools alone do not prove intended LED polarity, branch independence, or operating-current suitability, but those items were manually checked here and no defect was found.
- The final completion statement matches the generated project and validation evidence.

#### 5. Adapter — `adapter-01-uart-straight-run-1`

- The native schematic is correct and straight-through: both connectors use pin 1 = GND, pin 2 = 3V3, pin 3 = TX, and pin 4 = RX; TX and RX are not crossed or shorted in the schematic.
- **Required schematic/electrical change count: 0.** The purchase blocker is entirely in PCB materialization/routing.
- **Order-blocking layout work, excluded from the count/time:** correct connector transforms/placement, delete stale copper, and reroute all four nets. Independent DRC reports a GND trace shorting J2 TX, related mask/clearance issues, overlapping connector courtyards, and a residual 0.2 mm RX segment.
- **ERC/DRC blind spot:** no additional schematic fault was found. ERC/DRC still cannot establish that the pin naming matches the intended external cable or that the connector markings are usable, but the task reference and native netlist agree.
- The final response accurately described the pin map and the routing failure, and explicitly said the board was not orderable.

### Cross-case patterns

- Three of five native schematics need no functional rewiring: sensor, driver, and adapter. The power board's regulator topology is also sound; its changes concern no-connect representation and electrical metadata. The MCU header pin order is the only clear model-generated functional schematic error.
- Routing/geometry or routing-timeout behavior blocks three of five boards: MCU, sensor, and adapter.
- ERC/DRC is necessary but insufficient: it catches copper shorts and malformed no-connects, but not passive-connector semantics, output-domain metadata, or unsupported thermal assumptions.
- The topology evaluator needs equivalence handling for symmetric two-terminal passives; literal resistor pin-number matching produced a false sensor failure.

### Highest-value product fix

Make routing **pad-transform-aware and DRC-gated**, especially for rotated footprints: derive endpoints from the materialized pad coordinates, reject cross-net copper overlap before accepting a route, and return structured offending items for correction. This directly addresses the dominant purchase blocker or timeout loop in three of these five runs.

## Files Found

Path aliases used below:

- `V3B`: `/mnt/2T/pcbdraft-boardbench-campaigns/ai-pilot-r4-luna-five-category-v3b-20260822`
- `V3C`: `/mnt/2T/pcbdraft-boardbench-campaigns/ai-pilot-r4-luna-three-category-v3c-20260822`
- `PRIVATE`: `/mnt/2T/pcbdraft-boardbench-private/v1-ai-pilot-r4`

- `$PRIVATE/corpus.draft.json` — private task contracts and scoring rules for the five boards.
- `$V3B/runs/<run-id>/workspace/projects/<project-slug>/design.pcbir.json` — MCU and sensor managed project IR.
- `$V3C/runs/<run-id>/workspace/projects/<project-slug>/design.pcbir.json` — power, driver, and adapter managed project IR.
- The corresponding `*.kicad_sch` files — native schematics independently read through KiCad-exported netlists.
- Each run's `evaluation.json`, `score.json`, `erc.json`, `drc.json`, `stdout.txt`, and trace metadata — evaluator, checker, and agent-report evidence.

## Code / Evidence Patterns

- The reference MCU header contract defines UPDI, VCC, and GND pin roles (`$PRIVATE/corpus.draft.json:92`, rules at `:133`, `:143`, and `:152`); the generated assignment differs (`$V3B/runs/mcu-01-attiny-updi-run-1/workspace/projects/mcu-01-attiny-updi-run-1-81f8a455/design.pcbir.json:1`).
- MCU DRC records a real cross-net short plus geometry failures (`$V3B/runs/mcu-01-attiny-updi-run-1/drc.json:67`, `:90`, `:113`, and `:136`), while the final response both blocks release and misstates J2 (`$V3B/runs/mcu-01-attiny-updi-run-1/stdout.txt:1`, `:15`).
- Sensor IR contains the coherent I²C topology but unfinished routed nets (`$V3B/runs/sensor-01-tmp102-i2c-run-1/workspace/projects/sensor-01-tmp102-i2c-run-1-db1baff8/design.pcbir.json:1`); ERC lacks driven-power annotations (`$V3B/runs/sensor-01-tmp102-i2c-run-1/erc.json:37`, `:52`) and DRC records incomplete connectivity (`$V3B/runs/sensor-01-tmp102-i2c-run-1/drc.json:21`).
- The power reference specifies output, NC, and open connector behavior (`$PRIVATE/corpus.draft.json:3113`, rules at `:3173`, `:3181`, `:3189`, and `:3197`); generated one-pin labels conflict with those semantics (`$V3C/runs/power-03-ap2112-sensor-feed-run-1/erc.json:37`, `:60`, `:75`, and `:90`).
- Driver topology and clean checker results are captured in the generated IR and score (`$V3C/runs/driver-02-dual-led-run-1/workspace/projects/driver-02-dual-led-run-1-1d418cf5/design.pcbir.json:1`; `$V3C/runs/driver-02-dual-led-run-1/score.json:1`).
- Adapter schematic mapping is correct in IR, while the DRC identifies the purchase-blocking short and placement defects (`$V3C/runs/adapter-01-uart-straight-run-1/workspace/projects/adapter-01-uart-straight-run-1-d8d8836c/design.pcbir.json:1`; `$V3C/runs/adapter-01-uart-straight-run-1/drc.json:90`, `:113`, `:136`, `:159`, and `:343`).

## External References

- Microchip, *ATtiny202/204/402/404/406 Data Sheet*, DS40002318A — authoritative ATtiny402 SOIC pin roles: https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATtiny202-204-402-404-406-DataSheet-DS40002318A.pdf
- Diodes Incorporated, *AP2112 600 mA CMOS LDO Regulator With Enable* — authoritative SOT-25 thermal resistance, output accuracy, current rating, and junction limit: https://www.diodes.com/assets/Datasheets/AP2112.pdf

## Related Specs

- `.trellis/spec/backend/quality-guidelines.md` — verification must match changed behavior and report remaining gaps.
- `.trellis/spec/backend/error-handling.md` — structured failures must preserve actionable diagnostics.
- `.trellis/spec/backend/flat-pcb-toolbox.md` — PCB tool outputs and validation behavior are part of the product contract.

## Caveats / Not Found

- This was a desk review of generated IR, native KiCad schematic connectivity, evaluator evidence, and official datasheets. No Gerber fabrication review, assembly review, firmware download, power-on test, or other physical validation was performed.
- The sensor project is partial because the model process hit the 900-second timeout; its layout and final validation state therefore cannot be treated as a completed attempt.
- The stated minute ranges exclude the potentially substantial PCB placement/routing repairs on MCU, sensor, and adapter.
