# BoardBench Contract

> Executable contracts for natural-language BoardBench campaigns, immutable
> evidence, truthful cohort labels, and safe bounded execution.

## 1. Scope / Trigger

Use this contract when changing `scripts/boardbench.py`,
`pcbdraft.verification.boardbench*`, the internal BoardBench worker, campaign
schemas, scoring, review/correction imports, reports, sealing, or publication.
BoardBench exercises the ordinary natural-language Hermes/flat-tool product
path; it must not become a second generator or receive a prepared
`CircuitPlan`.

The deterministic fault-injection benchmark is a separate verifier regression
suite. Its pass rate is never reported as an end-to-end BoardBench result.

## 2. Signatures

The operator surface is the thin `scripts/boardbench.py` CLI:

```text
corpus validate --corpus PATH
campaign create --corpus PATH --output DIR --campaign-id ID
                [--wall-timeout-seconds 1..86400]
campaign run|resume --campaign DIR --corpus PATH [--run-id ID ...]
score --campaign DIR --corpus PATH (--run-id ID | --all)
review templates --campaign DIR --corpus PATH
review import --campaign DIR --corpus PATH --submission PATH
correction capture --campaign DIR --run-id ID --generated DIR --corrected DIR
report --campaign DIR --corpus PATH --output FRESH_DIR
seal --campaign DIR --corpus PATH --output FRESH_DIR
publication ...
```

`campaign run` without selectors considers every planned run in frozen order.
Repeated `--run-id` selects a bounded subset but never changes the immutable
campaign denominator.

The versioned Python boundaries are:

```python
planned_run_v2(campaign, planned_run) -> BoardBenchRunV2
start_run_v2(run, started_at) -> BoardBenchRunV2
terminal_run_v2(...) -> BoardBenchRunV2
load_run_v2(path) -> BoardBenchRunV2
load_normalized_run(path) -> NormalizedBoardBenchRun  # v1 or v2, read-only

evaluation_v5_from_legacy_score(...) -> BoardBenchEvaluationV5
compare_evaluations_v5(previous, current, ...) -> BoardBenchComparisonV5

comparison_manifest_from_campaign(source) -> ComparisonCampaignManifest
run_deterministic_preflight(output_root, ...) -> DeterministicPreflightResult
```

## 3. Contracts

### Cohorts and evidence claims

- `sealed_holdout_baseline` requires a fresh private corpus independently
  reviewed by a qualified human engineer before any model run.
- `public_corpus_rerun` is used after corpus publication.
- `ai_reviewed_pilot` may contain real-model evidence, but seal/publication as
  BoardBench v1 must reject it. AI reviews stay outside canonical human
  `reviews/`; they cannot populate engineer identity or physical evidence.
- Fake-provider and CI fixtures remain `non_baseline_fixture`.

Absent human review, orderability, fabrication, assembly, firmware, or bench
evidence stays unknown/not reviewed. ERC/DRC and AI inspection cannot promote
those states.

### Campaign and run immutability

A campaign freezes the exact corpus identity, 20 x 3 run plan, provider/model,
tool registry, source/environment identity, evaluator version, tool budget, and
wall limit. Each run starts with a fresh project and session. Terminal receipts
and raw run artifacts are immutable; retries use new diagnostic ids rather than
replacing denominator entries.

Configuration drift is a terminal observed result, not permission to update the
campaign fingerprint. Scoring and review generation bind to the exact campaign,
case, run, and (where applicable) score.

### Bounded parallel pilot execution

One `run_campaign` invocation is sequential. Limited multi-process execution is
permitted only when an operator deliberately shards an initialized campaign
into disjoint `--run-id` sets:

1. A single process initializes all planned receipts first.
2. Every active process owns a disjoint selector set.
3. Never run an unselected `run`/`resume` while selector workers are active.
4. Do not dispatch the same run id twice.
5. Prefer a small bounded concurrency (normally 3-4) so provider throttling and
   KiCad resource contention do not dominate the pilot result.

There is no campaign-wide execution lease. A selected runner that observes its
own run as `running` treats it as interrupted; therefore overlapping selectors
or a concurrent run-all can corrupt the intended execution history even though
individual JSON replacements are atomic.

### Scores and derived artifacts

Every planned run remains in report denominators. Missing evidence is
`unknown`, never pass. Scoring reopens retained projects and creates a fresh
per-run output directory. A partially written score attempt must be preserved
outside the canonical `scores/<run-id>/` path before a missing score is retried;
never overwrite it in place.

### Run v2 lifecycle and budgets

