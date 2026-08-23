# Research: five-category PCB manufacturability review

- Query: Lean human-substitute review of five actual BoardBench PCB outputs,
  limited to layout/routing, manufacturability, solderability, and
  purchase-blocking issues.
- Scope: internal
- Date: 2026-08-22

## Findings

### Review boundary

This is a file-only engineering review. It does not claim fabrication,
assembly, power-on, or functional hardware validation. Counts below are best
estimates of distinct PCB/layout/routing decisions, not mouse clicks, and
exclude schematic corrections. The independent checks were KiCad 10.0.5 DRC
runs with errors and warnings included; for example, the MCU record states the
tool/version and source at
`/mnt/2T/pcbdraft-boardbench-campaigns/ai-pilot-r4-luna-five-category-v3b-20260822/scores/mcu-01-attiny-updi-run-1/independent-validation/drc.json:1`.

| Run | Verdict | PCB changes before ordering | Engineer time | File-evident first-power short risk |
|---|---|---:|---:|---|
| MCU / ATtiny UPDI | needs changes | 4 | 25–45 min | yes |
| Sensor / TMP102 | incomplete | 7 | 35–60 min | no evident short; design incomplete |
| Power / AP2112 | needs changes | 5 | 25–40 min | no evident short |
| Driver / dual LED | pass | 0 | 0–10 min | no evident short |
| Adapter / UART straight-through | needs changes | 6 | 30–50 min | yes |

Total estimate: 22 distinct PCB changes and 115–205 engineer minutes. Only
the dual-LED board is presently a layout-orderable candidate. Manufacturing
package generation is separate: the MCU package exists but contains a failing
board; the power run retained BOM/Gerber but no complete drill/order bundle;
the driver retained a BOM only; sensor and adapter retained no order bundle.
Those export steps are purchase blockers but are not included in the PCB change
counts.

### 1. MCU — `mcu-01-attiny-updi-run-1`

**Verdict: needs changes.** The 30 × 30 mm board and specified footprints are
inside the task envelope, but independent DRC reports a real GND-to-3V3 short
at J1, J2 copper only 0.07 mm from the board edge against a 0.50 mm rule, a
U1/C1 courtyard overlap, and 0.12 mm C1-to-U1 clearance against 0.20 mm. The
short and edge errors are recorded at
`/mnt/2T/pcbdraft-boardbench-campaigns/ai-pilot-r4-luna-five-category-v3b-20260822/scores/mcu-01-attiny-updi-run-1/independent-validation/drc.json:24`,
the overlap at the same file `:70`, and the clearance error at `:116`.

Required PCB changes (4):

1. Move J2 inward while retaining an obvious pin-1 cue.
2. Move C1 clear of U1's courtyard while keeping it adjacent to the supply pins.
3. Rebuild C1's local VCC/GND loop after the move.
4. Delete and reroute J1's GND path from the correct pad; the current copper
   shorts the +3V3 pad.

Solderability: SOIC-8, 0603, and 2.54 mm through-hole headers are normally
hand-assembly friendly, but the present C1/U1 overlap and edge-adjacent J2 make
this revision non-assemblable as drawn. DRC does not establish useful connector
legends, mating-tool clearance, or a genuinely low-inductance decoupling loop.

Cause tags: `layout/router`, `model reasoning`. Estimated time assumes the
existing net assignments are retained and the repair is done directly in
KiCad, followed by DRC and Gerber regeneration.

### 2. Sensor — `sensor-01-tmp102-i2c-run-1`

**Verdict: incomplete.** The run ended on `wall_timeout`
(`/mnt/2T/pcbdraft-boardbench-campaigns/ai-pilot-r4-luna-five-category-v3b-20260822/runs/sensor-01-tmp102-i2c-run-1/artifacts/failure.json:1`).
The retained IR explicitly leaves SDA and SCL unrouted, and independent DRC
finds six missing physical connections: both C1 pads, two SDA links, and two
SCL links
(`/mnt/2T/pcbdraft-boardbench-campaigns/ai-pilot-r4-luna-five-category-v3b-20260822/scores/sensor-01-tmp102-i2c-run-1/independent-validation/drc.json:23`).
It also places a JST mounting pad on the board edge and overlaps J1/R2
(same file `:163` and `:186`).

