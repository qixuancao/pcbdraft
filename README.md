<p align="center">
  <img src="docs/assets/brand/copperwright-mark-256.png" width="180" alt="CopperWright copper PCB-trace W mark">
</p>

<h1 align="center">CopperWright</h1>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.zh-CN.md">简体中文</a> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.ko.md">한국어</a>
</p>

<p align="center"><strong>Conversational PCB design with deterministic KiCad engineering.</strong></p>

CopperWright is a local-first, Apache-2.0 application for turning a conversation
into a reviewable, validated, reversible KiCad project. Start a guided terminal
session with `copperwright chat`, or launch the loopback-only browser workshop with
`copperwright app`. KiCad remains the schematic/PCB, geometry, rule-checking, and
manufacturing backend; AI may interpret intent, while deterministic CopperWright
code owns parts, topology, placement, routing, output, validation, and release
identity.

The bounded v1 supports three real routed designs: ATtiny402/TMP102 I2C,
ATtiny402/BME280 SPI, and a 5 V-input ATtiny402 UART controller with an AP2112K
3.3 V LDO. Each passes applicable real KiCad ERC/DRC and CopperWright candidate
gates. None is called production-ready: qualified human review, live sourcing,
fabrication, bring-up, EMC, and measured physical results remain external gates.

> **Product status:** CopperWright 1.0.0 is a complete bounded application, not a
> general-purpose PCB autopilot. Its shared service, terminal/browser journeys,
> persistence, semantic edits, three profiles, and release path are exercised in
> the [product acceptance record](docs/PRODUCT_ACCEPTANCE.md). The older
> [R01–R44 report](docs/FINAL_REPORT_ZH.md) remains historical runtime evidence.

## What is implemented

- One authoritative application service shared by `copperwright chat` and
  `copperwright app`, with private persistent projects, conversations, decisions,
  jobs, structured events, restart recovery, and per-project concurrency locks.
- Focused clarification, a human-readable brief, assumptions, BOM, interfaces,
  constraints, scope decision, and explicit confirmation before engineering side
  effects.
- A responsive, keyboard-accessible local browser UI with progress/cancel/retry
  states, real schematic/PCB/3D previews, direct artifact paths, L0–L7 findings,
  open-in-KiCad, and candidate export.
- Authenticated local Codex, environment-configured OpenAI-compatible, and offline
  deterministic providers. Secrets are never accepted by the browser or stored in
  project conversations.
- Conversational semantic change preview/apply/discard/undo; current KiCad files
  remain untouched until a staged design passes candidate validation and the user
  confirms.
- Strict semantic circuit/PCB IR with typed interfaces, power domains,
  requirements, blocks, constraints, analyses, risk, and provenance.
- Deterministic canonical JSON and content hashes independent of input ordering.
- A CC0 trusted-part graph binding manufacturer/MPN, pins, symbols, footprints,
  ratings, lifecycle/source evidence, sourcing state, manufacturing contracts,
  and available models.
- Versioned, rule-validated reusable blocks whose declared parts, ports, evidence,
  and test references are checked against their deterministic implementations.
- Semantic change sets with preconditions, previews, semantic diffs, atomic
  publication, idempotency, conflict detection, backup, undo, and crash recovery.
- Requirements compilation, bounded placement optimization, deterministic
  multilayer A* routing, fine-pitch escape routing, filled reference planes,
  deterministic stitching vias, and native KiCad generation.
- Bidirectional KiCad synchronization for recognized footprint pose edits;
  topology, part, route, schematic, or rules drift fails closed.
- Honest L0–L7 validation states: `completed`, `not_applicable`, `unavailable`,
  `heuristic`, and `human_required`.
- Real KiCad ERC/DRC, schematic parity, BOM, placement, Gerber, drill, IPC-D-356,
  board statistics, PDF, SVG, render, and board-only STEP integration.
- Byte-reproducible content releases, timestamp normalization with original hashes
  retained in audit receipts, deterministic ZIPs, and an offline verifier.
- A versioned CLI, Python API, and bounded newline-delimited JSON-RPC 2.0 API.
- A 90-case independent CC0 error-injection corpus with detection, false-positive,
  repair, regression, repeatability, latency, and optional blinded model metrics.
- The reviewer/safe-patcher workflow remains available for unmanaged projects
  through bounded text replacement; managed semantic projects use typed change
  sets.

See [specification traceability](docs/SPEC_TRACEABILITY.md) for the requirement,
implementation, and test mapping. Exact product verification and remaining gates
are recorded in the [v1 Chinese report](docs/PRODUCT_REPORT_ZH.md); the historical
runtime report is preserved unchanged.