`BoardBenchRunV2` has one immutable campaign/run/case/repetition identity and
three execution states: `planned`, `running`, and `terminal`. Terminal runs keep
`process_status`, `task_outcome`, `termination_reason`, `stage_reached`,
`release_gate_passed`, `outcome_source`, worker exit code, artifact inventory,
actual-cost evidence, and context-quality evidence as separate fields. A later
worker crash/timeout/cancel or configuration drift outranks an earlier product
receipt; an unbound or stale receipt cannot manufacture a pass.

The ordered budget dimensions are `model_turns`, `pcb_tool_calls`,
`route_attempts`, `route_node_expansions`, `uncached_input_tokens`,
`output_tokens`, `cache_read_tokens`, and `wall_time`. The comparison constants
are 90 model turns, 500 PCB tool calls, and 3600 seconds. Limits not enforced by
the runtime remain null with observed/unknown consumption; they must not be
invented during v1 projection. The 501st PCB tool call is rejected before side
effects. An observed consumption exactly equal to its limit remains
`within_limit`; `exhausted` at that value requires a separate authoritative
runtime rejection/timeout signal. Inferred exhaustion requires consumption
strictly above the limit. Exhaustion is terminal
`budget_exhausted:<dimension>`, never passed.

All v2, v5, manifest, and launch timestamps must be real timezone-aware calendar
timestamps and obey lifecycle ordering. Boolean values never satisfy numeric or
sequence fields despite Python's `bool`/`int` relationship. Cross-artifact
adapters validate campaign, run, case, repetition, revision, and source identity
together; comparisons additionally require distinct old/new campaign ids.

### Evaluator v5 layers and causal evidence

Evaluator v5 writes three independent layers: `design_intent`,
`native_artifact`, and `delivery_readiness`. Each layer owns deterministic,
source-attributed evidence and derives `pass|fail|unknown` from that evidence.
Without qualified engineer or physical evidence, delivery readiness remains
unknown even when native ERC/DRC pass. Graph comparison supports only explicit
policy-backed equivalences (symmetric two-terminal parts, series topology,
connector slot permutation, legal no-connect, and unspecified pin order); true
opens, shorts, missing required endpoints, or non-bijective mappings fail.

`first_blocking_stage`, `terminal_stage`, `root_causes`, and `symptoms` are
separate causal fields. The first blocker cannot occur after the terminal stage
or be detached from every root cause. v5 reads v1/v4 evidence and writes to a
fresh namespace; it never rewrites old score/review/hardware artifacts.

### Deterministic preflight versus formal comparison

The preflight starts with natural-language text and enters the real Agent
closed-tool protocol, but uses an explicitly labelled deterministic fake
provider. It may validate semantic/native materialization and v2/v5 plumbing;
it is not model reasoning, human review, delivery readiness, or hardware
evidence. Its campaign/case/run ids live in the `deterministic-preflight`
namespace and are forbidden from the formal 60-run denominator.

The frozen comparison manifest accepts only the read-only `ai_reviewed_pilot`
campaign evaluated under v4, and contains exactly 20 cases x 3 repetitions,
Luna through `openai-codex`, KiCad 10.0.5, evaluator v5, and the fixed budget
constants. Both that manifest and a separately loaded launch record independently
reject a malformed denominator or any `deterministic-preflight` identity. The
manifest is a preflight plan while `formal_campaign_started=false`; only separate
operator authorization may start paid execution. Manifest/report writers require
fresh non-symlink directories, bounded reads, closed schemas, relative artifact
references, and exact identity/revision bindings.

## 4. Validation & Error Matrix

| Condition | Required behavior |
| --- | --- |
| Corpus/campaign/source binding differs | Raise `ValidationError`; do not score or run against the mismatch |
| Provider, source, tool registry, or environment drifts | Store terminal `configuration_drift`; preserve the planned id |
| Duplicate/unknown/empty explicit selector | Reject before starting a model call |
| Selected receipt is already terminal | Verify retained artifacts and skip without overwrite |
| Selected receipt is `running` | Recover as interrupted only when no other worker can still own it |
| Score output directory already exists without `score.json` | Reject as non-fresh; preserve the partial attempt before retry |
| Required automatic evidence is missing | Record `unknown`, never infer pass |
| AI review supplied as engineer review | Keep separate; do not call canonical review import |
| Pilot sent to seal/publication | Reject regardless of downstream artifact count |
| Human review or five-category physical evidence missing | Completeness/seal gate fails closed |
| Product receipt identity/revision/time window differs from the run | Ignore it as pass evidence and use trace/worker truth |
| Agent exits normally before release gate | Terminal v2 run is `incomplete` / `agent_returned_before_gate` |
| Authoritative runtime rejects the next turn/call or signals wall timeout | Stop on the named dimension; preserve all prior evidence and planned denominator |
| Observed consumption equals a configured hard limit without an exhaustion signal | Keep `within_limit`; do not invent a terminal reason |
| Timestamp is impossible/reversed, a boolean occupies a numeric field, or an identity binding differs | Reject the artifact or adapter transition before publication |
| v1 artifact lacks a budget or context field | Project it as unknown; never guess zero or a configured limit |
| v5 layer evidence is unavailable or contradicts its summary | Reject malformed artifact or derive `unknown`; never infer pass |
| Graph equivalence exceeds its bounded mapping search | Return unknown with the limit reason, not a guessed match |
| Preflight id appears in formal planned runs | Reject the manifest before execution |
| Preflight report claims autonomous model, human, or hardware evidence | Reject as malformed |

