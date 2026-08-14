#!/usr/bin/env bash
# Real generic chat lifecycle through a deterministic local model endpoint.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
OUTPUT=${1:-}
if [[ -z "$OUTPUT" ]]; then
    OUTPUT=$(mktemp -d "${TMPDIR:-/tmp}/pcbdraft-chat-e2e.XXXXXX")
else
    mkdir -p -- "$OUTPUT"
    OUTPUT=$(realpath -- "$OUTPUT")
fi

if [[ -n "${PCBDRAFT_EXE:-}" ]]; then
    PCBDRAFT_COMMAND=("$PCBDRAFT_EXE")
else
    PCBDRAFT_COMMAND=(uv run pcbdraft)
fi
if [[ -n "${PCBDRAFT_PYTHON:-}" ]]; then
    PYTHON_COMMAND=("$PCBDRAFT_PYTHON")
else
    PYTHON_COMMAND=(uv run python)
fi

WORKSPACE="$OUTPUT/workspace"
PROVIDER_READY=$(mktemp "$OUTPUT/.provider-url.XXXXXX")
PROVIDER_LOG="$OUTPUT/provider.log"
PROVIDER_PID=""
cleanup() {
    if [[ -n "$PROVIDER_PID" ]] && kill -0 "$PROVIDER_PID" 2>/dev/null; then
        kill "$PROVIDER_PID" 2>/dev/null || true
        wait "$PROVIDER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

cd "$REPO_DIR"
"${PYTHON_COMMAND[@]}" "$SCRIPT_DIR/fake_openai_provider.py" \
    --ready-file "$PROVIDER_READY" >"$PROVIDER_LOG" 2>&1 &
PROVIDER_PID=$!
for _attempt in $(seq 1 200); do
    if [[ -s "$PROVIDER_READY" ]]; then
        break
    fi
    if ! kill -0 "$PROVIDER_PID" 2>/dev/null; then
        printf 'local E2E provider exited early\n' >&2
        exit 2
    fi
    sleep 0.05
done
if [[ ! -s "$PROVIDER_READY" ]]; then
    printf 'timed out waiting for local E2E provider\n' >&2
    exit 2
fi

PCBDRAFT_CONFIG="$OUTPUT/model-config.toml"
PCBDRAFT_MODEL_BASE_URL=$(tr -d '\r\n' <"$PROVIDER_READY")
cat >"$PCBDRAFT_CONFIG" <<EOF
version = 1
active_provider = "local-e2e"
active_model = "pcbdraft-e2e-model"

[providers.local-e2e]
name = "Local E2E provider"
base_url = "$PCBDRAFT_MODEL_BASE_URL"
api_key = "pcbdraft-local-e2e-key"
models = ["pcbdraft-e2e-model"]
EOF
chmod 600 "$PCBDRAFT_CONFIG"
export PCBDRAFT_CONFIG

"${PCBDRAFT_COMMAND[@]}" chat \
    --workspace "$WORKSPACE" --provider openai-compatible \
    --new "CLI LED prototype" \
    --message "Design a small 3.3 V LED status indicator PCB with a power connector" \
    --json >"$OUTPUT/01-reviewed-plan.json"

PROJECT_ID=$("${PYTHON_COMMAND[@]}" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["project"]["id"])' \
    "$OUTPUT/01-reviewed-plan.json")

"${PCBDRAFT_COMMAND[@]}" chat \
    --workspace "$WORKSPACE" --provider openai-compatible --project "$PROJECT_ID" \
    --yes --json >"$OUTPUT/02-generated-validated.json"
"${PCBDRAFT_COMMAND[@]}" chat \
    --workspace "$WORKSPACE" --provider openai-compatible --project "$PROJECT_ID" \
    --release --json >"$OUTPUT/03-released.json"
"${PCBDRAFT_COMMAND[@]}" chat \
    --workspace "$WORKSPACE" --provider openai-compatible --list --json \
    >"$OUTPUT/04-reopened-list.json"

"${PYTHON_COMMAND[@]}" - "$OUTPUT" "$PCBDRAFT_MODEL_BASE_URL" <<'PY'
import json
import pathlib
import sys
import urllib.request

root = pathlib.Path(sys.argv[1])
base_url = sys.argv[2]
load = lambda name: json.loads((root / name).read_text(encoding="utf-8"))
reviewed = load("01-reviewed-plan.json")
generated = load("02-generated-validated.json")
released = load("03-released.json")
reopened = load("04-reopened-list.json")

project_id = reviewed["project"]["id"]
proposal = reviewed["conversation"]["proposal"]
assert reviewed["project"]["status"] == "awaiting_confirmation"
assert reviewed["design"] is None
assert proposal["clarifications"] == []
assert proposal["decisions"]["layers"] == 2
assert proposal["brief"]["confirmation_required"]
assert proposal["brief"]["board"]["layers"] == 2
assert {item["value"] for item in proposal["brief"]["bom"]} >= {
    "POWER", "1k", "LED"
}

assert generated["project"]["status"] == "validated"
assert generated["design"]
assert generated["artifacts"]["validation"]["candidate_ready"]
assert not generated["artifacts"]["validation"]["production_ready"]
assert not generated["artifacts"]["validation"]["production_claimed"]
assert len(generated["artifacts"]["validation"]["levels"]) == 8
assert generated["artifacts"]["previews"]
plan_path = pathlib.Path(generated["design"]["files"]["circuit_plan"])
assert plan_path.is_file()
plan = json.loads(plan_path.read_text(encoding="utf-8"))
assert plan["schema"] == "pcbdraft-circuit-plan"
assert {component["symbol"] for component in plan["components"]} >= {
    "Connector_Generic:Conn_01x02", "Device:LED", "Device:R"
}

assert released["project"]["status"] == "released"
assert released["artifacts"]["release"]["offline_verification"]["verified"]
assert not released["artifacts"]["release"]["production_claimed"]
assert any(
    item["id"] == project_id and item["status"] == "released"
    for item in reopened["projects"]
)
with urllib.request.urlopen(base_url.removesuffix("/v1") + "/stats", timeout=5) as response:
    provider_stats = json.loads(response.read())
assert provider_stats["requests"] >= 2

summary = {
    "schema": "pcbdraft-chat-e2e",
    "version": 2,
    "project_id": project_id,
    "provider": "local-openai-compatible",
    "provider_requests": provider_stats["requests"],
    "layer_selection_was_internal": True,
    "reviewed_before_side_effects": True,
    "native_circuit_plan_retained": True,
    "candidate_ready": True,
    "production_ready": False,
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