## Supported scope

The bundled profiles are intentionally narrow and explicit:

| Contract | Supported now |
|---|---|
| I2C | `low_voltage_i2c_controller_v1`: regulated 3.3 V input, ATtiny402, TMP102, Qwiic, UPDI, LED |
| SPI | `low_voltage_spi_environment_v1`: regulated 3.3 V input, ATtiny402, board-local BME280 four-wire SPI mode 0 at 1 MHz, UPDI |
| UART/LDO | `low_voltage_uart_ldo_controller_v1`: regulated 5 V input, AP2112K 3.3 V LDO, ATtiny402, 3.3 V CMOS UART, UPDI, LED |
| Copper stackup | 2 or 4 layers |
| Board envelope | 45 mm × 30 mm |
| Use | prototype or non-safety-critical low-voltage sensing/control |
| KiCad | major 10; exact acceptance tested on 10.0.5 |
| Python | 3.11+ |

USB 2.0 and buck conversion are recognized but remain unsupported because v1 does
not have a complete locally verified electrical/layout chain for them. RS-232
voltage levels are not the supported 3.3 V CMOS UART. Other board dimensions are
not silently applied to the fixed, verified placement/routing contract. DDR,
PCIe, SerDes, RF,
mains, high power, medical, aviation, and safety-critical work is explicitly
rejected rather than silently approximated.

KiCad itself cannot reload an odd three-copper-layer board produced through its
KiCad 10 Python API on the tested host, so the native contract is the common 2/4
layer subset of the analysis's 2–4-layer target.

## Requirements

- Linux and `uv`
- Python 3.11 or newer
- KiCad 10.x CLI, symbols, footprints, and system `pcbnew` Python bindings
- Git for diagnostics and development
- Optional: an authenticated Codex CLI for conversational intent, `review`,
  `patch`, and the live model-consistency benchmark
- Optional: an OpenAI-compatible Chat Completions endpoint configured only through
  `COPPERWRIGHT_OPENAI_BASE_URL`, `COPPERWRIGHT_OPENAI_MODEL`, and an API-key
  environment variable

KiCad 10.0.5 is the exact locally accepted version. Other 10.x versions are
reported as same-major but not exact-tested; other majors fail closed. Ubuntu
users can use KiCad's official `ppa:kicad/kicad-10.0-releases` instructions.

## Install

For a repository checkout:

```bash
scripts/deploy.sh
scripts/prepare-kicad-environment.sh
uv run copperwright doctor --json
```

For an isolated install from a built wheel:

```bash
uv build
uv venv /tmp/copperwright-venv
uv pip install --python /tmp/copperwright-venv/bin/python dist/*.whl
/tmp/copperwright-venv/bin/copperwright --version
```

`doctor.ok` means the deterministic core is usable. No paid or private credential
is needed for the offline provider, generation, validation, release, verification,
or deterministic benchmarking.

## Conversational quick start

Launch the browser application (loopback only by design):

```bash
copperwright app
# open http://127.0.0.1:8765 if the browser does not open automatically
```

Create a project, answer the layer question, review the brief/BOM/constraints,
then confirm generation. Ask “Change this board to 4 layers” to get a validated
semantic diff before choosing Apply; Undo restores the exact prior authoritative
state. Export candidate produces and offline-verifies the manufacturing bundle.

The same lifecycle works over SSH:

```bash
copperwright chat
# /new Greenhouse sensor
# Describe: Create a BME280 SPI environmental sensor controller
# Reply: 2 layers
# /confirm
# Change this board to 4 layers
# /confirm
# /undo
# /release
```

Scriptable automation can use `--new`, `--project`, `--message`, `--yes`,
`--undo`, `--validate`, `--release`, `--list`, and `--json`; see
`copperwright chat --help`.

![CopperWright browser project visuals](artifacts/product-e2e/copperwright-app-visuals.png)

## Providers and secrets

`--provider auto` prefers an installed authenticated Codex CLI, then a configured
OpenAI-compatible endpoint, then the offline classifier. Select explicitly with
`--provider codex`, `--provider openai-compatible`, or `--provider builtin`.

```bash
# Authenticate outside CopperWright; no token is copied into the project.
codex login
copperwright app --provider codex

# Or launch with an OpenAI-compatible endpoint. Do not put this in a project file.
COPPERWRIGHT_OPENAI_BASE_URL=https://provider.example/v1 \
COPPERWRIGHT_OPENAI_MODEL=model-id \
OPENAI_API_KEY='<secret>' \
copperwright app --provider openai-compatible
```

