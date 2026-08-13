# PCB design review

Run: 20260812T154551Z-8f8b4876<br>
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

No deterministic connectivity, unrouted-net, ERC, DRC, or typed semantic-rule defect is reported. The design is a coherent low-voltage prototype candidate. Residual risk is medium because manufacturing-release evidence, external I2C integration, UPDI source-direction enforcement, and worst-case rail behavior remain unverified. These conclusions are AI heuristics, not functional, SI/PI, thermal, EMI, safety, or manufacturing sign-off.

### Modules

- ATtiny402 core and programming: U1, C1, J2.
- TMP102B sensor and I2C biasing: U2, C2, R1, R2.
- Qwiic-compatible power and bus input: J1.
- GPIO status indicator: U1 PA6, R3, D1.

### Interfaces

- I2C sensor bus: 3.3 V, 100 kHz open-drain; U1 pins 4/5, U2 pins 6/1, J1 pins 3/4, and 4.7 kOhm pull-ups R1/R2.
- Qwiic-compatible input: J1 pin 1 GND, pin 2 3V3, pin 3 SDA, pin 4 SCL.
- UPDI service interface: J2 pin 1 UPDI to U1 pin 6, pin 2 /3V3 VTREF sense, pin 3 GND.
- Status output: U1 pin 2 through R3 1 kOhm and D1 to GND.

### Power domains

- v3v3: externally regulated 3.0-3.465 V, nominal 3.3 V, bounded to 0.1 A and 0.3465 W; physical source J1 pin 2 with J1 pin 1 return; J2 pin 2 is sense-only by policy.

### Missing constraints

- J1's permitted external peer role and controller-ownership/arbitration policy are not declared; the bus speed, pull-up, and capacitance limits themselves are explicitly present.
- The explicit 0.1 A total budget lacks per-load firmware-state allocation and quantitative rail droop, overshoot, and startup limits.
- ESD, EFT, hot-plug, cable-discharge, and connector handling exposure levels are not declared.
- No quantitative temperature-error, response-time, airflow, or calibration acceptance condition is supplied for U2.
- Enclosure, mounting, cable strain, connector-cycle, and service-access requirements are not supplied; board statistics report zero NPTH mounting holes.
- Fabricator/assembler stackup, solder-mask, paste, panelization, and yield criteria remain deferred by the explicit not-selected/external-review manufacturing status.

## AI heuristic findings

### 1. [INFO] Deterministic connectivity and rule gates passed

- Category: deterministic\_validation
- Confidence: 0.99
- Requires human: false
- Evidence: ERC and DRC each report 0 errors and 0 warnings.; Routing is completed with unrouted=\[\]; managed synchronization reports no drift.; Typed semantic-rule evaluation passed.; Decoupling metrics passed: C1/U1 1.41449 mm &lt;= 3.0 mm and C2/U2 1.836166 mm &lt;= 2.0 mm.
- Rationale: The supplied evidence supports a clean, internally synchronized design candidate, but these checks do not constitute physical or production validation.
- Proposed action: Retain these results as the baseline and rerun all gates after any design change.

### 2. [MEDIUM] Manufacturing release evidence remains incomplete

- Category: manufacturing\_readiness
- Confidence: 0.99
- Requires human: true
- Evidence: req\_manufacturing requires reproducible Gerber, drill, placement, and manifest artifacts.; The complete bounded inventory identifies no Gerber, drill, or placement outputs, and the generation evidence contains no manufacturing-release artifact receipt.; Constraint manufacturing records fabricator='not\_selected' and capability\_verification='external\_l4\_required'.; The assembly combines nine SMD parts, a 0.5 mm-pitch SOT-563 U2, one through-hole J2, and lead-free HASL.
- Rationale: Zero DRC violations only establish compliance with configured generic rules; the explicit manufacturing acceptance and external capability review remain open.
- Proposed action: Select the fabricator and assembler, run process-specific DFM/DFA, generate the full release package, and have a human verify layer plots, drill map, placement rotation, polarity, paste, and BOM-to-MPN correspondence.

### 3. [MEDIUM] J1 operation depends on a controlled external I2C contract

