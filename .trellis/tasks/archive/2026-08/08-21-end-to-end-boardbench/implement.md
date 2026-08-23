# BoardBench v1 implementation plan

## Execution rule

Do not optimize for a target success percentage. Preserve every planned run and
fix product defects only after the failing baseline artifact has been retained
and classified. Use focused checks during implementation; run the full suite and
release gate only at the final software integration/release checkpoint.

## Milestone 0 — restore truthful mainline gates

- [x] Audit every script, CI job, package assertion, and development-doc command
  for deleted paths or obsolete frontend assumptions.
- [x] Replace the obsolete Textual/TUI E2E with a fake-provider Hermes one-shot
  smoke that creates a fresh project through the current flat PCB tool surface,
  retains trace/project evidence, and uses real KiCad.
- [x] Remove the deleted `styles.tcss` assertions from packaging/release checks;
  update all renamed script references together.
- [x] Either restore a working wrapper for the existing 90-case deterministic
  benchmark or remove its stale documented command. Do the same for `smoke.sh`.
- [x] Keep the existing 90-case corpus load check and production-readiness
  truthfulness assertion.

Focused validation:

```bash
bash -n scripts/*.sh
uv run python -m unittest -v tests.interfaces.test_hermes_cli tests.hermes.test_debug_trace
git diff --check
```

Do not run `scripts/release-check.sh` yet; reserve it for Milestone 9.

## Milestone 1 — strict BoardBench contracts

- [x] Implement versioned dataclasses/loaders for corpus, campaign, run, score,
  review, correction, hardware, selection, and aggregate report artifacts.
- [x] Enforce 20 cases, 4 per category, unique IDs, closed fields, bounded text
  and arrays, fixed failure taxonomy, metric tri-state, UTC timestamps, hashes,
  and CC0-compatible corpus metadata.
- [x] Implement private campaign directory allocation, atomic records, terminal
  immutability, relative-path inventories, and deterministic hashes.
- [x] Add malformed/oversized/symlink/duplicate/version/NaN/unknown-field tests.

Focused validation:

```bash
uv run python -m unittest -v tests.verification.test_boardbench
uv run ruff check src/pcbdraft/verification/boardbench.py tests/verification/test_boardbench.py
uv run ruff format --check src/pcbdraft/verification/boardbench.py tests/verification/test_boardbench.py
git diff --check
```

## Milestone 2 — isolated real-product runner

Depends on Milestones 0 and 1.

- [x] Add the thin prompt-only worker that creates/binds a fresh trusted project
  and invokes the existing `launch_cli` one-shot path without an evaluator path,
  retaining its run-local usage receipt.
- [x] Implement campaign planning for exactly 60 run ids and a redacted default
  model/environment/tool-registry fingerprint.
- [x] Implement sequential subprocess execution with run-local repository config,
  trace path, timeout/output bounds, complete terminal receipts, and safe resume
  of only unstarted runs.
- [x] Copy all rotated trace members, stdout/stderr, project/intermediate state,
  and file inventory before evaluation. Detect trace gaps.
- [x] Abort a run as `configuration_drift` if the frozen default model/config or
  environment contract changes.
- [x] Add fake-provider tests proving the first model input is only the natural
  language prompt, every run gets a new project/session, no `CircuitPlan` is
  injected, failures persist, and terminal runs cannot be overwritten.

Focused validation:

```bash
uv run python -m unittest -v tests.verification.test_boardbench_runner tests.interfaces.test_boardbench_worker
uv run ruff check src/pcbdraft/verification/boardbench_runner.py src/pcbdraft/interfaces/boardbench_worker.py tests/verification/test_boardbench_runner.py tests/interfaces/test_boardbench_worker.py
uv run ruff format --check src/pcbdraft/verification/boardbench_runner.py src/pcbdraft/interfaces/boardbench_worker.py tests/verification/test_boardbench_runner.py tests/interfaces/test_boardbench_worker.py
git diff --check
```

## Milestone 3 — independent automatic evaluator

Depends on Milestones 1 and 2.

- [x] Reopen managed projects and independently run existing semantic/library,
  ERC, and DRC validation rather than trusting Agent claims or prior receipts.
- [x] Implement injective component-slot matching and net equivalence/inequality
  checks against hidden reference contracts.
- [x] Implement required support-circuit, forbidden condition, rating, and
  manufacturing-envelope predicates needed by the 20 v1 cases, reusing shared
  graph/net helpers.
- [x] Implement conservative Chinese/English completion-claim classification and
  `false_completion` derivation.
- [x] Reduce trace events into token/cost status, model/tool counts, timing,
  retries, errors, and missing-evidence states through one typed decoder.
- [x] Implement deterministic failure-stage/cause suggestions without overwriting
  later engineer classification.
- [x] Add clean/fault/adversarial fixtures for every metric, allowed alternatives,
  missing evidence, ambiguous completion language, and evaluator versioning.

