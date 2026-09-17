# PCBDraft architecture

PCBDraft is an agent-safe runtime around KiCad, not a replacement EDA GUI.
It exists to let an autonomous agent turn a standing engineering goal into
reviewable KiCad artifacts while retaining enough evidence to explain either
success or failure. The product is model-agnostic: a model interprets
requirements, plans circuit topology, and selects tools, but it never owns raw
KiCad text, geometry, filesystem writes, command execution, validation
outcomes, or release identity.

## Control ownership

Engineering decisions are free; permissions, data integrity, and real
execution are not:

    User Standing Goal
            |
            v
       PCBDraft Agent
       /      |      \
   inspect  design  modify
       \      |      /
      PCB Tool Registry (flat concrete operations)
            |
            v
     ApplicationService
            |
            v
     Semantic Graph / KiCad
            |
            v
     Facts and Evidence
            |
            v
       PCBDraft Agent
            |
            v
    continue / done / blocked

- **agent.loop.AIAgent** owns reasoning, conversation, and autonomous tool selection.
  After every tool result the model may freely choose the next tool; there is
  no mandatory plan/generate/validate/repair/release sequence.
- **GoalManager** (native `/goal`) owns standing-goal continuation
  and the done/continue/wait judgment. Goal state stays minimal: goal,
  status, turns_used, max_turns.
- **AgentOrchestrator / JobRunner** own durable dispatch, persistence,
  recovery, and budgets. Web conversations use **ConversationOrchestrator**,
  which journals the tools selected by the same AIAgent loop used in the terminal.
  The historical deterministic producer is limited to compatibility jobs and
  explicit shortcut turns.
- **PCBToolRegistry / PCBToolExecutor** own tool authority, schemas, and
  fixed dispatch; **PermissionBroker** owns approval policy;
  **ApplicationService** owns authoritative project mutation;
  **CircuitPlan / Design IR** own semantic PCB data; KiCad adapters own exact
  native file and geometry operations; **Verification** owns real engineering
  evidence.

## Code organization

Implementation modules live in responsibility-focused packages rather than the
package root:

    pcbdraft/
      core/          shared safety and runtime primitives
      domain/        PCB IR, requirements, parts, blocks, and deterministic rules
      agent/         constrained planning, events, tools, repair, and runtime
      model/         model configuration, transport, review, and providers
      kicad/         native KiCad generation, layout, routing, preview, and sync
      services/      application use cases, jobs, managed projects, transactions
      tools/         reusable tool implementations and external tool adapters
      verification/  evidence, validation, review, benchmark, and release gates
      interfaces/    the ``pcbdraft`` CLI, loopback GUI API, and legacy terminal facade
      terminal_client/  canonical TypeScript interactive terminal source

The canonical supported TypeScript terminal source is
`src/pcbdraft/terminal_client`; packaging includes that directory as an installed
resource used by the terminal launcher.

### Module ownership map

| Package | Owns | Does not own |
| --- | --- | --- |
| `core` | Generic errors, safe I/O, redaction, locking, process, run, and path primitives | PCB semantics, model calls, presentation, or application workflows |
| `domain` | Immutable PCB IR, parts, requirements, change sets, and deterministic invariants | Filesystem publication, provider transport, or UI rendering |
| `services` | Application use cases, project/session stores, jobs, managed projects, and transaction coordination | Presentation rendering or model-provider wire protocols |
| `agent` | Conversation control, planning contracts, tool selection, permissions, durable turns, and bounded agent behavior | Authoritative PCB writes or provider credential storage |
| `model` | Provider configuration, authentication, transports, response normalization, and model metadata | Project mutation, KiCad generation, or permission decisions |
| `tools` | Reusable tool implementations plus external tool and environment adapters | PCB project authority or agent turn policy |
| `interfaces` | CLI dispatch, the loopback HTTP/SSE API, launchers, and compatibility facades | Engineering decisions, transcript authority, or a second job store |
| `kicad` | Native KiCad generation, inspection, geometry, routing, previews, and synchronization | Product workflow policy or release claims |
| `verification` | Persisted evidence evaluation, validation gates, BoardBench, and release decisions | Design generation or authoritative project mutation |

### Implemented extraction ledger

The modules below are present on the current modularization integration branch.
Each extraction has focused unit coverage and passed the targeted checks for its
own boundary. That evidence establishes local compatibility; it is not a
whole-repository regression run or a release gate.

#### ApplicationService

- `services.native_operations` owns native KiCad projections, operation
  postconditions, routing-failure normalization, and native delta checks.
- `services.application_progress` owns immutable progress, stage, convergence,
  and route-retry projections.
- `services.application_status_projection` owns first-run/runtime diagnostics
  views and evidence-bound engineering-stage dictionaries. It combines
  read-only doctor, provider-capability, revision, and stage observations.