- Category: interface\_integration
- Confidence: 0.96
- Requires human: true
- Evidence: sensor\_i2c designates U1 pins 4/5 as controller while the same SDA/SCL nets are exposed directly at J1 pins 3/4.; The interface forbids external pull-ups and limits total bus capacitance to 200 pF, but J1 is only classified as an external member.; The calculated rise time is 796.462 ns against a 1000 ns limit, leaving finite system-level margin; sink current is 0.737 mA against 3 mA.
- Rationale: The board satisfies its declared 100 kHz RC budget, but a cable or attached device can add capacitance, pull-ups, or another controller that the PCB cannot itself prevent.
- Proposed action: Define the permitted J1 peer role, controller ownership/arbitration policy, cable and attached-device capacitance allocation, and pull-up inventory; then verify SDA/SCL waveforms with the worst allowed system configuration.

### 4. [MEDIUM] UPDI VTREF sense-only policy is not hardware-enforced

- Category: power\_source\_ownership
- Confidence: 0.99
- Requires human: true
- Evidence: J2 pin 2 is connected directly to /3V3 and is designated VTREF voltage sense.; The physical /3V3 source is J1 pin 2.; updi\_power\_policy forbids simultaneous external sources, but the J2-to-/3V3 endpoint contains no isolation or current-limiting component.
- Rationale: The sense-only intent is explicit but procedural. A programmer that sources J2 pin 2 could contend with or back-power the J1 supply.
- Proposed action: Use and label a verified input-only VTREF programmer adapter; for stronger fault tolerance, consider a series current limiter, isolation arrangement, or keyed interlock that preserves accurate sensing.

### 5. [MEDIUM] The power envelope lacks load and transient validation

- Category: power\_integrity
- Confidence: 0.9
- Requires human: true
- Evidence: The release-blocking power\_budget declares 0.1 A and 0.3465 W, while the associated analysis is marked required=false because maximum load is firmware-dependent.; Trusted records do not supply a summed worst-case operating-current calculation for U1 and U2.; C1 and C2 are the only capacitors and are each 100 nF; no input bulk reservoir is identified.; The /3V3 routing receipt reports 50.187503 mm aggregate length from the cable-supplied rail.
- Rationale: No budget violation is proven, but static routing capacity and local 100 nF decoupling do not demonstrate rail droop, overshoot, or startup margin.
- Proposed action: Complete a worst-case current table across 3.0-3.465 V and all firmware states, then measure startup and load-step behavior at J1, U1, and U2. Add appropriately rated input bulk capacitance near J1 if the source-and-cable specification does not bound transients sufficiently.

### 6. [LOW] External-interface protection is environment-dependent

- Category: external\_protection
- Confidence: 0.94
- Requires human: true
- Evidence: J1 connects /3V3, /GND, /I2C\_SDA, and /I2C\_SCL directly to board circuitry; J2 exposes /UPDI, /3V3, and /GND.; The component inventory contains no TVS, clamp, filtering, or dedicated hot-plug protection devices.; The stated scope is a non-safety-critical prototype and supplies no ESD or hot-plug immunity target.
- Rationale: Directly exposed MCU and sensor nets may be vulnerable during cable insertion or handling; actual risk depends on the intended enclosure and use environment.
- Proposed action: Document a controlled bench-only handling environment or define an immunity target and evaluate low-capacitance data-line protection plus appropriate supply transient protection near the connectors.

## Unsupported checks

- Firmware execution, startup sequencing, I2C arbitration, sensor acquisition, and LED behavior.
- Physical I2C waveforms with actual cables and attached devices, detailed signal integrity, crosstalk, EMI, and emissions.
- Power-distribution impedance, voltage drop, cable/source dynamics, decoupling-loop inductance, and transient measurements.
- Thermal simulation, sensor thermal bias, ambient correlation, response time, and calibration.
- ESD, EFT, surge, hot-plug, and regulatory immunity testing.
- Three-dimensional mechanical fit, connector mating clearance, enclosure interaction, and strain testing.
- Fabricator-specific DFM, assembler-specific DFA, solder-paste/yield review, panelization, and validation of absent release artifacts.
- Prototype assembly inspection, bring-up, boundary/functional testing, and production test coverage.
- Current stock, pricing, authorized-distributor availability, counterfeit controls, and required RoHS evidence.
- KiCad SPICE-model and footprint-filter checks that were explicitly ignored by the deterministic gates; trusted pin-mapping records provide semantic evidence but not those specific checks.
