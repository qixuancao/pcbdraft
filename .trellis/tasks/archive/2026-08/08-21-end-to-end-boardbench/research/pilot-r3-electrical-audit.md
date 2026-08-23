# Research: BoardBench R3 electrical/device audit

- Query: Independently audit all 20 R3 cases for electrical correctness, exact device identity, pin/package facts, ratings, support circuitry, unused pins, net partitioning, thermal limits, and manufacturability.
- Scope: mixed (private corpus and local implementation read-only; external evidence restricted to official manufacturer/datasheet/KiCad sources)
- Date: 2026-08-22

## Review record

- Reviewer: **Codex gpt-5.6-sol/max**, independent electrical/device reviewer A.
- Corpus: `/mnt/2T/pcbdraft-boardbench-private/v1-ai-pilot-r3/corpus.draft.json`
- Required raw SHA-256: `1bffb09b6cbb24f40526a1ed919ab1f401bdb76320ec1a323bbe505cb91bfe3d`
- Start raw SHA-256: `1bffb09b6cbb24f40526a1ed919ab1f401bdb76320ec1a323bbe505cb91bfe3d`
- End raw SHA-256: `1bffb09b6cbb24f40526a1ed919ab1f401bdb76320ec1a323bbe505cb91bfe3d`
- Method: inspect every case's requirement, exact component slots, same/different/forbidden net rules, support requirements, rating bounds, forbidden conditions, review rubric, assembly rules, and manufacturing envelope; resolve each part through the local catalog; cross-check exact MPN, pinout, electrical limits, package, and application requirements against official sources; cross-check symbol/pad mappings against the installed KiCad 10.0.5 official libraries; perform worst-point LED/resistor and LDO thermal calculations. No PCBDraft/Hermes model was called, no settings were changed, and no campaign, freeze, or review-pass record was created.
- Verdict rule: a false normative part fact is sufficient for `NEEDS_CHANGE`, even if the affected case does not exercise that entire rating range. `PASS` means the reference contract is electrically credible for its stated scope; it is not a production, sourcing, assembly, or safety approval.
- AI-only caveat: this review did not fabricate or assemble a board, inspect real copper or solder joints, purchase parts, verify distributor inventory, or substitute for independent human engineering review. Lifecycle pages establish real/current identities, not guaranteed stock at a future order date.

## Executive finding

The R3 topology and constraint remediation is otherwise strong, but one exact-device fact blocks five cases from `PASS`: the normative local record for `microchip.attiny402-ssn` states a +125 °C maximum operating temperature at [catalog.json:26](/mnt/2T/pcbdraft/src/pcbdraft/data/parts/catalog.json:26), while Microchip's official ordering table assigns **ATtiny402-SSN** a range of **-40 °C to +105 °C**. The +125 °C SOIC ordering code is **ATtiny402-SSF**, which also has a different 2.7–5.5 V / 16 MHz grade. See the [official Microchip ordering table](https://onlinedocs.microchip.com/oxy/GUID-5A56DB3A-31E1-4F46-984F-39186535C84E-en-US-7/GUID-CC06B137-262F-43C8-90FD-A14C59BF9C29.html) and [official data sheet](https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATtiny202-204-402-404-406-DataSheet-DS40002318A.pdf).

This is a ratings-fact defect rather than a schematic defect. Correct `ATtiny402-SSN` to +105 °C, or intentionally change the exact ordering code and all dependent limits, then re-run corpus/catalog validation. The affected cases are `mcu-01-attiny-updi`, `mcu-02-attiny-led`, `mcu-03-attiny-i2c-host`, `mcu-04-attiny-uart`, and `driver-03-attiny-dual-led`.

Result: **15 PASS / 5 NEEDS_CHANGE / 0 FAIL**. Do **not** enter real-model smoke yet; after the shared ATtiny rating fact is corrected and the five dependent cases are revalidated without other semantic changes, a bounded real-model smoke is recommended.

## Files found

