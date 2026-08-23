# Research: BoardBench R4 lean electrical review

- Query: Review all 20 R4 cases only for issues that could change real BoardBench results or hardware correctness: exact parts/pins/packages, topology, ratings/thermal limits, manufacturability, and prompt implementability.
- Scope: mixed (private R4 corpus read-only; production catalog/evaluator/tool contracts; installed official KiCad 10.0.5 libraries; official manufacturer sources)
- Date: 2026-08-22

## Findings

### Outcome

**20 PASS / 0 NEEDS_CHANGE.** No wrong device, pin, package, polarity, required support circuit, or impossible board envelope was found. The prompts expose enough electrical and physical information for the existing flat PCB tools to implement the cases: power-domain voltage/current is writable, as are board rules, rectangular outline, explicit footprint poses, and per-net routing (`src/pcbdraft/agent/tooling.py:1061`, `:1372`, `:1384`, `:1409`, `:1429`).

The production evaluator resolves exact catalog part/symbol/footprint candidates, checks net equality/separation/open endpoints, two-terminal support placement, power-domain/rating intervals, and manufacturing limits (`src/pcbdraft/verification/boardbench_evaluator.py:1156`, `:1285`, `:1310`, `:1353`, `:1466`). These machine checks align with the electrically decisive parts of the reviewed cases; thermal, polarity marking, local placement quality, connector access, and sensor-vent handling still require the planned engineering review of generated boards.

### Per-case disposition

| case_id | verdict | electrically decisive finding |
| --- | --- | --- |
| `mcu-01-attiny-updi` | PASS | ATtiny402 SOIC-8 VCC/GND and PA0/UPDI mapping, VTREF/return header, local 100 nF bypass, and five open GPIO rules are coherent. |
| `mcu-02-attiny-led` | PASS | PA6 drives one correctly oriented LED through 1 kΩ; at 5.5 V and a conservative 1.9 V LED drop, current is about 3.6 mA and resistor loss about 13 mW. UPDI and bypassing remain complete. |
| `mcu-03-attiny-i2c-host` | PASS | PA1/PA2 are valid default SDA/SCL pins; the JST order, separate 4.7 kΩ pull-ups, UPDI access, bypassing, and open GPIOs are complete. |
| `mcu-04-attiny-uart` | PASS | PA1/PA2 correctly map to USART TX/RX, with the stated connector order, target reference, return, and local bypass. |
| `sensor-01-tmp102-i2c` | PASS | TMP102 DRL pins 1–6 map to SCL/GND/ALERT/ADD0/V+/SDA; ADD0-low, open ALERT, separate bus pull-ups, and V+ bypassing are correct. |
| `sensor-02-bme280-i2c` | PASS | Both supplies/grounds are present, VDD and VDDIO have separate 100 nF capacitors, CSB is directly tied to VDDIO, and SDO has a defined low strap. |
| `sensor-03-bme280-spi` | PASS | SDI/MOSI, SDO/MISO, SCK, and CSB/CS map correctly and remain distinct; both supply domains and grounds are covered. |
| `sensor-04-dual-i2c` | PASS | Both sensors share one pull-up pair; TMP102 ADD0-low and BME280 SDO-high are valid distinct addresses, CSB is directly high, three local bypass capacitors are independently required, and TMP102 ALERT is open. |
| `power-01-ap2112-basic` | PASS | EN-to-VIN, NC-open, and separate 1 µF input/output capacitors match the AP2112 application. At the required 200 mA worst point, dissipation is 0.460 W and the official no-heatsink 184 °C/W figure gives about 124.6 °C at 40 °C ambient: valid but extremely close to the 125 °C case limit. |
| `power-02-ap2112-indicator` | PASS | The LED is on regulated 3.3 V with correct polarity/limiter; the 150 mA worst point is 0.345 W and about 103.5 °C using the same conservative thermal figure. |
| `power-03-ap2112-sensor-feed` | PASS | The JST exports only regulated 3.3 V and return; data pins and regulator NC are open, capacitors are correctly sided, and the 100 mA worst point is 0.230 W/about 82.3 °C. |
| `power-04-ap2112-dual-output` | PASS | Both connectors share the regulated rail and return, with one correctly placed output capacitor; the total 150 mA thermal point matches the preceding 103.5 °C estimate. |
| `driver-01-single-led` | PASS | One unbypassed 1 kΩ resistor feeds the LED anode and the cathode returns to ground; worst-point current/power are about 3.6 mA/13 mW. |
| `driver-02-dual-led` | PASS | Each parallel LED branch has its own resistor and distinct series junction; both polarities and rail/return nets are correct. |
| `driver-03-attiny-dual-led` | PASS | PA6 and PA7 drive independent 1 kΩ/LED branches, UPDI and bypassing are present, and unused PA1/PA2/PA3 remain open. At 3.6 V, each branch is about 1.7 mA with a 1.9 V LED. |
| `driver-04-i2c-alert-led` | PASS | The TMP102 open-drain ALERT sinks through 1 kΩ from an LED whose anode is at 3.3 V; worst current is about 1.7 mA, below TI's characterized 3 mA low-state condition. |
| `adapter-01-uart-straight` | PASS | GND/3V3/TX/RX are straight-through and mutually distinct; the passive, 3.0–3.6 V scope is clear. |
| `adapter-02-uart-crossover` | PASS | Supply/return are straight-through while TX/RX cross exactly once and remain separate. |
| `adapter-03-i2c-fanout` | PASS | All three JST ports share GND/3V3/SDA/SCL with exactly one named pull-up pair; the 45 mm square envelope is ample. |
| `adapter-04-i2c-to-spi-header` | PASS | This is correctly specified as a BME280 SPI breakout rather than a protocol converter: the six SPI signals are complete, auxiliary JST data pins are open, and both supplies are locally bypassed. |

