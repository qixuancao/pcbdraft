# Research: Pilot R2 electrical and reference audit

- Query: Independently audit all 20 pilot-R2 cases for electrical, part-reference, and evaluator-contract correctness without running the PCB-generating model.
- Scope: mixed (private corpus, local PCBDraft/KiCad facts, and primary manufacturer documentation)
- Date: 2026-08-22

## Findings

### Corpus identity and disposition

- Audited file: `/mnt/2T/pcbdraft-boardbench-private/v1-ai-pilot-r2/corpus.draft.json`.
- Raw-file SHA-256 independently recomputed before this report: `4f774ea99a18755c65601f61dd270d9a1a8833b52d801d68ca8013aef149c1b6`. It exactly matches the expected hash.
- The corpus schema validator accepts the file, and it contains 20 cases with four cases in each of the five categories.
- Verdict totals: **15 PASS, 2 NEEDS-CHANGE, 3 FAIL**.
- Verdict meaning: PASS means no blocking defect was found in the case reference; NEEDS-CHANGE means a correct design can pass but the reference can also admit a materially wrong design; FAIL means the current reference cannot produce complete passing evidence for an otherwise correct design.

### Twenty-case audit table

Source abbreviations point to the primary links under “External references.”

| `case_id` | Verdict | Exact blocking finding or verified result | Sources |
|---|---|---|---|
| `mcu-01-attiny-updi` | PASS | No blocker. SOIC-8 VDD/GND and PA0/UPDI pin mapping, target VTREF/return exposure, 3.0–3.6 V operation, and the dedicated 100 nF bypass relationship are coherent. | M, K, C, J |
| `mcu-02-attiny-led` | PASS | No blocker. PA6 is physical pin 2 and drives an unbypassed 1 kΩ-to-LED branch with KiCad polarity 2=A/1=K; UPDI remains on PA0. Even the conservative zero-forward-voltage bound at 5.5 V is only 5.5 mA and 30.3 mW, below the 20 mA design ceiling and 0.1 W resistor rating. | M, L, Y, K |
| `mcu-03-attiny-i2c-host` | PASS | No blocker. PA1/pin 4 is SDA and PA2/pin 5 is SCL; the lines have distinct 4.7 kΩ pull-ups, the JST order is coherent, and PA0/UPDI plus VTREF/GND remains accessible. | M, K, J, Y |
| `mcu-04-attiny-uart` | PASS | No blocker. PA1/pin 4 TxD and PA2/pin 5 RxD are valid alternate USART0 locations and are not swapped at the connector. Firmware must select the alternate PORTMUX route, which is a non-blocking firmware prerequisite rather than a PCB error. | M, K, J |
| `sensor-01-tmp102-i2c` | NEEDS-CHANGE | TMP102 pin 3 ALERT is an optional open-drain output but is absent from both topology predicates and explicit review coverage. The current reference can accept ALERT tied directly to V+, GND, SDA, or SCL. Require `sensor.3` open with a `forbidden_endpoint` rule and review item, or deliberately expose it with a valid pulled-up interface. All specified supply, ADD0=GND (address 0x48), SDA/SCL, pull-up, and bypass mappings otherwise pass. | T, K, J, Y |
| `sensor-02-bme280-i2c` | PASS | No blocker. Both grounds and both supplies are correct, CSB is directly high for I2C mode, SDO is pulled low for address 0x76, SDA/SCL are distinct, and separate VDD/VDDIO bypass capacitors are required. | B, K, J, C, Y |
| `sensor-03-bme280-spi` | PASS | No blocker. MOSI/SDI, MISO/SDO, SCK, and CSB map to BME280 pins 3/5/4/2 respectively; rails, both grounds, six-pin header order, and separate supply bypasses are coherent. | B, K, J, C |
| `sensor-04-dual-i2c` | NEEDS-CHANGE | TMP102 pin 3 ALERT (`temperature.3`) has the same unconstrained-output gap as `sensor-01-tmp102-i2c`; add an explicit open-endpoint rule/review check or a valid exposed alert interface. The bus itself passes: TMP102 ADD0=GND gives 0x48, while BME280 CSB is high and SDO is pulled high for 0x77, so there is no address conflict. | T, B, K, J, C, Y |
| `power-01-ap2112-basic` | FAIL | The part-rating predicate requests `max_current_a`, but the exact AP2112 catalog part publishes `max_output_current_a`. The evaluator performs a direct key lookup, so `regulator-current-capacity` is always `unknown` for a correct exact-part design. Change the fact key to `max_output_current_a` and add corpus-to-catalog fact-key validation. Hardware topology, 1 µF input/output capacitors, EN=VIN, NC open, 200 mA capacity direction, and the 0.46 W thermal review point otherwise pass. | A, C, K, J |
| `power-02-ap2112-indicator` | FAIL | Same nonexistent AP2112 `max_current_a` rating key, making complete ratings evidence impossible. After changing it to `max_output_current_a`, the regulated-rail LED topology, NC-open rule, two 1 µF capacitors, and 150 mA/0.345 W review point are coherent. | A, C, L, Y, K, J |
| `power-03-ap2112-sensor-feed` | PASS | No blocker. AP2112 pins, EN=VIN, NC open, 1 µF capacitors, regulated GND/3V3 export, and explicit open JST data pins are correct. Its thermal conclusion is intentionally conditional on the candidate’s declared load and ambient, so it must remain a human-review item. | A, C, K, J |
| `power-04-ap2112-dual-output` | FAIL | First blocker: the same nonexistent `max_current_a` AP2112 fact key makes ratings evidence `unknown`; use `max_output_current_a`. Second blocker: at the mandated 5.5 V to 3.2 V, 250 mA point, dissipation is 0.575 W. Applying the manufacturer’s SOT25 no-heatsink 184 °C/W value at 40 °C ambient estimates 145.8 °C junction, only 4.2 °C below the +150 °C absolute maximum. “Adequate margin” has no numeric acceptance threshold. Define a maximum junction temperature or minimum margin plus a board-specific thermal model (for example, Tj <=125 °C requires effective theta-JA <=147.8 °C/W), or reduce the operating point. | A, C, K, J |
| `driver-01-single-led` | PASS | No blocker. The series junction is distinct from rail/ground, LED orientation is correct, and the exact 1 kΩ branch is safe even with Vf=0: at 5.5 V it is bounded by 5.5 mA and 30.3 mW. | L, Y, K, J |
| `driver-02-dual-led` | PASS | No blocker. The two branches have separate physical 1 kΩ resistors and distinct series junctions; both LED polarities and worst-case current/power bounds match `driver-01-single-led`. | L, Y, K, J |
| `driver-03-attiny-dual-led` | PASS | No blocker. PA6/pin 2 and PA7/pin 3 feed distinct unbypassed branches, neither GPIO is collapsed onto a rail, PA0/UPDI remains accessible, and each 3.6 V branch is bounded by 3.6 mA/13.0 mW even with Vf=0. | M, L, Y, K |
| `driver-04-i2c-alert-led` | PASS | No blocker. TMP102 ALERT is used in its valid open-drain sink direction: V+ feeds the LED anode, then the cathode and 1 kΩ series resistor reach ALERT. At 3.6 V the zero-Vf upper bound is 3.6 mA/13.0 mW; using the LED’s real forward drop puts sink current below the TMP102 3 mA VOL test point. ADD0, bus pull-ups, and decoupling are correct. | T, L, Y, K, J |
| `adapter-01-uart-straight` | PASS | No blocker. GND, 3V3, TX, and RX are straight-through and all four intended nets remain distinct; exact BOM excludes an undeclared active device. | K, J |
| `adapter-02-uart-crossover` | PASS | No blocker. Power/return are straight-through, A-TX reaches B-RX, A-RX reaches B-TX, and the two data nets remain distinct. | K, J |
| `adapter-03-i2c-fanout` | PASS | No blocker. All three connectors share GND/3V3/SDA/SCL in the same order, with exactly one independently assigned 4.7 kΩ pull-up per bus signal. Cable capacitance and bus speed remain deployment-specific review items. | K, J, Y |
| `adapter-04-i2c-to-spi-header` | PASS | No blocker. Despite the shorthand case ID, the reference unambiguously implements a complete BME280 SPI header and a separate power-only JST; both JST data pads are explicitly open. BME280 pins, rails, grounds, and two local bypass capacitors are correct. | B, K, J, C |