- `services.application_project_store` owns bounded project-record loading,
  validated paths, atomic record/event writes, attempt reads, and public views.
- `services.application_project_lifecycle` owns private draft construction and
  publication of new project identities.
- `services.application_project_queries` owns read-only project listing, public
  project views, lock-consistent non-blocking snapshots, and validated project
  root lookup.
- `services.application_message_inputs` owns bounded message-text normalization,
  exactly-once reply delivery binding validation, and read-only duplicate
  delivery projection from retained conversation messages.
- `services.application_semantic_operations` owns semantic-operation argument
  normalization and grouped-operation preflight checks.
- `services.application_external_revision` owns review and explicit import of
  externally edited native placement revisions.
- `services.application_validation` owns application-level validation and
  generated-project preview workflows.
- `services.application_release` owns building, verifying, and retaining one
  manufacturing-candidate release.
- `services.application_modification_preview` owns staging an agent-planned
  project revision for review.
- `services.application_modification_revert` owns discard and atomic undo flows
  for staged or applied project revisions.
- `services.application_product_session` owns immutable terminal-outcome
  receipts for product-session turns.
- `services.application_tool_inspection` owns read-only PCB, transaction
  evidence, installed-library, and part-catalog inspection.
- `services.application_native_outputs` owns individual native PCB check,
  preview-render, and manufacturing-export result workflows.
- `services.application_agent_repair` owns read-only project-event projection
  and reviewable agent repair proposal normalization and preparation.

`services.application.ApplicationService` remains the composition root and the
only project mutation, transaction, publication, and project-state authority.
Transactional repair execution and writes, including pending repair artifacts,
project-state transitions, and failure publication, remain in this host. The
host also retains repository configuration and recovery, project creation,
message delivery and transcript writes, provider dispatch, generation
confirmation, modification application, and release verification. It also
retains expected-revision validation, native/validation/transaction evidence
reads, and progress-stage derivation consumed by the status projection. The
project-query, message-input, and status-projection modules receive late-bound
host adapters for their historical lock, validation, text-bound, sanitizer,
doctor, and validation-run-id patch points; they do not create a parallel
application service, lock projects, dispatch model requests, or write project
records.

#### SessionDB

- `services.session_db_runtime` owns reusable SQLite journal negotiation,
  runtime PRAGMAs, and persistence-error classification.
- `services.session_db_connection` owns connection construction, the bounded
  WAL read pool, write transaction retry and reconnect policy, checkpoints,
  and deterministic close behavior.
- `services.session_db_schema` owns schema creation, column reconciliation,
  FTS DDL, and compatibility backfills.
- `services.session_db_fts_integrity` owns FTS capability probes, trigger and
  schema self-healing, and runtime corrupt-index recovery decisions.
- `services.session_db_metadata` and `services.session_db_token_accounting` own
  mutable activity/model metadata and asynchronous token/model accounting.
- `services.session_db_transcript_write`,
  `services.session_db_transcript_query`, and
  `services.session_db_conversation` own transcript serialization/writes,
  bounded transcript queries, and resume reads.
- `services.session_db_inspection` owns single-session row projection, exact or
  unique-prefix ID resolution, dominant model-route reads, and the archived-row
  existence probe.
- `services.session_db_lineage` owns the read-only classification of explicit
  branch/delegate/tool children versus compression continuations and projects
  compression ancestor-to-tip chains. It performs no lifecycle or lease writes.
- `services.session_db_rewind` owns duplicate-replay detection and transcript
  tail soft-delete/restore behavior.
- `services.session_db_presentation`, `services.session_db_listing`, and
  `services.session_db_search_metrics` own titles/visibility/read state, list and
  usage projections, search, and lightweight store metrics.
- `services.session_db_deletion` and `services.session_db_pruning` own explicit
  deletion/file cleanup, archive/prune/stale-marker maintenance, empty TUI
  ghost removal, and orphaned-compression finalization.
- `services.session_db_maintenance` owns size measurement, compaction, FTS
  merging, checkpointing, and best-effort automatic maintenance workflows.
- `services.session_db_meta_store` owns namespaced `state_meta` values and the
  one-time kanban compatibility gates.
- `services.session_db_telegram_topics` owns Telegram DM topic-mode opt-in state
  and durable chat/thread-to-session bindings.
- `services.session_db_handoff` owns the durable cross-platform
  pending-to-running-to-completed-or-failed handoff state machine.
- `services.session_db_lifecycle` owns session-row creation and enrichment,
  end/reopen/reset transitions, transcript replacement, and compaction commit
  gateways.
- `services.session_db_compression_lease` owns compression lease acquisition,
  renewal, inspection, release, and in-transaction fence/recovery helpers. It
  uses the host write-transaction adapter and a late-bound process-liveness hook.
- `services.session_db_compression_health` owns durable compression cooldown,
  fallback and ineffective-compaction counters, plus gateway hygiene streaks.
  Connection and write-transaction authority remain on the host.
