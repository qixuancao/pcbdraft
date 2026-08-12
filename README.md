# PCB Agent Runtime

`pcb-agent-runtime` is an open-source, model-independent runtime for turning
bounded electronic requirements into reviewable, validated, reversible KiCad
projects. KiCad remains the schematic/PCB, geometry, rule-checking, and
manufacturing backend; the runtime adds semantic intent, trusted part contracts,
transactions, deterministic algorithms, evidence gates, and an agent-facing API.

The checked-in acceptance design is a real routed ATtiny402/TMP102 controller.
Its KiCad ERC and DRC are clean and its manufacturing candidate is reproducible,
but it is deliberately **not** called production-ready: qualified human review,
live sourcing, fabrication, bring-up, EMC, and measured physical results remain
external gates.

## What is implemented

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
- The original reviewer/safe-patcher workflow remains available for unmanaged
  projects, but raw text replacement is a legacy compatibility path, not the
  primary mutation model.

See [specification traceability](docs/SPEC_TRACEABILITY.md) for the requirement,
implementation, and test mapping.

## Supported scope

The bundled generator profile is intentionally narrow and explicit:

| Contract | Supported now |
|---|---|
| Profile | `low_voltage_i2c_controller_v1` |
| Circuit | externally regulated 3.3 V ATtiny402 + TMP102 + I2C/Qwiic + UPDI + LED |
| Copper stackup | 2 or 4 layers |
| Use | prototype or non-safety-critical low-voltage sensing/control |
| KiCad | major 10; exact acceptance tested on 10.0.5 |
| Python | 3.11+ |

SPI, UART, basic USB 2.0, LDO, and simple buck are recognized policy domains but
do not yet have bundled generation profiles. A request naming them is rejected
before generation instead of silently being mapped to the I2C fixture. DDR,
PCIe, SerDes, RF, mains, high power, medical, aviation, and safety-critical work
is explicitly rejected by the automated scope gate.

KiCad itself cannot reload an odd three-copper-layer board produced through its
KiCad 10 Python API on the tested host, so the native contract is the common 2/4
layer subset of the analysis's 2–4-layer target.

## Requirements

- Linux and `uv`
- Python 3.11 or newer
- KiCad 10.x CLI, symbols, footprints, and system `pcbnew` Python bindings
- Git for diagnostics and development
- Optional: an authenticated Codex CLI for `review`, legacy `patch`, and the live
  model-consistency benchmark

KiCad 10.0.5 is the exact locally accepted version. Other 10.x versions are
reported as same-major but not exact-tested; other majors fail closed. Ubuntu
users can use KiCad's official `ppa:kicad/kicad-10.0-releases` instructions.

## Install

For a repository checkout:

```bash
scripts/deploy.sh
uv run pcb-agent doctor --json
```

For an isolated install from a built wheel:

```bash
uv build
uv venv /tmp/pcb-agent-venv
uv pip install --python /tmp/pcb-agent-venv/bin/python dist/*.whl
/tmp/pcb-agent-venv/bin/pcb-agent --version
```

`doctor.ok` means the deterministic core is usable. Codex availability is
reported separately as `ai_review_available`; no paid or private credential is
needed for generation, validation, release, verification, or deterministic
benchmarking.

## End-to-end quick start

All output paths are create-only. Use new paths or remove prior disposable output
yourself.

```bash
pcb-agent compile \
  examples/attiny_sensor_controller/requirements.json \
  --output /tmp/controller.pcbir.json --json

pcb-agent generate \
  examples/attiny_sensor_controller/requirements.json \
  /tmp/controller --json

pcb-agent inspect /tmp/controller --json
pcb-agent validate /tmp/controller --output /tmp/controller-validation --json
pcb-agent release /tmp/controller /tmp/controller-release --json
pcb-agent release-verify /tmp/controller-release --json
```

