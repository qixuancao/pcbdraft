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

Native verification and convergence use explicit typed boundaries:

```python
inspect_native_consistency(
    design,
    schematic_path,
    board_path,
    *,
    candidate_revision: int,
    graph=None,
    system_python=None,
    require_routed_net_ids: frozenset[str] = frozenset(),
) -> NativeConsistencyReport

compare_progress(before: ProgressVector, after: ProgressVector) -> ProgressDelta

evaluate_convergence(
    observations,
    *,
    state_key: str,
    retry_key: str | None,
    policy=DEFAULT_CONVERGENCE_POLICY,
) -> ConvergenceDecision
```

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
- Hermes execution middleware may dispatch multiple registry-declared read-only
  `pcb_*` queries from one provider response, but dispatches at most one non-read
  PCB write transaction while returning a protocol result for every call id.
- `PCBToolSpec.evidence_stages` and `capabilities` are the sole authority for
  model schema projection. Hermes rebuilds provider schemas from that same
  registry using the current evidence-derived stage; projection only removes
  tools and never changes full-registry execution or permission validation.
  Unknown evidence keeps a bounded corrective subset, while known placement,
  routing, and check stages exclude unrelated project, catalog, administration,
  and delivery tools. Stage cache entries bind project id, live/design revision,
  and the derived evidence source; a change forces reinspection or `unknown`.
- Normal model receipts contain only stage/revisions, operation outcome,
  essential native/progress/convergence deltas, and an opaque transaction id.
  Full receipts remain on disk. `pcb_inspect_transaction` accepts only a current-
  project opaque id and returns fixed fields under item/file/model byte bounds;
  paths are never accepted as artifact identities.
- Trace cost accounting keeps uncached input, output, cache-read, and reported
  actual cost evidence separate from active-context length, repeated-content
  estimates, and newly added token/byte estimates. Missing provider cost remains
  explicitly unknown; cache-read tokens are not context-quality evidence.
- The only bounded semantic write groups are `pcb_connect_group` (1–16 endpoint
  connections to existing nets) and `pcb_place_group` (1–8 absolute footprint
  poses). Every entry is validated before staging; one group produces one
  ChangeSet, candidate revision, native materialization, scoped postcondition
  decision, and commit or rollback. Group payloads never imply auto-placement,
  routing, disconnection, or an arbitrary operation macro.
- IR v1 remains byte/hash compatible during load, inspection, and cloning. The
  first successful typed write promotes a copy to IR v2. IR v2 owns board
  outline, footprint poses, retained route segments, vias, explicit unrouted
  nets, provenance, and geometry revision.
- A write stages both semantic state and complete KiCad materialization outside
  the live design directory. It publishes only after validation, installed
  symbol/footprint resolution, synchronization checks, and revision/content-hash
  comparison under the project lock. On any later records/event failure, restore
  the live design and records; never retain a success receipt.
- The common native gate reopens the staged `.kicad_sch` and `.kicad_pcb`, then
  compares symbols, pins, pads, net partitions, footprint poses, outline,
  non-zero copper, and connectivity with the candidate IR. A consistency report
  may be `passed`, `failed`, or `unknown`; unknown DRC evidence never becomes a
  pass. Operation-specific native deltas are checked in addition to, not instead
  of, the common gate.
- KiCad has no durable native representation for a named net with zero symbol
  endpoints, zero pads, and zero copper. `pcb_add_net` may therefore commit that
  semantic-only IR state when the reopened native project contains no conflicting
  projection. Its operation delta records native projection as not applicable;
  it must not require `board_net=True` until a later connect or route transaction
  gives the net native membership. Once endpoints or copper exist, the usual
  exact native partition checks are mandatory.
- A transaction follows success-last ordering: stage candidate IR -> materialize
  native KiCad -> reopen/project -> check common and operation postconditions ->
  compare DRC delta when required -> publish live design -> publish durable
  receipt/event. Any failure after publication starts restores both live files
  and durable records. A compact model receipt is only a projection of the
  retained full transaction artifact.
- `pcb_route_net` is successful only when native KiCad contains non-zero copper
  for the requested net, the intended native endpoints are connected, no
  unintended net merge exists, and relevant fatal DRC does not regress. Router
  failure codes are exactly `invalid_seed`, `zero_length_seed`,
  `pad_escape_blocked`, `no_legal_channel`, `congestion_exhausted`,
  `search_budget_exhausted`, `native_commit_failed`,
  `native_connectivity_failed`, and `unintended_net_merge`. A `retry_key` binds
  the code/net/endpoints to the design revision and relevant routing state.
- Every committed modification carries a fixed-shape `ProgressVector` at its
  source revision. Each metric is independently `known`, `unknown`, or `stale`;
  unknown is never treated as zero. Convergence compares revisions, blocks blind
  repetition of the same retry key/state, and requires a strategy-changing state
  update before that route can be retried as a new attempt.
