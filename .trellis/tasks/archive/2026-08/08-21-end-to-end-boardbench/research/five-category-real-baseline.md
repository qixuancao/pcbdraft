# Five-category real-model baseline

Date: 2026-08-22  
Generation model: `openai-codex / gpt-5.6-luna`  
Evaluator: `boardbench-evaluator-v3`

## Scope

This is the first one-run-per-category BoardBench baseline. Every attempt began
from the private natural-language requirement and used the real PCBDraft agent;
no structured `CircuitPlan` was supplied. The five results span two campaigns
because two evaluator write-side effects were found and fixed during the smoke:

- `ai-pilot-r4-luna-five-category-v3b-20260822`: MCU and sensor
- `ai-pilot-r4-luna-three-category-v3c-20260822`: power, driver, and adapter

The interrupted campaigns were preserved rather than silently repaired or
retried.

## Results

| Category | Run | Automatic result | Codex electrical review | Codex PCB review | Estimated distinct changes | Estimated engineer time |
|---|---|---|---|---|---:|---:|
| MCU | `mcu-01-attiny-updi-run-1` | fail | needs changes | needs changes | 5 | 35–65 min |
| Sensor | `sensor-01-tmp102-i2c-run-1` | fail; wall timeout | incomplete | incomplete | 9 | 40–70 min |
| Power | `power-03-ap2112-sensor-feed-run-1` | fail | needs changes | needs changes | 9 | 40–70 min |
| Driver | `driver-02-dual-led-run-1` | unknown | pass | pass | 0 | 5–20 min |
| Adapter | `adapter-01-uart-straight-run-1` | fail | needs changes | needs changes | 6 | 30–55 min |

Automatic headline: **0 pass, 1 unknown, 4 fail**. Engineering headline:
**one orderable candidate before manufacturing export (dual LED), three need
changes, and one is incomplete**. Approximately 29 distinct corrections and
150–280 engineer minutes remain across the five boards. Counts deduplicate the
power board's NC correction, which appears in both schematic and PCB review.

No board was fabricated, assembled, powered, or functionally tested. First-power
success is therefore unknown for all five.

## Automatic metric summary

| Metric | Pass | Fail | Unknown |
|---|---:|---:|---:|
| Complete KiCad project | 5 | 0 | 0 |
| Installed parts and footprints | 5 | 0 | 0 |
| Reference topology | 2 | 3 | 0 |
| ERC | 3 | 2 | 0 |
| DRC | 2 | 3 | 0 |
| Required support circuits | 4 | 1 | 0 |
| Ratings | 2 | 1 | 2 |
| False-completion check | 4 | 0 | 1 |

No model response falsely claimed that a known failing board was ready to
order. Four attempts explicitly retained a release caveat; the timed-out sensor
run produced no final response.

## What the engineering review changed

- The sensor's automatic SDA/SCL/VCC topology failure is not a real functional
  wiring defect. The evaluator compared the pin numbers of symmetric resistors
  literally. The retained schematic is coherent, but the run timed out with
  unfinished routing and missing ERC power-source annotations.
- The dual-LED driver is electrically and physically clean in the retained
  files. Its automatic result is `unknown` only because the declared input
  voltage was not available as machine-readable operating evidence.
- The MCU has one real schematic defect: the UPDI header's UPDI and VTREF pin
  order is swapped. It also has a file-evident GND-to-3V3 short.
- The power topology is broadly sound, but NC pins were represented as one-pin
  nets, voltage metadata was wrong, the thermal assumption was unsupported,
  and the decoupling loops were not physically tight.
- The UART schematic is correct, but overlapping header transforms and stale
  copper created a real GND-to-TX short and an unusable RX route.

Three of five boards need no functional schematic rewiring: sensor, driver, and
adapter. Only the driver needs no PCB correction.

## Efficiency

Across the five attempts:

- Wall time: 2,800 seconds (46 min 40 sec)
- Model requests: 348
- PCB tool calls: 360
- Input tokens: 736,435
- Output tokens: 33,085
- Reasoning tokens: 11,822
- Provider errors: 0
- Dollar cost: unavailable because successful runs used subscription-included
  Codex access; the timed-out run did not produce a complete cost receipt

## Product findings

The main product bottleneck is now concrete: project creation, real component
resolution, and schematic materialization are comparatively reliable, while
placement and routing are not reliably footprint-transform-aware or DRC-gated.
That caused shorts, overlaps, edge violations, stale copper, or timeout loops in
the MCU, sensor, and adapter cases.

Highest-value next fix:

1. Derive route endpoints from materialized pad coordinates after every
   footprint transform.
2. Reject placement/routing commits that introduce shorts, unrouted items,
   courtyard overlap, board-edge violations, or hole collision.
3. When a footprint moves, invalidate or deterministically re-anchor its old
   copper before accepting the revision.

Secondary evaluation/tooling fixes:

- Treat symmetric two-terminal passive pins as interchangeable in topology
  matching.
- Persist operating voltage/current values in machine-readable design evidence.
- Represent true no-connect pins without creating one-pin nets.

## Infrastructure findings fixed during the smoke

- Independent scoring no longer refreshes retained project lock files.
- Independent KiCad validation now runs on a private temporary project copy, so
  KiCad preference files and other project-side effects cannot alter the sealed
  run.
- The focused evaluator suite passes 42 tests after these fixes.

## Review artifacts

- `five-category-electrical-review.md`
- `five-category-pcb-review.md`

Both are Codex desk reviews of the actual retained KiCad projects. Neither is a
substitute for fabrication or bench evidence.

## Follow-up: official v4 60-minute campaign

The 900-second default was too short for a real PCB-design agent and produced
false infrastructure failures while the trace was still advancing. New
campaigns now default to a 3,600-second hard fail-safe, retain the separate
500-tool-call budget, and accept an explicit `--wall-timeout-seconds` at
creation. Existing campaign manifests remain unchanged.

The official continuation campaign is
`ai-pilot-r4-luna-v4-60m-baseline-20260822` (60 frozen runs, Luna, evaluator
v4, 3,600-second wall limit).

Its first completed run, `sensor-01-tmp102-i2c-run-1`, returned normally after
787 seconds instead of being killed. V4 correctly matched both pull-up
resistors with one consistent 1/2 pin swap and passed every reference-topology
predicate. The run still failed overall for genuine ERC and DRC findings,
including three copper-clearance errors. The Agent explicitly reported that the
board was not ready to order.