- `/mnt/2T/pcbdraft-boardbench-private/v1-ai-pilot-r3/corpus.draft.json` — the 20-case R3 private corpus reviewed read-only.
- `/mnt/2T/pcbdraft/src/pcbdraft/data/parts/catalog.json` — normative exact part identities, ratings, symbol IDs, footprints, and evidence locators.
- `/mnt/2T/pcbdraft/src/pcbdraft/verification/boardbench.py` — schema validation for net rules, support requirements, forbidden conditions, and rating bounds.
- `/mnt/2T/pcbdraft/src/pcbdraft/verification/boardbench_evaluator.py` — reference predicate evaluation and injective component matching.
- `/usr/share/kicad/symbols/MCU_Microchip_ATtiny.kicad_sym` — installed official ATtiny symbol/pin mapping.
- `/usr/share/kicad/symbols/Sensor_Temperature.kicad_sym` — installed official TMP102 DRL symbol/pin mapping.
- `/usr/share/kicad/symbols/Sensor.kicad_sym` — installed official BME280 symbol/pin mapping.
- `/usr/share/kicad/symbols/Regulator_Linear.kicad_sym` — installed official AP2112 symbol/pin mapping.
- `/usr/share/kicad/symbols/Device.kicad_sym` — installed official LED symbol, pin 1 cathode and pin 2 anode.
- `/usr/share/kicad/footprints/Package_SO.pretty/SOIC-8_3.9x4.9mm_P1.27mm.kicad_mod`, `/usr/share/kicad/footprints/Package_TO_SOT_SMD.pretty/SOT-563.kicad_mod`, `/usr/share/kicad/footprints/Package_LGA.pretty/Bosch_LGA-8_2.5x2.5mm_P0.65mm_ClockwisePinNumbering.kicad_mod`, and `/usr/share/kicad/footprints/Package_TO_SOT_SMD.pretty/SOT-23-5.kicad_mod` — installed pad mappings and geometry checked against the declared manufacturing rules.

## Code and contract patterns

- Net rules are closed to `same_net`, `different_net`, `required_endpoint`, and `forbidden_endpoint` at [boardbench.py:143](/mnt/2T/pcbdraft/src/pcbdraft/verification/boardbench.py:143); arity rules are validated at [boardbench.py:583](/mnt/2T/pcbdraft/src/pcbdraft/verification/boardbench.py:583).
- The evaluator handles `forbidden_endpoint` at [boardbench_evaluator.py:1293](/mnt/2T/pcbdraft/src/pcbdraft/verification/boardbench_evaluator.py:1293), support relationships at [boardbench_evaluator.py:1310](/mnt/2T/pcbdraft/src/pcbdraft/verification/boardbench_evaluator.py:1310), and rating facts at [boardbench_evaluator.py:1353](/mnt/2T/pcbdraft/src/pcbdraft/verification/boardbench_evaluator.py:1353).
- Component assignment is injective rather than allowing one physical component to satisfy multiple named slots; success/failure evidence is emitted around [boardbench_evaluator.py:1867](/mnt/2T/pcbdraft/src/pcbdraft/verification/boardbench_evaluator.py:1867).
- Exact populated BOM rules plus representative intended-net partitions are present in all 20 cases. Virtual `PWR_FLAG`/no-connect ERC aids remain non-BOM objects; the local catalog explicitly marks `PWR_FLAG` as virtual and `bom: false` at [catalog.json:412](/mnt/2T/pcbdraft/src/pcbdraft/data/parts/catalog.json:412).
- Project guidance requires evidence-first reporting and forbids representing unavailable physical, human, sourcing, fabrication, or test evidence as completed at [quality-guidelines.md:24](/mnt/2T/pcbdraft/.trellis/spec/backend/quality-guidelines.md:24).

## Official external references

