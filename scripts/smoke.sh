#!/usr/bin/env bash
# Smoke-tests doctor, real KiCad ERC/DRC, and optional live model API review.
# Run: REAL_MODEL=0 scripts/smoke.sh (set DEMO_DIR to override the KiCad demo).
# Requires: uv, kicad-cli, and a configured model when REAL_MODEL=1.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
DEMO_SOURCE=${DEMO_DIR:-/usr/share/kicad/demos/ecc83}
REAL_MODEL=${REAL_MODEL:-0}

if [[ ! -d "$DEMO_SOURCE" ]]; then
    echo "smoke: KiCad demo directory not found: $DEMO_SOURCE" >&2
    exit 2
fi

SMOKE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/pcbdraft-smoke.XXXXXX")
cleanup() {
    rm -rf -- "$SMOKE_ROOT"
}
trap cleanup EXIT INT TERM

PROJECT_COPY="$SMOKE_ROOT/project"
RUNS_DIR="$SMOKE_ROOT/runs"
cp -a -- "$DEMO_SOURCE" "$PROJECT_COPY"

cd "$REPO_DIR"
uv run ./pcbdraft doctor --json

mapfile -t SCHEMATICS < <(find "$PROJECT_COPY" -maxdepth 1 -type f -name '*.kicad_sch' -print | sort)
mapfile -t BOARDS < <(find "$PROJECT_COPY" -maxdepth 1 -type f -name '*.kicad_pcb' -print | sort)
if [[ ${#SCHEMATICS[@]} -eq 0 || ${#BOARDS[@]} -eq 0 ]]; then
    echo "smoke: copied demo has no root-level schematic/board" >&2
    exit 2
fi

GATE_DIR="$SMOKE_ROOT/direct-gates"
mkdir -m 700 -- "$GATE_DIR"
timeout 120s kicad-cli sch erc --format json --severity-error --severity-warning \
    --output "$GATE_DIR/erc.json" "${SCHEMATICS[0]}"
timeout 120s kicad-cli pcb drc --format json --severity-error --severity-warning \
    --output "$GATE_DIR/drc.json" "${BOARDS[0]}"
chmod 600 "$GATE_DIR/erc.json" "$GATE_DIR/drc.json"

if [[ "$REAL_MODEL" == "1" ]]; then
    # ecc83 currently contains two matching root projects; select one without
    # touching the system demo so PCBDraft's ambiguity behavior remains strict.
    SELECTED="$SMOKE_ROOT/selected-project"
    mkdir -m 700 -- "$SELECTED"
    STEM=$(basename -- "${SCHEMATICS[0]}" .kicad_sch)
    cp -a -- "$PROJECT_COPY/." "$SELECTED/"
    find "$SELECTED" -maxdepth 1 -type f \( -name '*.kicad_sch' -o -name '*.kicad_pcb' \) \
        ! -name "$STEM.kicad_sch" ! -name "$STEM.kicad_pcb" -delete
    timeout 960s uv run ./pcbdraft review "$SELECTED" --output "$RUNS_DIR" --timeout 900
else
    echo "smoke: REAL_MODEL=0, skipped live model API review"
fi

echo "smoke: doctor and real KiCad ERC/DRC completed on a temporary copy"
echo "smoke: successful tool execution does not imply a clean design; review the reported counts"
