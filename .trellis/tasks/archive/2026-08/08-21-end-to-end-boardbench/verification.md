# BoardBench software integration verification

Recorded on 2026-08-21 (Asia/Shanghai). This record covers Milestone 9 only;
it is not evidence for the private holdout, real-model campaign, engineering
reviews, manufacturing, or hardware bring-up.

## Environment

- Development test interpreter: CPython 3.13.14 through `uv`.
- Installed-distribution smoke interpreter: CPython 3.11.
- KiCad CLI/pcbnew: 10.0.5.
- Final test inventory: 522 tests, with 3 environment-conditional skips.

## Final passing gates

- `git diff --check`: passed.
- `uv run ruff check src tests`: passed.
- `uv run ruff format --check src tests`: passed; 163 files checked.
- `uv run mypy`: passed; 96 source files checked.
- `scripts/test.sh`: passed; 522 tests, 3 skipped, 1107.468 seconds,
  73% aggregate coverage.
- `scripts/release-check.sh`: passed end to end in approximately 20 minutes.
  Its embedded 522-test suite passed, then wheel and sdist construction,
  archive checks, isolated wheel installation, installed package import, the
  bundled 90-case corpus load, and the installed-distribution Hermes/KiCad
  smoke all passed.
- The final installed smoke made 17 flat PCB tool calls after the natural
  language request, retained matching `.kicad_pro`, `.kicad_sch`, and
  `.kicad_pcb` files plus the trace, and recorded passing real ERC and DRC
  receipts. It explicitly retained `production_ready=false` and
  `production_claimed=false`.

## Failures found and fixed during the gate

The first full run failed rather than being summarized as passing. It exposed:

- unittest discovery shadowing the vendored Hermes `agent` package with
  `tests/agent`;
- retained legacy in-process handlers not being enabled for the compatibility
  runtime, while ordinary/Hermes/MCP execution remained flat-tool-only;
- KiCad bidirectional placement import and the 90-case repair benchmark still
  emitting the obsolete `update_component.placement` operation;
- a stale package-structure assertion that omitted the internal BoardBench
  worker;
- a release-smoke GND plane with one through-hole thermal anchor, producing a
  real KiCad `starved_thermal` DRC error;
- the smoke validator reading the outer tool envelope instead of the inner
  check receipt for the no-production-readiness assertion.

The failed release attempt completed its embedded suite (522 tests, 3 skipped,
1104.362 seconds) but stopped at the installed-distribution DRC check. The
fixture was corrected by adding a GND stitching via on the routed GND trace;
DRC strictness was not reduced or ignored. A retained source smoke then passed,
and the complete release script was rerun from the beginning and passed.

## Checks left to later milestones

None for Milestone 9. Milestones 6–8 and 10–11 remain deliberately unsatisfied:
they require a private engineer-reviewed holdout, 60 paid/default real-model
runs, 60 engineering reviews, purchasing/assembly/lab evidence for five boards,
and an explicitly authorized external publication.

## Milestone 6 draft status

A private authoring directory outside the repository now contains 20 original
Chinese-language prompts and closed reference contracts, balanced at four cases
per BoardBench category. The draft corpus validates under the production loader.
Its current draft SHA-256 is
`b6eb79f3e3cab5d9f935531004854369f6cbc278e09f0749b6d62a12706c8067`.

The accompanying environment check passed on KiCad 10.0.5: all 15 referenced
bundled parts match their recorded symbol and footprint, and every referenced
pin in all 20 cases resolves uniquely. A source-bound independent-engineering
review template and explicit coverage-decision checklist were generated beside
the corpus.

This is deliberately still a draft. The corpus has not been independently
reviewed, frozen, exposed to the tested Agent, used for product tuning, or used
for a real-model run. No 60-entry campaign manifest has been created. The
reviewer must explicitly accept or reject the narrower bundled-part-only scope,
which currently omits Buck, MOSFET, relay, motor, level-shifter, USB-C, and
RS-485 cases, before the freeze gate can pass.

## Milestone 6 review assertion and fail-closed decision

Recorded on 2026-08-22 after the conversation user replied “审查通过”. A
source-bound user-assertion record was written beside the private draft. It is
bound to corpus SHA-256
`b6eb79f3e3cab5d9f935531004854369f6cbc278e09f0749b6d62a12706c8067`,
review-template SHA-256
`4dd952da8a043714f20fe3fe63b046368d736fc1c102fc3179ea4b52bfc9d541`,
and availability-evidence SHA-256
`4188869c60b0faca2b37f24fc6c9f911d5d1dd6c5cba69af524f3f1c4febb229`.

The assertion truthfully establishes user approval of the displayed review
package. It does not provide reviewer identity or qualification, independence
from authoring, review start/completion timestamps, datasheet evidence, or the
180 required per-case boolean decisions in the review template. Those facts
were not inferred or fabricated. A machine-readable missing-evidence decision
was saved in the private draft directory.

