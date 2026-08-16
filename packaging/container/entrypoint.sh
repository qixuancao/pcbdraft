#!/bin/sh
set -eu

if ! pcbdraft setup >/dev/null; then
    printf '%s\n' 'PCBDraft container could not prepare its KiCad runtime.' >&2
    pcbdraft doctor >&2 || true
    exit 1
fi

exec pcbdraft "$@"
