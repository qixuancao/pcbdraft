# Flat PCB Toolbox Contract

## 1. Scope / Trigger

Apply this contract whenever a PCB capability is added to Hermes, a semantic or
native PCB write is changed, or a check, render, or manufacturing export is
added. The model-facing surface is the immutable `PCB_TOOL_SPECS` sequence in
`src/pcbdraft/agent/tooling.py`; it is an operating-system-like toolbox of
concrete actions, not a workflow engine.

Do not export phase macros, domain routers, an `operation` discriminator, or a
second transport-specific registry. Legacy macro names may be resolved only to
read old audit records and must fail closed if execution or replay is attempted.

## 2. Signatures

The cross-layer execution boundary is:

```python
ApplicationService.execute_pcb_tool(
    project_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    timeout: float,
    expected_revision: int,
) -> dict[str, Any]
```

Authoritative writes use `ApplicationService.apply_pcb_operation(...)` with the
same signature. Native materialization exposes explicit control rather than
inferring model intent:

```python
materialize_managed_design(
    requirements,
    design,
    output,
    *,
    auto_place: bool,
    route_net_ids: frozenset[str] | None,
    allow_incomplete: bool,
    ...,
) -> ManagedGeneration
```

Single-result evidence uses `run_pcb_check`, `render_pcb_output`, and
`export_pcb_output`, each with `(project_id, kind, *, timeout,
expected_revision)`.

## 3. Contracts

- Every exported name is one concrete `pcb_*` action with a closed,
  fully-required JSON object schema. Nested payloads are typed JSON structures,
  not JSON-encoded strings.
- Hermes and future MCP adapters derive exports from the same registry. The
  default Hermes config enables only `platform_toolsets.cli = ["pcbdraft"]` and
  disables tool search; it does not grant the general `hermes-cli` toolbox.
- Reads return persisted project or installed-library facts and are the only
  operations allowed by `read_only` permission mode. Evidence and design writes
  go through `PCBToolGateway`, bind a baseline revision, and dispatch one fixed
  handler.
- Installed symbol/footprint reads are global machine-local facts. All other
  model operations bind to the project selected through the trusted human CLI
  boundary; project listing/opening are not model tools.
- Symbol search returns bounded installed `Library:Symbol` identifiers without
  parsing every match. Exact full-ID queries rank first; one explicit symbol
  description call owns the comparatively expensive pin/detail extraction.
- A successful trusted `/new`, `/open`, or repository switch rotates to a fresh
  Hermes conversation before the next model request. `--project` selects the
  project before the fresh terminal session is constructed. Old project tool
  text is not submitted through session-boundary memory extraction.
- Canonical part search/description returns stable project catalog identities.
  Installed-KiCad registration validates the exact symbol pins, footprint pads,
  and mapping, then publishes catalog, IR, and native files in one transaction.
- Hermes execution middleware dispatches at most one `pcb_*` tool from each
  provider response while returning a protocol result for every call id.
- IR v1 remains byte/hash compatible during load, inspection, and cloning. The
  first successful typed write promotes a copy to IR v2. IR v2 owns board
  outline, footprint poses, retained route segments, vias, explicit unrouted
  nets, provenance, and geometry revision.
- A write stages both semantic state and complete KiCad materialization outside
  the live design directory. It publishes only after validation, installed
  symbol/footprint resolution, synchronization checks, and revision/content-hash
  comparison under the project lock. On any later records/event failure, restore
  the live design and records; never retain a success receipt.
- Flat semantic writes set `auto_place=False` and pass no nets to the router.
  Unplaced footprints remain unplaced until a placement tool is called.
  `pcb_route_net` passes only its requested net; all retained segments and vias
  remain authoritative obstacles and survive unchanged.
- Removing connected or routed objects is non-cascading: the model must call the
  corresponding disconnect or unroute tool first.
- Each check, render, and export runs only the requested action and retains
  evidence with its exact source design revision and content hash. Aggregate CLI
  shortcuts are never model tools.

## 4. Validation & Error Matrix

| Condition | Required behavior |
|---|---|
| Unknown, legacy, router, or macro tool name | Reject before service dispatch |
| Extra/missing field or invalid nested value | Reject against the closed tool schema |
| Write in `read_only`, or untrusted write in `review` | Deny before mutation |
| Baseline revision/hash changed while work ran | Reject as stale; do not publish |
| Symbol/footprint cannot be resolved locally | Reject with a bounded validation error |
| Post-swap state/event/receipt write fails | Roll back design and durable records |
| Remove net with endpoints or retained copper | Reject; require disconnect/unroute first |
| Via outside outline or below minimum drill | Reject before native publication |
| `pcb_route_net` would disturb another net | Reject/fail routing; never replace retained copper |

## 5. Good / Base / Bad Cases

- Good: call `pcb_add_component`, inspect it, assign a footprint, place it, then
  route one named net. Each call advances only the state it names.
- Base: a newly added footprint has no pose. KiCad staging may use an explicit
  unplaced origin, but no optimizer runs and the semantic pose remains absent.
- Bad: implement `pcb_update_component` by regenerating placement and routing for
  the entire board, or implement `pcb_export_gerbers` by building every release
  artifact. Both are hidden macros.

## 6. Tests Required

- Registry: assert the exact export count/names, closed nested schemas, fixed
  schema fingerprint, unique external names, and absence of macros/routers.
- Hermes/permissions: assert only `pcbdraft` is registered; parameterize all
  tools through workspace, review, and read-only modes.
- Dispatch: parameterize all 57 tools and assert one fixed handler, correct
  expected revision, stale-call rejection, and legacy replay failure.
- IR/operations: assert v1 stable reads, first-write v2 migration, bidirectional
  block membership, exact endpoint disconnect, non-cascading removal, and native
  geometry limits.
- Atomicity: inject native-generation, swap, state-write, event-write, and empty
  project publication failures; assert live design/records are unchanged.
- Routing: assert ordinary writes use `auto_place=False` and an empty net set;
  assert `route_net` sees only its target while retained geometry is preserved and
  registered as obstacles.
- Evidence: assert each check/render/export invokes only its requested primitive
  and remains discoverable with source revision/hash.

## 7. Wrong vs Correct

Wrong — a semantic edit silently runs a board workflow:

```python
materialize_managed_design(request, candidate, staging)  # defaults may place/route
```

Correct — the model must issue every physical action explicitly:

```python
materialize_managed_design(
    request,
    candidate,
    staging,
    auto_place=False,
    route_net_ids=frozenset(),
    allow_incomplete=True,
)
```

When intentionally changing this contract, update the focused fixed-fingerprint
test and review every exporter/permission/dispatch test in the same change.
