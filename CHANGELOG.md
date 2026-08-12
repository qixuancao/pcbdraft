# Changelog

All notable changes are documented here. The project follows semantic versioning
for the CLI/API package; individual on-disk schemas are independently versioned.

## 0.2.0 — 2026-08-12

- Added strict semantic circuit/PCB IR, trusted parts, and verified block registry.
- Added semantic transactions, preview/diff, locking, rollback, undo, and recovery.
- Added bounded requirements compiler, placement optimizer, multilayer router, and
  deterministic native KiCad schematic/PCB generation.
- Added managed projects and fail-closed bidirectional footprint-pose sync.
- Added honest L0–L7 validation and attributed external L6/L7 evidence import.
- Added real manufacturing candidate export, reproducible archives, and offline
  release verification.
- Added versioned JSON-RPC API and high-level CLI operations.
- Added independent 90-case CC0 benchmark and optional blinded model consistency.
- Added explicit I2C RC/sink-current and UPDI source-ownership contracts, a filled
  GND reference plane with deterministic stitching vias, native pad-edge
  decoupling metrics, and a consistent worst-case power envelope.
- Added synchronized managed semantic context to bounded Codex reviews; drifted
  projects automatically lose intent-authority status.
- Added KiCad 10 compatibility policy, hostile-input coverage, examples, CI, and
  release documentation.

## 0.1.0 — 2026-08-12

- Initial KiCad reviewer and staged replace-only safe patcher.