### Cross-case checks

- All 20 component alternatives resolve to a current local catalog identity with the same symbol and an allowed footprint; no alternative mismatch was found.
- Every case has one exact-BOM condition covering all declared slots. The evaluator uses a one-to-one slot assignment and rejects unmatched trusted BOM parts (`src/pcbdraft/verification/boardbench_evaluator.py:1417`, `:1779`).
- Every case has an intended-net-partition rule with one representative for each declared `same_net` equivalence class. The evaluator enforces `same_net`, `different_net`, and `forbidden_endpoint` exactly as encoded (`src/pcbdraft/verification/boardbench_evaluator.py:1285`).
- Three and only three part-rating predicates refer to a fact absent from their exact allowed part: `power-01-ap2112-basic`, `power-02-ap2112-indicator`, and `power-04-ap2112-dual-output`. The direct lookup that produces `part_rating_fact_unavailable` is at `src/pcbdraft/verification/boardbench_evaluator.py:1353`; the wrong corpus keys occur at raw corpus lines 2470, 2802, and 3374, while the actual catalog key is at `src/pcbdraft/data/parts/catalog.json:214`.
- Installed KiCad 10.0.5 pin facts match the manufacturer pinouts: ATtiny402 SOIC-8 is VDD/PA6/PA7/PA1/PA2/PA0-UPDI/PA3/GND; TMP102 DRL is SCL/GND/ALERT/ADD0/V+/SDA; BME280 is GND/CSB/SDI/SCK/SDO/VDDIO/GND/VDD; AP2112 SOT25 is VIN/GND/EN/NC/VOUT; `Device:LED` is pin 1 cathode and pin 2 anode.
- The official SOT-563 footprint has 0.15 mm edge-to-edge clearance between adjacent 0.35 mm pads on 0.50 mm pitch. The three SOT-563 cases now request 0.15 mm, so the earlier impossible 0.20 mm constraint is remediated.
- At the maximum 3.6 V bus rail, each 4.7 kΩ I2C pull-up draws at most about 0.77 mA when low, comfortably below the TMP102’s published 3 mA low-level test condition.
- AP2112 screening estimates using the published 184 °C/W no-heatsink value are: 124.6 °C junction for 0.46 W at 40 °C ambient, 103.5 °C for 0.345 W, and 145.8 °C for 0.575 W. These are screening calculations, not board-specific guarantees.