- `services.session_db_turn_lease` owns compression-lineage lease-key mapping
  and cross-process turn acquire/wait/refresh/release behavior. Connection,
  schema, and write-transaction authority remain on the host.
- `services.session_db_gateway_queries` owns read-only gateway session listing,
  origin and peer recovery lookup, and orphan-adoption candidate projection.
- `services.session_db_gateway_routing` owns durable gateway peer recording,
  expiry-finalization state, the scoped routing-index API, and never-active
  keyed-session cleanup. The host retains SQLite connection, transaction,
  schema, and session-deletion authority.

`services.session_db.SessionDB` remains the authoritative durable session store
and composition root. Callers still cross this host for connection, schema, and
FTS behavior; the corresponding mixins receive their shared state, constants,
and late-bound compatibility hooks from this boundary rather than forming a
second store. Compression and session-turn lease acquisition, renewal, and
release continue through the host API; compression leases are implemented by
the dedicated compression mixin, and session-turn leases by their dedicated
turn mixin. Gateway peer and routing-index persistence use the dedicated routing
mixin, while SQLite transaction authority and orphan adoption remain in the
host. Gateway peer reads use the dedicated query mixin. The lineage read model
owns none of those responsibilities, and none of the extracted modules imports
the `session_db` coordinator back.

#### AIAgent

- `agent.stream_delivery` and `agent.status_delivery` own visible stream
  delivery, callback ordering, terminal status buffering, and stream
  diagnostics.
- `agent.message_preparation`, `agent.api_message_helpers`, and
  `agent.response_cleanup` own provider-safe message/image preparation,
  tool-call/API message normalization, and visible response/reasoning cleanup.
- `agent.provider_capabilities` and `agent.error_normalization` own endpoint and
  API capability policy, timeout selection, and safe provider-error summaries.
- `agent.memory_lifecycle`, `agent.activity_tracking`, and
  `agent.client_lifecycle` own external-memory synchronization, activity/rate
  limit/credit observations, and best-effort resource teardown.
- `agent.request_client_lifecycle` owns request-scoped OpenAI and Anthropic
  client cache keys, creation, owner-close, and cross-thread abort behavior.
- `agent.api_hook_observability` owns bounded API request/response hook
  payloads, recursive secret redaction, error-hook dispatch, and debug dumps.
- `agent.session_persistence` owns persistence-time message cleanup, user-message
  override projection, append batching and intrinsic-marker deduplication, plus
  bounded adoption of a live compression continuation.
- `agent.session_record_projection` owns the pure in-memory projection used to
  roll history back before its last assistant record, normalize persisted
  assistant content, and redact text fields in plain or multimodal message
  records. It performs no snapshot or database writes.
- `agent.turn_result_formatting` owns pure user-facing rendering for bounded
  file-mutation failure footers, bare-path neutralization, and abnormal
  turn-completion explanations. It records no mutation or turn state.
- `agent.turn_control` owns cross-thread interrupt and hard-stop propagation,
  queued steering, and active-turn redirect coordination.

`agent.loop.AIAgent` still composes those mixins and owns the model turn,
request/conversation and tool loops, model invocation, cancellation lifecycle
entry points, and session or model switching. It also retains session snapshot
save orchestration and all persistence writes, file-mutation outcome recording,
display-gate configuration and caching, and turn-finalization orchestration.
The extracted modules preserve historical method names and late-bound
compatibility hooks without importing `agent.loop`.

#### Model authentication and auxiliary clients

- `model.auth_error_formatting` owns structured authentication errors,
  rate-limit classification, and user-facing authentication and entitlement
  guidance.
- `model.auth_credential_pool_store` owns the credential-pool portion of
  `auth.json`, including profile/global fallback reads, concurrency-safe pool
  writes and cooldown merging, and credential-source suppression state.
- `model.auth_provider_state` owns scoped provider-state and active-provider
  queries, explicit provider-configuration detection, credential clearing and
  deactivation, and unknown-provider configuration diagnostics.
- `model.auth_provider_endpoints` owns provider endpoint normalization,
  API-key discovery, and Z.AI endpoint probing and cached endpoint selection.
- `model.auth_provider_policy` owns provider credential expiry parsing and TTL
  normalization, plus Nous Portal and inference endpoint policy.
- `model.auth_store_persistence` owns auth-store paths, cross-process locking,
  atomic credential persistence, profile/global fallback reads, and provider
  state write-through.
- `model.auth_qwen_oauth` owns the existing Qwen CLI token read/write, refresh,
  runtime credential, and status lifecycle.
- `model.auxiliary_cancellation` owns synchronous-call cancellation,
  request-scoped interrupt protection, progress callbacks, and its isolated
  worker.
