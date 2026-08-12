#!/usr/bin/env bash
# Installs CopperWright into this repository's uv environment.
# Run: scripts/deploy.sh
# Requires: uv, KiCad 10, and git. Codex is optional for AI review.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

cd "$REPO_DIR"
uv sync --frozen
.venv/bin/copperwright doctor --json
