# CopperWright benchmark report

Measured on 2026-08-13. The machine-readable result is
[`artifacts/benchmark/benchmark-20260812.json`](artifacts/benchmark/benchmark-20260812.json).

## Independent corpus result

The bundled `independent-low-voltage-control-v1` corpus contains 90 independently
authored CC0 cases: 70 single-fault injections and 20 clean controls across 28
categories. It was created from generic low-voltage MCU/I2C/control invariants;
no competitor fixture, annotation, output, or label was used.

The deterministic semantic engine ran each case five times (450 evaluations):

| Metric | Result |
|---|---:|
| True positive / false negative | 70 / 0 |
| False positive / true negative | 0 / 20 |
| Recall / precision / specificity | 1.000 / 1.000 / 1.000 |
| False-positive rate | 0.000 |
| Expected rule-code recall | 1.000 |
| Repairable cases attempted/succeeded | 65 / 65 |
| Repairs introducing a new finding | 0 |
| Stable cases across five repetitions | 90 / 90 |
| Mean / median / p95 rule latency | 3.487032 / 2.913661 / 5.328245 ms |
| Deterministic benchmark wall time | 2.406665 s |

These numbers show regression performance on this bounded corpus, not population
accuracy for arbitrary electronics. The clean controls are variations of the same
base design, cases are not statistically independent field failures, geometry is
small-board geometry, and no RF/SI/PI/thermal/EMI/safety/physical outcome is being
predicted.

## Blinded live-model consistency

The final optional model run selected a SHA-256-stratified subset of 24
model-eligible cases (16 faults, 8 clean). Expected labels and rule codes were not
included in the prompt. It used the authenticated local Codex CLI with
`gpt-5.6-sol`, reasoning `max`, service tier `default`, Fast off, hooks off,
network off, and multi-agent off.

| Metric | Result |
|---|---:|
| Requested/completed independent repetitions | 2 / 2 |
| Correct predictions | 48 / 48 |
| Pairwise agreement | 1.000 |
| Unanimous cases | 24 / 24 |
| Run durations | 115.079 s, 48.005 s |

This is a consistency smoke, not evidence that a model should replace deterministic
rules or engineering review. Two repetitions and 24 cases are too small for broad
model comparisons; service behavior can change, and the selected cases are derived
from the same acceptance design. Raw structured outputs and receipts are preserved
beside the result artifact.

## Methodology

For every case the runner:

1. compiles the clean CC0 requirements fixture through the production compiler;
2. verifies that the clean baseline has no semantic findings;
3. applies one declared mutation, or a non-fault control transformation;
4. evaluates the same semantic rules used by managed-project validation;
5. records all finding codes and checks the target code for fault cases;
6. repeats and hashes normalized findings to measure repeatability;
7. for repairable cases, creates a public typed change set, applies it through the
   production operation engine, compares canonical bytes with the clean design,
   and checks for introduced findings;
8. records monotonic latency and confusion/repair statistics.

Corpus loading enforces exact schemas, unique IDs, known injection operations,
fault/control counts, CC0 declaration, model eligibility, and expected codes.

## Reproduce

Deterministic only:

```bash
scripts/benchmark.sh
# or choose a create-only output
BENCHMARK_OUTPUT=/tmp/benchmark.json REPETITIONS=5 scripts/benchmark.sh
```

Blinded model repetitions require an authenticated Codex CLI:

```bash
BENCHMARK_OUTPUT=/tmp/benchmark-model.json MODEL_RUNS=2 scripts/benchmark.sh
```

The corpus and methodology are in
[`src/copperwright/data/benchmark`](src/copperwright/data/benchmark).

## Historical competitor smoke

Before the semantic runtime existed, version 0.1.0 was compared with KiCad MCP Pro
on three defect fixtures from that competitor's own repository. The reviewer found
2/3 named defects in a mean 67.82 s; the competitor found 3/3 in 16.73 s. That
result correctly rejected any claim that the old reviewer was superior.

The historical comparison is not combined with the independent metrics above:
three competitor-owned fixtures are biased and too small; they lack clean controls,
repair/regression/repeatability measurements; and the current deterministic runtime
solves a different task. The old timing entry point remains
`scripts/reviewer-smoke-benchmark.sh` for transparent reproduction with a supplied
single-project fixture.