Focused validation:

```bash
uv run python -m unittest -v tests.verification.test_boardbench_evaluator tests.verification.test_gates tests.hermes.test_debug_trace
uv run ruff check src/pcbdraft/verification/boardbench_evaluator.py tests/verification/test_boardbench_evaluator.py
uv run ruff format --check src/pcbdraft/verification/boardbench_evaluator.py tests/verification/test_boardbench_evaluator.py
git diff --check
```

## Milestone 4 — review, correction, physical evidence, and reporting

Depends on Milestones 1 and 3.

- [x] Implement review templates/import validation for all 60 planned runs,
  including `not_applicable` failure records, modification decisions, active
  minutes, non-ERC/DRC findings, and final failure classification.
- [x] Implement generated/corrected snapshot import and normalized semantic/native
  diffs without mutating raw runs.
- [x] Link structured review/hardware artifacts through existing L6/L7 external
  evidence when the project supports it.
- [x] Implement five-category selection validation and hardware evidence import.
- [x] Implement JSON/Markdown aggregation with fixed denominators, unknown and
  not-applicable states, efficiency/cost status, and case/category/overall views.
- [x] Implement fail-closed seal and sanitized publication bundle creation.
- [x] Add tests for missing reviews, changed source hashes, duplicate selection,
  category gaps, fabricated/empty attachments, report denominators, and secret/
  absolute-path redaction.

Focused validation:

```bash
uv run python -m unittest -v tests.verification.test_boardbench_diff tests.verification.test_boardbench_evidence tests.verification.test_boardbench_report
uv run ruff check src/pcbdraft/verification/boardbench_diff.py src/pcbdraft/verification/boardbench_evidence.py src/pcbdraft/verification/boardbench_report.py tests/verification/test_boardbench_diff.py tests/verification/test_boardbench_evidence.py tests/verification/test_boardbench_report.py
uv run ruff format --check src/pcbdraft/verification/boardbench_diff.py src/pcbdraft/verification/boardbench_evidence.py src/pcbdraft/verification/boardbench_report.py tests/verification/test_boardbench_diff.py tests/verification/test_boardbench_evidence.py tests/verification/test_boardbench_report.py
git diff --check
```

## Milestone 5 — operator CLI and methodology

Depends on Milestones 1–4.

- [x] Add a thin `scripts/boardbench.py` interface with explicit commands for
  corpus validation, campaign creation/run/resume, score, review import,
  correction capture, hardware import, report, seal, and publication bundle.
- [x] Require explicit output/corpus paths for the sealed campaign; never locate
  a private holdout implicitly or print its contents.
- [x] Document campaign lifecycle, reviewer counting rules, safe bring-up
  boundaries, artifact schemas, cost interpretation, and public-rerun labeling.
- [x] Update architecture/project-structure/development documentation without
  presenting BoardBench as a model leaderboard or production attestation.
- [x] Add an end-to-end deterministic/fake-provider campaign fixture that proves
  the full artifact flow while labeling itself non-baseline.

Focused validation:

```bash
uv run python scripts/boardbench.py --help
uv run python -m unittest -v tests.verification.test_boardbench tests.verification.test_boardbench_runner tests.verification.test_boardbench_evaluator tests.verification.test_boardbench_diff tests.verification.test_boardbench_report tests.interfaces.test_boardbench_worker
git diff --check
```

## Milestone 6 — author and freeze the sealed holdout

Depends on Milestones 1, 3, and 5. This is an evidence/data milestone, not a
product-code shortcut.

- [x] Author 20 new prompts and reference contracts outside the public repository,
  four per category, using original/CC0-compatible data only.
- [ ] Have an engineer independently review every reference topology, equivalent
  choice, support requirement, rating bound, review rubric, and manufacturing
  envelope before any model run.
- [x] Validate installed KiCad symbol/footprint availability for the recorded
  environment without generating a candidate board for the model.
- [ ] Freeze corpus bytes/hash and create the 60-entry campaign manifest with one
  default provider/model/config fingerprint.
- [ ] Record that prompts were not used to tune PCBDraft before the freeze.

Gate: the corpus may not be committed or shown to the tested Agent before the
baseline is sealed.

Current gate record (2026-08-22): the conversation user asserted “审查通过”
after receiving the source-bound template and narrow-coverage decision. The
private directory retains that assertion, but it does not supply reviewer
identity/qualification, independence, review timestamps, or the 180 required
per-case decisions. M6 therefore remains fail-closed: no frozen corpus,
campaign manifest, or model run has been created.

## Separate AI-reviewed pilot track — does not satisfy Milestone 6

- [x] Add the explicit `ai_reviewed_pilot` corpus/campaign/report cohort without
  weakening `sealed_holdout_baseline` contracts or fixed 20 × 3 denominators.
