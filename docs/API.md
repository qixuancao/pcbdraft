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
project truth.

Normal application launches use one persistent PCB project repository. On
first run PCBDraft creates <code>~/PCBDraft</code> and records its location in
<code>~/.config/pcbdraft/repository.json</code>. Every application project is
created below <code>projects/</code> in that repository; the process working
directory is never used as a project destination. Use
<code>pcbdraft repository /path/to/repository</code> to choose a different
location, or <code>pcbdraft repository --json</code> to inspect it. The
explicit <code>--workspace</code> options remain isolated automation/test
overrides and do not change the persisted location.

The default terminal flow is:

    user request
      → requirement interpretation
      → clarification only for material facts
      → reviewed generic circuit plan
      → automatic advance across the transactional generation boundary
      → native KiCad generation attempt
      → previews and KiCad/PCBDraft checks
      → at most two schema-constrained plan repairs for generation or completed
        deterministic L1-L3 failures, with staged validation and atomic application

The service and parameterized clients still expose explicit confirmation for
manually staged and recovered projects. Automatic terminal progression does not
remove the persisted plan or the transaction boundary.

Automatic repair is an application-service capability, not raw model authority.
Feedback has the versioned <code>pcbdraft-agent-repair-feedback</code> shape;
the provider returns a full replacement plan through the normal compiler. Failed
native candidates and their validation reports remain under
<code>transactions/</code>. Non-deterministic and human-required findings are
reported to the user but are not fed back as automatic repair triggers.

The browser listens only on loopback by design. Its HTTP/SSE routes are an
internal UI transport, not a public remote API. Every API, SSE, preview, and
artifact request requires the per-process capability from the launch URL fragment;
writes also require same-origin and CSRF validation.

The builtin provider can collect requirements without a network credential, but
it does not invent an electrical topology. A configured model API or an
OpenAI-compatible provider is needed to produce a generic circuit plan. Every provider passes through the
same strict intent/plan validators and repair compiler. The transport records the
selected structured-output mode and attempt count, but never a prompt, credential,
or raw provider error body. Transient retries never change the selected model.

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