- `model.auxiliary_adapters` owns the Codex, Anthropic, and Bedrock completion
  adapters, chat shims, and client wrappers used by auxiliary calls.
- `model.auxiliary_response_projection` owns dict/object response text lookup,
  Responses-to-chat-completions shape recovery, and visible-content versus
  structured-reasoning text projection. It performs no network, credential,
  routing, accounting, or relay work.
- `model.auxiliary_input_helpers` owns the defensive type probe and pure URL
  query-default split used by auxiliary endpoint setup. It reads no runtime
  state and owns no request, transport, credential, provider, fallback, cache,
  accounting, or relay policy.
- `model.auxiliary_provider_config` owns provider/model catalog rules, endpoint
  normalization, and request-header construction.
- `model.auxiliary_provider_failures` owns provider-failure and recoverability
  classification that does not require routing or cache state.
- `model.auxiliary_fallbacks` owns auxiliary-provider health state, fallback
  destination planning, and synchronous/asynchronous fallback-chain execution.

`model.auxiliary_client` remains responsible for runtime state and endpoint or
proxy validation, request construction and HTTP dispatch, credential
resolution, provider selection and fallback-chain integration, pooling and
client/cache orchestration, `call_llm`, usage accounting, and relay completion.
`model.auth` composes the authentication boundaries and coordinates OAuth/token
lifecycle, top-level `resolve_provider`, and runtime credential resolution.

#### MCP and terminal presentation

- `tools.mcp_content` owns MCP content-block normalization, rendering, and local
  caching; `tools.mcp_connection_policy` owns remote URL/header/certificate and
  redirect/error policy; `tools.mcp_tool_schema` owns stateless tool naming,
  filtering, recursive provider-compatible input-schema normalization, schema
  conversion, and lifecycle-config parsing; and
  `tools.mcp_runtime_loop` owns process/discovery guards, dedicated event-loop
  startup, caller-context propagation, and synchronous MCP call delivery;
  `tools.mcp_connection_recovery` owns connection cooldown and circuit-breaker
  state, trust metadata and approval gating, and reconnect signaling/readiness
  waits; `tools.mcp_server_configuration` owns configuration source loading,
  interpolation, filtering, safe stdio environments, and command assembly;
  `tools.mcp_task_lifecycle` owns server-task transport setup and
  teardown, stdio subprocess cleanup, HTTP/SSE session lifecycle, keepalive,
  and recycle behavior; `tools.mcp_tool_discovery` owns capability-aware
  discovery, dynamic refresh, schema registration, and lazy-cache registration;
  and `tools.mcp_utility_handlers` owns resource/prompt utility dispatch,
  argument checks, and result normalization. `tools.mcp_tool` remains the
  actual RPC and generic tool-call coordinator, including authentication retry,
  connection orchestration, and registration dispatch.
- `terminal_client/src/display.ts` owns pure formatting of saved transcript
  messages and job-lifecycle status text; `assistant-preview.ts` owns transient
  delta rendering and saved-transcript reconciliation. `commands.ts` retains
  slash-command resolution and unique-prefix completion, `bridge.ts` retains
  typed GUI API and SSE I/O, and `main.ts` retains interactive control flow and
  composes those presentation responsibilities. `startup.ts` owns initial
  project selection, while `interfaces.terminal_launcher` starts the bundled
  client against the authoritative loopback GUI API.

This ledger describes implemented, targeted-tested boundaries only. The broader
modularization remains in progress: large coordinator modules and compatibility
facades still exist, and this state must not be treated as whole-project
completion, full regression evidence, or release readiness.

The dependency direction starts with `core` and `domain`. KiCad and model
adapters implement external boundaries. Services orchestrate those capabilities,
verification evaluates their persisted results, and interfaces translate user
input without becoming a second business-logic layer. See
[`PROJECT_STRUCTURE.md`](PROJECT_STRUCTURE.md) for placement rules and the
compatibility policy for historical module paths.

The TypeScript terminal and local Web workbench are presentation clients. Both
use the loopback GUI HTTP/SSE API, whose reconnect payload is defined by the
versioned types in `services.gui_session_contract`. This is the sole supported
client protocol: neither client imports Python service internals or creates a
second project, job, or transcript store.

BoardBench follows the same boundary: a thin repository script invokes the
verification-owned runner, evaluator, evidence importers, correction diff, and
report/seal APIs. It never becomes a second generator or exposes reference
contracts as model tools. Its source-hashed evidence lifecycle and physical-test
limits are documented in [`BOARDBENCH.md`](BOARDBENCH.md).

## Flat tool protocol

The model sees one layer of concrete `pcb_*` operations exported from the
canonical `PCBToolRegistry`. Project/inspection/library reads, semantic edits,
native placement/routing, checks, renders, and exports each have a distinct
closed schema. There is no model-facing macro and no `operation` router field.
PCBDraft and MCP descriptors derive from the same immutable specs and share a
regression-tested schema fingerprint.

