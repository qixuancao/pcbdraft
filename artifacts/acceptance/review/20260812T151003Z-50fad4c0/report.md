# PCB design review

Run: 20260812T151003Z-50fad4c0<br>
Project: /mnt/2T/copperwright/examples/attiny\_sensor\_controller/project<br>
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

AI heuristic result: medium residual risk. The synchronized prototype is coherent at the netlist and configured-rule level, with clean ERC/DRC, no unrouted nets, trusted part mappings, and passing declared static calculations. Release evidence remains incomplete because decoupling geometry is not demonstrably within its stated metric, the GND plane lacks local stitching, source-ownership and external-I2C policies rely on integration procedure, the power budget is unproved, and external manufacturing verification is pending. This is not functional, SI, PI, thermal, EMI, safety, or manufacturing sign-off.

### Modules

- attiny402\_core: U1, C1, and UPDI header J2.
- tmp102\_i2c\_sensor: U2, C2, and pull-ups R1/R2.
- qwiic\_power\_input: external connector J1 and virtual ERC source flags.
- gpio\_status\_led: D1 and current limiter R3.

### Interfaces

- sensor\_i2c: 3.3 V, 100 kHz open-drain bus connecting U1-4/U1-5, U2-6/U2-1, J1-3/J1-4, and 4.7 kOhm pull-ups R1/R2; 200 pF maximum and external pull-ups forbidden.
- qwiic\_power\_input: J1-1 GND and J1-2 regulated 3V3 source, with J1-3 SDA and J1-4 SCL.
- updi: J2-1 to U1-6, J2-2 as direct 3V3 target-voltage sense, and J2-3 GND.
- status\_gpio: U1-2 drives R3-D1 to GND.

### Power domains

- v3v3: externally regulated nominal 3.3 V, permitted 3.0-3.6 V, 0.1 A maximum; physical source J1-2 with common GND at J1-1 and VTREF sense at J2-2.

### Missing constraints

- A precise measurement and verification definition for decoupling max\_distance\_mm: component origin, relevant pad centers, or routed supply-return loop.
- An external-power transient contract covering source impedance, hot-plug overshoot, reverse connection, and the ESD environment at J1.
- A measurable temperature-sensing accuracy, response-time, operating-range, and thermal-placement target; general\_purpose is not quantitative.
- A joint voltage-current operating envelope reconciling the 0.33 W constraint with the permitted 3.6 V and 0.1 A maxima.

## AI heuristic findings

### 1. [INFO] Deterministic electrical and routing baseline is clean

- Category: deterministic verification
- Confidence: 1
- Requires human: false
- Evidence: KiCad ERC and DRC each report 0 errors and 0 warnings.; Managed synchronization reports no drift; semantic typed-rule evaluation passed.; Routing completed with unrouted=\[\]; J1 is GND/3V3/SDA/SCL and J2 is UPDI/3V3/GND.; Declared calculations give LED current 1.2 mA versus 5 mA limit and I2C rise time 796.462 ns versus 1000 ns limit.
- Rationale: The supplied evidence supports netlist coherence and configured-rule compliance, but not the physical and integration checks identified below.
- Proposed action: Preserve and rerun these deterministic gates after every schematic, layout, or BOM change.

### 2. [MEDIUM] Native decoupling geometry does not prove the declared limits

- Category: decoupling/layout
- Confidence: 0.9
- Requires human: true
- Evidence: Release-blocking decouple\_mcu requires at most 3.0 mm; native origins C1=\(17.75,11.0\) and U1=\(17.75,15.0675\) are 4.0675 mm apart.; Release-blocking decouple\_sensor requires at most 2.0 mm; native origins C2=\(31.0,12.75\) and U2=\(31.0,15.0975\) are 2.3475 mm apart.; U2's 3V3 route uses vias near \(30.5,13.4\) and \(32.9,15.1\), so the capacitor-to-supply path is not visibly direct.; Semantic rule evaluation explicitly used a method without approximate geometry; ERC/DRC do not establish decoupling placement compliance.
- Rationale: The constraints are explicit, but their measurement method is unspecified and the native coordinates do not demonstrate compliance under the component-origin metric.
- Proposed action: Define whether distance means component origins, relevant pad centers, or routed supply-return loop; then verify that metric and move/rotate C1 and especially C2 closer if necessary.

### 3. [MEDIUM] The GND reference plane lacks local stitching

