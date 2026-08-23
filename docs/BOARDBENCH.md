# BoardBench v1 methodology and operator guide

BoardBench measures whether the normal PCBDraft product path can turn unseen
natural-language requests into small, reviewable KiCad projects. It is an
evidence campaign, not a model leaderboard, an ERC/DRC benchmark, an engineering
sign-off, or a production-readiness certificate.

The v1 campaign contains 20 tasks across five categories, with three independent
runs per task. All 60 planned runs remain in every denominator. A timeout,
provider outage, refusal, budget exhaustion, configuration drift, tool failure,
or incomplete project remains a run result; it is never silently removed or
replaced.

## Trust and holdout boundary

Before the first sealed campaign, keep the corpus outside the source checkout
and outside every directory or process context available to the tested Agent.
The outer BoardBench coordinator may read the complete corpus to select a case,
but the child receives only that case's natural-language prompt. Never put the
corpus path, evaluator contract, prepared IR, reference netlist, or corrected
project in the child arguments, environment, repository, prompt, or tool
results.

The operator CLI never searches for a corpus. Every command that needs one
requires `--corpus`; it validates the file but does not print its prompts or
reference contracts. Campaign, input, and output paths are likewise explicit.

Use one of these cohort labels truthfully:

- `sealed_holdout_baseline` is reserved for the first campaign whose 20 tasks
  were independently reviewed by a qualified human engineer, kept private from
  the tested Agent, and not used to tune PCBDraft before the campaign was frozen.
- `public_corpus_rerun` is required after the corpus has been published. It says
  only that the evaluator was hidden at runtime; it does not claim that the
  model had never encountered the public tasks.
- `ai_reviewed_pilot` is for a private corpus whose reference contracts have
  only AI technical review. It may exercise the real product path and preserve
  fixed-denominator evidence, but it is not human engineering sign-off, a
  sealed holdout baseline, or publishable BoardBench v1 evidence. The seal and
  publication commands reject this cohort even if all downstream artifacts are
  present.
- Deterministic, fake-provider, unit, CI, and release-smoke runs are
  `non_baseline_fixture` evidence. They prove software behavior only and must
  never be reported as BoardBench baseline or physical-board results.

If no independent human reviewer is available, use `ai_reviewed_pilot` rather
than filling engineer fields with an AI identity. Keep each AI review and its
limitations as separate provenance beside the private corpus; the cohort label
does not prove that review happened. Once a pilot prompt is shown to the tested
model or used to improve PCBDraft, it cannot later be relabeled as an unseen
sealed baseline. A future formal baseline needs a fresh, independently
human-reviewed holdout.

## Operator lifecycle

The examples use placeholders deliberately. Do not place a private corpus in
the repository just to make the commands shorter.

Validate and freeze a campaign against the currently configured default model
and environment:

```bash
uv run python scripts/boardbench.py corpus validate \
  --corpus /secure/boardbench-v1/corpus.json

uv run python scripts/boardbench.py campaign create \
  --corpus /secure/boardbench-v1/corpus.json \
  --output /private/boardbench-campaigns \
  --campaign-id boardbench-v1-001 \
  --wall-timeout-seconds 3600
```

`campaign create` creates
`/private/boardbench-campaigns/boardbench-v1-001/`. The manifest freezes the
corpus hash, model/provider configuration, PCBDraft commit and dirty-state hash,
KiCad/Python/platform identity, evaluator version, budgets, and all 60 run IDs.
The per-run hard wall fail-safe defaults to 3600 seconds and accepts values from
1 through 86400; it does not replace the frozen 500-tool-call budget or monitor
idle progress. Changing that timeout or environment requires a new campaign.

Execute all still-planned runs sequentially, or resume safely after an
interruption:

```bash
uv run python scripts/boardbench.py campaign run \
  --campaign /private/boardbench-campaigns/boardbench-v1-001 \
  --corpus /secure/boardbench-v1/corpus.json

uv run python scripts/boardbench.py campaign resume \
  --campaign /private/boardbench-campaigns/boardbench-v1-001 \
  --corpus /secure/boardbench-v1/corpus.json
```