### Files found and code patterns

- `/mnt/2T/pcbdraft-boardbench-private/v1-ai-pilot-r2/corpus.draft.json:92` through `:5292` — the 20 sanitized case records; AP rating-key defects are at `:2470`, `:2802`, and `:3374`.
- `src/pcbdraft/data/parts/catalog.json:9` — ATtiny402 catalog pin/rating record.
- `src/pcbdraft/data/parts/catalog.json:40` — TMP102AIDRLR catalog record, including optional ALERT pin 3 and 0.15 mm pad-gap fact.
- `src/pcbdraft/data/parts/catalog.json:69` and `:94` — 4.7 kΩ and 1 kΩ resistor records.
- `src/pcbdraft/data/parts/catalog.json:119` and `:228` — 100 nF and 1 µF X7R capacitor records.
- `src/pcbdraft/data/parts/catalog.json:144` — LTST-C190KGKT LED pin/polarity record.
- `src/pcbdraft/data/parts/catalog.json:169` — BME280 pin and supply record.
- `src/pcbdraft/data/parts/catalog.json:200` — AP2112 pin record and `max_output_current_a` fact.
- `src/pcbdraft/verification/boardbench.py:558` and `:611` — closed net-rule and reference-requirement schemas.
- `src/pcbdraft/verification/boardbench_evaluator.py:1156` — exact part/symbol/footprint slot candidate matching.
- `src/pcbdraft/verification/boardbench_evaluator.py:1199` — endpoint resolution by catalog pin number or semantic function.
- `src/pcbdraft/verification/boardbench_evaluator.py:1233` — two-terminal support-component bridge check.
- `src/pcbdraft/verification/boardbench_evaluator.py:1285` — topology predicate semantics, including open endpoints.
- `src/pcbdraft/verification/boardbench_evaluator.py:1353` — direct attributed-rating fact lookup and unknown-state behavior.
- `src/pcbdraft/verification/boardbench_evaluator.py:1417` — exact-BOM predicate.
- `src/pcbdraft/verification/boardbench_evaluator.py:1779` — injective slot assignment.
- `/usr/share/kicad/symbols/MCU_Microchip_ATtiny.kicad_sym:7596` and `:22252` — installed ATtiny202 base and ATtiny402 derived symbol.
- `/usr/share/kicad/symbols/Sensor_Temperature.kicad_sym:20572` — installed TMP102xxDRL symbol.
- `/usr/share/kicad/symbols/Sensor.kicad_sym:2666` — installed BME280 symbol.
- `/usr/share/kicad/symbols/Regulator_Linear.kicad_sym:6806` and `:8744` — installed AP2112 derived symbol and five-pin base.
- `/usr/share/kicad/symbols/Device.kicad_sym:52766` — installed LED symbol with 1=K/2=A.
- `/usr/share/kicad/footprints/Package_TO_SOT_SMD.pretty/SOT-563.kicad_mod:204` — installed official SOT-563 pad geometry.
- `/usr/share/kicad/footprints/Package_LGA.pretty/Bosch_LGA-8_2.5x2.5mm_P0.65mm_ClockwisePinNumbering.kicad_mod:231` — installed official BME280 pad numbering.
- `/usr/share/kicad/footprints/Package_TO_SOT_SMD.pretty/SOT-23-5.kicad_mod:285` — installed official AP2112 package pad numbering.
- `/usr/share/kicad/footprints/LED_SMD.pretty/LED_0603_1608Metric.kicad_mod:132` — installed LED pad numbering/polarity geometry.

