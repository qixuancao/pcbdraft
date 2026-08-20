# Flatten PCB agent tools

## Goal

Replace the PCBDraft-specific two-layer tool protocol (high-level PCB macros
plus ``operation``-multiplexed domain routers) with a flat Hermes-style PCB
toolbox. The model remains the sole decision-maker: it observes project facts,
selects exactly one concrete PCB operation, reads its result, and selects the
next operation. It must not be forced through a fixed workflow.

The desired interaction is a series of small engineering actions such as
adding/removing a component, connecting/disconnecting pins, inspecting a
part/net/board, placing or routing a board element, running one verification
step, and exporting an artifact. Each action must be an individually named
tool, rather than an ``operation`` argument of a router or a one-call macro
covering an entire engineering phase.

## Background

- The default interactive product path is the vendored Hermes agent loop.
- PCBDraft currently exposes nine high-level PCB macros through
  ``agent/tooling.py`` and eight router tools through
  ``agent/capability_registry.py`` / ``agent/hermes_tools.py``.
- Most requested fine-grained write operations (semantic graph mutation,
  footprint movement, outline edits, per-net routing, standalone ERC/DRC)
  are currently declared but return ``supported: false``; generation and
  repair remain macro-backed.
- The current executor already provides useful invariants for concrete write
  operations: closed registry, strict schemas, allowed-status checks,
  revision binding, and fixed ApplicationService dispatch.
- ``domain/operations.py`` already implements validated, preconditioned
  semantic operations for blocks, components, nets, connections, constraints,
  board properties, and metadata. These operations are not yet exposed as
  individual Hermes tools.
- Existing generation rebuilds schematic/board artifacts from the semantic
  design; existing KiCad import recognizes placement changes only. Per-net
  route preservation, explicit via editing, independent ERC/DRC service entry
  points, and general incremental native synchronization require new backend
  work.

## Requirements

- R1: Remove the model-facing high-level macro tools and model-facing domain
  routers from the default PCB tool surface.
- R2: Expose each supported PCB action as one individually named Hermes tool
  with a strict schema and fact-only result.
- R3: The model autonomously chooses every next PCB tool based on the latest
  result; no fixed plan/generate/validate/repair/release controller may be
  reintroduced.
- R4: Individual PCB write tools retain local authority controls: schema
  validation, permission decision, transaction boundary where applicable,
  project-state preconditions, and revision-concurrency protection.
- R5: Unimplemented fine-grained actions must be either implemented as real
  tools or absent from the advertised model tool schema; they must not be
  represented as fake-success operations.
- R6: Existing projects and their retained KiCad/evidence artifacts remain
  usable after the protocol refactor.
- R7: This task delivers the complete fine-grained toolbox rather than only
  flattening the names of currently supported macro operations. The supported
  surface must include real semantic component/net editing, native board
  placement/routing/outline operations, individually invocable verification,
  inspection, rendering, and manufacturing export operations.
- R8: A tool may perform the bounded deterministic work inherent in one
  engineering operation (for example routing one selected net or running one
  DRC check), but it must not decide or automatically chain the next
  engineering operation. Every next action returns to the model.
- R9: Every successful PCB write tool must leave the authoritative semantic
  design and native KiCad project synchronized at the same new revision. If
  semantic validation, native materialization, or the operation's required
  checks fail, the complete operation rolls back and returns a factual failure.
- R10: The complete toolbox is bounded by the product's existing scope of
  small, low-voltage, non-safety-critical prototype boards. It does not imply
  arbitrary KiCad feature parity or production-readiness attestation.

## Acceptance Criteria

- [ ] The Hermes model schema contains no ``pcb_project`` / ``pcb_library`` /
  ``pcb_design``-style operation router and no phase-sized ``pcb_*_candidate``
  macro.
- [ ] A model can perform a PCB task by issuing a sequence of concrete,
  individually named tool calls, with a tool result returned between actions.
- [ ] Each exposed write tool is covered by focused tests for schema,
  permission, stale-revision, and service-dispatch behavior.
- [ ] The user-visible help/persona/architecture documentation accurately
  describes the flat tool model and its real capability limits.
- [ ] Existing durable project data opens without migration loss.
- [ ] The flat toolbox includes working tools for component add/remove/update,
  pin/net connect/disconnect, footprint assignment, board outline, footprint
  placement/movement, per-net routing, vias, ERC, DRC, previews, and
  manufacturing exports.
- [ ] After every concrete operation, control and a fact-only result return to
  Hermes before any subsequent engineering operation starts.
- [ ] No successful write result can leave semantic and native design hashes
  out of synchronization; injected materialization/check failures demonstrate
  complete rollback in focused tests.

## Out of Scope

- Raw model-authored KiCad text, arbitrary filesystem mutation, or arbitrary
  Python/shell as a substitute for PCB tools.
- Production-readiness claims, human engineering sign-off, fabrication, and
  physical test evidence.
- Thermal, SPICE, signal-integrity, and power-integrity engines that do not
  exist in the current product.
- Full manual-EDA parity beyond the explicitly named flat toolbox.

## Scope Decision

The user selected the complete fine-grained toolbox. Implementation may use
ordered internal milestones, but interface-only flattening is not an acceptable
final result for this task.

## Synchronization Decision

Every successful write tool synchronizes the semantic and native KiCad state
immediately and atomically. There is no model-facing deferred-sync workflow.
