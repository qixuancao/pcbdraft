# Deterministic implementation and adopted comparison verification

Initially recorded on 2026-08-24 for Milestones 0–8 and extended on 2026-08-25
after the operator explicitly adopted the pre-existing ignored local M9/M10
evidence. The retained real-model campaign, launch, run, score, review, and
comparison artifacts were read and verified in place; none were rewritten or
deleted.

## Scope reviewed

- native schematic/board projection, operation deltas, and fail-closed managed
  publication;
- all nine Router failure codes, retained-copper protection, progress vectors,
  convergence, and product terminal receipts;
- bounded semantic groups, compact Hermes receipts, stage-derived schema
  projection, and separate cost/context evidence;
- BoardBench run v2, evaluator v5, immutable comparison-plan artifacts, and the
  fake-provider/real-KiCad deterministic preflight.

The final audit tightened the campaign boundary so both a comparison manifest
and a separately loaded formal launch record independently require an exact 20
cases × 3 repetitions denominator, reject the deterministic-preflight namespace,
and bind the source to the frozen `ai_reviewed_pilot` / evaluator-v4 baseline.

## Final review fixes

- Corrected inclusive exact-limit budget semantics and required authoritative
  exhaustion evidence when consumption only equals a limit.
- Added real timestamp/lifecycle ordering, finite numeric, boolean-as-integer,
  campaign/run/revision identity, and portable artifact-path validation.
- Prevented immutable product-terminal receipt collisions when the same turn is
  retried by assigning a fresh attempt/session identity.
- Kept legacy run-v2 transitions source-bound and rejected recursive malformed
  trace JSON instead of accepting partial evidence.
- Tightened evaluator comparisons to distinct campaigns with matching run
  identities, and corrected heterogeneous retained route/via projection typing.
- Added focused regressions for every corrected boundary.

## Focused verification

- Progress, BoardBench v2, and evaluator v5: 47 tests passed.
- Native consistency and Router: 43 tests passed.
- Deterministic fake-provider/real-KiCad preflight: 10 tests passed, including
  the natural-language → native project → terminal v2 → evaluator-v5 path.
- Hermes tooling/orchestration/worker: 88 focused tests passed.
- BoardBench core/runner/CLI/evidence/gates: 82 tests passed.
- Application service: 28 tests passed.
- Flat PCB tools: 41 tests passed.
- Ruff check and format check passed for `scripts/boardbench.py`, `src`, and
  `tests` (174 formatted files checked).
- Mypy passed for all 101 source modules.
- `uv lock --check` and `git diff --check` passed.

## Adopted Milestone 9 evidence

The adopted campaign is
`artifacts/boardbench-local/campaigns/vbe-luna-tier-a-real-60-10x-v2-20260824/`.
Production `load_corpus()` plus `load_campaign_evidence()` reopened the complete
388 MiB campaign, recomputed all terminal inventories, and validated corpus,
campaign, run, and score source bindings in 30.11 seconds:

- corpus `boardbench-v1-ai-pilot-r4`, SHA-256
  `3a42558872399841534f0d01717d2bb271a2fb715e446263b7fceac978ae86f7`,
  20 cases and three repetitions;
- campaign SHA-256
  `6b98025d72f70040ce0d27fc7e61de7d3d289d881a42b97e51c1edc1424b7906`,
  `openai-codex` / `gpt-5.6-luna`, KiCad 10.0.5;
- 60 planned, 60 started, 60 terminal v2 runs and 60 source-bound v4 scores;
  no canonical human reviews and no fixture-labelled denominator entries;
- all 60 processes exited normally, all 60 PCB tasks were incomplete with
  `agent_returned_before_gate`, and zero release gates passed;
- stage distribution: 51 `footprint_net_sync`, 5 `erc_drc`, 2 `placement`, and
  2 `requirements_frozen`;
- 2,315 model turns, median 37.5, maximum 51, and zero model-turn exhaustion.

Production `load_formal_comparison_launch()` validated the immutable launch at
`artifacts/boardbench-local/vbe-m9-formal-launches-20260824/`. Its file SHA-256
is `64f3622be779fe9b92079881ddef9a0fdfaf8986134f03cf2ef976c4077376a8`.
It binds the same corpus, exact 60-run plan, provider/model/KiCad environment,
tool registry, v2 run schema, v4/v5 evaluators, and fixed 90/500/3600 limits.
The launch record truthfully retains `formal_campaign_started=false` because it
was published before workers spawned; the 60 v2 `started_at` values independently
prove execution started. The retained `vbe-m9-launch-10x-20260824.sh` and ten
worker logs cover ten disjoint six-run queues.

