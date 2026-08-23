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