PCBDraft binds each model session to the project selected by the trusted terminal
boundary. Project enumeration and switching remain human commands rather than
model tools. Installed symbol/footprint reads are machine-local facts and need no
project cursor. Canonical part search/description is project-scoped, and explicit
installed-KiCad part registration stages the catalog, semantic IR, and native
project together. Tool-execution middleware returns a result for every provider
call id but dispatches at most one `pcb_*` action from each model response.
Trusted project changes rotate the existing PCBDraft session before another model
request, partitioning the prior project's transcript while retaining the
persisted provider/model authority.

Every authoritative write maps to exactly one typed semantic/native operation.
`ApplicationService` stages the new IR and complete KiCad materialization,
verifies content-hash/native parity, then publishes under the project lock and
baseline-revision comparison. A failed semantic change, resolver lookup,
materialization, or publication leaves the authoritative project unchanged.
IR v1 remains byte/hash stable for reads and is promoted lazily to IR v2 on the
first successful write; v2 records outline, footprint poses, retained routes,
vias, explicit unrouted nets, and geometry provenance.

Tool results report facts only: what ran, whether it succeeded, what changed,
current state, findings, limitations, and evidence references. Results never
contain prescriptive `next_step` or `required_workflow_stage` fields; project
status (`draft`, `generated`, `validation_failed`, `validated`, ...) is an
engineering fact, not a router for the next tool call.

## Product path

    TypeScript terminal                    local Web workbench
             |                                    |
             +----- loopback GUI HTTP/SSE API ----+
                                  |
                       typed gui_session_contract
                                  |
                         GUI session adapter
                                  |
                JobRunner / ConversationOrchestrator
                                  |
                         agent.loop.AIAgent
                               |
                     model selects each tool
                               |
               per-conversation tool_session context
               (project, permission policy, application service)
                               |
               AgentTurnStore journals before dispatch
                               |
                               v
                  PermissionBroker: allow / ask / deny
                  - exact call + argument hash + baseline revision binding
                                       |
                                       v
                  PCBToolRegistry / PCBToolExecutor
                  - closed flat semantic/native operation catalog
                  - strict JSON Schema and allowed states
                  - source, effect, risk, and baseline-revision checks
                  - OpenAI Responses / MCP descriptor exports
                                       |
                                       v
                  ApplicationService: projects, events, locks,
                  confirmation, recovery, retained attempts
                  - emits the project event stream used by both clients
                        |
                        v
              requirement interpretation
              - preserve named parts
              - state assumptions and missing facts
              - retain complex domains and attach non-blocking warnings
                        |
                        v
              schema-constrained circuit plan
              - functional blocks, components, nets, power domains, interfaces
              - scalar constraints and locally evaluated assertions
              - no coordinates, traces, KiCad syntax, code, or commands
                        |
                        v
              local KiCad resolver
              - installed symbol and footprint availability
              - project-local PartGraph records
              - stock KiCad library identity and pin data
                        |
                        v
              deterministic topology preflight
              - power-input and rail-source evidence
              - applicable I2C pull-up / decoupling evidence
              - explicit findings; normal attempts remain possible
                        |
                        v
              semantic Design IR
              - immutable canonical form and content hash
              - transactions, snapshots, diffs, recovery
                        |
              +---------+-----------------------+
              |                                 |
              v                                 v
     KiCad schematic / bounded PCB attempt     L0–L7 evidence gates
              |                                 |
              +------------- retained attempt --+
                              |
                  bounded semantic repair (legacy macro path)
                  - sanitized generation evidence
                  - completed deterministic L1-L3 failures only
                  - replacement plan through the same compiler
                  - staged validation before atomic apply

The Python runtime remains authoritative: `ApplicationService` is the only
business write authority, `JobRunner` owns durable execution,
`ConversationOrchestrator` runs the conversation loop, and `AgentTurnStore`
owns durable turn and tool records. The GUI session adapter persists none of
that state. It exposes bounded presentation data through the versioned
`gui_session_contract` and the loopback GUI API.

The TypeScript terminal owns rendering, input, slash-command resolution, and
reconnect behavior only. During a turn it displays lifecycle events and bounded,
redacted `assistant.delta` previews exposed by the GUI SSE stream. Those preview
events are transient presentation hints rather than transcript authority. When
a terminal `job.complete` or `job.failed` event arrives, the client fetches the
session again and reconciles the display with the assistant message saved by the
Python runtime.

`AgentRuntime` and `JobRunner` turn synchronous, transactional application
operations into durable background turns and UI-neutral activity events.
Before any side effect, `AgentOrchestrator` persists a versioned `TurnRecord`
and `ToolRunRecord` under `agent-turns/`. The compound
`(thread_id, turn_id, tool_call_id)` identity owns progress, result, and approval
state. A waiting approval additionally binds the canonical argument hash and
observed engineering revision, so UI status alone can never authorize a write.

