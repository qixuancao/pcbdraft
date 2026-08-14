# PCBDraft × DeepSeek Harness

This directory supports both directions of the optional DeepSeek Harness (DSH)
integration. PCBDraft always remains the KiCad generator, validator, and
transaction authority.

## Harness behind the native Python TUI

Install the locked optional SDK and select the provider:

```bash
uv sync --extra harness
DEEPSEEK_API_KEY=... uv run pcbdraft --provider deepseek-harness
```

`src/pcbdraft/harness_bridge.py` owns a versioned one-request stdin/stdout
protocol. `src/pcbdraft/deepseek_bridge.py` is its optional SDK process.
Prompts never enter argv; responses, time, and combined process output are
bounded; intent and circuit-plan schemas are validated again by PCBDraft.
The minimal Cordis composition gives this planner no shell, filesystem, web,
editor, workflow, skill, or subagent tools. Harness final text is not treated as
native schema enforcement—the Python consumer is the authority.

## PCBDraft tools inside Harness

DSH can instead host the conversation and call the constrained PCB toolset.

## Setup and run

```bash
scripts/setup-deepseek-harness.sh
scripts/run-pcbdraft-agent.sh 'Design a low-power sensor board'
```

The runner uses an isolated local DSH profile in `.dsh/` and a generated-board
workspace in `.dsh-workspace/`; both are Git-ignored. Configure any DSH model
credential in DSH itself. PCBDraft stores no model credential.

## Model-visible tools

Only these tools are registered:

- `pcb_prepare` — creates a constrained request, stock-symbol context, and plan schema;
- `pcb_symbols` — searches installed KiCad symbols only;
- `pcb_generate` — produces a deterministic workspace-local candidate and validates it.

The plugin checks the PCBDraft API major version and required capabilities
before its first operation. `pcb_generate` uses repair attempt 0 followed by at
most attempts 1 and 2, retains failed native work under the configured
workspace, and returns bounded structured RPC diagnostics for replanning.

The setup profile disables DSH shell, filesystem, web, jobs, skills, subagent,
workflow, editor, todo, and code-runtime tools. The runner forces DSH native
Tool Mode. The plugin starts PCBDraft with a sanitized environment and only
allows output below its configured workspace.

## Verification

```bash
scripts/test-deepseek-harness.sh

# Or run each boundary independently:
node --test integrations/deepseek-harness/test/plugin.test.mjs
node integrations/deepseek-harness/test/dsh-runtime-contract.mjs
node integrations/deepseek-harness/test/real-generate-smoke.mjs
.venv/bin/python integrations/deepseek-harness/test/python-provider-runtime.py
```

The first test needs Node only. The second also needs the built DSH checkout at
`$DSH_ROOT` or `/mnt/2T/deepseek-harness`. The third uses local KiCad and writes
(or safely reuses) a test candidate under `.dsh-workspace/smoke/`; it does not
call a model. The final test boots the real SDK runtime with PCBDraft's
tool-free provider composition, also without calling a model.

DSH is used as an external optional dependency, not vendored into PCBDraft.
The inspected upstream DSH `LICENSE` is MIT.
