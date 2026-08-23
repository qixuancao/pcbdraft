# Research: Electrical/reference audit

- Query: Independently audit all 20 draft cases for electrical correctness, exact pin topology, supply/ground treatment, decoupling, pull-ups and straps, polarity, rating assertions, and functional coherence.
- Scope: mixed (private draft corpus read-only; bundled catalog/evaluator; manufacturer datasheets; official KiCad 10.0.5 libraries)
- Date: 2026-08-22

## Findings

This is an **AI review, not human engineering approval**. It does not satisfy the task's required independent human-engineer review, release approval, manufacturing approval, or bench-safety review.

### Disposition

There are 9 passes, 11 cases needing changes, and no irreparable failures. `needs-change` means the intended circuit is generally recoverable, but the case should not be frozen in its current form.

| case_id | verdict | concise finding |
| --- | --- | --- |
| `mcu-01-attiny-updi` | pass | ATtiny402 supply/return, PA0/UPDI access, target-voltage reference, and local 100 nF decoupling are coherent and use the correct SOIC-8 pins. |
| `mcu-02-attiny-led` | pass | The GPIO branch has correct LED polarity and an independent 1 kΩ limiter; worst-case current and resistor dissipation are comfortably below the catalog limits. Supply and UPDI treatment are coherent. |
| `mcu-03-attiny-i2c-host` | needs-change | The I²C pins, two independent pull-ups, connector power, and decoupling are correct, but a programmable MCU board has no UPDI access and does not explicitly say that a preprogrammed device is assumed. Add programming access or make the preprogrammed-only limitation explicit. |
| `mcu-04-attiny-uart` | pass | PA1/USART-TX and PA2/USART-RX, shared return, UPDI access, supply, and decoupling match the ATtiny402 pin functions. |
| `sensor-01-tmp102-i2c` | needs-change | The circuit topology is sound, but the selected orderable identity is not present in TI's official TMP102 package-option list; the matching documented orderable is `TMP102AIDRLR`. Correct the catalog identity and regenerate dependent hashes/artifacts. |
| `sensor-02-bme280-i2c` | needs-change | Supply pins, dual decoupling, bus pull-ups, and SDO address strap are otherwise correct, but Bosch requires CSB to be connected directly to VDDIO for I²C. A 10 kΩ CSB pull-up is not the required direct connection. |
| `sensor-03-bme280-spi` | pass | The four-wire SPI mapping, CS, SDO/MISO direction, both grounds, both supply pins, and separate 100 nF bypass capacitors match Bosch's pinout and interface-selection rules. |
| `sensor-04-dual-i2c` | needs-change | Correct the TMP102 orderable identity and replace the BME280 CSB resistor strap with a direct VDDIO connection. The machine contract also needs an exact-cardinality rule so one shared SDA/SCL pull-up pair cannot be supplemented by duplicate branch pull-ups. |
| `power-01-ap2112-basic` | needs-change | EN, NC usage, input/output capacitors, voltage range, and 200 mA electrical target are plausible. The current predicate only rejects a declared value above 200 mA; it does not prove the design supports 200 mA, and no thermal/ambient/copper envelope is asserted. |
| `power-02-ap2112-indicator` | needs-change | The regulator network and regulated-rail LED polarity are correct. The 150 mA capability is under-specified in the same direction as `power-01-ap2112-basic`, and thermal assumptions are not machine-checkable. |
| `power-03-ap2112-sensor-feed` | pass | EN, regulator pins, 1 µF input/output capacitors, power-only output, and deliberately open data pins are consistently required; the specified operating voltages are within manufacturer limits. |
| `power-04-ap2112-dual-output` | needs-change | The shared regulated domain is topologically correct, but the 250 mA capability is not proved by the upper-bound-only predicate. At the declared worst-case bounds, regulator dissipation can reach about 0.575 W; applying the datasheet's 184 °C/W SOT25 junction-to-ambient figure gives roughly 106 °C rise before ambient, so copper area, ambient, and derating must be explicit. |
| `driver-01-single-led` | pass | The series resistor, green-LED polarity, shared return, and connector polarity are correct; the 1 kΩ branch has ample current and resistor-power margin across the stated rail range. |
| `driver-02-dual-led` | pass | Both branches have distinct resistors and LEDs with correct polarity; the topology prevents the two signal inputs from being shorted together. |
| `driver-03-attiny-dual-led` | needs-change | Both GPIO branches, polarity, resistor values, supply, and decoupling are correct, but there is no programming path and no explicit preprogrammed-device assumption. Add UPDI access or state the functional limitation. |
| `driver-04-i2c-alert-led` | needs-change | The ALERT output is open-drain and the low-side LED/resistor arrangement draws only about 1 mA at 3.3 V, which is compatible with TI's ALERT sink specification. The case still depends on the undocumented TMP102 orderable identity and must be corrected. |
| `adapter-01-uart-straight` | needs-change | The straight-through signal, supply, and return mapping is electrically coherent, but the machine reference does not enforce the natural-language prohibition on active devices; unrelated active circuitry could pass. |
| `adapter-02-uart-crossover` | pass | TX/RX crossover, straight supply/return, and separation of the two signal nets are coherent for a passive 3.3 V UART adapter. |
| `adapter-03-i2c-fanout` | needs-change | The intended passive fanout and one shared pull-up pair are coherent, but the positive-only reference can accept additional pull-up pairs. Add exact component/cardinality or explicit duplicate-pull-up exclusions. |
| `adapter-04-i2c-to-spi-header` | pass | The BME280 SPI side, power-only JST side, open JST data pins, supplies, grounds, and two bypass capacitors are coherent. The case is a sensor/interface breakout, not an I²C-to-SPI protocol converter; the prompt itself makes that limitation clear. |

