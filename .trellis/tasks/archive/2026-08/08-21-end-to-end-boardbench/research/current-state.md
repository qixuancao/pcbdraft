# BoardBench current-state research

## Existing benchmark is not end to end

- `src/pcbdraft/verification/benchmark.py:151` starts from a bundled,
  precompiled deterministic design, injects clean/fault cases, and optionally
  asks a model to review those mutations. It does not start a board from a
  natural-language request (`benchmark.py:171-205`).
- The release check asserts that this legacy corpus still contains 90 cases
  (`scripts/release-check.sh:38-39`). BoardBench must coexist with it and use a
  distinct schema and report name.

## Real product execution boundary

- The default Hermes configuration exports only the `pcbdraft` toolset and
  disables tool search; shell, file, and code tools are outside model authority
  (`src/pcbdraft/model/hermes_config.py:31-45`). This is the runtime isolation
  boundary for a sealed evaluator.
- `src/pcbdraft/interfaces/hermes_cli.py:361-406` is the canonical activation
  path. It accepts Hermes argv, checks the configured provider, installs the
  PCBDraft persona/tool/plugin surface, and can run Hermes' single-query
  one-shot mode without introducing a second Agent controller; BoardBench also
  requests Hermes' run-local usage receipt so token/cost accounting is retained.
- Each BoardBench run can therefore use a small child process which creates
  and binds a fresh trusted project, reads only a prompt-only request artifact,
  and calls the same `launch_cli()` path used by the product.

## Existing observability and cost inputs

- The debug plugin records session boundaries, every model request/response,
  provider error, tool start/end, and the final reply
  (`src/pcbdraft/interfaces/hermes_plugin.py:1-18`, `43-56`).
- Model response events already retain canonical usage buckets and provider/model
  identity (`src/pcbdraft/interfaces/hermes_plugin.py:194-218`); tool events
  retain arguments, result, duration, status, and errors.
- The debug writer is bounded, redacted, append-only JSONL and can be directed
  to a run-local path with `PCBDRAFT_DEBUG_TRACE_PATH`
  (`src/pcbdraft/core/debug_trace.py:1-19`, `50-76`). BoardBench still needs to
  snapshot every rotated member and verify that the trace sequence is complete.
- Hermes' canonical pricing layer distinguishes actual, estimated,
  subscription-included, and unknown cost. BoardBench should aggregate trace
  usage through that existing calculator and retain status/source rather than
  inventing an exact price.

## Existing deterministic project evidence

- `open_managed_project()` validates the manifest, IR, project-local part graph,
  circuit plan, and component qualification provenance
  (`src/pcbdraft/services/managed.py:425-475`).
- `validate_managed_project()` reruns real KiCad ERC/DRC and existing semantic
  checks, then writes a source-hashed validation receipt
  (`src/pcbdraft/verification/validation.py:287-340`). It deliberately keeps
  `production_ready=false`; BoardBench must not weaken that claim boundary.
- L6/L7 evidence can already be copied, hashed, attributed, and marked as
  externally supplied through `record_external_evidence()`
  (`src/pcbdraft/verification/evidence.py:24-48`, `72-143`). BoardBench-specific
  review and hardware schemas can reuse this import boundary without treating
  the software as the reviewer or lab.

## Confirmed release drift

- `scripts/release-check.sh:28-45` still requires the deleted
  `src/pcbdraft/interfaces/tui/styles.tcss` and runs an obsolete TUI-named E2E.
- `scripts/tui-e2e.py` still targets the removed Textual workflow and legacy
  configuration/agent macros, despite the current frontend being Hermes.
- `docs/DEVELOPMENT.md:21-27` names deleted `scripts/benchmark.sh` and
  `scripts/smoke.sh` entrypoints.
- Phase 0 must repair these references and establish a real Hermes/KiCad release
  smoke before BoardBench claims the main branch is runnable.

## Constraints carried into design

- Use strict versioned JSON dataclasses, bounded reads, atomic writes, private
  run modes, and no database.
- Do not modify vendored Hermes by hand, expose evaluator data to the Agent,
  add model providers, or add a new workflow macro to the PCB toolbox.
- Paid real-model campaigns are explicit operator actions and never CI tests.
- Terminal failures, timeouts, and infrastructure faults remain in the planned
  denominator; terminal run directories are never overwritten.