## Adopted Milestone 10 evidence and result

Production loaders validated the unsealed canonical report, all 60 source-bound
review templates, 60 old plus 60 new evaluator-v5 records, and all 60 same-v5
comparisons. The separate Codex AI review contains exactly 60 unique planned run
ids across three reviewer groups, 0 functional passes and 60 failures, 181
checklist passes and 461 failures, and 60 `kicad_materialization` primary stages.
It explicitly has `human_engineering_approval=false` and remains outside
canonical `reviews/`.

The independently loaded evidence supports the factual summary at
`artifacts/boardbench-local/vbe-m10-report-20260824/factual-summary.md`:

- v4 DRC was 5 pass / 2 fail / 53 unknown, versus 20 / 40 / 0 in the old
  baseline; ERC was 6 / 1 / 53, versus 34 / 26 / 0;
- v5 new layers were design intent 0 pass / 60 fail, native artifact 0 / 60,
  and delivery readiness 0 pass / 0 fail / 60 unknown;
- model-request median was 38 over 52 reported runs (old 79), total-token median
  was 920,178 (old 5,030,390), and total tokens were 55,821,384 (old
  280,330,876);
- direct trace reduction found 3,269 tool results with median 676 bytes and
  4,151,770 total bytes, plus 71 policy-blocked calls.

The release criteria therefore failed: DRC reached only 5/60 rather than 40/60,
and AI functional correctness reached 0/60 rather than 50/60. Efficiency,
context-size, model-turn, and committed-consistency criteria passed, but they do
not override those product failures. No human approval, orderability,
fabrication, assembly, first-power, firmware, or physical-function evidence was
created or inferred.

## Empty-net root cause and post-fix evidence

Direct reduction of all retained transaction receipts found 1,561 transactions:
1,217 applied and 344 failed. No applied receipt had a failed native delta or
postcondition, and no failed candidate had a committed revision or changed live
state. No receipt reported `publication_failed`. Exactly 240 `add_net`
transactions across all 60 runs failed with `native_delta_failed` at
`native_net_projection`. The remaining failures were 62 new-DRC placement
failures, 33 placement attempts with unavailable DRC, 6 native component
materialization failures, and 3 native-consistency placement failures.

The primary blocker was a transaction-policy defect: KiCad cannot durably
represent a named net with zero endpoints, pads, and copper, but the campaign
version required that empty semantic net to appear immediately as a native board
net. Current `compare_native_operation_delta()` now accepts the absent projection
only for that exact empty-net case and records
`native_projection=not_applicable_empty_net`; non-empty or conflicting native
membership still fails closed. The existing deterministic regression and the
application transaction regression both passed on 2026-08-25 (0.013 seconds and
0.970 seconds respectively). No additional code correction was required.

## Fresh model-free preflight

A fresh deterministic fake-provider/real-KiCad preflight was generated without
Luna or any paid/default provider under:

`artifacts/boardbench-local/vbe-empty-net-postfix-preflight-20260825/deterministic-preflight/deterministic-preflight-ground-adapter-run-1/`

Generation and publication took 57.23 seconds. A separate ad hoc console-reporting
wrapper then raised an `AttributeError` while projecting the already returned
typed result; the producer had already written the complete artifact tree.
Production preflight-report, run-v2, evaluator-v5, managed-project, and inventory
loaders subsequently reopened it without mutation. Its nine real PCB tool actions
reached a terminal passed release gate. The committed native comparison had zero
mismatches and `consistency_passed=true`; the separately retained ERC and DRC
checks both passed. Evaluator v5 truthfully reports design intent unknown, native
artifact pass, and delivery readiness unknown; it claims no autonomous-model,
human, or physical evidence. This fixture validates current
transaction/native/v2/v5 plumbing but is not part of either formal 60-run
denominator.

## Next truthful state

Milestones 9 and 10 have retained, verified evidence, and the comparison release
gate failed for the reasons above. The empty-net defect is fixed in current code
and covered deterministically, but the adopted campaign remains immutable and
cannot demonstrate the post-fix real-model outcome. Any further real 20 × 3 Luna
rerun still requires separate explicit operator cost/time authorization. Until
then, delivery readiness remains unknown and no product release is claimed.
