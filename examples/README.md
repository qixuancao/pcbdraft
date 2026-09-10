# PCBDraft public tutorials

This directory defines three small, low-voltage tutorial requests:

- `led-3v3-330r`: 3.3 V indicator, 330 ohm 0805 resistor and green 5 mm LED;
- `rc-1k-100nf`: connector-to-connector 1 kohm / 100 nF RC interface;
- `i2c-3v3-pullups`: 3.3 V I2C adapter with two 4.7 kohm pull-ups.

The files under `prompts/` are the exact natural-language inputs. They contain
no prepared CircuitPlan, reference answer or deterministic fixture result.

There are two different ways to use these examples:

- **Inspect an exported result:** open the published native KiCad files and
  the real `board.svg` / `board-top.png` previews directly. This does not
  require a model account or API key.
- **Reproduce from the prompts:** run PCBDraft against a configured model. This
  consumes model service usage and may produce a different result; use the
  bounded, single-attempt command below.

## Run the real examples

Use an installed PCBDraft wheel built from a known clean Git commit and a
private, already configured runtime snapshot:

```bash
python scripts/run-public-examples.py \
  --python /path/to/clean-venv/bin/python \
  --source-root /path/to/clean-pcbdraft-worktree \
  --runtime-template /private/path/to/runtime-snapshot
```

The current runner gives every case a private repository and removes inherited
`PCBDRAFT_HOME` and repository-configuration overrides from the worker
environment. Each prompt gets exactly one fresh project repository, runtime,
application home, trace and usage receipt. The default limits are 600 seconds
and 80 PCB tool calls per case. Failures are retained; the runner never retries
or selects the best attempt. Its stdout/stderr files are real command captures
with start and completion timestamps in `result.json`, not recordings of an
interactive TUI session.

Raw runs default to the user's PCBDraft data directory, outside this checkout.
They may contain credentials, model traces and private paths and must not be
committed. A successful worker exit only means the one-shot process completed;
it does not prove that the electrical design is correct, ERC/DRC passed, the
board is manufacturable, or hardware works.

## Recorded real run

The following is the evidence for the three-case run in
[`results/2026-09-09-single-attempts`](results/2026-09-09-single-attempts). Each case had exactly one
attempt; none was rerun or replaced with a nicer result.

| Case | Worker result | Current native checks | Preview |
| --- | --- | --- | --- |
| `led-3v3-330r` | exit 0; `491.874634 s` | ERC passed; DRC passed | Real `board.svg` and `board-top.png` |
| `rc-1k-100nf` | exit 0; `550.240892 s` | ERC passed; DRC passed | Real `board.svg` and `board-top.png` |
| `i2c-3v3-pullups` | timeout; `600.166599 s`; return code `-15` | ERC passed; DRC receipt is incomplete/running, not a pass | No current preview |

“ERC passed” and “DRC passed” above are individual, receipt-bound KiCad
check statuses for the original source files. The summaries retain the KiCad
ignored-rule keys; a pass is not a claim that every possible rule was enabled. They are not a genuine overall success verdict: worker exit 0
only says that the bounded process completed, and neither check result proves
design correctness, manufacturability or hardware operation. The I2C ERC pass
does not change its timeout or incomplete DRC status.

### Trace and usage counts

The main agent verified these counts against the raw trace and usage receipts.
No token count is inferred for the timed-out I2C attempt.

| Case | Model requests | PCB tool activity | Elapsed / result | Usage receipt |
| --- | ---: | --- | --- | --- |
| `led-3v3-330r` | 26 | 59 tool completions | `491.874634 s`; exit 0 | input `133501`; output `4595`; cache_read `555264`; total `693360`; included USD `0` |
| `rc-1k-100nf` | 31 | 50 tool completions | `550.240892 s`; exit 0 | input `136999`; output `7567`; cache_read `461568`; total `606134`; included USD `0` |
| `i2c-3v3-pullups` | 37 | 54 tool starts; 53 tool completions | `600.166599 s`; timeout | usage unknown; token fields are never inferred |

The attempts used the exact clean package/source commit
`4cde65c8ae8a58c8ef070b3913626933e9d4f251`, on Linux with KiCad `10.0.6`,
provider `openai-codex`, and model `gpt-5.6-sol`. The invoked driver SHA was
`d9fb3e7a17a2f2402958734c41dff69c88a0c0fdd0384704b7f83858c85ee3e3`.

The `included` / USD `0` values are account-specific runtime reporting, not a
claim that model inference is generally free. Token totals include cache reads.

The requested/editable LED value is green, while the stock KiCad 5 mm LED 3D
model renders red. That is a library-render limitation: the preview is a real,
unchanged render, not recolored or fabricated, and it is not hardware evidence.

These are tutorial demonstrations, not a benchmark, benchmark samples, or a
success-rate estimate. There was no human engineering review, hardware test,
fabrication sign-off, or production-readiness assessment. No result here is a
claim of hardware or production success.

