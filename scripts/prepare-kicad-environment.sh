#!/usr/bin/env bash
# Initialize KiCad 10 library tables for a fresh, noninteractive Linux account.
set -euo pipefail

: "${HOME:?HOME must be set}"
if [[ "$HOME" != /* ]]; then
    printf 'HOME must be an absolute path: %s\n' "$HOME" >&2
    exit 2
fi
if ! command -v kicad-cli >/dev/null 2>&1; then
    printf 'kicad-cli is required\n' >&2
    exit 2
fi

KICAD_VERSION=$(kicad-cli --version)
if [[ "$KICAD_VERSION" != 10.* ]]; then
    printf 'KiCad 10 is required, found: %s\n' "$KICAD_VERSION" >&2
    exit 2
fi

TEMPLATE_DIR=${KICAD_TEMPLATE_DIR:-/usr/share/kicad/template}
CONFIG_DIR="$HOME/.config/kicad/10.0"
if [[ -e "$CONFIG_DIR" && ! -d "$CONFIG_DIR" ]]; then
    printf 'KiCad configuration path is not a directory: %s\n' "$CONFIG_DIR" >&2
    exit 2
fi
if [[ ! -d "$CONFIG_DIR" ]]; then
    install -d -m 0700 -- "$CONFIG_DIR"
fi

for TABLE in sym-lib-table fp-lib-table; do
    SOURCE="$TEMPLATE_DIR/$TABLE"
    TARGET="$CONFIG_DIR/$TABLE"
    case "$TABLE" in
        sym-lib-table) HEADER='(sym_lib_table' ;;
        fp-lib-table) HEADER='(fp_lib_table' ;;
    esac

    if [[ ! -s "$SOURCE" ]] || ! grep -Fq "$HEADER" "$SOURCE"; then
        printf 'valid KiCad template is missing: %s\n' "$SOURCE" >&2
        exit 2
    fi
    if [[ ! -e "$TARGET" ]]; then
        install -m 0644 -- "$SOURCE" "$TARGET"
    elif [[ ! -f "$TARGET" || ! -s "$TARGET" ]] || ! grep -Fq "$HEADER" "$TARGET"; then
        printf 'existing KiCad library table is invalid: %s\n' "$TARGET" >&2
        exit 2
    fi
done

printf 'KiCad %s user symbol and footprint library tables are ready.\n' "$KICAD_VERSION"