Both commands use the same idempotent runner. Terminal receipts are verified
and skipped; only still-planned work runs. An interrupted `running` receipt is
closed explicitly rather than replayed as though it had never happened.

For a bounded pilot, repeat `--run-id` to execute only named entries from the
immutable campaign plan:

```bash
uv run python scripts/boardbench.py campaign run \
  --campaign /private/boardbench-campaigns/boardbench-pilot-001 \
  --corpus /secure/boardbench-pilot/corpus.json \
  --run-id CASE-ONE-RUN-1 \
  --run-id CASE-TWO-RUN-1
```

The selector does not create a smaller campaign: all 60 receipts are
initialized and returned, selected still-planned runs execute in frozen
campaign order, and unselected receipts are left unchanged (new receipts remain
`planned`; evidence from earlier invocations keeps its recorded state). Omitting
the selector retains the normal run/resume-all behavior. An unknown or duplicate
ID is rejected before any run starts. Existing terminal evidence across all 60
denominator entries is integrity-checked before a selected run begins.

Score each terminal run independently. The run ID must be one of the 60 IDs in
the frozen `campaign.json`:

```bash
uv run python scripts/boardbench.py score \
  --campaign /private/boardbench-campaigns/boardbench-v1-001 \
  --corpus /secure/boardbench-v1/corpus.json \
  --run-id CASE-RUN-ID
```

To resume scoring across the whole campaign, use `--all` instead of
`--run-id`. Runs are considered in frozen campaign order; nonterminal runs are
skipped, existing scores are source-validated and preserved, and only missing
terminal scores are created:

```bash
uv run python scripts/boardbench.py score \
  --campaign /private/boardbench-campaigns/boardbench-v1-001 \
  --corpus /secure/boardbench-v1/corpus.json \
  --all
```

Scoring reopens retained projects and reruns available validation. It does not
trust the Agent's completion statement or its earlier ERC/DRC receipts.

Reference contracts are opt-in per case. Component slots are matched
injectively, and a case that needs exact populated-BOM cardinality may list all
allowed slots with `forbidden_unmatched_bom_component`. Any additional
attributed `bom=true` component then fails that predicate. Attributed non-BOM
virtual items such as `PWR_FLAG` are ignored; provisional records cannot make
themselves exempt merely by declaring `bom=false`. A missing slot assignment or
unknown BOM classification remains `unknown`, never a pass. Corpus validation
also requires `support_circuits` to be applicable whenever this predicate is
present. Cases without the predicate retain the normal behavior of allowing
additional components.

Create source-bound review templates only after runs are terminal, preferably
after their automatic scores exist. Engineers must inspect the generated
snapshot before editing it, complete the JSON outside the immutable run tree,
and then import it:

```bash
uv run python scripts/boardbench.py review templates \
  --campaign /private/boardbench-campaigns/boardbench-v1-001 \
  --corpus /secure/boardbench-v1/corpus.json

uv run python scripts/boardbench.py review import \
  --campaign /private/boardbench-campaigns/boardbench-v1-001 \
  --corpus /secure/boardbench-v1/corpus.json \
  --submission /review-work/CASE-RUN-ID/review.json
```

Each template binds the canonical campaign, full corpus, complete hidden case,
terminal run, and (when present at template/import time) automatic score by
SHA-256. Checklist IDs include the case, kind, and requirement content in their
digest; they are not portable labels that can be replayed against a different
case. Template creation and import both require the campaign's exact corpus via
`--corpus`; reports, selection imports, sealing, and publication repeat the same
source checks.

Every case-authored `review_rubric` and `assembly_constraints` item is
applicable whenever the run retained a nonempty `.kicad_sch`. A completed
applicable review must give every item `pass` or `fail` plus a nonempty evidence
note; a passing outcome requires every item to be `pass`. Reviewers cannot mark
an individual case-authored obligation `not_applicable`. Whole-review
`not_applicable` is accepted only when the canonical terminal inventory has no
nonempty `.kicad_sch`; the inverse is also enforced, so an applicable outcome
cannot be asserted for a run without one. This inventory rule establishes that
a schematic artifact exists, not that the schematic is correct or readable by
every KiCad version.