The M6 gate therefore remains blocked. The corpus was not copied to a freeze
directory, no campaign manifest or run receipt was created, the prompts remain
unused for PCBDraft tuning, and no real or paid model call was started. The next
accepted input is a completed copy of `engineer-review.draft.json`, bound to the
unchanged corpus hash, with all top-level reviewer/independence/time/coverage
fields and every required per-case decision completed.

## AI-reviewed pilot software track

Recorded on 2026-08-22 after the user authorized an AI-only pilot because no
independent human reviewer is currently available. This is a separate progress
track and does not change the failed-closed Milestone 6 record above.

The strict contracts now accept an explicit `ai_reviewed_pilot` cohort for the
corpus, campaign, and unsealed report. A pilot report states that it is not an
independent human-reviewed or sealed baseline. Both direct construction of a
sealed pilot report and the canonical campaign seal/publication paths reject
the cohort before producing output.

The runner and operator CLI also accept repeatable explicit `--run-id`
selection. A selected invocation still initializes and returns the full set of
60 receipts, executes only selected still-planned ids in frozen campaign order,
and leaves every unselected receipt planned. Omitting the selector preserves the
original run/resume-all behavior; duplicate or unknown selectors fail before
execution.

Focused verification completed:

- Five new boundary tests passed individually in 0.761 seconds.
- `tests.verification.test_boardbench`, `test_boardbench_runner`,
  `test_boardbench_report`, and `test_boardbench_cli`: 69 tests passed in
  9.312 seconds.
- Ruff check and format check passed for all eight changed Python/test files.
- Focused mypy passed for the three changed production modules.
- `git diff --check` passed.
- `scripts/boardbench.py campaign run --help` displayed the repeatable bounded
  selector.

No private corpus bytes or review records were modified by this software change,
no campaign was frozen, and no real or paid model call was made. Completing two
AI technical reviews and creating a separately named pilot corpus/campaign are
still evidence steps; they cannot be recorded as human engineering approval or
counted toward Milestones 6–8.

## Full AI-reviewed pilot campaign

Recorded on 2026-08-23 after the user explicitly authorized all 60 planned
real-model runs. The retained campaign is
`ai-pilot-r4-luna-v4-60m-parallel4-full-20260822`, labeled
`ai_reviewed_pilot`, using `openai-codex` / `gpt-5.6-luna`. Four disjoint
run-id queues executed concurrently; the campaign plan remained fixed at 60.

All 60 receipts are terminal: 59 returned normally and one reached the recorded
60-minute wall limit. No failed or timed-out run was replaced. All 60 runs have
automatic score records. A draft report records 4 automatic passes, 46 fails,
and 10 unknown results. Every run produced a complete project; the independent
checks passed library resolution for 55, reference topology for 37, ERC for 34,
DRC for 20, support-circuit requirements for 43, and ratings for 36, with 21
ratings remaining unknown.

Two Codex reviewers then inspected disjoint 30-run sets against the retained
corpus, source-bound review templates, automatic scores, traces, semantic IR,
and native KiCad artifacts. Their results remain under `ai-reviews/` and were
not imported as canonical human engineering reviews. An independent Codex check
confirmed exactly-once coverage of all 60 planned runs, agreement with all 60
automatic states, terminal receipt consistency, and that every cited evidence
path stayed inside the campaign.

The combined AI-only judgment is 50 functionally correct schematics and 10
functionally incorrect schematics. Twenty-seven candidates were estimated to
need at least one schematic change before ordering. The reviewers estimated
204 independent modification decisions and 3,590 active engineering minutes;
these are AI estimates with different reviewer calibration, not measured human
work. Primary failure-stage judgments were routing 30, validation 9, layout 9,
circuit design 7, KiCad materialization 3, and no identified failure 2.

This evidence does not satisfy Milestones 6–8: no qualified independent human
review has been imported, the pilot corpus has been exposed to the tested model,
no corrected snapshots have been captured, and no orderability, fabrication,
assembly, first-power, firmware, or functional bench evidence exists. The
canonical `reviews/` directory remains absent, so the pilot cannot seal or
publish as BoardBench v1.

## User-directed factual closure

Recorded on 2026-08-23 after the user asked to end the task because no human
engineer is currently available. The task is closed with a partial outcome:
the BoardBench software, full 60-run AI-reviewed pilot, automatic scoring, draft
report, and separate Codex review evidence are complete. The requested sealed
BoardBench v1 is not complete. Its independent human review, corrected-project
diffs, five-board manufacturing/bring-up, seal, and publication milestones stay
unchecked and must not be inferred from the Trellis lifecycle status
`completed` that archive writes.

Final focused quality verification passed `git diff --check`, Ruff check and
format for BoardBench and its direct compatibility changes, targeted mypy for
the seven BoardBench production modules, 158 BoardBench unit tests, the focused
KiCad sync test, the focused 90-case benchmark repair test, and shell syntax.
The full suite and release check were not rerun during closure; their earlier
successful integration-gate evidence remains recorded above.
