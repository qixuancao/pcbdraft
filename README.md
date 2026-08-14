<p align="center">
  <img src="docs/assets/brand/copperwright-mark-256.png" width="180" alt="CopperWright mark">
</p>

<h1 align="center">CopperWright</h1>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.zh-CN.md">简体中文</a> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.ko.md">한국어</a>
</p>

<p align="center"><strong>An open-source, local, agent-safe KiCad generator.</strong></p>

CopperWright turns a reviewable circuit plan into a native KiCad project. Its
generic generator uses only the stock KiCad symbols and footprints installed on
your machine—no vendor libraries, supplier integrations, or KiCad plugins.

It generates a schematic, places components, makes a bounded routing attempt,
and keeps the request and semantic design beside the KiCad files. When a symbol,
route, ERC/DRC check, or export fails, CopperWright reports the real failure and
retains the available work instead of claiming success.

## Quick start

Requirements: Linux, Python 3.11+, `uv`, and KiCad 10 with its standard symbol
and footprint packages.

    uv sync --extra dev
    scripts/prepare-kicad-environment.sh
    uv run copperwright doctor --json

Generate the included stock-library example:

    uv run copperwright agent-generate \
      examples/basic_stock_board/request.json \
      examples/basic_stock_board/circuit-plan.json \
      build/basic-stock-board

On the installed KiCad 10.0.5 environment, this example produces a routed board
with zero ERC violations, DRC violations, unconnected items, or schematic-parity
errors. The source files and circuit explanation are in
<a href="examples/basic_stock_board">examples/basic_stock_board</a>.

## Output

`build/basic-stock-board/` contains:

- `basic-stock-board.kicad_sch` — native KiCad schematic;
- `basic-stock-board.kicad_pcb` — native routed PCB;
- `basic-stock-board.kicad_pro` — KiCad project settings;
- `circuit-plan.json` and `design.pcbir.json` — editable plan and semantic IR;
- `requirements.pcbreq.json` — the retained generation request; and
- `project.copperwright.json` — hashes and generation details.

Open the `.kicad_pro` file in KiCad to inspect or continue editing the design.

## Conversational use

For model-assisted circuit planning, start the local browser app:

    uv run copperwright app --provider codex

Or use the compact terminal client:

    uv run copperwright agent --provider codex

Describe the board, review the proposed stock KiCad parts and nets, then confirm
generation. Requests mentioning RF, mains, high voltage, high power, medical,
aviation, safety-critical, or other complex domains follow the same generation
path. CopperWright may warn that it lacks domain-specific validation, but the
domain label itself does not reject the request.

The offline `--provider builtin` extracts requirements but does not invent a
circuit. Use the included example without a provider, or configure a planning
provider for free-form requests.

## Plan and API commands

Search the installed KiCad symbol libraries:

    uv run copperwright symbols SHT31 --json

Compile a request and reviewed plan without generating native files:

    uv run copperwright agent-compile REQUEST.json PLAN.json \
      --ir-output design.pcbir.json \
      --parts-output parts.copperwright.json \
      --json

Generate the native project:

    uv run copperwright agent-generate REQUEST.json PLAN.json OUTPUT_DIR --json

The newline-delimited JSON-RPC API exposes `symbols.find`,
`agent.plan.compile`, and `agent.project.generate`; see
<a href="docs/API.md">docs/API.md</a>.

## Limitations

- The router is bounded. If it cannot finish, the schematic and failed PCB
  attempt are retained and the unrouted nets are reported.
- ERC and DRC prove only the checks KiCad performed. They do not establish
  circuit function, electrical safety, regulatory compliance, RF/SI/PI,
  thermal behavior, or manufacturing fitness.
- Model-generated pin choices, values, topology, and layout still need review.
- CopperWright currently targets 2- and 4-layer boards on KiCad 10.

Detailed architecture and historical validation material remain available in
<a href="docs/ARCHITECTURE.md">docs/ARCHITECTURE.md</a> and the other `docs/`
files. They are reference material, not extra gates on ordinary generation.

## Tests

Run formatting, lint, unit/integration tests, and available real-KiCad tests:

    scripts/test.sh

Run the standalone KiCad smoke test:

    REAL_CODEX=0 scripts/smoke.sh

## License

Apache-2.0. See <a href="LICENSE">LICENSE</a> and <a href="NOTICE">NOTICE</a>.
