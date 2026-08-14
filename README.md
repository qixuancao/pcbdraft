<p align="center">
  <img src="docs/assets/brand/pcbdraft-mark-256.png" width="180" alt="PCBDraft mark">
</p>

<h1 align="center">PCBDraft</h1>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.zh-CN.md">简体中文</a> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.ko.md">한국어</a>
</p>

<p align="center"><strong>An open-source, local, agent-safe KiCad generator.</strong></p>

PCBDraft turns a reviewable circuit plan into a native KiCad project. Its
generic generator uses only the stock KiCad symbols and footprints installed on
your machine—no vendor libraries, supplier integrations, or KiCad plugins.

It generates a schematic, places components, makes a bounded routing attempt,
and keeps the request and semantic design beside the KiCad files. When a symbol,
route, ERC/DRC check, or export fails, PCBDraft reports the real failure and
retains the available work instead of claiming success.

## Quick start

Requirements: Linux, Python 3.11+, `uv`, and KiCad 10 with its standard symbol
and footprint packages.

    uv sync --extra dev
    scripts/prepare-kicad-environment.sh
    uv run pcbdraft doctor --json

Generate the included stock-library example:

    uv run pcbdraft agent-generate \
      examples/basic_stock_board/request.json \
      examples/basic_stock_board/circuit-plan.json \
      build/basic-stock-board

On the installed KiCad 10.0.5 environment, this example produces a routed board
with zero ERC violations, DRC violations, unconnected items, or schematic-parity
errors. The source files and circuit explanation are in
<a href="examples/basic_stock_board">examples/basic_stock_board</a>.
Two additional stock-library acceptance examples exercise a passive RC network
and explicit I2C pull-ups:
<a href="examples/rc_filter_board">examples/rc_filter_board</a> and
<a href="examples/i2c_pullup_adapter">examples/i2c_pullup_adapter</a>.

## Output

`build/basic-stock-board/` contains:

- `basic-stock-board.kicad_sch` — native KiCad schematic;
- `basic-stock-board.kicad_pcb` — native routed PCB;
- `basic-stock-board.kicad_pro` — KiCad project settings;
- `circuit-plan.json` and `design.pcbir.json` — editable plan and semantic IR;
- `parts.pcbdraft.json` and `component-qualification.json` — exact local
  part records, symbol/footprint pad-map evidence, and explicit datasheet/MPN
  qualification state;
- `requirements.pcbreq.json` — the retained generation request; and
- `project.pcbdraft.json` — hashes and generation details.

Open the `.kicad_pro` file in KiCad to inspect or continue editing the design.

## Conversational use

For model-assisted circuit planning, start the local browser app:

    uv run pcbdraft app --provider codex

Or launch the full-screen terminal conversation (the default command):

    uv run pcbdraft --provider codex

Describe the board once. The terminal queues a durable agent turn and streams
its requirement, planning, generation, preview, and validation activity while
remaining responsive. Unless you explicitly state a layer count, PCBDraft
chooses an initial stackup as part of the design attempt. Press Esc or use
`/stop` to request a stop before the next PCB tool starts. A reviewable circuit
plan is always retained; `/confirm` remains available for a manually staged or
recovered project, but it is not an extra step in the default terminal flow.
The terminal remembers only the last local project identifier and resumes it on
restart; it never stores prompts or provider credentials in the TUI session
record. Use `/review` for the retained plan and staged semantic diff, `/logs on`
for expanded activity, and `/retry` to explicitly rerun a recovered failed or
interrupted job. Recovery never replays work automatically.

For generic parts, “found in KiCad” is deliberately not reported as “qualified.”
The compiler verifies that every selected symbol pin maps to a real pad number
in the selected installed footprint, retains any KiCad datasheet locator as an
unverified reference, and marks manufacturer identity, ratings, lifecycle, and
package suitability for engineering review. Deterministic topology failures—
including supply/ground polarity, implausible rail sources, output contention,
reversed ground-referenced LEDs, missing per-line I2C pull-ups, and missing IC
bypass topology—can enter the same bounded repair loop; unknown datasheet or
human evidence never does.