### Cross-case electrical conclusions

- Official pin maps support the corpus mappings for ATtiny402, TMP102, BME280, AP2112K, and the generic LED. In the installed KiCad library, the ATtiny402 inherits the ATtiny202 SOIC-8 pin block; TMP102 is SOT-563; BME280 is the clockwise-pin-numbered Bosch LGA-8; AP2112K-3.3 uses SOT-23-5; and `Device:LED` defines pin 1 as cathode and pin 2 as anode.
- ATtiny402 operates from 1.8 V to 5.5 V. Its PA0 pin is the UPDI/reset pin, and the audited PA1/PA2 serial-interface assignments are valid defaults. The MCU cases' 100 nF supply bypass is appropriate.
- TMP102 uses pins 1/2/3/4/5/6 for SCL/GND/ALERT/ADD0/V+/SDA. Its supply range is 1.4 V to 3.6 V, and ALERT is open-drain. The audited 3.3 V topology is electrically valid after the orderable-identity correction.
- BME280 requires both grounds, VDD and VDDIO, and recommends 100 nF bypassing on both supply pins. Bosch explicitly requires a direct CSB-to-VDDIO connection in I²C mode; this is stronger than merely ensuring a logic-high level through a resistor.
- AP2112K uses VIN/GND/EN/NC/VOUT on pins 1/2/3/4/5 and is specified for 2.5 V to 6.0 V recommended operation with up to 600 mA output. The bundled catalog's 3.8 V minimum is conservative but does not match the current manufacturer recommended-operating range. All four draft cases remain inside either range.
- The LED cases use at least 1 kΩ at 3.3 V or 5 V. Even the conservative zero-forward-voltage calculation at the declared 5.5 V maximum is 5.5 mA and 30.25 mW, below the catalog's 20 mA LED and 0.1 W resistor assertions. Polarity matches the official KiCad symbol.
- The AP2112 1 µF X7R capacitor choice follows the regulator datasheet. Final production review should still use effective capacitance under DC bias, not nominal capacitance alone.

### Reference-contract patterns

- Required slot matching is injective, which correctly prevents one physical part from satisfying two required slots (`src/pcbdraft/verification/boardbench_evaluator.py:1653`, `src/pcbdraft/verification/boardbench_evaluator.py:1717`). It does not globally forbid extra parts.
- Net rules only test the enumerated endpoints for presence, equality, inequality, or absence; they do not establish net exclusivity or reject unlisted endpoints (`src/pcbdraft/verification/boardbench_evaluator.py:1284`). This is material where the prose says “no active devices” or “exactly one pull-up pair.”
- Support checks verify a selected two-terminal part lies between the requested nets, but do not count all equivalent supports in the design (`src/pcbdraft/verification/boardbench_evaluator.py:1232`, `src/pcbdraft/verification/boardbench_evaluator.py:1308`).
- Operating-rating checks compare the candidate-declared power-domain interval with an upper/lower bound (`src/pcbdraft/verification/boardbench_evaluator.py:1255`, `src/pcbdraft/verification/boardbench_evaluator.py:1348`). An upper current bound therefore limits a declared load but does not demonstrate capacity for a requested maximum load.
- Forbidden-part evaluation only rejects listed catalog part IDs (`src/pcbdraft/verification/boardbench_evaluator.py:1383`). A natural-language class prohibition needs a class-wide predicate or exhaustive, version-locked set.

### Files found

