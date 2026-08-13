# CopperWright changelog

All notable changes are documented here. The project follows semantic versioning
for the CLI/API package; individual on-disk schemas are independently versioned.

## 1.0.0 — 2026-08-13

- Added one authoritative application service shared by `copperwright chat` and
  the loopback-only `copperwright app` browser application.
- Added focused conversational clarification, human-readable briefs, explicit
  confirmation, persistent projects/sessions/events/jobs, crash recovery,
  progress/cancel/retry states, real previews, safe open-in-KiCad actions, and
  manufacturing-candidate export with offline verification.
- Added strict authenticated Codex and OpenAI-compatible intent providers plus a
  deterministic offline provider. Credentials remain outside browser and project
  records; untrusted model output cannot directly edit engineering files.
- Added preview/validate/apply/undo semantic conversations with isolated staged
  validation and authoritative-state hashes.
- Added fully verified BME280/SPI and AP2112K/UART/LDO profiles alongside the
  original TMP102/I2C profile, including trusted part/block contracts, routing,
  real KiCad ERC/DRC, L0–L7 gates, examples, and independent tests. Their verified
  envelope is explicitly limited to 45 mm × 30 mm and 2/4 copper layers.
- Added real clean-HOME terminal and Firefox WebDriver product E2E acceptance,
  restart/reopen checks, provider compatibility tests, and managed 0.2 project
  migration.
- USB 2.0, buck, high-speed, RF, mains, high-power, medical, aviation, and
  safety-critical design remain explicitly unsupported; no physical or
  production sign-off is claimed.

## 0.2.0 — 2026-08-13 (first CopperWright release)

- Adopted CopperWright branding, durable mark/icon/social-preview assets, the
  `copperwright` distribution name, and a primary `copperwright` CLI.
- Retained `pcb-agent` as a CLI alias and kept internal modules, schemas, on-disk
  compatibility names, and historical evidence stable.

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
