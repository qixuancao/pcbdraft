#!/usr/bin/env bash
# Remove only repository-local Python build products that can pollute packages.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

remove_generated_tree() {
    local target=$1
    if [[ -L "$target" ]]; then
        printf 'refusing to clean linked path: %s\n' "$target" >&2
        exit 2
    fi
    if [[ -e "$target" && ! -d "$target" ]]; then
        printf 'refusing to clean non-directory path: %s\n' "$target" >&2
        exit 2
    fi
    if [[ -d "$target" ]]; then
        find "$target" -depth -mindepth 1 -delete
        rmdir -- "$target"
    fi
}

remove_generated_tree "$REPO_DIR/build"
remove_generated_tree "$REPO_DIR/dist"
remove_generated_tree "$REPO_DIR/src/pcbdraft.egg-info"

printf 'removed repository-local build products\n'