- Microchip: [ATtiny202/204/402/404/406 data sheet](https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATtiny202-204-402-404-406-DataSheet-DS40002318A.pdf), [ATtiny402 product status](https://www.microchip.com/en-us/product/attiny402), and [exact ordering-code table](https://onlinedocs.microchip.com/oxy/GUID-5A56DB3A-31E1-4F46-984F-39186535C84E-en-US-7/GUID-CC06B137-262F-43C8-90FD-A14C59BF9C29.html).
- Texas Instruments: [TMP102 data sheet](https://www.ti.com/lit/ds/symlink/tmp102.pdf) and [TMP102AIDRLR exact part page](https://www.ti.com/product/TMP102/part-details/TMP102AIDRLR).
- Bosch Sensortec: [BME280 data sheet](https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bme280-ds002.pdf), [product page](https://www.bosch-sensortec.com/en/products/environmental-sensors/humidity-sensors-bme280), and [handling/soldering instructions](https://www.bosch-sensortec.com/media/boschsensortec/downloads/handling_soldering_mounting_instructions/bst-bme280-hs006.pdf).
- Diodes Incorporated: [AP2112 product page](https://www.diodes.com/part/view/AP2112/) and [AP2112 data sheet](https://www.diodes.com/datasheet/download/AP2112.pdf).
- Lite-On: [LTST-C190KGKT data sheet](https://optoelectronics.liteon.com/upload/download/DS22-2000-074/LTST-C190KGKT.PDF).
- Murata: [100 nF GRM188R71C104KA01 record](https://www.murata.com/en-global/products/productdetail?partno=GRM188R71C104KA01%23) and [1 µF GRM188R71A105KA61D record](https://www.murata.com/en-global/products/productdetail?partno=GRM188R71A105KA61D).
- YAGEO: [RC-series official data sheet](https://www.yageo.com/upload/media/product/productsearch/datasheet/rchip/PYu-RC_Group_51_RoHS_L_13.pdf) and [part-number search](https://www.yageo.com/en/ProductSearch/PartNumberSearch).
- JST: [SH-series data sheet](https://www.jst-mfg.com/product/pdf/eng/eSH.pdf) and [SM04B-SRSS-TB official part search](https://www.jst-mfg.com/product/index.php?search_product=SM04B-SRSS-TB&type=1).
- Samtec exact configurations: [TSW-102-07-G-S](https://www.samtec.com/products/tsw-102-07-g-s), [TSW-103-07-G-S](https://www.samtec.com/products/tsw-103-07-g-s), [TSW-104-07-G-S](https://www.samtec.com/products/tsw-104-07-g-s), and [TSW-106-07-G-S](https://www.samtec.com/products/tsw-106-07-g-s).
- KiCad: [official symbol libraries](https://gitlab.com/kicad/libraries/kicad-symbols) and [official footprint libraries](https://gitlab.com/kicad/libraries/kicad-footprints).

## Per-case findings

### `mcu-01-attiny-updi` — NEEDS_CHANGE

- Electrical topology is correct: ATtiny402 SOIC-8 VDD/GND are pins 1/8, PA0/UPDI is pin 6, VTREF and return reach the debug header, the 100 nF capacitor is required across the local rail, unused GPIO endpoints are forbidden, and the rail/return/UPDI partition is explicit.
- Exact SOIC and header footprints are practical inside the envelope, with connector polarity/pin-one marking and hand-assembly requirements.
- Blocking finding: the exact `ATtiny402-SSN` record overstates maximum operating temperature by 20 °C. Official ordering evidence is +105 °C, not +125 °C. Correct the shared part fact before this case can pass.

### `mcu-02-attiny-led` — NEEDS_CHANGE

- PA6 drives the green LED through one unbypassed 1 kΩ resistor; KiCad and Lite-On both map pin 1 to cathode and pin 2 to anode. At 5.5 V and a plausible 1.9 V low forward voltage, current is about 3.6 mA and resistor dissipation about 13 mW, safely below the case's 20 mA / 0.1 W limits. UPDI, local decoupling, and all named unused pins remain correctly constrained.
- Blocking finding: the same exact `ATtiny402-SSN` +125 °C catalog claim is false; official maximum is +105 °C.

### `mcu-03-attiny-i2c-host` — NEEDS_CHANGE

- ATtiny pins PA1/PA2 correctly implement SDA/SCL; the JST order is GND/3V3/SDA/SCL; each line has its own 4.7 kΩ pull-up. At 3.6 V each asserted-low pull-up contributes at most about 0.77 mA, a credible bus load. UPDI, decoupling, unused pins, exact BOM, and net separation are present.
- Blocking finding: the referenced exact MCU's local temperature rating is +125 °C, versus +105 °C in Microchip's `ATtiny402-SSN` ordering row.

### `mcu-04-attiny-uart` — NEEDS_CHANGE

- Microchip's multiplex table confirms PA1 as USART0 TXD and PA2 as RXD; the connector is not accidentally crossed, the target reference and return are present on UPDI, and power/decoupling/unused-pin rules are complete.
- Blocking finding: the referenced exact MCU's maximum operating-temperature fact must be corrected from +125 °C to +105 °C.

### `sensor-01-tmp102-i2c` — PASS

- TMP102 DRL pins are correctly mapped: SCL 1, GND 2, open-drain ALERT 3, ADD0 4, V+ 5, SDA 6. ADD0 is low, ALERT is explicitly copper-free, both I2C pull-ups are independent, and local bypassing is required between V+ and GND. The 3.0–3.6 V range is within the official 1.4–3.6 V range.
- The official SOT-563 footprint has a 0.15 mm nominal pad-to-pad gap, equal to the case's minimum clearance. Reflow, pin-one marking, and cable-exit constraints make the design feasible, though fabrication capability must actually support that boundary value.

### `sensor-02-bme280-i2c` — PASS

- BME280 pin mapping and I2C mode are correct: both grounds, VDD/VDDIO, SDA/SCL, direct CSB-to-VDDIO selection, and a defined low SDO address strap. VDD and VDDIO each have a separately named local 100 nF capacitor, matching Bosch's application guidance; 4.7 kΩ is Bosch's normal pull-up example.
- LGA reflow, pin-one/orientation, vent keepout, and cable clearance are explicit. The 3.0–3.6 V supply remains within both device domains.

### `sensor-03-bme280-spi` — PASS

- MOSI/SDI, MISO/SDO, SCK, and CSB all map to the correct BME280 pins and remain distinct. Both supply pins, both grounds, and two local bypass capacitors are covered.
- The exact six-pin Samtec identity is real; the board requires BME280 reflow, visible pin one, and an unobstructed vent. No unused active pin is left ambiguous.

### `sensor-04-dual-i2c` — PASS

- TMP102 and BME280 share one correctly pulled-up SDA/SCL pair. TMP102 ADD0 low and BME280 SDO high select distinct legal addresses; BME280 CSB is directly high for I2C. Both supply domains and all three named local bypass capacitors are independently constrained, and unused TMP102 ALERT is forbidden from copper.
- The 0.15 mm clearance and dual fine-pitch reflow requirement are feasible but process-sensitive; BME280 vent protection and pin-one marking are explicitly required.

### `power-01-ap2112-basic` — PASS

- Official AP2112 SOT-25/SOT-23-5 mapping is VIN 1, GND 2, EN 3, NC 4, VOUT 5. EN is tied to VIN, NC is explicitly open, and separate input/output 1 µF X7R capacitors meet the data-sheet stability guidance.
- At the required worst point, `(5.5 - 3.2) V × 0.200 A = 0.460 W`. Using the official no-heatsink `theta_JA = 184 °C/W` gives about 124.6 °C junction at 40 °C ambient; quiescent-current dissipation adds less than 0.1 °C. This only narrowly meets the 125 °C review limit and 25 °C margin to the 150 °C absolute maximum. The rubric correctly requires actual declared current and a documented board-dependent copper/thermal assumption; a candidate cannot treat the headline 600 mA current rating as thermally available. This `PASS` is therefore conditional on the candidate-specific thermal proof, not an approval of 600 mA operation.

### `power-02-ap2112-indicator` — PASS

- Regulator pinout, EN/NC handling, and both stability capacitors are correct. The LED is on regulated 3.3 V, has correct polarity, and has its own 1 kΩ limiter; LED current is approximately 1.3 mA at 3.2 V and 1.9 V forward voltage, far below 20 mA.
- The required 150 mA worst-point load dissipates 0.345 W; the same conservative 184 °C/W estimate yields about 103.5 °C at 40 °C ambient before the negligible indicator/ground-current refinement. The case requires the actual declared load and thermal model, so unsafe higher declarations remain review failures.

### `power-03-ap2112-sensor-feed` — PASS

- Only return and regulated 3.3 V are connected on the JST; its two data pins are explicit forbidden endpoints. EN/NC and the two 1 µF capacitors are correct.
- The required 100 mA worst point is 0.230 W and about 82.3 °C junction at 40 °C using 184 °C/W. Current bounds use the correct `max_output_current_a` fact and require at least 100 mA without pretending the full 600 mA is thermally usable.

### `power-04-ap2112-dual-output` — PASS

- Both outputs share the regulated rail and common return, without duplicating or misplacing the single output capacitor; EN is high and NC remains open. The exact BOM and intended-net partition prevent accidentally split or raw-input outputs.
- The requirement is explicitly **total** current. At the 150 mA minimum the worst-point thermal estimate is the same 0.345 W / about 103.5 °C at 40 °C, leaving credible margin while still requiring a candidate-specific copper model.

### `driver-01-single-led` — PASS

- The exact 1 kΩ resistor is unbypassed and in series with the correctly oriented LED. At the 5.5 V maximum and 1.9 V forward-voltage low, current is about 3.6 mA and resistor power about 13 mW. Both are comfortably inside the encoded conservative limits, and connector polarity is review-visible.

### `driver-02-dual-led` — PASS

- Each LED owns one injectively assigned 1 kΩ resistor and a separate resistor/LED junction, while input and return are shared. The `different_net` rules prevent the two branch junctions from being shorted.
- Each branch has the same approximately 3.6 mA / 13 mW worst-point calculation as `driver-01-single-led`; polarity, exact BOM count, and series integrity are explicitly reviewed.

### `driver-03-attiny-dual-led` — NEEDS_CHANGE

- PA6 and PA7 drive separate 1 kΩ/LED branches; the two GPIO and two series junctions remain distinct. At 3.6 V, even a 1.9 V forward voltage yields only about 1.7 mA and 2.9 mW per resistor. UPDI, local bypassing, and named unused GPIO constraints are correct.
- Blocking finding: this case also references exact `ATtiny402-SSN`, so the shared false +125 °C rating prevents `PASS`; official maximum is +105 °C.

### `driver-04-i2c-alert-led` — PASS

- The TMP102 open-drain ALERT sinks rather than sources current: LED anode is at V+, its cathode returns through 1 kΩ to ALERT. At 3.6 V and a plausible 1.9 V LED low, sink current is about 1.7 mA before ALERT's low-state drop, below TI's characterized 3 mA `VOL` condition; resistor power is below 3 mW. SDA/SCL pull-ups, ADD0 low, bypassing, and net separation are correct.
- The rubric should continue to review the sensor sink limit as part of functional correctness; the fixed present values are safe even though the explicit numerical rating bound is on the LED/resistor rather than ALERT.

### `adapter-01-uart-straight` — PASS

- Ground, 3.3 V, TX, and RX are straight-through and mutually separated where required. Exact 4-pin Samtec headers, pin-one visibility, labels, THT drills, and board envelope are manufacturable. The passive adapter intentionally provides no level shifting or protection, so both sides must remain in the stated 3.0–3.6 V domain.

### `adapter-02-uart-crossover` — PASS

- Power and return are straight-through; A-TX crosses once to B-RX and A-RX once to B-TX. The two data nets cannot collapse under the explicit separation rules. The exact THT identities and mechanical marking constraints are credible.

### `adapter-03-i2c-fanout` — PASS

- Host and both branches share the same GND/VCC/SDA/SCL without swaps, and exactly one logical 4.7 kΩ pull-up pair serves the bus. SDA and SCL remain distinct; the JST part and horizontal cable-exit intent are explicit.
- Real deployment must still validate aggregate cable/device capacitance, rise time, bus speed, and whether downstream boards add pull-ups. Those system values are outside this small passive-topology case and are not falsely asserted by the reference answer.

### `adapter-04-i2c-to-spi-header` — PASS

- Despite the name, the electrical contract correctly places the BME280 in full SPI mode on the six-pin header: SDI/MOSI, SDO/MISO, SCK, and CSB are all distinct and correctly pinned. The auxiliary JST carries only ground and supply; both data pins are explicit forbidden endpoints.
- VDD and VDDIO each have local 100 nF bypassing. BME280 LGA reflow/vent rules, exact connector identities, pin-one marking, and manufacturing geometry are sufficient for a credible reference answer.

## Verdict table

| case_id | verdict | decisive basis |
|---|---|---|
| `mcu-01-attiny-updi` | NEEDS_CHANGE | Exact `ATtiny402-SSN` temperature rating overstated (+125 °C vs official +105 °C) |
| `mcu-02-attiny-led` | NEEDS_CHANGE | Same exact-MPN rating defect; circuit/LED limits otherwise correct |
| `mcu-03-attiny-i2c-host` | NEEDS_CHANGE | Same exact-MPN rating defect; I2C topology otherwise correct |
| `mcu-04-attiny-uart` | NEEDS_CHANGE | Same exact-MPN rating defect; USART pin mapping otherwise correct |
| `sensor-01-tmp102-i2c` | PASS | Pinout, address, pull-ups, bypass, unused ALERT, and reflow constraints credible |
| `sensor-02-bme280-i2c` | PASS | Correct I2C selection/address, dual-domain bypass, and LGA/vent handling |
| `sensor-03-bme280-spi` | PASS | Complete correct SPI pin map, dual supply/ground, and assembly constraints |
| `sensor-04-dual-i2c` | PASS | Shared bus, distinct addresses, local bypasses, unused ALERT, fine-pitch handling |
| `power-01-ap2112-basic` | PASS | Correct LDO topology; 200 mA thermal point is feasible but narrowly and explicitly gated |
| `power-02-ap2112-indicator` | PASS | Correct regulated indicator and safe 150 mA/LED/thermal envelope |
| `power-03-ap2112-sensor-feed` | PASS | Correct power-only feed, explicit unused data pins, safe 100 mA envelope |
| `power-04-ap2112-dual-output` | PASS | Correct common regulated rail and total-current/thermal constraint |
| `driver-01-single-led` | PASS | Correct series/polarity topology with large current/power margin |
| `driver-02-dual-led` | PASS | Independent branches/resistors and safe current/power |
| `driver-03-attiny-dual-led` | NEEDS_CHANGE | Exact `ATtiny402-SSN` temperature rating defect; driver topology otherwise correct |
| `driver-04-i2c-alert-led` | PASS | Correct open-drain sink topology and safe sensor/LED current |
| `adapter-01-uart-straight` | PASS | Correct passive straight-through topology and voltage scope |
| `adapter-02-uart-crossover` | PASS | Correct one-time TX/RX crossover and separated nets |
| `adapter-03-i2c-fanout` | PASS | Correct common bus and single pull-up pair; deployment capacitance remains external |
| `adapter-04-i2c-to-spi-header` | PASS | Correct BME280 SPI topology and power-only auxiliary connector |

Totals: **PASS 15 / NEEDS_CHANGE 5 / FAIL 0 / total 20**.

Real-model smoke recommendation: **NO at the current corpus/catalog state**. Correct and revalidate the shared `ATtiny402-SSN` operating-temperature fact first; then **YES**, proceed with a bounded real-model smoke while retaining human electrical/layout review as the real gate.

## Related specs

- `.trellis/spec/backend/flat-pcb-toolbox.md` — exact installed-library facts, flat operations, and evidence provenance requirements.
- `.trellis/spec/backend/quality-guidelines.md` — evidence-first claims and prohibition on fabricated sourcing/fabrication/human-review evidence.
- `.trellis/spec/guides/cross-layer-thinking-guide.md` — cross-layer traceability from corpus contract through catalog and evaluator behavior.
- `.trellis/tasks/08-21-end-to-end-boardbench/prd.md` and `design.md` — BoardBench gate intent and separation between AI pilot evidence and real human/model gates.

## Caveats / Not Found

- No destructive or mutating action was performed on the private corpus, code, catalog, model configuration, campaign state, freeze state, or review records.
- Exact MPN identities were found on official manufacturer sources. Immediate authorized-distributor stock, price, MOQ, lead time, counterfeit risk, and future lifecycle were not established; the local catalog itself says stock is not checked.
- No candidate board exists in this audit, so physical capacitor placement, copper area, actual AP2112 thermal resistance, BME280 vent clearance, solderability, connector accessibility, DRC/ERC cleanliness, and unrouted-net count remain candidate-level checks.
- The 0.15 mm SOT-563 cases sit exactly on the declared clearance boundary; this is geometrically feasible in the official footprint but should be confirmed against the selected fabricator's process.
- `power-01-ap2112-basic` has little analytical thermal headroom at its minimum accepted 200 mA under the data-sheet no-heatsink `theta_JA`; real copper/layout evidence must not be inferred from this corpus-only review.
