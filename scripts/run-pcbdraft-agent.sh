#!/usr/bin/env bash
# Run PCBDraft's DeepSeek Harness PCB agent with its isolated profile.
# Run: scripts/run-pcbdraft-agent.sh 'Design a low-power sensor board'
# Needs: scripts/setup-deepseek-harness.sh completed and a runtime DeepSeek credential.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PCBDRAFT_ROOT="${PCBDRAFT_ROOT:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
DSH_ROOT="${DSH_ROOT:-/mnt/2T/deepseek-harness}"
DSH_HOME="${DSH_HOME:-$PCBDRAFT_ROOT/.dsh}"
DSH_PROFILE="${DSH_PROFILE:-headless}"
PATCH_FILE="$DSH_HOME/pcbdraft-pcb.patch.yml"

[[ -f "$PATCH_FILE" ]] || { echo "run scripts/setup-deepseek-harness.sh first" >&2; exit 1; }
[[ -f "$DSH_ROOT/package.json" ]] || { echo "missing DeepSeek Harness source" >&2; exit 1; }
[[ $# -gt 0 ]] || { echo "usage: $0 'describe the PCB to generate'" >&2; exit 2; }

export DSH_HOME
export DSH_TOOLS_MODE=native
cd "$DSH_ROOT"
exec pnpm dsh --profile "$DSH_PROFILE" --patch "$PATCH_FILE" "$@"