The generated project contains the source requirements, semantic IR, native
`.kicad_sch/.kicad_pcb/.kicad_pro`, an isolated-worker receipt, semantic snapshots,
native pad-edge constraint measurements, routing/reference-plane evidence, and a
hash manifest. The release contains cross-checked manufacturing files,
normalized validation evidence, execution receipts, a content manifest, and a
deterministic ZIP.

The committed reference outputs are in:

- [`examples/attiny_sensor_controller`](examples/attiny_sensor_controller)
- [`artifacts/acceptance/release`](artifacts/acceptance/release)
- [`artifacts/acceptance/review`](artifacts/acceptance/review)
- [`artifacts/benchmark/benchmark-20260812.json`](artifacts/benchmark/benchmark-20260812.json)

## Semantic transactions

Agents should emit a typed `pcb-agent-change-set`, then use the transaction
commands rather than editing KiCad text:

```bash
pcb-agent semantic-preview design.pcbir.json change-set.json --output /tmp/tx
pcb-agent semantic-apply /tmp/tx
pcb-agent semantic-undo /tmp/tx
pcb-agent semantic-recover /tmp/tx
```

Operations cover requirements, components, nets/endpoints, constraints, board
rules, and metadata. Each operation carries a reason and can carry field-level
expectations. The runtime verifies the base hash, applies every operation in
memory, validates the resulting IR, writes a semantic diff, and only then creates
staging. Publication rechecks source and staged hashes under a resource lock.

To import a reviewed native KiCad footprint move:

```bash
pcb-agent sync /tmp/controller --json
pcb-agent sync /tmp/controller --apply --json
pcb-agent sync-undo /tmp/.pcb-agent-transactions/sync-...
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

For the bundled profile, the power envelope is one simultaneous maximum contract
(`3.465 V × 0.1 A = 0.3465 W`, a +5% source ceiling with margin below the
sensor's 3.6 V operating limit), I2C is bounded to 200 pF with 4.7 kOhm pull-ups and
no external pull-ups, UPDI VTREF is sense-only, and decoupling distance is measured
between relevant native copper-pad rectangles. The I2C routing contract requires a
filled GND reference plane and at least two deterministic GND stitching vias.

Managed-project reviews receive strictly parsed requirements, IR, trusted part and
block records, generation receipts, and native semantic exports. If any tracked file
drifts, those intent records are labeled non-authoritative rather than silently used.
The model response remains a heuristic review and cannot satisfy L6.

## CLI and agent API

Run `pcb-agent --help` and `pcb-agent COMMAND --help` for the authoritative CLI.
Major command groups are:

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
  | pcb-agent api
```

The process accepts at most 10,000 requests and 4 MiB per request. Parameter sets
are exact, paths and numeric bounds are validated, and protocol errors preserve
JSON-RPC framing. See [API reference](docs/API.md).

## Benchmark

Run the deterministic corpus without a model or network:

```bash
scripts/benchmark.sh
# or
pcb-agent benchmark /tmp/pcb-agent-benchmark.json --repetitions 5 --json
```

An explicitly requested live model consistency run uses two or more blinded,
isolated repetitions:

```bash
MODEL_RUNS=2 scripts/benchmark.sh
```

Current measured results and limitations are in [BENCHMARK.md](BENCHMARK.md).
The benchmark is a regression corpus, not a claim about all PCB failures.

## Development and release checks

```bash
scripts/test.sh
scripts/smoke.sh                 # real KiCad demo; no model by default
scripts/compatibility.sh         # Python 3.11–3.14 core matrix
scripts/release-check.sh         # tests, wheel/sdist, clean install, E2E release
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
[`src/pcb_agent/data/LICENSE.md`](src/pcb_agent/data/LICENSE.md). Generated example
designs use official KiCad library material under the KiCad libraries' CC-BY-SA 4.0
design exception. Dependency and attribution notes are in [NOTICE](NOTICE).

No warranty or engineering certification is provided. Always perform qualified
review appropriate to the product, jurisdiction, and risk.