The semantic design graph is a working engineering representation. The agent
may inspect it with concrete inspection tools, then extend or revise it with
individual add/remove/update/connect operations. Native board intent is stored
beside semantic topology so regenerated KiCad artifacts preserve deliberate
outline, pose, route, unroute, and via decisions.

### Legacy deterministic producer

The historical next-tool producer is retained as an explicit legacy mode. At
the start of a natural-language turn it permitted at most one native OpenAI
Responses function-tool decision, and only when the selected provider used the
built-in OpenAI preset, provider ID `openai`, and the exact `api.openai.com`
hostname; every later operation in that turn used the deterministic producer.
This hybrid routing is no longer the controller of the default PCBDraft agent —
the model now re-selects tools after every result — but it still drives explicit
compatibility jobs and shortcut turns. Web natural-language turns use the native
conversation loop with durable dispatch. All other providers,
including custom OpenAI-compatible endpoints, use that local-policy fallback
for the legacy path even though they may still supply schema-constrained
requirement interpretation and circuit planning.

Before the native request is sent, the router writes
`agent-turns/model-decisions/{turn_id}-router.json`. The journal binds
the request hash to the project, turn, durable message, project status and revision,
provider, model, endpoint, and offered tools. A completed decision can be reused
as the same call. A dispatched decision without an exact result, and a recorded
failure, are not automatically POSTed again; the producer falls back to the
deterministic local decision. This journal is control-plane evidence only. A
model selection or model-generated prose cannot assert that an engineering
operation succeeded; only executor receipts, local state, and validation
evidence can do that.

Every proposed operation still crosses `PermissionBroker`, the closed
`PCBToolRegistry`, and `PCBToolExecutor`, which reject unknown tools,
extra/nested arguments, stale revisions, and invalid states. The registry emits
strict OpenAI Responses function declarations from the
same specs, but the adapter receives no handlers or direct
`ApplicationService`, filesystem, shell, or raw KiCad authority.

The durable Job envelope is also an authority boundary. Version 2 snapshots the
exact permission mode, registry authority fingerprint, and tool-call limit when
the job is submitted. Both recovered execution and final dispatch revalidate
that binding. A legacy/unbound job, old direct action, missing turn, symlinked
record, or policy mismatch becomes a terminal audit record without dispatch;
restarting under a different policy can never promote it into executable work.
After a durable dispatch marker, an exception without an exact effect receipt is
stored as an interrupted, non-replayable outcome rather than a normal failed call.
A model-selected direct intent that fails or is denied is also fail-closed: local
state policy cannot reinterpret it as a different operation during retry.

The bundled TypeScript terminal (`src/pcbdraft/terminal_client`) and local Web
workbench use the same loopback GUI API and therefore the same native
`agent.loop.AIAgent`, model
configuration, authentication, and project/session authority. Terminal
commands select projects through that trusted boundary; jobs bind their own
project and permission context, including in propagated tool-worker contexts.
`ConversationOrchestrator` records each model-selected tool before dispatch,
retains model conversation history in the project session database, and writes
assistant replies to the durable turn. Approval resumes the exact pending tool
once and provides its receipt to the model; cancellation interrupts the model.
The default `workspace` permission policy allows requested project-local work;
`review` retains an exact checkpoint before authoritative writes.
The deterministic producer remains only for compatibility jobs and explicit
shortcut actions.
Follow-up messages on a generated project are compiled into an isolated replacement,
validated under `transactions/`, and only then atomically applied by policy.
The durable runtime retains bounded tool history across recent turns, including
effect, risk, argument hash, baseline/result revisions, bounded arguments, and
the local result receipt. A restart marks an
incomplete job and active tool interrupted rather than replaying its side
effects. A retry creates another Job attempt over the same `turn_id`; completed
tool receipts are reused. A call that was durably marked as dispatched but
lacks an exact matching result receipt is ambiguous even when the project
revision did not advance; it is failed closed and is never dispatched again.
The user must inspect the retained project and submit a new turn.

The historical Python terminal is isolated behind
`interfaces.tui.app`, a compatibility facade that forwards existing imports to
`interfaces.tui.legacy_app`. It remains available for compatibility but is not
the client protocol or the implementation of the supported TypeScript terminal.
Bare `pcbdraft` is the supported TypeScript terminal entrypoint;
`pcbdraft terminal` is its explicit alias. The terminal source ships once as a
Python package resource, so source checkouts, wheels, and sdists use the same
files. The launcher validates Bun, starts a GUI service on `127.0.0.1` when the
selected port is free, or reuses an already healthy PCBDraft GUI on that
loopback port. It forwards an initial `--project` selection to the client and
never accepts a non-PCBDraft service occupying the selected port. Legacy
permission modes remain available through the explicit
`pcbdraft legacy-terminal` compatibility entrypoint; unsupported provider and
timeout flags fail clearly instead of being discarded.

