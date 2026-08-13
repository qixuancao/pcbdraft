# PCB design review

Run: 20260812T153507Z-51010176<br>
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

No deterministic connectivity, ERC, DRC, routing-completion, or typed semantic-rule failure is present in the supplied synchronized evidence. Residual heuristic risk is medium for this non-safety-critical prototype because fabricator qualification and the firmware-dependent power budget remain open, while input-voltage, UPDI source ownership, and external I2C assumptions require system-level enforcement. This review is not functional, SI, PI, thermal, EMI, safety, manufacturing, or human sign-off.

### Modules

- ATtiny402 core: U1, C1, and J2
- TMP102B sensor interface: U2, C2, R1, and R2
- Qwiic power/I2C input: J1 and virtual ERC source flags
- GPIO status indicator: R3 and D1

### Interfaces

- sensor\_i2c: 3.3 V, 100 kHz open-drain bus on /I2C\_SDA and /I2C\_SCL connecting U1, U2, R1/R2, and J1
- J1 Qwiic-style external interface: pin 1 GND, pin 2 3V3, pin 3 SDA, pin 4 SCL
- J2 programming interface: pin 1 UPDI, pin 2 target-voltage sense, pin 3 GND
- Status output: U1 pin 2 through R3 and D1 to GND

### Power domains

- v3v3: externally regulated 3.3 V nominal, 3.0–3.6 V, 100 mA maximum; physical source J1 pin 2, with J2 pin 2 designated sense-only

### Missing constraints

- Worst-case firmware operating profile needed for the 100 mA envelope, including clock, active/sleep duty cycle, GPIO states, and simultaneous loads.
- 3V3 source tolerance, ripple, startup/hot-plug overshoot, and source/cable impedance beyond the steady 3.0–3.6 V range.
- Required ESD/EFT and connector hot-plug robustness level for J1 and J2.
- Quantitative temperature accuracy and response-time acceptance in the final enclosure, airflow, and thermal environment.
- Project-specific fabricator and assembler capability confirmation, which is explicitly deferred to external qualification.

## AI heuristic findings

### 1. [MEDIUM] Manufacturing qualification remains release-blocking

- Category: manufacturability
- Confidence: 0.99
- Requires human: true
- Evidence: The release-blocking manufacturing constraint records fabricator="not\_selected" and capability\_verification="external\_l4\_required".; Actual minima are 0.2000 mm track width, 0.3000 mm drill, and 0.1356 mm clearance; track and drill are exactly at their declared limits.; The complete bounded inventory contains no Gerber, drill, placement, or CAM-review artifacts.
- Rationale: Zero-error KiCad DRC demonstrates compliance with configured generic rules but does not close the explicitly deferred external manufacturing qualification.
- Proposed action: Select the fabricator and assembler, map their rules to the actual geometry, and perform supplier-specific DFM/DFA and CAM review, including the 0.5 mm-pitch U2 footprint, lead-free HASL finish, and mixed SMT/THT assembly.

### 2. [MEDIUM] External 3V3 source needs a hard 3.6 V ceiling

- Category: component\_ratings
- Confidence: 0.98
- Requires human: true
- Evidence: Power domain v3v3 permits a maximum of 3.6 V.; U2 TMP102B has a recorded maximum operating supply of 3.6 V and absolute maximum of 4.0 V.; J1 pin 2 connects directly through /3V3 to U2 pin 5; the component inventory contains no regulator, clamp, or isolation element.
- Rationale: The steady-state voltage contract is explicit and nominal 3.3 V operation is supported, but its upper bound equals U2's operating maximum and therefore provides no operating guardband.
- Proposed action: Require the upstream source to remain at or below 3.6 V including tolerance, startup, hot-plug, and cable-induced overshoot, or add suitable regulation/clamping and validate it.

### 3. [MEDIUM] Declared power budget is not yet demonstrated

- Category: power\_budget
- Confidence: 0.97
- Requires human: true
- Evidence: The release-blocking power budget permits 0.1 A and 0.36 W at a 3.6 V basis.; The power\_budget analysis is marked required=false because the maximum load is firmware-dependent.; No worst-case load summation or measured current result is present in the supplied constraint metrics or rule evaluation.
- Rationale: The limit itself is well bounded, but the evidence does not demonstrate that all firmware-dependent operating states remain inside it.
- Proposed action: Close a worst-case current budget using MCU clock and operating modes, sensor current, LED current, simultaneous I2C pull-up current, programmer leakage, and design margin; verify representative hardware current afterward.

