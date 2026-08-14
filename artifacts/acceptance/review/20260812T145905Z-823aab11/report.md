# PCB design review

Run: 20260812T145905Z-823aab11<br>
Project: /mnt/2T/pcbdraft/examples/attiny\_sensor\_controller/project<br>
Schematic: attiny\_sensor\_controller.kicad\_sch<br>
Board: attiny\_sensor\_controller.kicad\_pcb

## Scope and interpretation

The ERC/DRC section below is deterministic evidence produced by the local `kicad-cli`.
The design interpretation and findings are AI heuristics, not a safety or engineering sign-off.
This report does not establish functional correctness, SI/PI, thermal, EMI, timing, tolerance,
manufacturability, regulatory compliance, or production readiness.

## Deterministic ERC/DRC evidence

- ERC: tool_status=ok, exit_code=0, errors=0, warnings=0
- DRC: tool_status=ok, exit_code=0, errors=0, warnings=0

### Parsed violation evidence

- No error/warning records in parsed gate JSON.

## AI heuristic overview

Risk: **medium**

Deterministic KiCad ERC and DRC both pass with 0 errors and 0 warnings. Heuristic medium risk remains around the implicit shared-3V3 source policy, external I2C integration, and the absence of GND-tagged vias despite multiple layer-changing vias. These observations are conditional review findings, not functional, SI, PI, EMI, safety, thermal, manufacturing, or compliance sign-off.

### Modules

- U1 ATtiny402 MCU core.
- U2 TMP102B digital temperature sensor.
- J1/R1/R2 Qwiic power and I2C interface.
- J2 UPDI programming and target-reference interface.
- D1/R3 green status indicator.
- C1/C2 100 nF rail bypass network.

### Interfaces

