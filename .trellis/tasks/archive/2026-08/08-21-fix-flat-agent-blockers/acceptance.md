# Acceptance record

## Real CLI run

- Date: 2026-08-21 (Asia/Shanghai)
- Installed command: `uv tool install --force /mnt/2T/pcbdraft`
- Runtime: global `pcbdraft` 0.1.0, KiCad CLI/pcbnew 10.0.5,
  `openai-codex` OAuth with `gpt-5.6-luna`
- Isolated workspace: `/tmp/pcbdraft-cli-acceptance.w3GuX2`
- Seeded old project: `old-project-sentinel-do-not-use-6f7feffc`
- Accepted new project:
  `green-5mm-led-330r-0805-3v3-prototype-6a8d1564`
- Hermes session: `20260821_101628_f45b59`
- Trace: `/tmp/pcbdraft-cli-acceptance.w3GuX2/agent-trace.jsonl`

The Agent registered project-local generic records for `Device:LED` with
`LED_THT:LED_D5.0mm` and `Device:R` with
`Resistor_SMD:R_0805_2012Metric`, then used individual flat tools to add the
block, D1, R1, three nets, four pin connections, a 30 mm by 20 mm outline, two
fixed footprint poses, three selected-net route actions, and standalone checks.
The persisted project ended at application revision 22 and design revision 18.

## Trace policy and project isolation

- 38 model responses: 37 contained exactly one `pcb_*` call; the final response
  contained no tool call. Maximum calls in one response was one.
- 37 tool dispatches were all individually named `pcb_*` tools.
- No `pcb_list_projects` or `pcb_open_project` dispatch occurred.
- Neither the old project ID nor its sentinel text occurred in the accepted
  session trace.
- Installed symbol lookup uses the lightweight ID-only search path; a cold
  exact `Device:R` smoke lookup completed in about 1.01 seconds and ranked the
  exact identifier first.

## Persisted design

- Components: D1 (`project.green_led_d5`) and R1
  (`project.resistor_330r_0805`), both explicitly fixed on the front side.
- Topology: `3V3 -> R1.1`, `R1.2 -> D1.2` on `LED_A`, and `D1.1 -> GND`.
- Native intent: rectangular outline, both poses, four retained F.Cu segments
  for `LED_A`, no vias, no unrouted nets; generated PCB also contains the
  retained GND reference plane on B.Cu.
- Manifest SHA-256 values match the actual board, schematic, project, IR,
  catalog, requirements, and worker receipt files.
- The 22 event record sequence is contiguous. Eighteen applied design
  transaction receipts are present, plus a factual failed receipt for the
  deliberately invalid first LED pin-name registration attempt.

## Independent KiCad checks

KiCad CLI was run directly against the generated files, outside the Agent tool
results:

- PCB DRC: zero violations and zero unconnected items.
- Schematic ERC: two warnings, both `isolated_pin_label`: `3V3` and `GND` each
  terminate on one component pin because this two-component prototype has no
  supply connector. There are no ERC errors.

Independent reports are retained at
`/tmp/pcbdraft-cli-acceptance.w3GuX2/independent-check/`.

## Focused quality gates

- Independent Trellis review: 114 focused tests passed after deterministic
  fixes; targeted post-search compatibility tests also passed.
- Final `git diff --check`: passed.
- Ruff check: passed; all 23 changed/new Python files are formatted.
- Mypy: passed for all 89 source files.
- A final repeated nine-module unit-test command was stopped at the repository's
  90-second quick-check budget with no observed failure. It was not extended or
  represented as complete; the earlier completed focused runs and the real CLI
  acceptance remain the completion evidence.
