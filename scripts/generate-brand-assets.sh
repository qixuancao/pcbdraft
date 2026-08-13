#!/usr/bin/env bash
# Deterministically derive CopperWright icons and the GitHub social preview.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
BRAND_DIR="$REPO_DIR/docs/assets/brand"
WEB_MARK="$REPO_DIR/src/copperwright/web/copperwright-mark-128.png"
SOURCE="$BRAND_DIR/copperwright-mark-v1-source.png"
SOURCE_SHA256=eb9a60f013b0e9413ee58442779884e2fed67f8080b105038367412b406b4004
MODE=${1:-write}

if [[ "$MODE" != "write" && "$MODE" != "--check" ]]; then
    printf 'usage: %s [--check]\n' "$0" >&2
    exit 2
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
    printf 'ffmpeg is required\n' >&2
    exit 2
fi
if [[ ! -f "$SOURCE" ]]; then
    printf 'source mark is missing: %s\n' "$SOURCE" >&2
    exit 2
fi
ACTUAL_SHA256=$(sha256sum "$SOURCE" | cut -d ' ' -f 1)
if [[ "$ACTUAL_SHA256" != "$SOURCE_SHA256" ]]; then
    printf 'source mark hash mismatch: expected %s, got %s\n' \
        "$SOURCE_SHA256" "$ACTUAL_SHA256" >&2
    exit 2
fi

FONT_FILE=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
if [[ ! -f "$FONT_FILE" ]]; then
    printf 'deterministic banner font is missing: %s\n' "$FONT_FILE" >&2
    exit 2
fi

STAGING=$(mktemp -d "${TMPDIR:-/tmp}/copperwright-brand.XXXXXX")
cleanup() {
    rm -rf -- "$STAGING"
}
trap cleanup EXIT INT TERM

for SIZE in 32 64 128 256 512; do
    OUTPUT="$STAGING/copperwright-mark-${SIZE}.png"
    ffmpeg -nostdin -hide_banner -loglevel error -y \
        -fflags +bitexact -i "$SOURCE" -map_metadata -1 \
        -vf "scale=${SIZE}:${SIZE}:flags=lanczos+accurate_rnd,format=rgb24" \
        -frames:v 1 -threads:v 1 -c:v png -compression_level 9 -pred mixed \
        "$OUTPUT"
done

BANNER="$STAGING/copperwright-social-preview-1280x640.png"
ffmpeg -nostdin -hide_banner -loglevel error -y -fflags +bitexact \
    -f lavfi -i "color=c=0x1b1c1d:s=1280x640:d=1:r=1" -i "$SOURCE" \
    -map_metadata -1 -filter_complex \
    "[1:v]scale=640:640:flags=lanczos+accurate_rnd,format=rgb24[mark];\
[0:v][mark]overlay=0:0:shortest=1[base];\
[base]drawbox=x=672:y=287:w=112:h=5:color=0xf28a35:t=fill,\
drawtext=fontfile=${FONT_FILE}:text='CopperWright':fontcolor=0xf28a35:fontsize=72:x=668:y=196,\
drawtext=fontfile=${FONT_FILE}:text='Evidence-driven PCB automation':fontcolor=white:fontsize=27:x=672:y=326,\
drawtext=fontfile=${FONT_FILE}:text='for KiCad':fontcolor=white:fontsize=27:x=672:y=368,\
drawtext=fontfile=${FONT_FILE}:text='Semantic. Deterministic. Reversible.':fontcolor=0xb9bdc2:fontsize=20:x=672:y=454,\
format=rgb24[out]" \
    -map '[out]' -frames:v 1 -threads:v 1 -c:v png -compression_level 9 \
    -pred mixed "$BANNER"

STATUS=0
for GENERATED in "$STAGING"/*.png; do
    TARGET="$BRAND_DIR/$(basename -- "$GENERATED")"
    if [[ "$MODE" == "--check" ]]; then
        if [[ ! -f "$TARGET" ]] || ! cmp -s -- "$GENERATED" "$TARGET"; then
            printf 'brand asset is stale: %s\n' "$TARGET" >&2
            STATUS=1
        fi
    elif [[ ! -f "$TARGET" ]] || ! cmp -s -- "$GENERATED" "$TARGET"; then
        install -m 0644 -- "$GENERATED" "$TARGET"
    fi
done

GENERATED_WEB_MARK="$STAGING/copperwright-mark-128.png"
if [[ "$MODE" == "--check" ]]; then
    if [[ ! -f "$WEB_MARK" ]] || ! cmp -s -- "$GENERATED_WEB_MARK" "$WEB_MARK"; then
        printf 'brand asset is stale: %s\n' "$WEB_MARK" >&2
        STATUS=1
    fi
elif [[ ! -f "$WEB_MARK" ]] || ! cmp -s -- "$GENERATED_WEB_MARK" "$WEB_MARK"; then
    install -m 0644 -- "$GENERATED_WEB_MARK" "$WEB_MARK"
fi

if [[ "$MODE" == "--check" && "$STATUS" == 0 ]]; then
    printf 'CopperWright brand assets are reproducible and current.\n'
fi
exit "$STATUS"
