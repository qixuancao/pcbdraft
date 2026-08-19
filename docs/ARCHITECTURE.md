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
       Hermes Agent
       /      |      \
   inspect  design  modify
       \      |      /
      PCB Tool Registry (macros + domain routers)
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
       Hermes Agent
            |
            v
    continue / done / blocked

- **Hermes** owns reasoning, conversation, and autonomous tool selection.
  After every tool result the model may freely choose the next tool; there is
  no mandatory plan/generate/validate/repair/release sequence.
- **GoalManager** (vendored Hermes `/goal`) owns standing-goal continuation
  and the done/continue/wait judgment. Goal state stays minimal: goal,
  status, turns_used, max_turns.
- **AgentOrchestrator / JobRunner** own durable dispatch, persistence,
  recovery, and budgets for the legacy TUI job path. They do not decide PCB
  engineering next steps.
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
      verification/  evidence, validation, review, benchmark, and release gates
      interfaces/    the ``pcbdraft`` CLI and the interactive Hermes terminal

The dependency direction starts with `core` and `domain`. KiCad and model
adapters implement external boundaries. Services orchestrate those capabilities,
verification evaluates their persisted results, and interfaces translate user
input without becoming a second business-logic layer. See
[`PROJECT_STRUCTURE.md`](PROJECT_STRUCTURE.md) for placement rules and the
compatibility policy for historical module paths.

## Tool layers

The agent sees two layers, both exported from one canonical catalog:

- **High-level macros** (compatibility tools / shortcuts for simple projects):
  `pcb_plan_request`, `pcb_generate_candidate`, `pcb_validate`,
  `pcb_repair_candidate`, `pcb_apply_candidate`, `pcb_discard_candidate`,
  `pcb_undo_last_change`, `pcb_render_previews`, `pcb_build_release`. Each
  macro completes a bounded chunk of work in one call.
- **Domain routers**: `pcb_project`, `pcb_library`, `pcb_design`,
  `pcb_board`, `pcb_inspect`, `pcb_verify`, `pcb_export`, `pcb_analysis`.
  Each router selects a concrete capability via `operation` + `arguments`
  (for example `pcb_library(operation="search_symbols", ...)`). Every router
  supports `operation="capabilities"`, which lists what is actually supported
  — including honest `supported: false` entries with reasons for declared but
  unimplemented capabilities (fine-grained graph mutation, per-footprint board
  operations, standalone exports, and all analysis engines).

The canonical capability catalog lives in
`pcbdraft/agent/capability_registry.py`. Hermes tool schemas, MCP tool
descriptors, and future native model tools all derive from the same
registry, so no transport maintains a second hand-written tool list.

Tool results report facts only: what ran, whether it succeeded, what changed,
current state, findings, limitations, and evidence references. Results never
contain prescriptive `next_step` or `required_workflow_stage` fields; project
status (`draft`, `generated`, `validation_failed`, `validated`, ...) is an
engineering fact, not a router for the next tool call.

## Product path

          interactive terminal (default, Hermes)
                        |
                        v
                 Hermes agent loop
                        |
              +--- durable JobRunner (legacy) ---+
              |                                  |
              v                                  v
                  AgentOrchestrator <--> AgentTurnStore
                  - durable Thread + Turn + ToolRun + Approval identities
                  - persist intent before side effects
                  - resume the same checkpoint after failure or restart
                                       |
                                       v
                  Tool intent source
                  - default Hermes agent: model re-selects the next tool
                    after every result (no fixed sequence)
                  - legacy mode: one initial model selection, then the
                    deterministic producer for explicit shortcut turns
                                       |
                                       v
                  PermissionBroker: allow / ask / deny
                  - exact call + argument hash + baseline revision binding
                                       |
                                       v
                  PCBToolRegistry / PCBToolExecutor / capability registry
                  - closed semantic tool and capability catalog
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

