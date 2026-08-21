# Journal - qixuancao (Part 1)

> AI development session journal
> Started: 2026-08-19

---



## Session 1: Integrate Hermes provider authentication

**Date**: 2026-08-20
**Task**: Integrate Hermes provider authentication
**Branch**: `main`

### Summary

Replaced legacy PCBDraft provider TOML authority with isolated vendored-Hermes onboarding, auth, model persistence, runtime planning, safe rollback/redaction, deferred in-REPL connection handoff, packaging and focused tests; synchronized uv runtime constraints and installed the verified local build.

### Git Commits

| Hash | Message |
|------|---------|
| `ba8271d` | (see git log) |
| `554d432` | (see git log) |

### Status

[OK] **Completed**


## Session 2: Flatten PCB agent tools

**Date**: 2026-08-20
**Task**: Flatten PCB agent tools
**Branch**: `main`

### Summary

Replaced Hermes PCB routers and phase macros with 56 concrete flat tools, added atomic semantic/native writes with IR v2 intent, split checks/renders/exports, prevented hidden placement/routing, and added focused contract and rollback coverage.

### Git Commits

| Hash | Message |
|------|---------|
| `f7b0730` | (see git log) |
| `513d462` | (see git log) |

### Status

[OK] **Completed**


## Session 3: Unblock flat PCB Agent and accept LED prototype

**Date**: 2026-08-21
**Task**: Unblock flat PCB Agent and accept LED prototype
**Branch**: `main`

### Summary

Added session-bound project authority, one-call PCB middleware, installed KiCad part discovery/registration, atomic native publication fixes, and completed isolated global-CLI acceptance for a 3.3V LED prototype with zero DRC violations.

### Git Commits

| Hash | Message |
|------|---------|
| `db7a7f1` | (see git log) |

### Status

[OK] **Completed**


## Session 4: Improve cross-platform one-command installation

**Date**: 2026-08-21
**Task**: Improve cross-platform one-command installation
**Branch**: `main`

### Summary

Unified Linux/macOS and Windows installers around non-mutating preflight, immutable provenance, idempotent KiCad and stock-library repair, visible setup/doctor verification, one-line docs, focused contract CI, and disposable Ubuntu 24.04/26.04 rootless Docker acceptance.

### Git Commits

| Hash | Message |
|------|---------|
| `a9d0541` | (see git log) |

### Status

[OK] **Completed**
