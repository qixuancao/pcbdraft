#!/usr/bin/env bash
# Run the independent CC0 semantic fault/repair benchmark.
# Optional: BENCHMARK_OUTPUT=/new/file.json MODEL_RUNS=2 REPETITIONS=5.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
REPETITIONS=${REPETITIONS:-5}
MODEL_RUNS=${MODEL_RUNS:-0}
MODEL_TIMEOUT=${MODEL_TIMEOUT:-420}

if [[ -n "${BENCHMARK_OUTPUT:-}" ]]; then
    OUTPUT=$BENCHMARK_OUTPUT
else
    BENCHMARK_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/pcbdraft-benchmark.XXXXXX")
    OUTPUT="$BENCHMARK_ROOT/benchmark.json"
fi

cd "$REPO_DIR"
uv run pcbdraft benchmark "$OUTPUT" \
    --repetitions "$REPETITIONS" \
    --model-runs "$MODEL_RUNS" \
    --model-timeout "$MODEL_TIMEOUT" \
    --json
printf 'benchmark artifact: %s\n' "$OUTPUT"