## 5. Good / Base / Bad Cases

- Good: create one fixed 60-run campaign, initialize receipts once, execute four
  disjoint selector queues, preserve a timeout, score all 60, and publish only
  an explicitly unsealed AI-pilot report.
- Base: run the same campaign sequentially with no selector; every failure and
  timeout remains in the denominator.
- Bad: start `campaign resume` with no selector while selector workers are
  active. The run-all process may mark another live worker's receipt
  interrupted.
- Bad: copy an AI review into `reviews/<run-id>/review.json` and call it an
  engineer review.
- Bad: delete a timed-out run and rerun the same id to improve the success rate.
- Good: a worker exits zero but its bound product receipt says the release gate
  was not reached; v2 records an exited process and incomplete PCB task.
- Base: exactly 90 turns, 500 accepted PCB calls, or 3600 observed seconds is
  still within the inclusive comparison limit unless the runtime separately
  records that the next action was rejected or the process timed out.
- Base: deterministic preflight proves nine real PCB tools can reach a native
  KiCad/v2/v5 report. Design intent and delivery readiness remain unknown.
- Bad: count that fake-provider preflight as one of the 60 Luna runs or promote
  native DRC pass to engineer orderability.

## 6. Tests Required

- `tests.verification.test_boardbench`: closed schemas, cohort boundaries,
  terminal immutability, path limits, and campaign binding.
- `tests.verification.test_boardbench_runner`: 20 x 3 planning, real worker
  boundary, configuration drift, timeout/interruption retention, selector
  validation, and terminal-artifact verification.
- `tests.verification.test_boardbench_evaluator`: pass/fail/unknown metrics,
  false-completion behavior, component/net matching, ratings, and evaluator
  versioning.
- `tests.verification.test_boardbench_evidence` and
  `test_boardbench_diff`: review/correction/hardware source binding without raw
  artifact mutation.
- `tests.verification.test_boardbench_report`: fixed denominators, truthful
  cohort warnings, incomplete evidence, and pilot seal/publication rejection.
- `tests.verification.test_boardbench_cli` and
  `tests.interfaces.test_boardbench_worker`: CLI argument boundaries and the
  natural-language worker path.
- `tests.verification.test_boardbench_v2`: strict lifecycle combinations,
  v1 unknown preservation, terminal precedence/binding, named budget evidence,
  inclusive exact-limit semantics, real/ordered timestamps, denominator
  identity, boolean rejection, and the actual 500-call pre-side-effect guard.
- `tests.verification.test_boardbench_evaluator_v5`: three-layer evidence,
  bounded electrical graph equivalence, bijective mapping, causal ordering,
  fresh writers, immutable legacy inputs, and traversal/symlink rejection.
- `tests.verification.test_boardbench_preflight`: exact 20 x 3 manifest,
  non-formal namespace, natural-language Agent boundary, real native KiCad
  integration, v2/v5/report round trips, and truthful unknown human/hardware
  claims.

Focused iteration uses the nearest modules plus Ruff/format and
`git diff --check`; paid campaigns and physical work never run in CI.

## 7. Wrong vs Correct

### Wrong

```bash
# Overlaps every selected worker and can mis-recover live receipts.
python scripts/boardbench.py campaign resume \
  --campaign "$campaign" --corpus "$corpus"
```

```json
{"reviewer": {"name": "Codex", "role": "human_engineer"}}
```

### Correct

```bash
# After one-process receipt initialization, each coordinator receives a unique id.
python scripts/boardbench.py campaign run \
  --campaign "$campaign" --corpus "$corpus" --run-id "$unique_run_id"
```

```text
AI inspection is retained under ai-reviews/ with
human_engineering_approval=false; canonical reviews remain absent until an
actual qualified engineer supplies them.
```

Wrong — collapsing process completion, native checks, and delivery into one
success flag:

```json
{"status": "completed", "overall": "pass", "order_ready": true}
```

Correct — preserve independent lifecycle and evaluation layers:

```json
{
  "process_status": "exited",
  "task_outcome": "incomplete",
  "termination_reason": "agent_returned_before_gate",
  "design_intent": "unknown",
  "native_artifact": "pass",
  "delivery_readiness": "unknown"
}
```
