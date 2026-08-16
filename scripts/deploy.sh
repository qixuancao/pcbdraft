#!/usr/bin/env bash
# Installs PCBDraft into this repository's uv environment.
# Run: scripts/deploy.sh
# Requires: uv, stable KiCad 10.0.x, and git. A model API is optional for AI review.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

cd "$REPO_DIR"
uv sync --frozen
.venv/bin/pcbdraft doctor --json