Required PCB changes (7):

1. Move J1 inward enough to clear its mounting pads while preserving cable exit.
2. Move R2 outside J1's courtyard.
3. Route SDA end-to-end.
4. Route SCL end-to-end.
5. Reconnect C1 to 3V3.
6. Reconnect C1 to GND with a short local return.
7. Connect or remove the isolated B.Cu GND fill, remove dangling stubs, and refill.

Solderability: the 0603 parts are conventional, but SOT-563 and the 1.0 mm JST-SH
connector should be treated as stencil/reflow assembly, not casual iron-only
work. DRC cannot judge paste process, connector cable-access after the move,
sensor thermal response, or whether the tiny pin-1 indications remain readable.

Cause tags: `infrastructure timeout`, `layout/router`. The time estimate assumes
the retained component placement is used as a starting point rather than
re-laying the board from scratch.

### 3. Power — `power-03-ap2112-sensor-feed-run-1`

**Verdict: needs changes.** Independent DRC has no copper-rule violations or
unconnected items, but it records an NC-pad schematic-parity warning
(`/mnt/2T/pcbdraft-boardbench-campaigns/ai-pilot-r4-luna-three-category-v3c-20260822/scores/power-03-ap2112-sensor-feed-run-1/independent-validation/drc.json:21`).
The retained placement puts C1 and C2 at x=11.5 mm and x=23.5 mm around U1 at
x=17.5 mm—about 6 mm center-to-center on each side—with multi-segment local
power/return paths. That does not meet the benchmark's explicit “physically
close, short loop” review requirement. The task also requires a defensible
board-dependent thermal assumption, which DRC cannot supply
(`/mnt/2T/pcbdraft-boardbench-private/v1-ai-pilot-r4/corpus.draft.json:3113`).

Required PCB changes (5):

1. Move C1 immediately adjacent to U1 VIN/GND.
2. Move C2 immediately adjacent to U1 VOUT/GND.
3. Reroute the VIN/EN input loop after moving C1.
4. Reroute the 3V3/output-return loop after moving C2.
5. Clear the artificial net assignment on U1 NC pin 4 and resynchronize the PCB.

Solderability: SOT-23-5 and 0603 are practical with reflow or a fine iron; the
JST-SH connector is best reflowed. The existing B.Cu GND fill is helpful, but
DRC cannot validate the claimed junction-temperature estimate, effective copper
heat-spreading area, transient stability, or cable accessibility.

Cause tags: `model reasoning`, `layout/router`, `compiler/materialization`.
The estimate assumes the 35 mm outline and connector positions remain.

### 4. Driver — `driver-02-dual-led-run-1`

**Verdict: pass.** The 28 × 24 mm board uses the requested THT header and 0603
parts, has no retained unrouted nets, and its independent DRC contains no
schematic-parity, unconnected, or board violations
(`/mnt/2T/pcbdraft-boardbench-campaigns/ai-pilot-r4-luna-three-category-v3c-20260822/scores/driver-02-dual-led-run-1/independent-validation/drc.json:17`).
The placement separates the two branches and keeps the header accessible; the
actual component/route inventory is retained in
`/mnt/2T/pcbdraft-boardbench-campaigns/ai-pilot-r4-luna-three-category-v3c-20260822/runs/driver-02-dual-led-run-1/artifacts/repository/projects/driver-02-dual-led-run-1-1d418cf5/design/design.pcbir.json:1`.

Required PCB changes: **0**. Allow 0–10 minutes for a final polarity/pin-1
visual check and manufacturing export. The through-hole header is easy to
solder; 0603 LEDs/resistors are hand-solderable with magnification or routine
reflow. DRC does not confirm the purchased LED's physical polarity marking,
assembly-house rotation convention, or final indicator visibility.

Cause tags: none for layout/manufacturing. No physical board has been powered.

### 5. Adapter — `adapter-01-uart-straight-run-1`