### External references

- **M — Microchip:** [ATtiny202/204/402/404/406 data sheet](https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATtiny202-204-402-404-406-DataSheet-DS40002318A.pdf) and [official UPDI target pinout guidance](https://onlinedocs.microchip.com/oxy/GUID-E7CBBF9B-B23F-4E8A-8B9D-C66C24729842-en-US-1/GUID-7A4ED699-B2E5-41C7-96FA-37960E78DE50.html).
- **T — Texas Instruments:** [TMP102 data sheet](https://www.ti.com/lit/ds/symlink/tmp102.pdf) and [TMP102AIDRLR official part record](https://www.ti.com/product/TMP102/part-details/TMP102AIDRLR).
- **B — Bosch Sensortec:** [BME280 data sheet](https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bme280-ds002.pdf) and [BME280 product page](https://www.bosch-sensortec.com/en/products/environmental-sensors/humidity-sensors-bme280/).
- **A — Diodes Incorporated:** [AP2112 data sheet](https://www.diodes.com/assets/Datasheets/AP2112.pdf), [AP2112 product page](https://www.diodes.com/part/view/AP2112), and [manufacturer thermal-resistance application note](https://www.diodes.com/assets/Uploads/Understanding-Thermal-Resistance-in-the-Real-World-Application-Note.pdf?v=5).
- **L — Lite-On:** [LTST-C190KGKT data sheet](https://optoelectronics.liteon.com/upload/download/DS22-2000-074/LTST-C190KGKT.PDF).
- **Y — YAGEO:** [official part-number search](https://www.yageo.com/en/ProductSearch/PartNumberSearch).
- **C — Murata:** [100 nF part record](https://www.murata.com/en-global/products/productdetail?partno=GRM188R71C104KA01%23), [1 µF part record](https://www.murata.com/en-global/products/productdetail?partno=GRM188R71A105KA61D), and [manufacturer DC-bias guidance](https://www.murata.com/en-eu/support/faqs/capacitor/ceramiccapacitor/char/0005).
- **J — connector manufacturers:** [JST SH-series data sheet](https://www.jst-mfg.com/product/pdf/eng/eSH.pdf) and [Samtec TSW series](https://www.samtec.com/products/tsw).
- **K — KiCad:** [official symbol-library repository](https://gitlab.com/kicad/libraries/kicad-symbols), [official footprint-library repository](https://gitlab.com/kicad/libraries/kicad-footprints), [schematic/ERC documentation](https://docs.kicad.org/10.0/en/eeschema/eeschema.html), and [PCB/DRC documentation](https://docs.kicad.org/10.0/en/pcbnew/pcbnew.pdf).

### Related specs

- `.trellis/workflow.md` — task workflow and evidence discipline.
- `.trellis/tasks/08-21-end-to-end-boardbench/design.md:120` — AI-reviewed pilot is explicitly separate from the independent-human sealed baseline.
- `.trellis/tasks/08-21-end-to-end-boardbench/implement.md:190` — two AI technical reviews are pilot evidence, not human engineering approval.
- `.trellis/spec/backend/flat-pcb-toolbox.md` — model-facing PCB operations must remain concrete, evidence-bound actions.
- `.trellis/spec/backend/quality-guidelines.md` — fabricated human, physical, sourcing, or test evidence is forbidden.

## Caveats / Not Found

- This is an **AI-only technical review, not an independent human electrical-engineering review or approval**. It cannot satisfy the human-reviewed sealed-baseline gate.
- The PCB-generating model was not run. No candidate project, native ERC/DRC result, placement, routing, silkscreen, connector access, assembly quality, or physical hardware was inspected. PASS applies only to the case/reference definition at the confirmed raw hash.
- AP2112 theta-JA is strongly board-, copper-, and airflow-dependent. The calculations above intentionally use the manufacturer’s published SOT25 no-heatsink figure as a conservative screening reference; a candidate still needs a documented board-specific thermal assumption.
- The Lite-On PDF is indexed at the official manufacturer URL, but direct command-line retrieval failed local TLS-chain verification. The indexed primary document reports 30 mA DC absolute maximum; the corpus uses a more conservative 20 mA design ceiling. The zero-Vf calculations keep every audited LED branch below either value.
- The YAGEO catalog PDF locator bundled in the local part record currently returns HTTP 404; the manufacturer part-search URL remains available. Resistor-stress conclusions are not close calls: the worst zero-Vf 1 kΩ case is 30.3 mW versus the catalogued 0.1 W rating.
- Optional unused MCU GPIOs are outside the declared reference topology. Their candidate-specific treatment must still be checked by a human; unlike the two TMP102 ALERT findings, they are programmable general-purpose pins and no case requires them to be electrically open.