## Goal Mode

The default agent loop is a simple Ralph-style goal loop built on the native
PCBDraft `GoalManager` (no PCBDraft-specific task system is added):

1. the user's PCB request becomes a standing goal (`/goal <objective>`);
2. the PCBDraft agent runs one normal turn with the full tool surface;
3. after the turn, a judge decides `done`, `continue`, or `wait`;
4. on `continue`, a plain continuation message is appended to the same session
   — it restates the goal and asks the agent to inspect current project state
   and take the next concrete engineering action it judges most useful; it
   never names plan/generate/validate/repair/release as required stages;
5. a new user message can pause, change, or replace the goal;
6. generic turn/tool budgets pause the loop honestly instead of pretending
   completion.

Goal state stays minimal — goal, status, turns_used, max_turns (plus the
optional verification contract the native manager already supports). There is
no WorkPlan, TaskGraph, milestone graph, or risk ledger.

## Generic request and plan

<code>AgentDesignRequest</code> is a durable statement of the user's request:
board envelope, scope, named parts, functions, power assumptions, and source
context. It does not encode a board profile.

<code>CircuitPlan</code> is a compact, versioned plan proposed by a configured
planner. Version 2 contains only:

- component identity, KiCad symbol, optional footprint, role, and exact user name;
- functional blocks with acyclic parent links and complete component ownership;
- nets whose endpoints use real component IDs and symbol pin numbers, with
  optional links to declared power domains and interfaces;
- power-domain sources and interface membership expressed with those same
  endpoints;
- supported semantic constraints with named scalar parameters, including
  complete connector pinouts, exact net labels, named placement regions,
  anchored rectangular board keepouts, and differential-pair acceptance
  criteria, plus a finite set of assertions that the runtime evaluates locally;
  and
- assumptions, summary, and review notes.

The runtime looks up actual local symbol candidates before planning. It rejects
unknown symbols, invalid pins, duplicate pin-to-net assignments, incomplete or
cyclic block ownership, dangling domain/interface references, raw geometry,
executable expressions, and a plan that drops an explicitly requested part.
Persisted version-1 plans remain readable and compile through their original
component-per-block compatibility path; providers receive only the version-2
schema. This is not a guarantee that the plan is electrically correct; it is a
controlled boundary between model text and engineering data.

Spatial intent is deliberately symbolic at that boundary. The model selects a
finite region or anchor name, dimensions, and a copper-layer scope; deterministic
code derives the rectangle from the reviewed board envelope, applies it to
placement and bounded routing, and records exact generated-geometry metrics.
Differential-pair checks measure routed width, edge-to-edge gap, coupled-length
ratio, and length mismatch. They do not infer impedance and the current router
does not synthesize coupled pairs. Board keepouts are currently generator
constraints and receipts, not native KiCad rule-area objects, so later manual
editing still requires a fresh engineering review.

After compile, the runtime produces deterministic <code>plan_review</code>
evidence from the selected local symbols and topology. It flags missing
power-input coverage, supply/ground polarity, implausible rail sources, output
contention, two-terminal shorts, ground-referenced LED polarity, per-line I2C
pull-ups, and applicable decoupling evidence. A separate versioned component
qualification report verifies installed symbol/footprint availability and exact
symbol-pin-to-native-pad coverage. KiCad datasheet URLs remain explicitly
reference-only, and extracted manufacturer/MPN claims remain unverified. These
are not a fixed part template or a general-part denylist: a finding explains
what an attempted project still needs. The default terminal continues the
bounded attempt automatically, while manual clients may leave the plan staged
for review.

## Stock KiCad component resolution

<code>PartGraph</code> owns canonical component records. For the generic path,
the resolver extracts a record from the locally installed KiCad symbol and
footprint libraries. Such records and their retained
<code>component-qualification.json</code> have:

- trust state <code>extracted</code>;
- lifecycle state <code>unknown</code>;
- local-library and generation-time pad-map provenance;
- a datasheet locator classified as reference-only when KiCad supplies one; and
- no claimed manufacturer verification, sourcing status, rating, simulation, or
  layout qualification.

These internal records let the existing KiCad adapters work without requiring a
manufacturer, MPN, datasheet, supplier, import, or non-stock library from the
user. They do not turn a library symbol into an authoritative electrical model.

## KiCad generation and retention

The semantic IR is compiled through the existing KiCad adapters:

- <code>kicad/schematic.py</code> emits a native schematic through
  <code>kicad-sch-api</code>;
- <code>kicad/pcb.py</code> uses deterministic placement seeds plus the bounded
  placement/routing backend and KiCad's PCB API;
- <code>services/managed.py</code> stages all files in a sibling directory and publishes
  only on success.