The single conversation surface has a slash-command palette: type `/` to see
all commands, use arrow keys to select, Tab to complete, and Enter to complete
or run a command. `--workspace`, `--project`, and `--timeout` are also available
before the default launcher. Requests mentioning RF, mains, high voltage, high
power, medical, aviation, safety-critical, or other complex domains follow the
same generation path. PCBDraft may warn that it lacks domain-specific
validation, but the domain label itself does not reject the request.

The palette provides `/help`, `/new [name]`, `/projects`, `/open ID`, `/status`,
`/review`, `/logs [on|off]`,
`/model [auto|codex|deepseek-harness|openai-compatible|builtin]`, `/stop`,
`/retry`, `/confirm`, `/validate`, `/undo`, `/discard`, `/release`, and `/quit`.
`/model` reports the active planner/provider model when it is configured and
changes only this running application service; it never writes credentials or
provider configuration.

The offline `--provider builtin` extracts requirements but does not invent a
circuit. Use the included example without a provider, or configure a planning
provider for free-form requests. For scripts, `chat` remains explicitly
parameterized—for example, `uv run pcbdraft chat --new NAME --message TEXT
--json`; it does not open an interactive prompt.

## Optional DeepSeek Harness agent

The all-Python PCBDraft runtime is the primary product path. The DSH
integration is optional: it can supply external model orchestration while the
native CLI/TUI, event boundary, KiCad generation, and validation remain
independent.

To use Harness as the planner behind the native Python TUI:

    uv sync --extra harness
    DEEPSEEK_API_KEY=... uv run pcbdraft --provider deepseek-harness

The adapter sends prompts over a versioned stdin/stdout bridge, bounds time and
output, and validates every returned intent or plan again in PCBDraft. User
prompts and credentials are not placed in subprocess arguments or receipts.

Alternatively, let DeepSeek Harness host the agent and mount PCBDraft as
three constrained PCB tools. This profile exposes no general shell, filesystem,
browser, subagent, or code runtime:

    scripts/setup-deepseek-harness.sh
    scripts/run-pcbdraft-agent.sh 'Design a low-power sensor board'

Its isolated local profile and generated boards are Git-ignored. See
<a href="integrations/deepseek-harness/README.md">the integration guide</a> for
its tool boundary, credential handling, and reproducible verification commands.

## Plan and API commands

Search the installed KiCad symbol libraries:

    uv run pcbdraft symbols SHT31 --json

Compile a request and reviewed plan without generating native files:

    uv run pcbdraft agent-compile REQUEST.json PLAN.json \
      --ir-output design.pcbir.json \
      --parts-output parts.pcbdraft.json \
      --json

Generate the native project:

    uv run pcbdraft agent-generate REQUEST.json PLAN.json OUTPUT_DIR --json

The newline-delimited JSON-RPC API exposes `symbols.find`,
`agent.request.prepare`, `agent.plan.compile`, and `agent.project.generate`; see
<a href="docs/API.md">docs/API.md</a>.

## Limitations

- The router is bounded. If it cannot finish, the schematic and failed PCB
  attempt are retained and the unrouted nets are reported.
- ERC and DRC prove only the checks KiCad performed. They do not establish
  circuit function, electrical safety, regulatory compliance, RF/SI/PI,
  thermal behavior, or manufacturing fitness.
- Model-generated pin choices, values, topology, and layout still need review.
- PCBDraft accepts an agent-selected or user-specified positive layer count;
  the installed KiCad build decides whether the requested stackup can be generated.

Detailed architecture and historical validation material remain available in
<a href="docs/ARCHITECTURE.md">docs/ARCHITECTURE.md</a> and the other `docs/`
files. The evidence-based next milestones are in
<a href="docs/ROADMAP.md">docs/ROADMAP.md</a>. These documents are reference
material, not extra gates on ordinary generation.

## Tests

Run formatting, lint, unit/integration tests, and available real-KiCad tests:

    scripts/test.sh

Run the standalone KiCad smoke test:

    REAL_CODEX=0 scripts/smoke.sh

## License

Apache-2.0. See <a href="LICENSE">LICENSE</a> and <a href="NOTICE">NOTICE</a>.
