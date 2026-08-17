# PCBDraft application and agent API reference

## Conversational application

<code>pcbdraft</code> with no subcommand, <code>pcbdraft chat</code>, and
<code>pcbdraft app</code> all use <code>ApplicationService</code>. The default
full-screen terminal creates a local project from its first plain-language
message and presents slash commands through an in-place palette. <code>chat</code>
retains its noninteractive argument surface and requires an explicit action; it
does not fall back to a line REPL. The service owns project state, confirmation,
events, locks, recovery, generation attempts, and validation; clients do not
reimplement engineering logic. The terminal wraps it in a durable,
non-blocking <code>AgentRuntime</code>; it renders persisted activity events and
can request cooperative cancellation at the next safe tool boundary.
The terminal stores only a mode-0600 pointer to the last local project. It
restores retained history without replaying an interrupted action; retry is an
explicit command. Plan/change review and expanded logs are terminal
presentations over persisted application evidence, not a second source of
project truth. Browser messages and action buttons enter this same Agent job
path. A project response includes a bounded read-only projection of recent
durable turns, tool runs, and any pending approval, so the browser can render
stable <code>pcb_*</code> activity in the conversation instead of reconstructing
a plan/confirm wizard from project status. The current browser server always
uses the <code>workspace</code> permission policy and has no approval mutation
endpoint; a checkpoint created by a review-mode TUI is visible there but must be
resolved in that TUI.

Normal application launches use one persistent PCB project repository. On
first run PCBDraft creates <code>~/PCBDraft</code> and records its location in
<code>~/.config/pcbdraft/repository.json</code>. Every application project is
created below <code>projects/</code> in that repository; the process working
directory is never used as a project destination. Use
<code>pcbdraft repository /path/to/repository</code> to choose a different
location, or <code>pcbdraft repository --json</code> to inspect it. The
explicit <code>--workspace</code> options remain isolated automation/test
overrides and do not change the persisted location.

The default terminal presents one continuous agent turn:

    user request
      → bounded PCB tool calls and inline activity
      → assistant result with inspectable plan, artifacts, and evidence

Internally, each message first creates a versioned durable turn. The background
Job stores its <code>turn_id</code>, not a second copy of the message. Every tool
intent is persisted before execution with
<code>thread_id + turn_id + tool_call_id</code>, canonical argument hash,
baseline revision, source, effect, risk, status, and a bounded result receipt.
An explicit retry reuses that turn and starts at the next unfinished boundary;
it does not call requirement interpretation again after a completed receipt.
“Unfinished” here means a call that was not dispatched. If dispatch was recorded
but no exact result receipt exists, the turn fails closed and retry will not
replay that potentially effective write, regardless of whether the visible
project revision changed.

The current call producer uses a hybrid router rather than delegating the whole
workflow to a model. Only the built-in OpenAI preset with provider ID
<code>openai</code> and exact host <code>api.openai.com</code> may use OpenAI
Responses native function calling. It makes at most one model decision at the
start of each natural-language turn, selecting an eligible initial
<code>pcb_*</code> tool. Slash-command turns and all mandatory continuation after
the first call&mdash;generation, validation, evidence-driven repair, approval, and
revision handling&mdash;use the deterministic local policy. Every other provider,
including a custom OpenAI-compatible endpoint, uses the same local fallback for
intent routing; it can still participate in schema-constrained requirement
interpretation and circuit planning.

The native request is journaled before dispatch under
<code>agent-turns/model-decisions/{turn_id}-router.json</code>. A completed
decision is reused as the exact retained call. A decision left dispatched
without a result, or one recorded as failed, is never automatically sent to the
model again; the runtime falls back to a deterministic local decision. The
journal is an intent-routing receipt, not proof that PCB work succeeded.

Plan, generation, validation, repair, apply, discard, undo, preview, and release
calls all pass through the same <code>PermissionBroker</code>, closed
<code>PCBToolRegistry</code>, and <code>PCBToolExecutor</code>. The registry
exports strict OpenAI Responses function tools and MCP Tool descriptors using
stable <code>pcb_*</code> names. Neither a model call nor an MCP caller receives
private handlers or direct application-service, filesystem, shell, or raw KiCad
authority; strict arguments, allowed project states, permission decisions, and
baseline revisions remain local.

The default TUI and browser <code>workspace</code> policy automatically performs
requested project-local work, matching a coding-agent interaction instead of
exposing a fixed four-step wizard. TUI <code>review</code> mode pauses a high-risk
or autonomous authoritative-write tool. Its durable approval binds one exact
tool call, argument hash, and project revision; <code>/confirm</code> approves that
call once and <code>/discard</code> rejects it. Automatic progression never removes
the persisted plan, candidate validation, or transaction boundary.

