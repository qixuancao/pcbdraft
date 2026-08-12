#!/usr/bin/env bash
# Benchmarks pcb-agent review and safe patch on a KiCad project copy.
# Run: FIXTURE_DIR=/path/to/project scripts/benchmark.sh
# Needs: pcb-agent prerequisites; writes only under BENCH_ROOT.
set -euo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
: "${FIXTURE_DIR:?set FIXTURE_DIR to one unambiguous KiCad project}"
FIXTURE_DIR=$(realpath -e -- "$FIXTURE_DIR")
BENCH_ROOT=$(realpath -m -- "${BENCH_ROOT:-/tmp/pcb-agent-runtime-benchmark}")
TIMEOUT=${TIMEOUT:-420}
REQUEST=${REQUEST:-}

if [[ "$BENCH_ROOT" == / || "$BENCH_ROOT" == "$HOME" || "$BENCH_ROOT" == "$ROOT_DIR" ||
      "$FIXTURE_DIR" == "$BENCH_ROOT" || "$FIXTURE_DIR" == "$BENCH_ROOT/"* ]]; then
  printf 'unsafe BENCH_ROOT: %s\n' "$BENCH_ROOT" >&2
  exit 2
fi

rm -rf -- "$BENCH_ROOT"
install -d -m 700 "$BENCH_ROOT/project" "$BENCH_ROOT/runs"
cp -a -- "$FIXTURE_DIR"/. "$BENCH_ROOT/project"/

start=$SECONDS
"$ROOT_DIR/pcb-agent" review "$BENCH_ROOT/project" \
  --output "$BENCH_ROOT/runs" --timeout "$TIMEOUT"
review_seconds=$((SECONDS - start))

if [[ -n "$REQUEST" ]]; then
  start=$SECONDS
  "$ROOT_DIR/pcb-agent" patch "$BENCH_ROOT/project" \
    --request "$REQUEST" --output "$BENCH_ROOT/runs" --timeout "$TIMEOUT"
  patch_seconds=$((SECONDS - start))
else
  patch_seconds=null
fi

printf '{"bench_root":"%s","review_seconds":%d,"patch_seconds":%s}\n' \
  "$BENCH_ROOT" "$review_seconds" "$patch_seconds"
