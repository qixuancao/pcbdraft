#!/usr/bin/env bash
# Installs pcb-agent-runtime into this repository's uv environment.
# Run: scripts/deploy.sh
# Requires: uv, codex, kicad-cli, and git on PATH.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

cd "$REPO_DIR"
uv sync --frozen
.venv/bin/pcb-agent doctor --json