#!/usr/bin/env bash
# Exercise the supported Python matrix. Set COMPAT_FULL=1 for every test module.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
MATRIX_VERSIONS=${MATRIX_VERSIONS:-"3.11 3.12 3.13 3.14"}
MATRIX_FULL=${MATRIX_FULL:-0}

cd "$REPO_DIR"
for version in $MATRIX_VERSIONS; do
    printf 'Python %s test matrix\n' "$version"
    if [[ "$MATRIX_FULL" == 1 ]]; then
        uv run --frozen --python "$version" \
            python -m unittest discover -s tests -v
    else
        uv run --frozen --python "$version" python -m unittest -v \
            tests.test_api \
            tests.test_benchmark \
            tests.test_ir \
            tests.test_operations \
            tests.test_parts \
            tests.test_requirements \
            tests.test_scope \
            tests.test_security \
            tests.test_transactions
    fi
done