`orderable_state` is backed by structured `orderability_evidence`, not a bare
enum and prose assertion. Each applicable review maps every case component slot
exactly once to a manufacturer part number, a typed status, an `as_of` UTC time,
a source kind/name, an HTTPS source URL, and a reviewer note. `pass` requires
all recorded statuses to be `orderable`; a known `not_orderable` item derives
`fail`, and any remaining `unknown` derives `unknown`. This is dated,
reviewer-attributed sourcing evidence only. PCBDraft does not query or guarantee
live stock, lead time, authorized-channel status, pricing, lifecycle continuity,
or future availability.

When a bound automatic score is not `pass`, a completed review must provide the
reviewer's `final_failure`. The score's `failure_suggestion` remains raw
automatic evidence and is never substituted into final report/seal failure
counts. Conversely, an automatic pass plus a no-change engineering pass cannot
carry a contradictory final failure classification.

Review records use schema version 3. Versions 1 and 2 are intentionally
rejected rather than guessed forward: v1 lacked source-authored checklist
evidence, and v2 lacked portable campaign/corpus/case/score binding, structured
orderability evidence, and canonical applicability enforcement. Regenerate
templates from the campaign's exact frozen corpus after scoring, then re-enter
all dispositions, orderability sources, and final classifications. Raw runs,
scores, and campaign records remain compatible. Any correction record bound to
an old review hash must be recaptured after the review is migrated.

For a modified candidate, retain the generated project, engineer-corrected
project, and optional manufacturing candidate as separate trees. Capture the
source-bound correction without editing anything below `runs/`:

```bash
uv run python scripts/boardbench.py correction capture \
  --campaign /private/boardbench-campaigns/boardbench-v1-001 \
  --run-id CASE-RUN-ID \
  --generated /private/boardbench-campaigns/boardbench-v1-001/runs/CASE-RUN-ID/artifacts/PROJECT \
  --corrected /review-work/CASE-RUN-ID/corrected \
  --manufacturing-candidate /review-work/CASE-RUN-ID/manufacturing-candidate
```

After engineering review and manufacturing-candidate validation, import a
selection covering all five categories. Physical evidence is imported only
after the external fabrication, assembly, and lab work has actually occurred:

```bash
uv run python scripts/boardbench.py selection import \
  --campaign /private/boardbench-campaigns/boardbench-v1-001 \
  --corpus /secure/boardbench-v1/corpus.json \
  --submission /hardware-work/selection.json

uv run python scripts/boardbench.py hardware import \
  --campaign /private/boardbench-campaigns/boardbench-v1-001 \
  --submission /hardware-work/CASE-RUN-ID
```

A release-backed hardware record additionally requires `--release-artifact`.
The importer checks its hash; it does not place an order, operate lab equipment,
or infer a result from a render, Gerber file, ERC, or DRC.

Draft reports preserve missing evidence as unknown or not reviewed:

```bash
uv run python scripts/boardbench.py report \
  --campaign /private/boardbench-campaigns/boardbench-v1-001 \
  --corpus /secure/boardbench-v1/corpus.json \
  --output /private/boardbench-reports/draft-001
```

Seal only after all 60 runs, scores, and completed reviews are present; every
modified candidate has a correction; failures are classified; selection covers
all five categories; and every selected run has physical evidence:

```bash
uv run python scripts/boardbench.py seal \
  --campaign /private/boardbench-campaigns/boardbench-v1-001 \
  --corpus /secure/boardbench-v1/corpus.json \
  --output /private/boardbench-reports/sealed-001

uv run python scripts/boardbench.py publication bundle \
  --campaign /private/boardbench-campaigns/boardbench-v1-001 \
  --corpus /secure/boardbench-v1/corpus.json \
  --report /private/boardbench-reports/sealed-001/report.json \
  --output /private/boardbench-publication/v1
```

The publication command creates a sanitized metadata bundle. It does not upload
anything. Publishing, ordering boards, or sending data to another service is a
separate external action requiring explicit authorization and a human review of
the bundle.

## Canonical campaign layout

Raw run evidence and derived evidence have separate authorities:

```text
campaign-root/
├── campaign.json
├── runs/<run_id>/
│   ├── run.json
│   └── artifacts/                 immutable prompt/trace/project evidence
├── scores/<run_id>/
│   ├── score.json
│   └── evaluation.json
├── review-templates/<run_id>/review.json
├── reviews/<run_id>/review.json
├── corrections/<run_id>/
│   ├── correction.json
│   └── artifacts/                 generated/corrected/candidate snapshots + diff
├── selection.json
└── hardware/<run_id>/
    ├── hardware.json
    └── attachments/
```

All JSON artifacts use closed versioned schemas, bounded readers, atomic writes,
private modes, relative inventories, and source hashes. Derived evidence never
rewrites a terminal `run.json` or its `artifacts/`. A correction copies the
generated and corrected trees and records normalized semantic/native changes in
components, identities, footprints, nets, power/rules, board geometry,
placement, routes, and files.

Reports, seal manifests, and publication bundles go to fresh explicit output
directories. They are projections of the campaign evidence, not mutable fields
inside raw runs.

## Denominators and engineering review counting

- The run denominator is always the 60 entries in `campaign.json`, including
  infrastructure failures and absent or unknown downstream evidence.
- Case summaries use three planned runs. Category summaries use twelve. Missing
  evidence is not a pass and is not removed from either denominator.
- `pass_without_schematic_change` is the only no-schematic-change engineering
  approval. `pass_after_changes` is not counted as an unmodified success.
- `not_applicable` remains a reviewed run in the denominator and requires a
  concrete reason; canonical import permits it only when terminal inventory has
  no nonempty `.kicad_sch`.
- `not_reviewed` is a campaign-progress state. A baseline cannot be sealed while
  any review remains in that state.
- Modification count means independent engineering decisions, not mouse clicks,
  saved files, or diff lines. Record presentation-only cleanup separately from
  functional, manufacturing, firmware, or documentation decisions.
- Active engineer minutes count hands-on review and correction time from the
  generated result to an orderable candidate. Do not include unattended model,
  CI, board-house, shipping, or queue time.
- ERC/DRC findings and engineer findings remain separate. Passing ERC/DRC is
  necessary evidence, not proof of circuit function, ratings, assembly,
  manufacturability, thermal/EMC/SI behavior, safety, or production readiness.

## Cost interpretation

Cost is evidence with provenance, not a number filled in for every run:

- `actual` has a reported amount and currency from a billing or provider source.
- `estimated` has an amount and currency derived from documented rates; it is
  not an invoice or guaranteed charge.
- `subscription_included` means the request was covered by a subscription and
  has no defensible per-run currency amount.
- `unknown` means cost evidence is unavailable or incomplete and has no amount.

Never convert `subscription_included` or `unknown` to zero, and never sum them
into priced totals. Report the distribution of cost statuses alongside any sum
of `actual` and `estimated` amounts. Token and trace gaps likewise remain
reported/derived/partial/unknown rather than becoming invented values.

## Physical bring-up boundary

The software stops at validating and importing attributed evidence. A qualified
engineer remains responsible for fabrication review, purchasing approval,
assembly inspection, firmware preparation, and a written board-specific lab
procedure.

Before first power, inspect the unpowered board for assembly errors and shorts,
confirm polarity and component orientation, verify the intended input range and
expected rails, and choose a protected supply with a conservative current limit.
Use suitable fusing, isolation, grounding, fixtures, PPE, and emergency shutdown
for the board's energy and regulatory domain. Do not use this generic workflow
for mains, hazardous voltage, high stored energy, batteries, motors, heaters, or
safety-critical hardware without the applicable specialist procedure and
supervision.

Record the actual first-power current/short observation, measured rails with
units and tolerances, firmware-download result, core-function test, revision,
serial, operator, date, test procedure, and non-empty attachments. If a step was
not performed, use `not_tested`; never infer a physical pass from software or
manufacturing files.

## What the software fixture proves

Unit tests and deterministic fake-provider flows may exercise the complete
artifact lifecycle without paid model calls. Such a fixture proves parsing,
isolation, source-hash linkage, fixed denominators, and fail-closed publication.
It does not prove that 20 requests were unseen, that a real model designed a
board correctly, that an engineer approved it, or that hardware was fabricated
and powered. Keep the `non_baseline_fixture` label visible in retained fixture
evidence and never merge its results into a real campaign.
