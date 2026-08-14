#!/usr/bin/env bash
# Configure the isolated DeepSeek Harness profile used by PCBDraft.
# Run: scripts/setup-deepseek-harness.sh
# Needs: uv, Node.js 22+, pnpm, DeepSeek Harness source, and PCBDraft .venv.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PCBDRAFT_ROOT="${PCBDRAFT_ROOT:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
DSH_ROOT="${DSH_ROOT:-/mnt/2T/deepseek-harness}"
DSH_HOME="${DSH_HOME:-$PCBDRAFT_ROOT/.dsh}"
DSH_PROFILE="${DSH_PROFILE:-headless}"
PLUGIN_ROOT="$PCBDRAFT_ROOT/integrations/deepseek-harness"
PATCH_FILE="$DSH_HOME/pcbdraft-pcb.patch.yml"
WORKSPACE="${PCBDRAFT_DSH_WORKSPACE:-$PCBDRAFT_ROOT/.dsh-workspace}"

[[ -x "$PCBDRAFT_ROOT/.venv/bin/python" ]] || { echo "missing PCBDraft .venv" >&2; exit 1; }
[[ -f "$PLUGIN_ROOT/package.json" ]] || { echo "missing PCBDraft DSH plugin" >&2; exit 1; }
[[ -f "$DSH_ROOT/package.json" ]] || { echo "missing DeepSeek Harness source" >&2; exit 1; }
command -v node >/dev/null || { echo "missing node" >&2; exit 1; }
command -v pnpm >/dev/null || { echo "missing pnpm" >&2; exit 1; }
command -v uv >/dev/null || { echo "missing uv" >&2; exit 1; }

# Install the pinned optional Python SDK used when the native PCBDraft TUI
# selects --provider deepseek-harness. The Node-hosted plugin below remains a
# separate integration direction and shares only the versioned PCB contracts.
(cd "$PCBDRAFT_ROOT" && uv sync --frozen --all-extras)
"$PCBDRAFT_ROOT/.venv/bin/python" -c 'import deepseek_harness'

if [[ ! -d "$DSH_ROOT/node_modules" ]]; then
  (cd "$DSH_ROOT" && pnpm install --ignore-scripts --frozen-lockfile)
fi

mkdir -p "$DSH_HOME" "$WORKSPACE"
export DSH_HOME
(cd "$DSH_ROOT" && pnpm dsh plugin --profile "$DSH_PROFILE" add "$PLUGIN_ROOT")

node - "$PATCH_FILE" "$PCBDRAFT_ROOT" "$WORKSPACE" <<'NODE'
import { writeFileSync } from 'node:fs'
const [path, root, workspace] = process.argv.slice(2)
const quote = value => JSON.stringify(value)
const disabled = [
  'tool-bash', 'tool-pwsh', 'tool-jobs', 'tool-fs', 'tool-fs-search',
  'tool-skill', 'tool-subagent-control', 'tool-subagent-list-agents',
  'tool-subagent', 'tool-subagent-fork', 'tool-subagent-report',
  'tool-workflow', 'tool-ralph', 'tool-str-replace-editor', 'tool-todo',
  'tool-goal', 'tool-web', 'agent-instructions', 'plan-mode', 'code-runtime',
]
const rows = [
  '- id: pcbdraft-pcb',
  '  config:',
  `    pcbdraftRoot: ${quote(root)}`,
  `    workspace: ${quote(workspace)}`,
  ...disabled.flatMap(id => [`- id: ${id}`, '  disabled: true']),
]
writeFileSync(path, `${rows.join('\n')}\n`, 'utf8')
NODE

(cd "$DSH_ROOT" && pnpm dsh --profile "$DSH_PROFILE" --patch "$PATCH_FILE" --dump-config >/dev/null)
printf 'PCBDraft DeepSeek Harness profile is ready: %s\n' "$DSH_PROFILE"
