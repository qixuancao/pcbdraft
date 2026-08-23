# First real BoardBench Luna smoke

Date: 2026-08-22

This was the first real, prompt-only BoardBench run through the normal Hermes
Agent and PCBDraft/KiCad tool path. It used the configured
`openai-codex / gpt-5.6-luna` model and the `mcu-01-attiny-updi` case. It was an
AI-reviewed pilot run, not a human-reviewed or sealed baseline.

## Outcome

- The run terminated normally after 641 seconds and retained a KiCad project,
  schematic, PCB, semantic IR, transaction history, validation evidence,
  previews, BOM, Gerbers, drill output, placement output, and STEP output.
- The model made 77 requests. Provider usage reported 163,963 input tokens,
  6,303 output tokens, 1,617 reasoning tokens, and subscription-included cost
  rather than a dollar amount.
- The retained trace contains 79 PCB tool attempts, including 3 policy-denied
  attempts. No provider error or retry was recorded.
- The generated board did **not** pass DRC. Hard findings were two courtyard
  overlaps, one 0.105 mm clearance where 0.200 mm was required, and a missing
  physical connection between the VCC trace and J2 pin 1. Several silkscreen
  warnings were also retained.
- The final Agent response did not falsely claim production readiness: it
  explicitly reported DRC failure and said the board should not be ordered.

## Evaluator defects exposed by the run

The original v2 score is retained unchanged. It incorrectly stopped at
`complete_project` because a normal blank-project product flow has an empty
top-level IR provenance list even though the strict managed manifest and all
required files are present. Its trace reducer also treated policy-blocked calls
as unmatched tool ends, making the tool count unknown.

Both defects were fixed in evaluator v3 with focused regression tests. A
read-only check against the retained run now recognizes the project as complete
and reduces the trace to 79 tool calls with 3 denied calls. This does not erase
or excuse the real DRC and connection failures above.

## Initial attribution

- Product result: failed at layout/routing/validation, with model/tool behavior
  contributing.
- Evidence infrastructure: evaluator v2 misclassified project completeness and
  tool-count coverage; fixed before the next campaign.
- No schematic-change engineering approval or physical result is claimed yet.
