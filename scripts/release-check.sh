#!/usr/bin/env bash
# Full local release acceptance: tests, packages, clean install, and TUI E2E.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
CHECK_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/pcbdraft-release-check.XXXXXX")
cleanup() {
    "$REPO_DIR/scripts/clean.sh" >/dev/null
    rm -rf -- "$CHECK_ROOT"
}
trap cleanup EXIT INT TERM

cd "$REPO_DIR"
scripts/test.sh
"$REPO_DIR/scripts/clean.sh"

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
uv run python -c \
    'import sys, tarfile; names=tarfile.open(sys.argv[1]).getnames(); required=("/constraints/build.txt", "/constraints/runtime.txt", "/scripts/tui-e2e.py", "/src/pcbdraft/interfaces/tui/styles.tcss"); assert all(any(name.endswith(item) for name in names) for item in required)' \
    "$SDIST"

uv venv --python 3.11 "$CHECK_ROOT/venv"
uv pip install \
    --python "$CHECK_ROOT/venv/bin/python" \
    --constraints "$REPO_DIR/constraints/runtime.txt" \
    "$WHEEL"
"$CHECK_ROOT/venv/bin/pcbdraft" --version
"$CHECK_ROOT/venv/bin/python" -c \
    'from pcbdraft.verification.benchmark import load_corpus; assert len(load_corpus()[1]) == 90'
"$CHECK_ROOT/venv/bin/python" -c \
    'from importlib.resources import files; assert files("pcbdraft").joinpath("interfaces", "tui", "styles.tcss").is_file()'

uv run python scripts/tui-e2e.py \
    --executable "$CHECK_ROOT/venv/bin/pcbdraft" \
    --output "$CHECK_ROOT/tui-e2e"

printf 'release check passed: wheel, sdist, clean install, and TUI E2E\n'