Automatic repair is an application-service capability, not raw model authority.
Feedback has the versioned <code>pcbdraft-agent-repair-feedback</code> shape;
the provider returns a full replacement plan through the normal compiler. Failed
native candidates and their validation reports remain under
<code>transactions/</code>. Non-deterministic and human-required findings are
reported to the user but are not fed back as automatic repair triggers.
On an existing generated project, a normal follow-up message uses the same
bounded revision channel to generate and validate an isolated replacement under
<code>transactions/</code>. The authoritative design remains unchanged until the
checked candidate is atomically applied; failed candidates retain their evidence.

The browser listens only on loopback by design. Its HTTP/SSE routes are an
internal UI transport, not a public remote API. Every API, SSE, preview, and
artifact request requires the per-process capability from the launch URL fragment;
writes also require same-origin and CSRF validation.

<code>GET /api/projects/{project_id}</code> adds an <code>agent</code> object to
the normal public project view. Its schema is
<code>pcbdraft-browser-agent-view</code> version 1. It contains the main thread's
20 most recent turns in oldest-first display order, bounded tool-run receipts,
the server permission mode, and at most one enriched pending approval. This is a
read-only presentation contract; it does not expose tool handlers or confer
approval authority.

A configured model API or OpenAI-compatible provider is required to produce a
generic circuit plan. Every provider passes through the same strict intent/plan
validators and repair compiler. The transport records the selected structured-output mode and attempt
count, but never a prompt, credential, or raw provider error body. Transient
retries never change the selected model.

Durable Agent Jobs use schema version 2 and carry an execution-policy snapshot:
the exact permission mode, closed-registry authority fingerprint, and per-turn
tool-call limit. Startup recovery and explicit retry dispatch only when the
current runtime exactly matches that snapshot. Legacy jobs without the binding,
historical direct actions, missing durable turns, and mismatched policies fail
closed into visible terminal audit states; they are never upgraded into runnable
authority during recovery.

Once a tool has crossed its durable dispatch marker, an exception is not treated
as a retryable failure unless an exact local effect receipt reconciles it. An
unreconciled call is retained as an interrupted, non-replayable effect. Likewise,
a failed or denied model-selected direct intent cannot be replaced by a different
state-derived operation on retry; the caller must inspect the project and submit
a new turn.

## MCP stdio

<code>pcbdraft mcp</code> exposes the closed PCB registry to an external MCP host
over stdin/stdout. It is separate from the newline-delimited JSON-RPC interface
documented below and has no network listener.

    pcbdraft mcp --project PROJECT_ID
    pcbdraft mcp --project PROJECT_ID --workspace /absolute/repository

<code>--project</code> is required and must name an existing project. If
<code>--workspace</code> is omitted, the configured PCBDraft repository is used;
an explicit override must be absolute. Each server process binds that single
project before stdout becomes protocol-only. MCP tool schemas therefore contain
neither project IDs nor paths, and each <code>tools/call</code> executes exactly
one requested durable operation rather than auto-running the remaining PCB
workflow.

The default is <code>--approval-mode review</code>. A high-risk or
authoritative-write tool that requires review is not executed: the call returns
an error result with <code>status=approval_required</code>, the durable turn/tool
identity, and the exact revision-bound checkpoint. Resolve that checkpoint in a
review-mode TUI and do not retry the same MCP call. <code>workspace</code> and
<code>read_only</code> are also available; <code>--timeout SEC</code> bounds each
tool call.

If an MCP timeout or request cancellation races with an active durable Job, or a
post-submit Job/Turn read or dispatched tool cannot be reconciled, the
adapter returns <code>status=outcome_unknown</code>,
<code>retry_safe=false</code>, and the retained <code>job_id</code> /
<code>turn_id</code> reconciliation identities. It does not call an in-flight,
non-idempotent PCB effect a normal timeout. The caller must inspect those local
records and must not retry the MCP call while the outcome is unknown.

The current server targets MCP <code>2025-11-25</code> with the official Python
SDK 1.x. The dependency is constrained to <code>mcp&gt;=1.29,&lt;2</code> because the
KiCad dependency chain currently requires an SDK version below 2. The implemented
surface is stdio <code>tools/list</code> and <code>tools/call</code> only; there is
no Streamable HTTP transport, resources/prompts surface, or built-in MCP client.
The calendar year 2026 does not imply a 2026 protocol implementation, and clients
requiring a post-<code>2025-11-25</code> revision or SDK v2 behavior are outside
the current compatibility claim.

## JSON-RPC

<code>pcbdraft api</code> runs newline-delimited JSON-RPC 2.0 over
stdin/stdout. It has no network listener. Start by discovering the actual
runtime capabilities:

    {"jsonrpc":"2.0","id":1,"method":"runtime.capabilities","params":{}}

Every request is one JSON object with <code>jsonrpc</code>, <code>id</code>,
<code>method</code>, and optional <code>params</code>. Inputs are limited to
4 MiB, fields are exact, and domain errors are returned as JSON-RPC errors rather
than tracebacks.

## Generic agent methods