**Verdict: needs changes.** The two opposing 1×4 headers overlap: the inner pin-4
holes are effectively coincident, their courtyards overlap, and the GND route
crosses J2's TX pad, creating a GND/TX short. Independent DRC records the short
at
`/mnt/2T/pcbdraft-boardbench-campaigns/ai-pilot-r4-luna-three-category-v3c-20260822/scores/adapter-01-uart-straight-run-1/independent-validation/drc.json:139`,
the placement overlap at `:70`, and the hole collision at `:323`. The retained
IR also shows RX reduced to a 0.2 mm segment between the nearly coincident pads
(`/mnt/2T/pcbdraft-boardbench-campaigns/ai-pilot-r4-luna-three-category-v3c-20260822/runs/adapter-01-uart-straight-run-1/artifacts/repository/projects/adapter-01-uart-straight-run-1-d8d8836c/design/design.pcbir.json:1`).

Required PCB changes (6):

1. Reposition the two headers with valid courtyard and hole-to-hole spacing.
2. Reroute GND.
3. Reroute 3V3.
4. Reroute TX.
5. Reroute RX.
6. Add readable GND/3V3/TX/RX legends on both sides while preserving pin-1 cues.

Solderability: the selected THT headers are intrinsically easy to solder, but
the current overlapping holes make this revision physically impossible to
assemble. DRC does not decide whether the legends communicate “straight-through”
clearly enough to prevent field misconnection.

Cause tags: `model reasoning`, `layout/router`. The time estimate assumes the
25 mm outline is retained and both headers can be spaced within it.

### Cross-case patterns and highest-value fix

- Placement ignored real footprint extents in three cases: MCU edge/courtyard,
  sensor edge/courtyard, and adapter courtyard/hole overlap.
- Routing completion was not a reliable stopping condition: sensor retained six
  missing connections, while MCU and adapter retained real shorts.
- DRC-clean does not equal engineering-ready: the power board still needs local
  capacitor-loop and thermal review; the driver board is the only clean result
  that also passes this lean physical review.
- All five PCB files have no custom board-level `gr_text`; only footprint-native
  markings remain. This is purchase-blocking for the adapter's explicit clear
  pin-legend requirement and a non-blocking usability concern elsewhere.

**Single highest-value product fix:** make every placement/routing commit
footprint-extent-aware and gate it on immediate native DRC connectivity,
courtyard, board-edge, and hole-spacing checks; when a footprint moves, discard
or deterministically re-anchor its old copper before accepting the revision.
That one feedback loop directly targets the dominant failures in MCU, sensor,
and adapter.

## Files Found

- `/mnt/2T/pcbdraft-boardbench-private/v1-ai-pilot-r4/corpus.draft.json` — task
  manufacturing, assembly, and physical-review requirements.
- Each run's `design/design.pcbir.json` and `.kicad_pcb` — retained component
  placement, board outline, routes, vias, pours, and footprints.
- Each score's `independent-validation/drc.json` — independent KiCad 10.0.5 DRC
  evidence used for the findings above.
- MCU, power, and driver release directories — retained BOM/manufacturing
  outputs; sensor and adapter have no release output.
- Component-qualification records — all five cases report zero pad-mapping
  failures; this review therefore found no footprint-identity blocker.

## Related Specs

- `.trellis/spec/backend/flat-pcb-toolbox.md` — physical placement and routing
  are explicit model actions; retained copper must survive ordinary writes.
- `.trellis/spec/backend/quality-guidelines.md` — automated evidence must not be
  represented as physical validation.
- `.trellis/tasks/08-21-end-to-end-boardbench/prd.md` — DRC is necessary but not
  sufficient for manufacturability, solderability, or first-power evidence.

## Caveats / Not Found

- No generated board render/preview was retained for these five runs; review used
  the native PCB, IR geometry, independent DRC, footprints, and available BOMs.
- No fabrication-house acceptance, assembly inspection, power-on, rail
  measurement, firmware, or functional-test evidence exists. “Short risk” above
  means visible in the files only.
- Stock, lead time, price, stencil design, panelization, impedance, EMC, and
  regulatory compliance were not assessed.
