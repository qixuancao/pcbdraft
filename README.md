# pcb-agent-runtime

`pcb-agent-runtime` is a runnable Linux MVP for a **KiCad AI Design Reviewer + Safe Patcher**.
It runs the local `kicad-cli` for deterministic ERC/DRC evidence and the already-authenticated
local Codex CLI for bounded, structured heuristic review. It is not a KiCad GUI and does not
claim functional correctness, SI/PI, thermal, EMI, timing, safety, regulatory, manufacturing,
or production sign-off. Human engineering review remains required.

```bash
scripts/deploy.sh
uv run ./pcb-agent doctor --json
uv run ./pcb-agent review /path/to/project --output /tmp/pcb-runs
uv run ./pcb-agent patch /path/to/project --request "Make this exact reviewed text change" --output /tmp/pcb-runs
# After inspecting a ready transaction:
uv run ./pcb-agent apply /tmp/pcb-runs/20260812T120000Z-deadbeef
```

Security boundary: project files and embedded text are treated as untrusted data. Codex receives
the task prompt on stdin, uses `gpt-5.6-sol` with reasoning `max`, service tier `default`, Fast off,
multi-agent off, hooks off, and the Codex `read-only` sandbox. User config and project exec rules
are ignored while the existing Codex login state is reused; no token is read, copied, or printed by
this program. Approval escalation, login shells, native web/browser/app/computer/image-generation tools, and
project-scoped Codex config are also disabled for the invocation. On the tested Codex 0.147.0/Linux
combination the runtime explicitly selects legacy Landlock because the newer bubblewrap read-only
sandbox cannot initialize on this host; that switch is deprecated upstream, so a future Codex upgrade
requires a live smoke. `read-only` is a Codex tool policy, **not an OS/container security boundary**:
Codex is
still a local process under the invoking user, can read files allowed by that OS account, and may
send reviewed project information to the configured OpenAI service. Run only on projects you are
authorized to disclose and use an external sandbox/container if stronger isolation is required.

## Requirements

- Linux, Python 3.11+, and `uv`
- `codex` CLI 0.147.0-compatible command surface with an existing login
- `kicad-cli` with JSON ERC/DRC support (tested locally with KiCad 10.0.5)
- `git` for `doctor` diagnostics

The Python package uses only the standard library. `pyproject.toml` provides the installed
`pcb-agent` entry point; executable `./pcb-agent` is the convenient repository-local entry.

## Commands

### `doctor [--json]`

Finds `codex`, `kicad-cli`, and `git`, executes their real version commands with time/output
bounds, and reports whether each is usable. It does not inspect or display authentication data.

### `review PROJECT [--output DIR] [--timeout SEC]`

The project path is canonicalized. Discovery prefers the root, but requires exactly one matching
`.kicad_sch`/`.kicad_pcb` stem; ambiguity is an error. The run records a bounded relative-path,
size, and SHA-256 inventory, raw ERC/DRC JSON, normalized deterministic evidence, and bounded
semantic exports (schematic XML netlist, board statistics, and IPC-D-356 connectivity). Codex
receives those exports as normalized data and is instructed to return immediately without shell
or project-file access. The run also records bounded Codex JSONL, the schema-validated final
message, `report.json`, `report.md`, and `receipt.json`.

KiCad violations are findings, not process crashes. The runtime does not use
`--exit-code-violations`; it parses JSON and separately records the gate process exit code. A
missing/invalid report, timeout, output overflow, launch failure, or non-violation nonzero exit is
a tool failure and stops the review before AI analysis.

### `patch PROJECT --request TEXT [--output DIR] [--timeout SEC]`

The source project is fully hashed, then copied into a private transaction `staging/` directory.
Baseline gates run there. Codex remains read-only and returns a schema-constrained change set; the
deterministic engine permits at most 20 `replace_text` operations and 128 KiB of total replacement
text. Targets must be existing UTF-8 KiCad/text configuration files inside staging; symlinks and
path traversal are rejected, and each nonempty `old_text` must match exactly once.

After applying operations only in staging, the runtime ensures no files appeared/disappeared,
runs ERC/DRC again, and writes `changes.patch`, `change_set.json`, both gate sets, and a receipt.
The transaction is `rejected` if a baseline-runnable gate becomes a tool failure or an ERC/DRC
error count increases; otherwise it is `ready`. Warnings remain visible but do not alone reject,
because the MVP requirement uses error regression as the acceptance threshold. The original is
never modified by `patch`.

### `apply RUN_DIR`

Only a `ready` transaction is accepted. `apply` verifies the complete current source manifest
against the baseline and revalidates staged hashes. It copies every changed original into private
`backup/`, then atomically replaces only existing files. ERC/DRC runs immediately on the source.
Any gate failure or error-count increase triggers an automatic atomic restore from backup and a
nonzero exit. A successful receipt is marked `applied`; transactions are deliberately one-shot.

Crash caveat: per-file writes are atomic and post-write failures are rolled back, but this standard-
library MVP has no filesystem-wide atomic transaction. Process termination or power loss between
multiple file replacements can leave a partially applied project; the run-local `backup/` remains
the recovery source. A concurrent editor can also race the pre-apply hash check, so close KiCad and
quiesce other writers. Keep the project under version control and inspect `changes.patch` before apply.

## Artifacts and privacy

Run IDs use UTC time plus a random suffix. The default parent is
`~/.local/share/pcb-agent-runtime/runs`; `--output` selects another parent. Run directories and
runtime-created subdirectories are mode `0700`, and artifacts are tightened to `0600` where the
filesystem supports POSIX permissions. JSON writes and target replacements use same-directory
temporary files, `fsync`, and `os.replace`.

Run output must live outside the source project. Agent workflows also reject symlinks and special
files anywhere in the project tree, preventing an apparent project member from redirecting reads
outside that tree. Hard links and the invoking account's general read permissions remain outside
the guarantees of this MVP and are another reason to use external isolation for hostile projects.

Receipts contain runtime/tool versions where available, redacted argv (project, staging, and run
paths use markers), input hashes, gate exit codes/counts, and Codex completion/schema status. They
never include the full environment or Codex token. Codex prose plus exit code zero is not treated
as approval: schema validation, deterministic operation checks, hash checks, and gates are separate.

## Tests and smoke

```bash
scripts/test.sh
REAL_CODEX=0 scripts/smoke.sh
```

The unittest suite includes offline end-to-end review and patch/apply flows using executable fake
Codex/KiCad processes, plus ambiguity, traversal/symlink, unique replace, regression policy,
baseline drift, and pinned Codex argv tests. It does not need network access.

`scripts/smoke.sh` accepts `DEMO_DIR=/path/to/demo` and otherwise copies
`/usr/share/kicad/demos/ecc83` to a temporary directory. It runs `doctor` and real local KiCad
ERC/DRC without modifying the system demo. Set `REAL_CODEX=1` to additionally run a real review;
the script selects one project from the ambiguous multi-project `ecc83` demo copy.

The current competitor smoke results and a reusable single-project timing entry point are in
[`BENCHMARK.md`](BENCHMARK.md) and `scripts/benchmark.sh`.