| Method | Required params | What it does |
|---|---|---|
| <code>agent.request.prepare</code> | <code>request_summary</code>, <code>design_name</code>, <code>layers</code>, <code>requested_parts</code>, <code>functions</code> | creates a bounded generic request, resolves relevant installed symbol context, and returns the exact plan schema; conversational clients normally choose layers internally before this call |
| <code>symbols.find</code> | <code>query</code>; optional <code>limit</code> | searches the installed stock KiCad symbols and returns actual symbols, default footprints, descriptions, and pins |
| <code>agent.plan.compile</code> | <code>request</code>, <code>plan</code> | validates the generic request/plan, resolves local symbols, preserves named parts, and returns semantic IR, a project-local part graph, and deterministic <code>plan_review</code> evidence |
| <code>agent.project.generate</code> | <code>request</code>, <code>plan</code>, <code>output</code>; optional <code>retain_failed_attempt</code> | compiles and creates a new native KiCad managed-project attempt; successful projects retain the reviewed <code>circuit-plan.json</code> in their manifest, and the optional directory receives available staging on failure |

The request document has schema
<code>pcbdraft-agent-design-request</code>. The plan document has schema
<code>pcbdraft-circuit-plan</code>. Providers receive the strict version-2 plan
schema and may declare functional blocks, components/pins/nets, power domains,
interfaces, supported semantic constraints, and locally evaluated assertions.
Those constraints include complete connector pinouts, exact net labels, named
placement regions, anchored rectangular board keepouts, and measurable
differential-pair acceptance criteria. Region and anchor names are converted to
coordinates only inside deterministic code. Raw KiCad, coordinates, trace
geometry, commands, and executable text are rejected by the schema and
compiler. Persisted version-1 plans remain accepted for read/compile
compatibility but are not emitted to planners.

<code>plan_review</code> checks only facts that can be determined from the
reviewed topology and local libraries: real symbol-to-footprint pad coverage,
power-input coverage and polarity, plausible non-ground rail sources, output
contention, two-terminal shorts, ground-referenced LED polarity, per-line I2C
pull-ups, applicable decoupling evidence, and board rules. Findings do not block
a generation attempt. Completed deterministic failures do block engineering-
candidate readiness and may enter bounded repair; reference-only datasheets and
human-required evidence do not trigger repair.

Example:

    pcbdraft api <<'EOF'
    {"jsonrpc":"2.0","id":"symbols","method":"symbols.find","params":{"query":"SHT31"}}
    {"jsonrpc":"2.0","id":"caps","method":"runtime.capabilities","params":{}}
    EOF

Creating native files is not an electrical or manufacturing validation claim. A later
<code>review</code> run on an intact generic managed project receives the parsed
request, persisted circuit plan, project-local part records (including their
trust state), a deterministic preflight recomputed from the persisted plan and
current semantic IR, and native KiCad evidence.

## Other methods

| Method | Required params | Effect |
|---|---|---|
| <code>parts.find</code> | none | query curated reusable part records |
| <code>project.inspect</code> | <code>project</code> | report project identity, tracked files, and drift |
| <code>project.validate</code> | <code>project</code>, <code>output</code> | create L0–L7 evidence |
| <code>project.release</code> | <code>project</code>, <code>output</code> | try to build a candidate evidence bundle; gates may block it |
| <code>release.verify</code> | <code>release</code> | verify a previously created bundle |
| <code>sync.preview</code> / <code>sync.apply</code> | <code>project</code> | inspect/import recognized native KiCad edits |
| <code>evidence.record</code> | project and attributed evidence fields | record L4, L6, or L7 external evidence |
| <code>benchmark.run</code> | <code>output</code> | run the independent error-injection corpus |

Validation and release results distinguish `candidate_ready` from
`production_evidence_complete`. The latter means only that all declared external
record slots contain passing, integrity-checked submissions. `production_ready`
and `production_claimed` remain false because the runtime cannot issue or verify a
production attestation.

## Legacy fixture methods

<code>requirements.compile</code> and <code>project.generate</code> are retained
for the old deterministic fixture corpus. They are explicitly compatibility
methods, not the general conversational generation route. Consumers building new
agent tooling should use <code>symbols.find</code>,
<code>agent.request.prepare</code>, <code>agent.plan.compile</code>, and
<code>agent.project.generate</code>.

## Stability and output truthfulness

<code>api_version</code> follows major compatibility. Semantic IR, generic
request/plan, manifest, validation, release, and evidence records have their own
schema/version fields. Write targets are create-only except for explicitly
transactional publication and lock-protected evidence indexes.

Domain labels do not trigger API rejection. Layer count can be omitted and
selected by the agent as an internal design parameter. The API carries every
positive user-specified or agent-selected layer count; installed KiCad
determines actual stackup support during generation and reports unavailable
requests from that attempt. Complex domains may produce diagnostic warnings.
Only tool results that actually ran are reported as validation evidence.
