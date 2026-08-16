#!/usr/bin/env bash
# Initialize KiCad 10 library tables for a fresh Linux or macOS account.
set -euo pipefail

: "${HOME:?HOME must be set}"
if [[ "$HOME" != /* ]]; then
    printf 'HOME must be an absolute path: %s\n' "$HOME" >&2
    exit 2
fi
KICAD_CLI=${KICAD_CLI:-}
if [[ -z "$KICAD_CLI" ]] && command -v kicad-cli >/dev/null 2>&1; then
    KICAD_CLI=$(command -v kicad-cli)
fi
if [[ -z "$KICAD_CLI" && -x /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli ]]; then
    KICAD_CLI=/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli
fi
if [[ -z "$KICAD_CLI" || ! -x "$KICAD_CLI" ]]; then
    printf 'kicad-cli is required\n' >&2
    exit 2
fi

KICAD_VERSION=$("$KICAD_CLI" --version)
if [[ ! "$KICAD_VERSION" =~ (^|[^0-9])10\.0\.[0-9]+([^0-9]|$) ]] \
    || [[ "$KICAD_VERSION" =~ ([Rr][Cc]|[Aa]lpha|[Bb]eta|[Nn]ightly|[Dd]ev) ]]; then
    printf 'The supported KiCad range is stable >=10.0.0,<10.1.0; found: %s\n' "$KICAD_VERSION" >&2
    exit 2
fi

if [[ "$(uname -s)" == "Darwin" ]]; then
    TEMPLATE_DIR=${KICAD_TEMPLATE_DIR:-/Applications/KiCad/KiCad.app/Contents/SharedSupport/template}
    CONFIG_DIR="$HOME/Library/Preferences/kicad/10.0"
else
    TEMPLATE_DIR=${KICAD_TEMPLATE_DIR:-/usr/share/kicad/template}
    CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/kicad/10.0"
fi
if [[ -e "$CONFIG_DIR" && ! -d "$CONFIG_DIR" ]]; then
    printf 'KiCad configuration path is not a directory: %s\n' "$CONFIG_DIR" >&2
    exit 2
fi
if [[ ! -d "$CONFIG_DIR" ]]; then
    mkdir -p -- "$CONFIG_DIR"
    chmod 0700 -- "$CONFIG_DIR"
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
        cp "$SOURCE" "$TARGET"
        chmod 0644 "$TARGET"
    elif [[ ! -f "$TARGET" || ! -s "$TARGET" ]] || ! grep -Fq "$HEADER" "$TARGET"; then
        printf 'existing KiCad library table is invalid: %s\n' "$TARGET" >&2
        exit 2
    fi
done

printf 'KiCad %s user symbol and footprint library tables are ready.\n' "$KICAD_VERSION"