Fine-pitch escape segments are checked against exact pad/track rectangles before
their terminals are reserved on the bounded routing grid. Half-grid coordinates
use one stable rounding rule, so a 0.5 mm pad array cannot alternately collapse
to 0.4 mm. If routing is incomplete, reference-plane work is skipped and the
error retains the unrouted nets, bounded-search count, and concrete pad/escape
diagnostics. Reference-plane connections are demand-driven: an existing
through-hole ground pad or routed ground via counts as a real connection; a
pure-SMD board receives one safe tie instead of an arbitrary universal via count.

For a generic success, the exact reviewed <code>circuit-plan.json</code> is a
tracked managed member alongside the request, IR, and project-local part graph.
Its hash must match the IR provenance before publication; the parsed plan is
therefore available to later review without trusting arbitrary adjacent JSON.

For an application generation failure, the attempt directory retains the approved
request, circuit plan, IR, project-local part graph, error, and every native
artifact that had already been produced. A router failure is therefore a useful,
inspectable result, not a hidden substitution, a later stitching error that masks
the cause, or a false success.

The legacy TUI path may make at most two bounded repair attempts per turn; the
default PCBDraft agent decides itself how many repair cycles are useful within the
generic turn/tool budgets. A repair provider
receives a bounded JSON feedback record and must return a complete replacement
<code>CircuitPlan</code>; it cannot patch native KiCad text. The replacement is
resolved against the same installed symbols and compiled through the same
semantic boundary. For an already generated project, native files and validation
evidence are first created under a retained transaction. A failed candidate never
changes the authoritative design. A candidate without completed deterministic
L1-L3 failures is
exposed as a semantic diff and atomically applied by the agent; the exact previous
managed project remains available for undo. Unknown, heuristic, and
human-required evidence never causes an automatic repair loop.

## Replaceable planning providers

Planning providers implement the same three operations: interpret requirements,
propose a complete circuit plan, and revise a complete plan from bounded tool
feedback. The configured model API and OpenAI-compatible endpoints therefore do
not own separate PCB workflows. Provider output always enters the same schema, installed-symbol,
semantic-plan, generation, validation, and transaction boundaries.

Provider identity is resolved to a small declarative wire profile before the
shared Chat Completions transport builds a request. Profiles select native JSON
Schema, JSON Object, or prompt-constrained JSON; the output is always decoded and
validated again with the full local Draft 2020-12 schema. They also own compatible
token and sampling parameter names. Transient 408/409/425/429 and 5xx responses,
timeouts, and network failures have one three-attempt retry policy bounded by the
caller's original deadline. Authentication, billing, malformed requests, TLS
verification, redirects, and schema failures are never retried. No policy silently
changes the configured provider or model.

## Validation and release

L0–L7 reports distinguish completed, unavailable, heuristic, human-required, and
not-applicable evidence. In particular:

- L0 checks project/file/IR coherence;
- L1 checks symbols, pins, actual footprint pad numbers, generic electrical
  topology, component evidence, and trust state;
- L2 runs the applicable KiCad ERC/DRC checks;
- L3 checks semantic/interface rules when the design supplies such rules;
- L4–L7 require sourcing/manufacturing, simulation/physical analysis, human
review, and board-test evidence as applicable.

The existing <code>review</code> workflow uses this provenance on intact generic
managed projects. It receives the generation request, persisted plan, part
records and their trust states, a deterministic preflight recomputed from the
persisted plan/current IR, and native KiCad evidence. Generic
projects do not fabricate <code>verified_blocks</code>; that field is empty
unless a legacy deterministic fixture actually supplies one.

ERC/DRC are necessary but not proof of a functioning or manufacturable board.
Generic local-library records keep their unknown evidence state in detailed
validation reports. That state does not block generation or turn a successfully
created KiCad project into a failed application operation.

## Domain handling

Layer count is an internal design parameter when the user does not specify it;
the requirement interpreter selects an initial stackup without asking the user
to understand PCB layer planning. If the user does specify a positive count,
the backend preserves it through the planning path. The installed KiCad build
determines actual stackup support during generation; an unavailable stackup is
reported from the attempt rather than being preemptively rejected. Domain
classification does not gate the request: mains, high power, DDR, PCIe, SerDes,
RF, medical, aviation, safety-critical, and unfamiliar domains use the same
plan and generation path. Diagnostics state which domain-specific electrical,
regulatory, RF, thermal, or safety checks were not performed. Generation,
routing, ERC/DRC, and export failures are reported from the actual attempted
operation.

## Existing deterministic fixtures

The repository still contains a deterministic requirements compiler, blocks, and
sample designs. They are useful regression fixtures for IR, KiCad generation,
validation, and the error-injection corpus. They are not the conversational
product routing and must not be presented as a list of boards that the AI can
only generate. New product features extend the generic plan and high-level
runtime above.
