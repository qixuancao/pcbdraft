#!/usr/bin/env bash
# Runs lint, format, syntax, offline, and available real-KiCad tests.
# Run: scripts/test.sh
# Requires: uv. Codex is faked; compatible installed KiCad enables native tests.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

cd "$REPO_DIR"
uv sync --frozen --extra dev
uv run ruff check src tests
uv run ruff format --check src tests
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src tests
git diff --check