- `.trellis/tasks/08-21-end-to-end-boardbench/prd.md` — benchmark objectives, acceptance criteria, and independent-review requirement.
- `.trellis/tasks/08-21-end-to-end-boardbench/design.md` — corpus/evaluator architecture and evidence boundaries.
- `.trellis/tasks/08-21-end-to-end-boardbench/implement.md` — milestone status; the corpus-review milestone is still open.
- Private draft corpus (read-only) — 20 cases audited; prompt and reference-contract text intentionally not reproduced here.
- `src/pcbdraft/data/parts/catalog.json:42` — TMP102 catalog identity that needs manufacturer-orderable correction.
- `src/pcbdraft/data/parts/catalog.json:202` — AP2112K-3.3 catalog record.
- `src/pcbdraft/verification/boardbench_evaluator.py:1155` — slot candidate construction and evaluator entry patterns.
- `/usr/share/kicad/symbols/MCU_Microchip_ATtiny.kicad_sym:7596` and `:22252` — installed KiCad 10.0.5 ATtiny202 base and ATtiny402 derived symbol.
- `/usr/share/kicad/symbols/Sensor_Temperature.kicad_sym:20572` — installed TMP102xxDRL symbol.
- `/usr/share/kicad/symbols/Sensor.kicad_sym:2666` — installed BME280 symbol.
- `/usr/share/kicad/symbols/Regulator_Linear.kicad_sym:6806` — installed AP2112K-3.3 symbol.
- `/usr/share/kicad/symbols/Device.kicad_sym:52766` — installed LED symbol and 1=K/2=A mapping.
- `/usr/share/kicad/footprints/Package_SO.pretty/SOIC-8_3.9x4.9mm_P1.27mm.kicad_mod:1`, `/usr/share/kicad/footprints/Package_TO_SOT_SMD.pretty/SOT-563.kicad_mod:1`, `/usr/share/kicad/footprints/Package_LGA.pretty/Bosch_LGA-8_2.5x2.5mm_P0.65mm_ClockwisePinNumbering.kicad_mod:1`, `/usr/share/kicad/footprints/Package_TO_SOT_SMD.pretty/SOT-23-5.kicad_mod:1`, and `/usr/share/kicad/footprints/Connector_JST.pretty/JST_SH_SM04B-SRSS-TB_1x04-1MP_P1.00mm_Horizontal.kicad_mod:1` — installed official footprint facts.

### External references

- Microchip, [ATtiny202/204/402/404/406 data sheet](https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATtiny202-204-402-404-406-DataSheet-DS40002318A.pdf) and [UPDI target connector guidance](https://onlinedocs.microchip.com/oxy/GUID-E7CBBF9B-B23F-4E8A-8B9D-C66C24729842-en-US-1/GUID-7A4ED699-B2E5-41C7-96FA-37960E78DE50.html).
- Texas Instruments, [TMP102 data sheet and package-option addendum](https://www.ti.com/lit/ds/symlink/tmp102.pdf) and [TMP102AIDRLR official part record](https://www.ti.com/product/TMP102/part-details/TMP102AIDRLR).
- Bosch Sensortec, [BME280 data sheet](https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bme280-ds002.pdf) and [BME280 product page](https://www.bosch-sensortec.com/en/products/environmental-sensors/humidity-sensors-bme280/).
- Diodes Incorporated, [AP2112 data sheet](https://www.diodes.com/assets/Datasheets/AP2112.pdf) and [AP2112 product page](https://www.diodes.com/part/view/AP2112).
- Murata, [MLCC component list](https://www.murata.com/-/media/webrenewal/tool/library/common-pdf/static-model/component-list-s-mlcc-2506.ashx?cvid=20250805040438000000&la=ko-kr) and [DC-bias FAQ](https://www.murata.com/en-eu/support/faqs/capacitor/ceramiccapacitor/char/0005).
- JST, [SH-series manufacturer data sheet](https://www.jst-mfg.com/product/pdf/eng/eSH.pdf) and [SM04B-SRSS-TB product search](https://www.jst-mfg.com/product/index.php?search_product=SM04B-SRSS-TB&type=1).
- Samtec, [TSW-102-07-G-S official product record](https://www.samtec.com/products/tsw-102-07-g-s) and [TSW series page](https://www.samtec.com/products/tsw).
- KiCad, [official symbol-library repository](https://gitlab.com/kicad/libraries/kicad-symbols) and [official footprint-library repository](https://gitlab.com/kicad/libraries/kicad-footprints).
- Lite-On, [LTST-C190KGKT manufacturer data-sheet locator](https://optoelectronics.liteon.com/upload/download/DS22-2000-229/LTST-C190KGKT.pdf).
- Yageo, [official part-number search](https://www.yageo.com/en/ProductSearch/PartNumberSearch).

### Related specs

- `.trellis/spec/backend/database-guidelines.md` — durable artifact identity, schema/version, and provenance expectations.
- `.trellis/spec/backend/quality-guidelines.md` — validation and failure-reporting expectations.
- `.trellis/spec/backend/directory-structure.md` — package and bundled-data placement.

## Caveats / Not Found

- No tested PCB model was run. No corpus, code, catalog, task spec, or private artifact was modified.
- The draft corpus has not received the required independent human electrical-engineering approval. This document is evidence for pilot triage only.
- TI's official current package-option addendum and part search did not expose `TMP102BDRLR`; absence was checked against the current manufacturer material, but a discontinued historical ordering record was not found either. Treat the bundled identity as unverified until TI or an authorized change record proves otherwise.
- The Lite-On manufacturer PDF endpoint currently rejects automated retrieval, and the Yageo catalog PDF locator currently returns no usable document. Their URLs are retained above, but the LED maximum-current and resistor power assertions were not independently re-extracted from reachable primary PDFs during this review. The circuit calculations remain far below the bundled assertions; a human freeze review should retrieve and archive the current primary documents.
- AP2112 thermal resistance is board- and layout-dependent. The computed temperature rises are screening estimates, not junction-temperature guarantees.
- Generic connector headers have no built-in polarity or keying. Silk labels, pin-1 marking, mating orientation, and assembly drawings remain human-review items even where the electrical net order passes.
