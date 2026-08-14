#!/usr/bin/env bash
# Validate both DeepSeek Harness integration directions without calling a model.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

cd "$REPO_DIR"
uv sync --frozen --extra dev --extra harness
uv run ruff check \
    src/pcbdraft/harness_bridge.py \
    src/pcbdraft/deepseek_bridge.py \
    tests/test_harness_provider.py \
    integrations/deepseek-harness/test/python-provider-runtime.py
uv run python -m unittest -v tests.test_harness_provider
uv run python integrations/deepseek-harness/test/python-provider-runtime.py
node --test integrations/deepseek-harness/test/plugin.test.mjs
node integrations/deepseek-harness/test/dsh-runtime-contract.mjs
node integrations/deepseek-harness/test/real-generate-smoke.mjs
