# Flat PCB Toolbox Design

## Summary

PCBDraft will expose a flat set of concrete Hermes tools. A tool name denotes
one engineering action; no model-facing tool multiplexes behavior through an
``operation`` field, and no tool owns a multi-phase engineering workflow.
Hermes receives one fact-only result after each call and the model chooses the
next call.

The implementation reuses the existing closed registry, revision binding,
semantic operations, managed-project generation, locks, and evidence model. It
adds the missing native incremental-state representation and one atomic
semantic-to-KiCad transaction boundary shared by all write tools.

## Model-Facing Tool Surface

The concrete names below are the target public surface. Final schemas remain
strict JSON objects with no extra properties.

### Project and inspection

- ``pcb_list_projects``, ``pcb_create_project``, ``pcb_open_project``
- ``pcb_inspect_project``, ``pcb_inspect_design``, ``pcb_inspect_component``
- ``pcb_inspect_net``, ``pcb_inspect_board``, ``pcb_inspect_events``
- ``pcb_inspect_evidence``

``pcb_create_project`` publishes an empty, valid, synchronized semantic/KiCad
project so every later write starts from real native state.

### Installed-library facts

- ``pcb_search_symbols``, ``pcb_describe_symbol``
- ``pcb_search_footprints``, ``pcb_describe_footprint``

These tools only report installed local KiCad facts. They do not infer part
qualification or use the network.

### Semantic circuit actions

- ``pcb_add_block``, ``pcb_remove_block``
- ``pcb_add_component``, ``pcb_remove_component``, ``pcb_update_component``
- ``pcb_assign_footprint``
- ``pcb_add_net``, ``pcb_remove_net``, ``pcb_rename_net``
- ``pcb_connect_pin``, ``pcb_disconnect_pin``
- ``pcb_add_power_domain``, ``pcb_update_power_domain``,
  ``pcb_remove_power_domain``
- ``pcb_add_interface``, ``pcb_update_interface``, ``pcb_remove_interface``
- ``pcb_add_constraint``, ``pcb_update_constraint``,
  ``pcb_remove_constraint``
- ``pcb_update_board_rules``

Each maps to one typed semantic operation with explicit object identity and
preconditions. Removal never silently cascades through connected objects; the
model must issue the required disconnect/remove calls itself.

### Native board actions

- ``pcb_set_board_outline``
- ``pcb_place_footprint``, ``pcb_move_footprint``,
  ``pcb_rotate_footprint``, ``pcb_unplace_footprint``
- ``pcb_route_net``, ``pcb_unroute_net``
- ``pcb_add_via``, ``pcb_remove_via``

One routing call may deterministically search and materialize the selected net,
but it cannot choose a different net or automatically continue to validation.
Existing routes are preserved as obstacles and retained geometry. Failed or
incomplete routing leaves the authoritative project unchanged and returns its
diagnostics.

### Individual checks and outputs

- ``pcb_check_semantics``, ``pcb_check_connectivity``
- ``pcb_run_erc``, ``pcb_run_drc``
- ``pcb_render_schematic``, ``pcb_render_board``, ``pcb_render_3d``
- ``pcb_export_gerbers``, ``pcb_export_drill``, ``pcb_export_bom``
- ``pcb_export_pick_place``, ``pcb_export_step``

Checks and outputs are individually callable evidence writes. There is no
``pcb_validate`` or ``pcb_build_release`` aggregation tool in the model schema.

## Canonical Registry

Replace the separate macro registry and capability registry with one flat
``PCBToolRegistry`` whose specs contain:

- public tool name and description;
- exact JSON input schema;
- read/write effect, risk, and MCP annotations;
- allowed project states;
- fixed service dispatch key;
- result projection policy.

Hermes, MCP descriptors, permissions, tests, and future transports derive from
this same registry. ``agent/hermes_tools.py`` registers every spec under the
PCBDraft-only toolset. ``capability_registry.py`` and model-facing macro specs
are removed after all consumers migrate.

## Atomic Write Contract

All semantic and board writes use one application-service primitive:

```text
load revision-bound authoritative project
  -> construct exactly one typed operation
  -> apply to an in-memory design copy
  -> validate semantic and installed-library contracts
  -> materialize a complete sibling KiCad project
  -> run operation-specific structural/parity checks
  -> atomically swap the synchronized design root
  -> persist receipt, event, hashes, revision
  -> return fact-only result
```

Any failure before publication deletes/retains only an attempt artifact; any
failure during publication restores the prior root. The tool reports success
only when the semantic hash, native snapshots, manifest hashes, project
revision, and receipt agree.

This may initially regenerate native files internally. That is acceptable
because the external action remains one bounded engineering operation; native
incremental optimizations may be introduced later without changing the tool
contract.

## Persisted Geometry

The current semantic IR stores component placement and rectangular board rules
but does not retain selected-net routes or explicit vias. Introduce a versioned
native-intent section containing:

- board outline geometry supported by the product;
- footprint pose overrides;
- route segments keyed by net and stable segment identity;
- explicit vias keyed by stable identity and net;
- geometry provenance and revision/hash binding.

Materialization consumes this state before invoking deterministic placement or
routing. Automatic layout/routing may only run inside the specifically selected
placement/routing tool and may not replace geometry owned by prior successful
calls. Readers migrate existing IR v1 projects to an equivalent in-memory v2
default with empty explicit geometry; the first successful write persists the
new version. Existing artifacts remain openable without destructive eager
migration.

## Verification and Export Split

Refactor the current combined validation function so ERC, DRC, semantic rules,
and connectivity have reusable individual runners with their own receipts.
Candidate readiness becomes a derived inspection fact over retained evidence,
not a tool that forces the checks to run in a particular order.

Likewise, expose each manufacturing/render output separately. Any human CLI
shortcut may call several concrete operations outside the model loop, but it
must not reappear as an agent tool or determine the model's next action.

## Permission and Authority

Hermes handlers use ``PCBToolGateway`` rather than calling
``PCBToolExecutor`` directly. The CLI permission mode is injected into the
gateway. Read-only denies writes; review requests approval for high-risk
native changes; workspace allows project-confined operations. Hermes' general
terminal/file/code-execution toolsets are not part of the default PCB agent
surface.

## Compatibility

- Existing project and evidence formats remain readable.
- Historical durable turns and receipts retain their old tool names for audit
  but are never automatically replayed through a new flat tool.
- Removed router/macro names are absent from new model schemas; they are not
  silently aliased because that would preserve ambiguous workflow authority.
- Human slash commands may be retained as explicit user shortcuts, implemented
  on top of concrete service calls, provided they do not alter the model tool
  surface.

## Risks and Controls

- Native regeneration per atomic write may be slow. Preserve the contract and
  optimize internally only after profiling.
- Route preservation is the highest-risk geometry change. Use stable route/via
  identities, fixture boards, native inspection parity, and DRC-focused tests.
- Schema count will grow. Keep descriptions compact and derive all transports
  from the registry; do not add a router merely to reduce schema count.
- Existing planning-provider code assumes whole-plan generation. Keep it only
  as non-default legacy compatibility until the flat agent path no longer
  imports it, then remove it under focused compatibility tests.

## Rollback

The code change can be reverted without rewriting existing projects. IR v2
readers must never eagerly destroy v1 artifacts; new v2-authored geometry may
require the new runtime, which will be recorded in the managed manifest and
release notes.