- Product termination is a single `ProductSessionTerminalReceipt` separating
  `process_status` (`exited|crashed|cancelled|timed_out`) from `task_outcome`
  (`passed|failed|blocked|incomplete`) and `termination_reason`. A task is passed
  only when the native-derived release gate passed at the same source revision;
  a normal model return, budget exhaustion, or missing/stale evidence is not a
  product pass. Receipt timestamps must be real timezone-aware calendar values;
  session and turn identities are non-empty bounded UTF-8 values. A retried turn
  retains its earlier immutable terminal receipt and uses a fresh attempt/session
  identity rather than overwriting or conflicting with the earlier result.
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
| Empty, oversized, duplicate, conflicting, or unresolved group entry | Reject the whole group before staging |
| Write in `read_only`, or untrusted write in `review` | Deny before mutation |
| Baseline revision/hash changed while work ran | Reject as stale; do not publish |
| Symbol/footprint cannot be resolved locally | Reject with a bounded validation error |
| Post-swap state/event/receipt write fails | Roll back design and durable records |
| Remove net with endpoints or retained copper | Reject; require disconnect/unroute first |
| Added net has zero endpoints, pads, and copper and no conflicting native net | Commit semantic net; record native projection as not applicable |
| Added or updated net has endpoints/pads/copper but native membership is missing or differs | Fail the postcondition; leave the live revision unchanged |
| Via outside outline or below minimum drill | Reject before native publication |
| `pcb_route_net` would disturb another net | Reject/fail routing; never replace retained copper |
| IR says routed but native copper is zero or endpoints are disconnected | Roll back and return a typed native routing failure |
| Native projection is unavailable or stale | Preserve `unknown`/`stale`; do not commit a verified-success claim |
| Same route retry key repeats without relevant state change | Return `strategy_change_required` or `no_progress`; do not call the Router again blindly |
| Agent exits before the release gate | Persist `process_status=exited`, `task_outcome=incomplete`, `termination_reason=agent_returned_before_gate` |
| Product terminal timestamp is impossible, identity is empty/oversized, or evidence revision differs | Reject before durable publication or pass projection |
| The same turn is retried after a failed provider attempt | Allocate a fresh attempt/session identity and retain both immutable terminal receipts |

## 5. Good / Base / Bad Cases

- Good: call `pcb_add_component`, inspect it, assign a footprint, place it, then
  route one named net. Each call advances only the state it names.
- Base: a newly added footprint has no pose. KiCad staging may use an explicit
  unplaced origin, but no optimizer runs and the semantic pose remains absent.
- Base: a newly declared empty net exists only in IR. It becomes native-verifiable
  when `pcb_connect_group`, `pcb_connect_pin`, or routing adds membership.
- Good: a routed candidate is staged, reopened with native connectivity enabled,
  verified to contain non-zero copper and connected target endpoints, and only
  then committed with a progress delta and success receipt.
- Base: ERC or DRC evidence is missing for the candidate revision. The semantic
  change may remain inspectable, but release readiness stays unknown and cannot
  produce a passed terminal receipt.
- Bad: A* returns coordinates, so the service updates IR and reports `routed`
  before reopening the staged board. Search success is not native route success.
- Bad: implement `pcb_update_component` by regenerating placement and routing for
  the entire board, or implement `pcb_export_gerbers` by building every release
  artifact. Both are hidden macros.

## 6. Tests Required

- Registry: assert the exact export count/names, closed nested schemas, fixed
  schema fingerprint, unique external names, and absence of macros/routers.
- Hermes/permissions: assert only `pcbdraft` is registered; parameterize all
  tools through workspace, review, and read-only modes.
- Dispatch: parameterize all 60 tools and assert one fixed handler, correct
  expected revision, stale-call rejection, and legacy replay failure.
- IR/operations: assert v1 stable reads, first-write v2 migration, bidirectional
  block membership, exact endpoint disconnect, non-cascading removal, and native
  geometry limits.
- Atomicity: inject native-generation, swap, state-write, event-write, and empty
  project publication failures; assert live design/records are unchanged.
- Native consistency: cover net merge/split, missing/extra endpoints, valid
  no-connect alternatives, wrong pad nets, non-zero copper, and stable mismatch
  ordering. Assert unknown DRC never sets `passed=true`.
- Operation postconditions: parameterize every mutation family and assert both a
  commit and rollback, including failures after receipt/event publication starts.
- Net projection: assert a zero-endpoint `pcb_add_net` commits without inventing
  a native board net, then assert later endpoint attachment materializes the exact
  native partition. A missing projection with real endpoints must still fail and
  preserve the prior live revision.
- Router: cover all nine machine codes and assert a success has native copper,
  target connectivity, no merge, and no new relevant fatal DRC.
- Progress/terminal: assert unknown versus zero, stale revisions, deterministic
  progress priority, repeated retry stopping, strategy reset, early return,
  no-progress, crash/timeout/cancel, real timestamps, bounded identities,
  retry-attempt receipt separation, and release-gate consistency.
- Semantic groups: assert closed bounded schemas, full prevalidation, one
  ChangeSet/revision/materialization, per-target native deltas, exact group
  poses, retained-copper rejection, and all-or-nothing rollback.
- Routing: assert ordinary writes use `auto_place=False` and an empty net set;
  assert `route_net` sees only its target while retained geometry is preserved and
  registered as obstacles.
- Evidence: assert each check/render/export invokes only its requested primitive
  and remains discoverable with source revision/hash.
- Model context: assert representative normal receipts have a median serialized
  size at or below 1,024 bytes, first-binding paths do not repeat, explicit
  transaction inspection is bounded/current-project-only, stage schemas remain
  deterministic registry subsets across provider shapes, and cost/context
  metrics remain separate with unknown monetary cost preserved.

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

Wrong — equating software exit with product completion:

```json
{"process_status": "exited", "task_outcome": "passed", "release_gate_passed": false}
```

Correct — lifecycle and PCB outcome remain independent and internally bound:

```json
{
  "process_status": "exited",
  "task_outcome": "incomplete",
  "termination_reason": "agent_returned_before_gate",
  "release_gate_passed": false
}
```

Wrong — requiring KiCad to persist an empty semantic net:

```json
{"operation": "add_net", "expected": "board_net=True", "endpoints": 0, "pads": 0}
```

Correct — defer native membership until the net has electrical content:

```json
{
  "operation": "add_net",
  "semantic_net_added": true,
  "native_projection": "not_applicable_empty_net",
  "postcondition_passed": true
}
```

When intentionally changing this contract, update the focused fixed-fingerprint
test and review every exporter/permission/dispatch test in the same change.
