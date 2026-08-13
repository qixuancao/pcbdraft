#!/usr/bin/env bash
# Full local release acceptance: tests, packages, clean install, benchmark, E2E.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
CHECK_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/copperwright-release-check.XXXXXX")
cleanup() {
    rm -rf -- "$CHECK_ROOT"
}
trap cleanup EXIT INT TERM

cd "$REPO_DIR"
scripts/test.sh

DIST_DIR="$CHECK_ROOT/dist"
uv build --out-dir "$DIST_DIR"
WHEEL=$(find "$DIST_DIR" -maxdepth 1 -type f -name '*.whl' -print -quit)
SDIST=$(find "$DIST_DIR" -maxdepth 1 -type f -name '*.tar.gz' -print -quit)
if [[ -z "$WHEEL" || -z "$SDIST" ]]; then
    printf 'wheel or sdist missing\n' >&2
    exit 2
fi
uv run python -m zipfile -t "$WHEEL"
tar -tzf "$SDIST" >/dev/null

uv venv --python 3.11 "$CHECK_ROOT/venv"
uv pip install --python "$CHECK_ROOT/venv/bin/python" "$WHEEL"
"$CHECK_ROOT/venv/bin/copperwright" --version
"$CHECK_ROOT/venv/bin/pcb-agent" --version
"$CHECK_ROOT/venv/bin/python" -c \
    'from pcb_agent.benchmark import load_corpus; assert len(load_corpus()[1]) == 90'

COPPERWRIGHT_EXE="$CHECK_ROOT/venv/bin/copperwright" \
COPPERWRIGHT_PYTHON="$CHECK_ROOT/venv/bin/python" \
    scripts/chat-e2e.sh "$CHECK_ROOT/chat-e2e"
uv run python scripts/browser-e2e.py \
    --executable "$CHECK_ROOT/venv/bin/copperwright" \
    --output "$CHECK_ROOT/browser-e2e"

"$CHECK_ROOT/venv/bin/copperwright" benchmark \
    "$CHECK_ROOT/benchmark.json" --repetitions 2 --json
"$CHECK_ROOT/venv/bin/copperwright" generate \
    "$REPO_DIR/examples/attiny_sensor_controller/requirements.json" \
    "$CHECK_ROOT/project" --json
"$CHECK_ROOT/venv/bin/copperwright" validate \
    "$CHECK_ROOT/project" --output "$CHECK_ROOT/validation" --json
"$CHECK_ROOT/venv/bin/copperwright" release \
    "$CHECK_ROOT/project" "$CHECK_ROOT/release-a" --json
"$CHECK_ROOT/venv/bin/copperwright" release \
    "$CHECK_ROOT/project" "$CHECK_ROOT/release-b" --json
"$CHECK_ROOT/venv/bin/copperwright" release-verify \
    "$CHECK_ROOT/release-a" --json

"$CHECK_ROOT/venv/bin/python" -c \
    'import json, pathlib, sys; a=json.loads((pathlib.Path(sys.argv[1])/"receipt.json").read_text()); b=json.loads((pathlib.Path(sys.argv[2])/"receipt.json").read_text()); assert a["manifest_sha256"] == b["manifest_sha256"]; assert a["archive_sha256"] == b["archive_sha256"]' \
    "$CHECK_ROOT/release-a" "$CHECK_ROOT/release-b"

printf 'release check passed: wheel, sdist, clean install, chat/browser E2E, benchmark, and release\n'