- Category: return path/layout
- Confidence: 0.92
- Requires human: true
- Evidence: The routing contract names GND as the continuous reference for I2C.; A filled B.Cu GND zone of 1217.863083 mm² exists, but via\_count\_by\_net contains no GND vias.; All components except through-hole J2 are SMD; the evident B.Cu plane connection is therefore J2-3 at the left edge.; I2C\_SDA uses 37.087503 mm and 4 vias; I2C\_SCL uses 30.187503 mm and 2 vias, including B.Cu routing.
- Rationale: The plane exists electrically, but the supplied geometry does not show local transitions that make it an effective continuous reference. This is a heuristic concern, not an SI or EMI conclusion.
- Proposed action: Inspect the filled copper and return paths visually. Prefer routing I2C on F.Cu over uninterrupted B.Cu GND and add local GND stitching near J1, U1/C1, U2/C2, and signal layer transitions.

### 4. [MEDIUM] UPDI VTREF sense-only behavior is procedural, not hardware-enforced

- Category: power-source ownership
- Confidence: 0.99
- Requires human: true
- Evidence: J1-2 and J2-2 are directly connected to 3V3.; The source-ownership constraint designates J1 as the physical source and J2-2 as voltage sense, while forbidding simultaneous external sources.; J2 is an unkeyed 1x3 pin header, and no isolation, jumper, or current-limiting component exists between its VTREF pin and 3V3.
- Rationale: A programmer that sources its voltage pin can contend with J1 or back-power the board; the current design relies on operator and fixture behavior to enforce the policy.
- Proposed action: Use an unmistakably labeled or keyed sense-only programming cable and fixture interlock. If programmer misuse is credible, add an appropriate isolation or configurable connection and bench-test all power sequencing cases.

### 5. [MEDIUM] The release-blocking power budget is not quantitatively closed

- Category: power budget
- Confidence: 0.99
- Requires human: true
- Evidence: The release-blocking budget is 0.1 A and 0.33 W.; The power-budget analysis is marked optional because maximum load is firmware-dependent; no worst-case component-current sum is supplied.; Trusted part records do not provide operating-current bounds for U1 or U2.; Requirements permit 3.6 V and 0.1 A, whose simultaneous maxima equal 0.36 W rather than 0.33 W.
- Rationale: Connectivity and trace width do not prove the release-blocking consumption limit, and the independent voltage/current maxima need explicit derating to satisfy 0.33 W.
- Proposed action: Create a worst-case current table across voltage, temperature, clock, GPIO, LED, and sensor states; define the allowed voltage-current envelope and verify it on hardware.

### 6. [MEDIUM] Manufacturing release remains externally blocked

- Category: manufacturing readiness
- Confidence: 1
- Requires human: true
- Evidence: The manufacturing constraint states fabricator=not\_selected and capability\_verification=external\_l4\_required.; Actual minimum track width and drill are exactly the declared 0.2 mm and 0.3 mm process minima.; The complete bounded inventory contains no Gerber, drill, placement, assembly, or fabrication-manifest outputs.; Only ERC and DRC production gates are supplied; the requirement also calls for reproducible fabrication and placement artifacts.
- Rationale: The board is a fabricable candidate under generic rules, but the explicit external manufacturing gate and required production artifacts remain incomplete.
- Proposed action: Select the fabricator and assembler, map their rules, review the 0.5 mm-pitch SOT-563 and lead-free HASL process, then generate and independently inspect Gerber, drill, BOM, placement, stencil, and manifest outputs.

### 7. [LOW] External I2C compliance depends on system-level enforcement

- Category: I2C integration
- Confidence: 0.97
- Requires human: true
- Evidence: J1 exposes I2C externally while the interface contract forbids external pull-ups.; The 4.7 kOhm/200 pF calculation gives 796.462 ns against a 1000 ns rise-time limit, leaving finite capacitance margin.; No extracted or measured total capacitance, cable allocation, or host pull-up configuration evidence is supplied.
- Rationale: The declared budget passes mathematically, but connector-attached equipment can violate its capacitance or pull-up assumptions without any board-level enforcement.
- Proposed action: Document the allowed cable and peripheral capacitance, require host pull-ups to be disabled, and measure SDA/SCL rise time and low level with the intended complete system.

## Unsupported checks

- Firmware behavior, I2C transactions, sensor addressing/readout, and status logic under real operating states.
- Extracted or measured rail impedance, transient droop, decoupling effectiveness, inrush, and worst-case current consumption.
- Signal-integrity simulation, total bus-capacitance extraction, crosstalk, return-current field analysis, EMI emissions, or immunity.
- ESD, electrical-fast-transient, hot-plug, reverse-polarity, and overvoltage testing at J1 or J2.
- Thermal simulation or measurement of sensor accuracy, response time, self-heating, and heat-source coupling.
- 3D mechanical fit, connector mating access, cable strain, enclosure clearance, and physical serviceability.
- Gerber/drill/PnP/stencil/assembly drawing generation, reproducibility, and independent CAM/DFM review.
- Assembly yield, solder-joint quality, component polarity inspection, procurement availability, RoHS documentation, and fabricator/assembler sign-off.
- Physical prototype bring-up, programming-fixture validation, environmental testing, safety assessment, and regulatory certification.