The browser has no credential input. Model output is schema-constrained, bounded,
normalized, scope-checked, and cannot cause engineering side effects before user
confirmation. Provider logic never selects parts or edits KiCad.

## Deterministic runtime quick start

All output paths are create-only. Use new paths or remove prior disposable output
yourself.

```bash
copperwright compile \
  examples/attiny_sensor_controller/requirements.json \
  --output /tmp/controller.pcbir.json --json

copperwright generate \
  examples/attiny_sensor_controller/requirements.json \
  /tmp/controller --json

copperwright inspect /tmp/controller --json
copperwright validate /tmp/controller --output /tmp/controller-validation --json
copperwright release /tmp/controller /tmp/controller-release --json
copperwright release-verify /tmp/controller-release --json
```

The generated project contains the source requirements, semantic IR, native
`.kicad_sch/.kicad_pcb/.kicad_pro`, an isolated-worker receipt, semantic snapshots,
native pad-edge constraint measurements, routing/reference-plane evidence, and a
hash manifest. The release contains cross-checked manufacturing files,
normalized validation evidence, execution receipts, a content manifest, and a
deterministic ZIP.

The committed reference outputs are in:

- [`examples/product_profiles`](examples/product_profiles) — all three current v1
  profiles with native projects, validation, and previews
- [`examples/attiny_sensor_controller`](examples/attiny_sensor_controller)
- [`artifacts/product-e2e`](artifacts/product-e2e) — clean-HOME browser/chat
  product-flow evidence and screenshots
- [`artifacts/acceptance/release`](artifacts/acceptance/release)
- [`artifacts/acceptance/review`](artifacts/acceptance/review)
- [`artifacts/benchmark/benchmark-20260812.json`](artifacts/benchmark/benchmark-20260812.json)

## Semantic transactions

Agents should emit a typed `copperwright-change-set`, then use the transaction
commands rather than editing KiCad text:

```bash
copperwright semantic-preview design.pcbir.json change-set.json --output /tmp/tx
copperwright semantic-apply /tmp/tx
copperwright semantic-undo /tmp/tx
copperwright semantic-recover /tmp/tx
```

Operations cover requirements, components, nets/endpoints, constraints, board
rules, and metadata. Each operation carries a reason and can carry field-level
expectations. The runtime verifies the base hash, applies every operation in
memory, validates the resulting IR, writes a semantic diff, and only then creates
staging. Publication rechecks source and staged hashes under a resource lock.

To import a reviewed native KiCad footprint move:

```bash
copperwright sync /tmp/controller --json
copperwright sync /tmp/controller --apply --json
copperwright sync-undo /tmp/.copperwright-transactions/sync-...
```

Only pose changes are imported. Unknown board bytes, footprint changes, new or
removed components, routes, net mappings, schematic changes, and project-rule
changes are rejected rather than lost.

## Validation and evidence

The validation ladder is reported per check and per level:

| Level | Runtime evidence |
|---|---|
| L0 | manifest/hash integrity, semantic parsing, native KiCad parsing |
| L1 | canonical part, pin, footprint/pad, connectivity, schematic/PCB parity |
| L2 | real KiCad ERC and DRC reports |
| L3 | interface, decoupling, pull-up, current, placement, routing, and intent rules |
| L4 | lifecycle/BOM/manufacturing contracts, DFM proxies, release cross-checks, and external sourcing/fabricator evidence |
| L5 | deterministic DC/power checks where applicable; SI/PI/thermal/EMI unavailable unless evidence exists |
| L6 | attributed qualified human review imported as external evidence |
| L7 | attributed board serial/test-plan/result artifacts imported as external evidence |

Candidate readiness requires all blocking locally implementable gates. Production
readiness additionally requires valid L4 sourcing/fabricator, L6 review, and L7
physical evidence. The runtime copies and
hashes supplied evidence but labels it
`externally_supplied_not_independently_verified`; it never self-signs it.

The I2C profile bounds the bus to 200 pF with 4.7 kOhm pull-ups and no external
pull-ups. The SPI profile fixes one board-local BME280 to four-wire mode 0 at 1 MHz
with a verified CS pull-up. The UART/LDO profile enforces AP2112K input/output,
load, bypass, stability, enable, and 3.3 V CMOS 8-N-1 (not RS-232) contracts.
Decoupling distance is measured between relevant native copper-pad rectangles;
all routing contracts require a filled GND reference plane and deterministic GND
stitching vias.