### 4. [MEDIUM] UPDI VTREF safety depends on operator policy

- Category: power\_source\_contention
- Confidence: 0.98
- Requires human: true
- Evidence: J2 pin 2 and J1 pin 2 are directly connected by /3V3.; The explicit updi\_power\_policy identifies J2 pin 2 as voltage-sense-only and forbids simultaneous external sources.; No series isolation, jumper, diode, or current-limiting component separates J2 VTREF from the rail.
- Rationale: The source-ownership constraint is explicit and sufficient as design intent, but compliance is procedural; an incorrectly configured programmer could contend with Qwiic power.
- Proposed action: Use only a programmer whose VTREF pin is guaranteed high impedance, provide unambiguous physical labeling/keying, and consider hardware isolation or current limiting if operator error is plausible.

### 5. [LOW] I2C timing pass is conditional on attached-system assumptions

- Category: interface\_integration
- Confidence: 0.97
- Requires human: true
- Evidence: R1 and R2 provide one 4.7 kOhm pull-up each on /I2C\_SDA and /I2C\_SCL.; The bounded calculation gives 796.462 ns rise time against a 1000 ns limit and 0.766 mA sink current against a 3 mA limit.; The calculation assumes at most 200 pF and explicitly forbids external pull-ups, while J1 exposes both bus lines externally.
- Rationale: The board-side I2C calculation passes, but external cabling and devices can change capacitance and effective pull-up resistance.
- Proposed action: Document and enforce the no-external-pull-up and 200 pF limits, then measure SDA/SCL rise time and low level with the intended controller, cable, and attached-device population.

### 6. [LOW] Solid GND-plane connection may complicate J2 soldering

- Category: assembly
- Confidence: 0.9
- Requires human: true
- Evidence: The B.Cu /GND zone covers 1217.863083 mm² and uses pad\_connection="solid".; J2 is a through-hole header whose pin 3 connects to /GND.
- Rationale: A solid connection from a through-hole pad to a large copper plane can increase soldering heat demand and reduce process margin.
- Proposed action: Inspect the generated copper around J2 pin 3 and use thermal relief spokes unless the selected soldering process is qualified for the solid connection.

### 7. [INFO] Supplied deterministic rule and connectivity gates are clean

- Category: verification
- Confidence: 1.0
- Requires human: false
- Evidence: Managed intent and native evidence are synchronized with no reported drift.; ERC and DRC each completed with 0 errors and 0 warnings.; Semantic typed-rule evaluation passed, routing reports no unrouted nets, and both decoupling-distance metrics passed.; Native connectivity matches the intended J1 pin order, U1/U2 I2C connections, LED chain, and J2 UPDI/VTREF/GND assignments.
- Rationale: The supplied deterministic evidence shows no configured-rule or connectivity violation, although it is not functional, physical, manufacturing, or human sign-off.
- Proposed action: Preserve these reports as the review baseline and rerun all gates after any design change.

## Unsupported checks

- Firmware behavior, sensor-acquisition correctness, register configuration, and functional simulation.
- Closed worst-case current budget, dynamic rail transient response, decoupling loop impedance, and full power-integrity analysis.
- Signal-integrity and return-path analysis, including waveforms on the intended external I2C cable and attached devices.
- EMI/EMC emissions or susceptibility, ESD/EFT, surge, and hot-plug testing.
- Thermal simulation, TMP102 self-heating and board-heat bias, and environmental accuracy testing.
- Mechanical and 3D enclosure fit, cable clearance, connector retention, mounting, and service-access validation.
- Supplier-specific DFM/DFA, stackup and material verification, soldermask/paste review, panelization, and assembly-process qualification.
- Gerber, drill, placement, BOM/manifest reproducibility, and visual CAM comparison because those artifacts were not supplied.
- Bench bring-up, in-circuit programming compatibility, boundary testing, and production test coverage.
- Footprint-filter conformance, SPICE-model issues, and tuning-profile geometry checks were ignored by the reported KiCad gates.
- Independent post-CAM netlist comparison; the supplied IPC-D-356 records cover 15 vias and the three J2 pads but do not independently enumerate the SMT pads.
