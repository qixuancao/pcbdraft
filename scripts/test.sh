#!/usr/bin/env bash
# Runs the bounded offline test suite and syntax checks.
# Run: scripts/test.sh
# Requires: uv; tests use local fake Codex and KiCad executables.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

cd "$REPO_DIR"
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src tests
git diff --check