### Smoke recommendation

**Proceed with one bounded smoke.** Prefer `mcu-02-attiny-led`: it exercises exact catalog selection, SOIC/SMD/THT placement, UPDI, supply decoupling, a polarized series load, deliberately open GPIOs, routing, and ERC/DRC without adding the AP2112 thermal edge case or fine-pitch LGA/SOT-563 assembly as confounders.

Do not interpret a smoke pass as manufacturing or human-engineering approval. For later AP2112 runs, the candidate must declare and thermally justify its actual maximum current. In particular, `power-01-ap2112-basic` should use **200 mA**, not the headline 600 mA; any higher declaration must be recalculated and will quickly violate the case's 125 °C limit.

## Files found

- R4 private corpus and availability packet — twenty prompt/reference cases and their locally verified part availability, read only and not reproduced here.
- `src/pcbdraft/data/parts/catalog.json:9` — exact ATtiny402-SSN identity and corrected +105 °C limit.
- `src/pcbdraft/data/parts/catalog.json:40` — exact active TMP102AIDRLR SOT-563 identity and pin map.
- `src/pcbdraft/data/parts/catalog.json:169` — BME280 LGA-8 identity, pins, supplies, and limits.
- `src/pcbdraft/data/parts/catalog.json:200` — AP2112K-3.3TRG1 SOT-23-5 identity, pins, capacitors, and current rating.
- `src/pcbdraft/data/parts/catalog.json:278` and `:305` — exact JST SH and Samtec six-pin connector identities.
- Installed KiCad 10.0.5 official symbols/footprints — ATtiny402-SS, TMP102xxDRL, BME280, AP2112K-3.3, LED, SOIC-8, SOT-563, Bosch LGA-8, SOT-23-5, JST SH, and pin headers.

## External references

- Microchip, [ATtiny202/204/402/404/406 data sheet](https://ww1.microchip.com/downloads/aemDocuments/documents/MCU08/ProductDocuments/DataSheets/ATtiny202-204-402-404-406-DataSheet-DS40002318A.pdf) — 8-pin map, peripheral multiplexing, UPDI, supply grades, and the exact `ATtiny402-SSN` -40…+105 °C ordering row.
- Texas Instruments, [TMP102 data sheet](https://www.ti.com/lit/ds/symlink/tmp102.pdf) — SOT-563 pinout, ADD0 choices, ALERT behavior/current, supply range, and active `TMP102AIDRLR` package listing.
- Bosch Sensortec, [BME280 data sheet](https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bme280-ds002.pdf) and [product page](https://www.bosch-sensortec.com/en/products/environmental-sensors/humidity-sensors-bme280) — LGA pinout, I²C/SPI selection, direct CSB-to-VDDIO requirement, 100 nF capacitors, supply limits, and vent/package facts.
- Diodes Incorporated, [AP2112 data sheet](https://www.diodes.com/datasheet/download/AP2112.pdf) and [product page](https://www.diodes.com/part/view/AP2112) — SOT25/SOT-23-5 pinout, 1 µF X5R/X7R application, 600 mA electrical rating, 150 °C absolute junction limit, and 184 °C/W no-heatsink thermal resistance.
- Lite-On, [LTST-C190KGKT data sheet](https://optoelectronics.liteon.com/upload/download/DS22-2000-074/LTST-C190KGKT.PDF) — exact green 0603 LED identity and polarity/rating facts.
- JST, [SH-series product record](https://www.jst-mfg.com/product/index.php?search_product=SM04B-SRSS-TB&type=1), and Samtec, [TSW-106-07-G-S product record](https://www.samtec.com/products/tsw-106-07-g-s) — exact connector identities and mounting styles.
- KiCad, [official symbol libraries](https://gitlab.com/kicad/libraries/kicad-symbols) and [official footprint libraries](https://gitlab.com/kicad/libraries/kicad-footprints) — independent symbol-pin and pad-number mapping source.

## Related specs

- `.trellis/spec/backend/flat-pcb-toolbox.md` — model-visible flat tool and explicit placement/routing contract.
- `.trellis/spec/backend/quality-guidelines.md` — evidence-first and no-fabricated-hardware-evidence requirements.
- `.trellis/tasks/08-21-end-to-end-boardbench/prd.md` — BoardBench electrical/reference acceptance boundary.

## Caveats / Not Found

- This was a desk review. No model campaign, candidate board, fabrication, assembly, power-up, or measurement was performed.
- `power-01-ap2112-basic` has only about 0.4 °C screening margin at exactly 200 mA under the official 184 °C/W no-heatsink estimate. Thermal resistance is board-dependent; generated copper area and the declared current must be reviewed rather than inferred from the 600 mA electrical headline.
- The SOT-563 footprint's adjacent-pad gap is exactly 0.15 mm, equal to the two TMP102 cases' minimum-clearance rule. It is feasible for the declared process but has no geometric margin; a fabricator requiring more than 0.15 mm needs a different process/footprint decision.
- Passive UART/I²C adapters provide no level shifting, ESD protection, or cable-capacitance guarantee. That is acceptable only within the prompts' narrow 3.3 V/local-interconnect scope.
- Lifecycle/product-page checks establish current manufacturer identities, not future stock or procurement guarantees. This AI review does not satisfy the separate human engineering gate.
