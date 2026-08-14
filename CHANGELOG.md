# PCBDraft changelog

All notable changes are documented here. The project follows semantic versioning
for the CLI/API package; individual on-disk schemas are independently versioned.

## Unreleased

- Replaced the erroneous hard-coded RP2040/TMP117 product route with a generic
  request → circuit plan → local KiCad resolution → semantic IR path.
- Added <code>AgentDesignRequest</code>, a schema-constrained
  <code>CircuitPlan</code>, local installed-symbol discovery, and project-local
  extracted <code>PartGraph</code> records.
- Explicitly named parts must survive from the request into the reviewed plan and
  compiled part graph. A missing symbol, invalid pin, or routing failure is
  retained as evidence, never silently replaced by a demo board.
- Added generic planner operations to the application, CLI, and JSON-RPC:
  <code>agent.request.prepare</code>, <code>symbols.find</code>,
  <code>agent.plan.compile</code>, and <code>agent.project.generate</code>.
- Application attempts retain request, plan, IR, part graph, available native
  staging, phase, and sanitized error. The generic CLI now also retains failed
  native staging by default.
- Added a UI-neutral Python <code>AgentRuntime</code>, durable non-blocking jobs,
  persisted activity events, cooperative stop, explicit retry, review/log views,
  restart recovery without replay, and a single-window slash-command TUI.
- Added bounded repair for generation and completed deterministic L1–L3 failures.
  Replacement plans pass through the same compiler, staged validation, atomic
  application, and exact undo; unknown/human evidence never self-approves.
- Added optional DeepSeek Harness support in both directions: a strict Python
  planner-provider bridge and a constrained Harness-hosted PCB tool plugin.
- Added versioned component-qualification evidence with actual native footprint
  pad-number checks, honest datasheet/identity states, and deterministic power,
  source, contention, passive, LED, I2C, and decoupling preflight rules.
- Fixed fine-pitch escape routing by separating exact physical-clearance checks
  from the bounded grid, preserving half-pitch spacing, and reporting detailed
  unrouted-net evidence before reference-plane work. Reference-plane ties are
  now added from actual connectivity need instead of a universal via count.
- Added LED, passive RC, and I2C pull-up stock-library examples that route and
  reach the candidate gate under real KiCad checks. The intentionally incomplete
  STM32F405/SHT31 fixture now routes but remains correctly blocked by electrical
  evidence gates.
- Local-library generic data remains provisional. Exact mapping and deterministic
  electrical failures block candidate readiness; unqualified identity/datasheet
  evidence blocks production claims without pretending generation failed.
- Rewrote README, architecture, API, acceptance, traceability, development, and
  open-source-reuse records to distinguish the generic product path from legacy
  deterministic fixtures.

## 1.0.0 — 2026-08-13

- Added one authoritative application service shared by `pcbdraft chat` and
  the loopback-only `pcbdraft app` browser application.
- Added focused conversational clarification, human-readable briefs, explicit
  confirmation, persistent projects/sessions/events/jobs, crash recovery,
  progress/cancel/retry states, real previews, safe open-in-KiCad actions, and
  manufacturing-candidate export with offline verification.
- Added strict authenticated Codex and OpenAI-compatible intent providers plus a
  deterministic offline provider. Credentials remain outside browser and project
  records; untrusted model output cannot directly edit engineering files.
- Added preview/validate/apply/undo semantic conversations with isolated staged
  validation and authoritative-state hashes.
- Added deterministic fixture designs, part/block contracts, routing, real KiCad
  ERC/DRC, L0–L7 gates, examples, and independent tests. These records are
  regression infrastructure, not the current generic product interface.
- Added real clean-HOME terminal and Firefox WebDriver product E2E acceptance,
  restart/reopen checks, provider contract tests, and managed-project coverage.
- USB 2.0, buck, high-speed, RF, mains, high-power, medical, aviation, and
  safety-critical design remain explicitly unsupported; no physical or
  production sign-off is claimed.

## 0.2.0 — 2026-08-13 (first PCBDraft release)

- Established the PCBDraft brand, durable mark/icon/social-preview assets,
  the `pcbdraft` distribution name, CLI, package, schemas, and on-disk
  namespaces.

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
