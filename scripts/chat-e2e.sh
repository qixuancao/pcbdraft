#!/usr/bin/env bash
# Real scriptable chat lifecycle: clarify, review, confirm, change, undo, release.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
OUTPUT=${1:-}
if [[ -z "$OUTPUT" ]]; then
    OUTPUT=$(mktemp -d "${TMPDIR:-/tmp}/copperwright-chat-e2e.XXXXXX")
else
    mkdir -p -- "$OUTPUT"
    OUTPUT=$(realpath -- "$OUTPUT")
fi

if [[ -n "${COPPERWRIGHT_EXE:-}" ]]; then
    COPPERWRIGHT_COMMAND=("$COPPERWRIGHT_EXE")
else
    COPPERWRIGHT_COMMAND=(uv run copperwright)
fi
if [[ -n "${COPPERWRIGHT_PYTHON:-}" ]]; then
    PYTHON_COMMAND=("$COPPERWRIGHT_PYTHON")
else
    PYTHON_COMMAND=(uv run python)
fi

WORKSPACE="$OUTPUT/workspace"
cd "$REPO_DIR"

"${COPPERWRIGHT_COMMAND[@]}" chat \
    --workspace "$WORKSPACE" --provider builtin \
    --new "CLI temperature controller" \
    --message "Create a TMP102 I2C temperature sensor and controller board" \
    --json >"$OUTPUT/01-clarification.json"

PROJECT_ID=$("${PYTHON_COMMAND[@]}" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["project"]["id"])' \
    "$OUTPUT/01-clarification.json")

"${COPPERWRIGHT_COMMAND[@]}" chat \
    --workspace "$WORKSPACE" --provider builtin --project "$PROJECT_ID" \
    --message "2 layers" --json >"$OUTPUT/02-reviewed-brief.json"
"${COPPERWRIGHT_COMMAND[@]}" chat \
    --workspace "$WORKSPACE" --provider builtin --project "$PROJECT_ID" \
    --yes --json >"$OUTPUT/03-generated-validated.json"
"${COPPERWRIGHT_COMMAND[@]}" chat \
    --workspace "$WORKSPACE" --provider builtin --project "$PROJECT_ID" \
    --message "Change this board to 4 layers" --json \
    >"$OUTPUT/04-semantic-preview.json"
"${COPPERWRIGHT_COMMAND[@]}" chat \
    --workspace "$WORKSPACE" --provider builtin --project "$PROJECT_ID" \
    --yes --json >"$OUTPUT/05-semantic-applied.json"
"${COPPERWRIGHT_COMMAND[@]}" chat \
    --workspace "$WORKSPACE" --provider builtin --project "$PROJECT_ID" \
    --undo --json >"$OUTPUT/06-undone.json"
"${COPPERWRIGHT_COMMAND[@]}" chat \
    --workspace "$WORKSPACE" --provider builtin --project "$PROJECT_ID" \
    --release --json >"$OUTPUT/07-released.json"
"${COPPERWRIGHT_COMMAND[@]}" chat \
    --workspace "$WORKSPACE" --provider builtin --list --json \
    >"$OUTPUT/08-reopened-list.json"

"${PYTHON_COMMAND[@]}" - "$OUTPUT" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
load = lambda name: json.loads((root / name).read_text(encoding="utf-8"))
clarification = load("01-clarification.json")
reviewed = load("02-reviewed-brief.json")
generated = load("03-generated-validated.json")
preview = load("04-semantic-preview.json")
applied = load("05-semantic-applied.json")
undone = load("06-undone.json")
released = load("07-released.json")
reopened = load("08-reopened-list.json")

project_id = clarification["project"]["id"]
assert clarification["project"]["status"] == "needs_clarification"
assert clarification["design"] is None
assert reviewed["project"]["status"] == "awaiting_confirmation"
assert reviewed["conversation"]["proposal"]["brief"]["confirmation_required"]
assert reviewed["design"] is None
assert generated["project"]["status"] == "validated"
assert generated["artifacts"]["validation"]["candidate_ready"]
assert not generated["artifacts"]["validation"]["production_ready"]
original_hash = generated["design"]["content_hash"]
assert preview["project"]["status"] == "change_ready"
assert preview["active_change"]["diff"]["board_fields"]["layers"] == {
    "before": 2,
    "after": 4,
}
assert preview["design"]["content_hash"] == original_hash
assert applied["design"]["content_hash"] != original_hash
assert undone["design"]["content_hash"] == original_hash
assert released["project"]["status"] == "released"
assert released["artifacts"]["release"]["offline_verification"]["verified"]
assert not released["artifacts"]["release"]["production_claimed"]
assert any(
    item["id"] == project_id and item["status"] == "released"
    for item in reopened["projects"]
)

summary = {
    "schema": "copperwright-chat-e2e",
    "version": 1,
    "project_id": project_id,
    "clarified": True,
    "confirmed_before_side_effects": True,
    "candidate_ready": True,
    "production_ready": False,
    "semantic_preview_preserved_authoritative_hash": True,
    "semantic_apply_changed_hash": True,
    "undo_restored_original_hash": True,
    "offline_release_verified": True,
    "reopen_preserved_project": True,
}
(root / "chat-e2e.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
PY

printf 'chat E2E evidence: %s/chat-e2e.json\n' "$OUTPUT"