- [x] Reject pilot cohorts at sealed-report construction and canonical
  seal/publication gates; render an explicit non-human, non-baseline warning.
- [x] Add repeatable explicit run-id selection that executes a bounded subset
  while retaining all 60 immutable plan entries and planned receipts.
- [x] Complete and retain two AI technical reviews of the private draft, with
  reviewer limitations and disagreements recorded rather than fabricated as
  human engineering approval.
- [x] Freeze a separately named pilot corpus/campaign and run only the explicitly
  authorized bounded real-model subset; do not count it toward M7.

Pilot results may guide tooling decisions, but their prompts are considered
exposed after use. A later sealed baseline requires a fresh holdout plus the
independent human-engineering review still required above.

## Milestone 7 — execute and classify 60 real-model runs

Depends on Milestones 2, 3, and 6.

- [ ] Run the complete sequential campaign; retain every timeout, refusal,
  infrastructure failure, partial project, and successful result.
- [ ] Do not silently retry or replace a planned run. Supplemental diagnostic
  reruns receive new ids and are reported separately.
- [ ] Verify all 60 terminal receipts, trace inventories, project/failure
  artifacts, automatic scores, cost statuses, tool counts, and reasons.
- [ ] Snapshot the software/environment identity used by the whole campaign.

Gate: `campaign.json` still has exactly the original 60 planned entries and all
are terminal; no result has been overwritten.

## Milestone 8 — complete 60 engineering reviews and corrections

Depends on Milestone 7.

- [ ] Review every run before editing and record `not_applicable` for runs with no
  inspectable schematic.
- [ ] Confirm/override every failure-stage suggestion and eliminate
  `not_reviewed`/`unclassified` states.
- [ ] Record modification decisions and active minutes; capture a corrected
  snapshot and structural diff for each modified candidate.
- [ ] Produce a verified manufacturing-candidate bundle for the selected board in
  each category, without claiming production readiness.

Gate: all 60 review records and required correction diffs validate against their
source hashes.

## Milestone 9 — software integration/release verification

Depends on Milestones 0–5. This is the first point where the full local gates are
expected, because the task is preparing its mainline/release claim.

```bash
git diff --check
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
scripts/test.sh
scripts/release-check.sh
```

- [x] Record commands, environment, duration, pass/fail, and any checks left to
  CI. Never summarize an interrupted or unavailable check as passed.
- [x] Confirm no script, package assertion, documentation page, or CI job refers
  to deleted files.

## Milestone 10 — manufacture and bring up five boards

Depends on Milestone 8 and successful manufacturing-candidate checks. Requires
human purchasing, assembly, firmware, and lab work.

- [ ] Select exactly one initial board from each category with a source-hash
  selection record.
- [ ] Obtain explicit purchasing authorization, place real orders, and retain
  board-house acceptance and order/manufacturing parameters.
- [ ] Assemble and inspect; record solderability.
- [ ] Perform safe current-limited first power, short check, rail measurements,
  firmware download, and category-specific core functional tests.
- [ ] Import attributed measurements/artifacts and record every respin.

Gate: at least five valid physical records cover all five categories. Software
tests or fabricated fixtures cannot satisfy it.

## Milestone 11 — seal and publish BoardBench v1

Depends on Milestones 7–10.

- [ ] Run the completeness/seal gate and create a final hash manifest.
- [ ] Generate the public corpus, machine-readable results, report, representative
  success/failure cases, and large-artifact archive.
- [ ] Review the publication bundle for credentials, private paths, third-party
  library redistribution, and unsupported claims.
- [ ] Update README with real observed denominators, no-edit engineering approval,
  first-power/core-function results, typical failures, and capability limits.
- [ ] Publish/upload only after explicit external-write authorization.
- [ ] Label later runs as `public_corpus_rerun`, never as sealed holdout results.

## Dependency summary

```text
gate repair ───────────────┐
contracts -> runner -> evaluator -> operator CLI -> sealed corpus -> 60 runs
     └────> review/diff/report ────────────────────────────────┘
60 runs -> 60 reviews/corrections -> five-category hardware -> seal/publication
software modules + gate repair -> full test/release verification
```

## Risk and rollback points

- Do not edit `vendor/hermes`; use the existing activation and lifecycle hooks.
- Treat `interfaces/hermes_cli.py`, debug trace payloads, artifact schemas, and
  `scripts/release-check.sh` as high-risk boundaries with focused regressions.
- Never mutate a terminal campaign/run or raw generated project. Corrections and
  rescoring are new artifacts, so rollback does not destroy evidence.
- Keep paid campaigns and physical work out of CI. Fake-provider tests must carry
  an explicit `non_baseline_fixture` label.
- If a schema proves insufficient before the campaign starts, bump/reset the
  draft corpus version. After the first run, preserve it and create a new
  campaign/version instead of migrating results in place.