- J1 \`QWIIC\`: pin 1 \`/GND\`, pin 2 \`/3V3\`, pin 3 \`/I2C\_SDA\`, pin 4 \`/I2C\_SCL\`.
- J2 \`UPDI\_VTREF\_SENSE\`: pin 1 \`/UPDI\`, pin 2 \`/3V3\`, pin 3 \`/GND\`.
- Internal I2C: U1 PA1/PA2 connects to U2 SDA/SCL and J1 through buses pulled up by R1/R2.
- LED output: U1 PA6 drives D1 through R3=1 kOhm; D1 cathode returns to \`/GND\`.
- U2 ALERT and U1 PA3/PA7 are explicitly unconnected.

### Power domains

- \`/3V3\`: one shared external rail powering U1 and U2, biasing R1/R2, and exposed at J1.2 and J2.2.
- \`/GND\`: common return for U1, U2, C1, C2, D1, J1, and J2; U2 ADD0 is strapped to this domain.
- No regulated, isolated, switched, or separately protected supply domain is evident.

### Missing constraints

- Permitted 3V3 source, voltage tolerance, current budget, source priority, hot-plug behavior, and back-power policy.
- UPDI programmer behavior, especially whether its VTREF pin senses or sources the target rail.
- I2C clock rate, cable length, total capacitance, attached-device count, total pull-up resistance, and address map.
- Allowed supply ripple/transients and required bulk-capacitance margin.
- Selected PCB stack-up, copper weight, impedance assumptions, and fabricator/assembler limits.
- Connector exposure, ESD/transient environment, and required compliance level.
- Mechanical mounting, enclosure fit, connector edge placement, cable orientation, and strain relief.
- Firmware pin configuration, I2C initialization, LED duty cycle, and fault behavior.
- Quantitative acceptance criteria from \`requirements.pcbreq.json\` were not included in the supplied semantic evidence.

## AI heuristic findings

### 1. [INFO] Enabled ERC and DRC checks pass

- Category: deterministic\_erc\_drc
- Confidence: 1.0
- Requires human: true
- Evidence: \`attiny\_sensor\_controller.kicad\_sch\`: KiCad ERC completed successfully with 0 errors and 0 warnings.; \`attiny\_sensor\_controller.kicad\_pcb\`: KiCad DRC completed successfully with 0 errors and 0 warnings.; ERC ignored SPICE-model and footprint-filter checks; DRC ignored tuning-profile geometry and footprint-filter mismatch checks.
- Rationale: The deterministic gates establish compliance with the enabled KiCad rules, not functional correctness or suitability of those rules.
- Proposed action: Retain the clean reports, but review the ignored checks and confirm that project design rules match fabrication and electrical requirements.

### 2. [MEDIUM] The shared 3V3 source policy is implicit

- Category: power\_architecture
- Confidence: 0.97
- Requires human: true
- Evidence: \`attiny\_sensor\_controller.kicad\_sch\`: \`/3V3\` directly joins J1.2, J2.2, U1.1, U2.5, C1.1, C2.1, R1.1, and R2.1.; J2 is labeled \`UPDI\_VTREF\_SENSE\`, but its pin 2 is directly tied to the same rail exposed by Qwiic connector J1.; The complete 10-component schematic contains no regulator, fuse, load switch, isolation diode, or power multiplexer; rail capacitance consists only of C1 and C2 at 100 nF each.
- Rationale: Simultaneously driven J1 and J2 supplies could contend or back-power equipment. The two 100 nF capacitors provide high-frequency bypassing but do not demonstrate cable-fed transient margin.
- Proposed action: Define one permitted 3V3 source and explicitly document J2.2 as sense-only if applicable. If either connector may source power, add suitable isolation/current limiting and validate whether cable impedance requires local bulk capacitance.

### 3. [MEDIUM] External I2C pull-up and address assumptions need validation

- Category: i2c\_integration
- Confidence: 0.96
- Requires human: true
- Evidence: \`attiny\_sensor\_controller.kicad\_sch\`: R1 and R2 are each 4.7 kOhm pull-ups from \`/I2C\_SDA\` and \`/I2C\_SCL\` to \`/3V3\`.; The same buses leave through J1.3 and J1.4, allowing external boards to contribute additional pull-ups.; U2 ADD0 pin 4 is fixed to \`/GND\`, providing no visible address-selection option.
- Rationale: Parallel pull-ups can exceed device sink-current limits, while excessive resistance or capacitance can violate rise-time limits. A fixed address can conflict with another device on the external bus.
- Proposed action: Calculate the effective pull-up resistance using every expected attached board, bus capacitance, and clock rate. Consider DNP or disconnectable pull-ups and a configurable ADD0 option if bus composition is not fixed.

### 4. [MEDIUM] No dedicated ground stitching vias are evident

- Category: layout\_return\_path
- Confidence: 0.9
- Requires human: true
- Evidence: \`attiny\_sensor\_controller.kicad\_pcb\`: all 13 reported vias belong to \`/LED\_CTRL\` \(2\), \`/I2C\_SDA\` \(4\), \`/3V3\` \(4\), \`/I2C\_SCL\` \(2\), or \`/UPDI\` \(1\); no \`/GND\` via is reported.; All 10 components are placed on the front, while B.Cu contains 1251.378 mm² of copper over a 1350 mm² board.; The supplied connectivity evidence does not identify the large B.Cu copper region's net or local return geometry.
- Rationale: The absence of dedicated GND vias may leave long decoupling loops or poor return-current transitions. This is a heuristic concern because zone geometry and component placement were not supplied.
- Proposed action: Inspect the B.Cu zone assignment and return paths. If B.Cu is the reference plane, add localized GND vias near IC decouplers and appropriate signal layer transitions, and reduce unnecessary layer changes where practical.

### 5. [LOW] Connector transient protection is environment-dependent

- Category: interface\_protection
- Confidence: 0.92
- Requires human: true
- Evidence: J1 directly exposes \`/3V3\`, \`/GND\`, \`/I2C\_SDA\`, and \`/I2C\_SCL\`; J2 directly exposes \`/UPDI\`, \`/3V3\`, and \`/GND\`.; The complete component list contains no explicit TVS, ESD array, or other connector transient-protection device.
- Rationale: Directly connected MCU and sensor pins may be vulnerable to connector transients or ESD, although protection may be unnecessary in a controlled, enclosed environment.
- Proposed action: Define whether either connector is user-accessible, hot-plugged, or connected through long cables. Add appropriately selected protection only if the deployment environment requires it.

### 6. [LOW] Board minima require fabricator-rule confirmation

- Category: manufacturability
- Confidence: 0.98
- Requires human: true
- Evidence: \`attiny\_sensor\_controller.kicad\_pcb\`: minimum track clearance is 0.1356 mm, minimum track width is 0.2000 mm, and minimum drill diameter is 0.3000 mm.; The supplied DRC passed against project rules, but no selected-fabricator capability specification was supplied.
- Rationale: Passing project DRC does not establish that the reported minima are within a particular fabrication process or yield target.
- Proposed action: Import or encode the intended fabricator's minimum clearance, drill, annular-ring, stack-up, and copper constraints, then rerun DRC and fabrication-output checks.

## Unsupported checks

- Visual placement, routing, trace-length, copper-neck, plane-island, return-loop, and decoupler-proximity review.
- Signal-integrity, power-integrity, impedance, crosstalk, and timing simulation.
- Thermal analysis and component temperature-rise validation.
- EMI/EMC, ESD, surge, EFT, and radiated/conducted compliance testing.
- Mechanical fit, connector mating orientation, enclosure interference, and cable-access review.
- Land-pattern dimensional verification, paste-mask review, assembly clearance, panelization, and fabrication-output inspection.
- Datasheet-limit, MCU pin-multiplexing, current-drive, and absolute-maximum validation from primary component documentation.
- Firmware behavior and system-level functional testing.
- Footprint-filter conformity and tuning-profile geometry checks explicitly ignored by the supplied gates.
- Safety, reliability, environmental qualification, and manufacturing sign-off.