Managed-project reviews receive strictly parsed requirements, IR, trusted part and
block records, generation receipts, and native semantic exports. If any tracked file
drifts, those intent records are labeled non-authoritative rather than silently used.
The model response remains a heuristic review and cannot satisfy L6.

## CLI and agent API

Run `copperwright --help` and `copperwright COMMAND --help` for the authoritative CLI.
Major command groups are:

- product: `chat`, `app`
- design: `compile`, `generate`, `inspect`, `parts`
- validation/release: `validate`, `release`, `release-verify`, `evidence-record`
- synchronization/transactions: `sync`, `sync-undo`, `sync-recover`,
  `semantic-preview`, `semantic-apply`, `semantic-undo`, `semantic-recover`
- evaluation: `benchmark`
- unmanaged compatibility: `review`, `patch`, `apply`
- automation: `api`

The API reads one JSON-RPC request per line from stdin and writes one response per
line to stdout. Start with `runtime.capabilities`; it is the machine-readable
source of scope and method support.

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"runtime.capabilities","params":{}}' \
  | copperwright api
```

The process accepts at most 10,000 requests and 4 MiB per request. Parameter sets
are exact, paths and numeric bounds are validated, and protocol errors preserve
JSON-RPC framing. See [API reference](docs/API.md).

## Benchmark

Run the deterministic corpus without a model or network:

```bash
scripts/benchmark.sh
# or
copperwright benchmark /tmp/copperwright-benchmark.json --repetitions 5 --json
```

An explicitly requested live model consistency run uses two or more blinded,
isolated repetitions:

```bash
MODEL_RUNS=2 scripts/benchmark.sh
```

Current measured results and limitations are in [BENCHMARK.md](BENCHMARK.md).
The benchmark is a regression corpus, not a claim about all PCB failures.

## Product identity

CopperWright is distributed, imported, and invoked as `copperwright`; it is the
only public CLI and Python package. Its on-disk and protocol identifiers use the
same namespace, including `copperwright-*` schemas,
`project.copperwright.json`, `.copperwright-*` transaction/lock directories, and
the `COPPERWRIGHT_*` test/config namespace. Checked-in engineering receipts and
benchmark artifacts use these CopperWright identifiers as well.

## Development and release checks

```bash
scripts/test.sh
scripts/smoke.sh                 # real KiCad demo; no model by default
scripts/python-matrix.sh         # Python 3.11–3.14 core matrix
scripts/chat-e2e.sh              # scriptable terminal product journey
uv run python scripts/browser-e2e.py  # real Firefox journey and restart
uv run python scripts/generate-product-examples.py
scripts/release-check.sh         # full clean-install product/release hard gate
```

`scripts/test.sh` automatically runs real KiCad tests when the compatible local
toolchain is present and otherwise records skips through `unittest`. CI definitions
are in [`.github/workflows/ci.yml`](.github/workflows/ci.yml). See
[development guide](docs/DEVELOPMENT.md) for corpus and profile contribution rules.

## Security model

Project content, model output, metadata, archives, and file names are untrusted.
The runtime uses strict schemas, byte/member/depth limits, non-finite-number
rejection, non-login subprocesses, time/output bounds, create-only outputs,
canonical paths, symlink/hardlink/special-file rejection, file manifests,
resource locks, atomic writes, and post-write validation.

The isolated `pcbnew` worker receives an internally generated bounded JSON job and
runs system Python with `-I`; it does not import project code. Codex review uses a
read-only tool policy, disables project configuration, hooks, multi-agent, network,
and privileged tools, and passes prompts on stdin. That policy is not an OS sandbox:
run hostile projects in a container/VM and only send data you are authorized to
disclose. See [SECURITY.md](SECURITY.md).

## Licensing

Runtime source and documentation are Apache-2.0; see [LICENSE](LICENSE). The bundled
part/block catalogs and independent benchmark data are CC0-1.0 as documented in
[`src/copperwright/data/LICENSE.md`](src/copperwright/data/LICENSE.md). Generated example
designs use official KiCad library material under the KiCad libraries' CC-BY-SA 4.0
design exception. Dependency and attribution notes are in [NOTICE](NOTICE).
The bounded public-project study and actual reuse decisions are recorded in
[`docs/OPEN_SOURCE_REUSE.md`](docs/OPEN_SOURCE_REUSE.md); no studied project code or
assets were copied.

No warranty or engineering certification is provided. Always perform qualified
review appropriate to the product, jurisdiction, and risk.