The application service is the only business write authority. `AgentRuntime`
and `JobRunner` turn its synchronous, transactional operations into durable
background turns and UI-neutral activity events; the interactive terminal
owns rendering and input only. Before any side effect, `AgentOrchestrator` persists a
versioned `TurnRecord` and `ToolRunRecord` under `agent-turns/`. The compound
`(thread_id, turn_id, tool_call_id)` identity owns progress, result, and approval
state. A waiting approval additionally binds the canonical argument hash and
observed engineering revision, so UI status alone can never authorize a write.

The semantic design graph is a working engineering representation. The agent
may inspect it (`pcb_design` inspect operations), extend it, and revise it
over time; the architecture no longer assumes a complete JSON plan must exist
before other work can begin. Today semantic changes flow through the
`pcb_plan_request` / `pcb_repair_candidate` macros; fine-grained graph
mutation capabilities (add/remove/update component, connect/disconnect,
semantic patch) are declared extension points that honestly report
`supported: false`.

### Legacy deterministic producer

The historical next-tool producer is retained as an explicit legacy mode. At
the start of a natural-language turn it permitted at most one native OpenAI
Responses function-tool decision, and only when the selected provider used the
built-in OpenAI preset, provider ID `openai`, and the exact `api.openai.com`
hostname; every later operation in that turn used the deterministic producer.
This hybrid routing is no longer the controller of the default Hermes agent —
the model now re-selects tools after every result — but it still drives the
durable TUI job path and explicit shortcut turns. All other providers,
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

The interactive terminal (a bare `pcbdraft` launch) runs the vendored Hermes
`prompt_toolkit` runtime as the only product frontend. Its PCBDraft slash
commands (`/new`, `/projects`, `/project`, `/open`, plus the PCB workflow
commands) translate terminal input into `ApplicationService` calls over the
configured project repository. The legacy durable JobRunner path still submits
explicit actions through Agent jobs over the same orchestrator and project
store, but it is no longer a product frontend: this durable job path is the
legacy compatibility mode — its producer follows the historical deterministic
continuation, while the default Hermes agent re-selects every tool
autonomously. Its default `workspace` permission policy continues requested
project-local work without artificial pauses; a `review` policy can retain an
exact call checkpoint and ask once immediately before a high-risk or
authoritative-write tool.
Follow-up messages on a generated project are compiled into an isolated replacement,
validated under `transactions/`, and only then atomically applied by policy.
The terminal retains bounded tool history across recent turns; collapsed mode
shows concise call activity, while expanded logs include effect, risk, argument
hash, baseline/result revisions, bounded arguments, and the local result receipt.
A restart marks an
incomplete job and active tool interrupted rather than replaying its side
effects. `/retry` creates another Job attempt over the same `turn_id`; completed
tool receipts are reused. A call that was durably marked as dispatched but lacks
an exact matching result receipt is ambiguous even when the project revision did
not advance; it is failed closed and is never dispatched again. The user must
inspect the retained project and submit a new turn.

## Goal Mode

The default agent loop is a simple Ralph-style goal loop built on the vendored
Hermes `GoalManager` (no PCBDraft-specific task system is added):

1. the user's PCB request becomes a standing goal (`/goal <objective>`);
2. the Hermes agent runs one normal turn with the full tool surface;
3. after the turn, a judge decides `done`, `continue`, or `wait`;
4. on `continue`, a plain continuation message is appended to the same session
   — it restates the goal and asks the agent to inspect current project state
   and take the next concrete engineering action it judges most useful; it
   never names plan/generate/validate/repair/release as required stages;
5. a new user message can pause, change, or replace the goal;
6. generic turn/tool budgets pause the loop honestly instead of pretending
   completion.

Goal state stays minimal — goal, status, turns_used, max_turns (plus the
optional verification contract the vendored manager already supports). There is
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
default Hermes agent decides itself how many repair cycles are useful within the
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