### Publication-only metadata sanitation

All selected source native files were checked against the managed-project
hashes. The public RC `.kicad_pcb` differs only in one stock capacitor footprint
`descr` URL: its display query was removed to keep URL queries out of the bundle.
The manifest records both `source_sha256` and the published `sha256`, with an
explicit sanitation marker. No circuit geometry, connections, source evidence,
or preview bytes were changed. ERC/DRC receipts describe the original source;
this metadata-sanitized public copy was not rechecked. Other native files and
all published previews are byte-identical to their selected source artifacts.
The two original KiCad SVGs retain 57 trailing-whitespace lines (26 LED, 31 RC),
so the asset-import `git diff --check` reports those warnings. They are retained
for receipt-byte identity; no whitespace checks or lint rules were disabled.

## Additional platform and release context

Separate platform-matrix runs were recorded as follows (not model cases):

| Platform run | Commit | Result |
| --- | --- | --- |
| [34319401027](https://github.com/qixuancao/pcbdraft/actions/runs/34319401027) | `07034fd` | 6/7 jobs passed; Windows native worker timed out |
| [34320557670](https://github.com/qixuancao/pcbdraft/actions/runs/34320557670) | `db7f710` | 6/7 jobs passed; diagnostics identified Windows `inspect_board`, 30 s |

Linux/macOS/Windows installer contracts, macOS/Windows package/core checks, and
macOS native KiCad passed. Windows native KiCad remains blocked; neither run
is a green platform gate. Timeout caps and the full ATtiny fixture were retained.

Full lint on the clean package/source commit
`4cde65c8ae8a58c8ef070b3913626933e9d4f251` still reports `10569` findings across
`122` rule codes. It remains a release blocker. These partial platform results
do not authorize a merge to `main` or a Release publication.

## Export the reviewed bundle

To collect another reviewed run, choose a fresh output directory (the exporter
refuses to overwrite the published bundle). The command used for this bundle:

```bash
python scripts/export-public-examples.py \
  --content-reviewed \
  --reviewer-kind automated_assistant \
  --run-root /private/path/to/tutorial-run \
  --output examples/results/2026-09-09-single-attempts
```

`--content-reviewed` confirms review of the proposed publication contents,
including the allowlist, manifest, metadata, and secret/private-content
exposure. `--reviewer-kind automated_assistant` records the reviewer type.
This is publication-content review, not human engineering review: it is not
schematic or PCB signoff, an ERC/DRC review, a manufacturability assessment, or
a hardware test. The bundle keeps `human_engineering_review=false` unless a
human actually performs that engineering review.

The runner's current private-repository provenance is the authoritative path
for new runs. The collector also supports the older isolated-run provenance
where the application home held `application/projects/` instead of the
runner's repository inventory: it records that correction in a separate
`collection.json` without mutating the raw manifest or case results. Raw
runtime/auth/config files, full traces, terminal output and private paths are
not exported.

The public layout is:

```text
examples/results/2026-09-09-single-attempts/
├── manifest.json
├── collection.json
└── cases/
    ├── led-3v3-330r/
    │   ├── native/
    │   │   ├── <project-id>.kicad_pro
    │   │   ├── <project-id>.kicad_sch
    │   │   └── <project-id>.kicad_pcb
    │   └── previews/
    │       ├── board.svg
    │       └── board-top.png
    ├── rc-1k-100nf/
    │   ├── native/
    │   │   ├── <project-id>.kicad_pro
    │   │   ├── <project-id>.kicad_sch
    │   │   └── <project-id>.kicad_pcb
    │   └── previews/
    │       ├── board.svg
    │       └── board-top.png
    └── i2c-3v3-pullups/
        └── native/
            ├── <project-id>.kicad_pro
            ├── <project-id>.kicad_sch
            └── <project-id>.kicad_pcb
```

The preview files are included only for LED and RC. I2C has no current preview
because its sole attempt timed out; no preview is synthesized or linked for it.

## 真实预览图库

[Public manifest](results/2026-09-09-single-attempts/manifest.json) ·
[Corrected collection receipt](results/2026-09-09-single-attempts/collection.json)

The gallery links only the original receipt-bound preview files:

- LED: [`board.svg`](results/2026-09-09-single-attempts/cases/led-3v3-330r/previews/board.svg) · [`board-top.png`](results/2026-09-09-single-attempts/cases/led-3v3-330r/previews/board-top.png)
- RC: [`board.svg`](results/2026-09-09-single-attempts/cases/rc-1k-100nf/previews/board.svg) · [`board-top.png`](results/2026-09-09-single-attempts/cases/rc-1k-100nf/previews/board-top.png)
- I2C: no preview link; the single attempt timed out at `600.166599 s`, with usage unknown.

The exported manifest and collection receipt are the publication record, not a
benchmark record or an engineering sign-off.
