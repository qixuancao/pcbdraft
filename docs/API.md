# CopperWright application and agent API reference

## Conversational application

<code>copperwright agent</code>, <code>copperwright chat</code>, and
<code>copperwright app</code> all use <code>ApplicationService</code>. The compact
<code>agent</code> terminal starts with a direct board question and creates a local
project from its first plain-language message; <code>chat</code> retains its
noninteractive argument surface. The service owns project state, confirmation,
events, locks, recovery, generation attempts, and validation; clients do not
reimplement engineering logic.

The normal flow is:

    user request
      → requirement interpretation
      → clarification only for material facts
      → reviewed generic circuit plan
      → explicit confirmation
      → native KiCad generation attempt
      → optional KiCad/CopperWright checks

The browser listens only on loopback by design. Its HTTP/SSE routes are an
internal UI transport, not a public remote API.

The builtin provider can collect requirements without a network credential, but
it does not invent an electrical topology. A Codex or OpenAI-compatible provider
is needed to produce a generic circuit plan.

## JSON-RPC

<code>copperwright api</code> runs newline-delimited JSON-RPC 2.0 over
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
| <code>symbols.find</code> | <code>query</code>; optional <code>limit</code> | searches the installed stock KiCad symbols and returns actual symbols, default footprints, descriptions, and pins |
| <code>agent.plan.compile</code> | <code>request</code>, <code>plan</code> | validates the generic request/plan, resolves local symbols, preserves named parts, and returns semantic IR, a project-local part graph, and deterministic <code>plan_review</code> evidence |
| <code>agent.project.generate</code> | <code>request</code>, <code>plan</code>, <code>output</code>; optional <code>retain_failed_attempt</code> | compiles and creates a new native KiCad managed-project attempt; successful projects retain the reviewed <code>circuit-plan.json</code> in their manifest, and the optional directory receives available staging on failure |

The request document has schema
<code>copperwright-agent-design-request</code>. The plan document has schema
<code>copperwright-circuit-plan</code>. The planner may declare semantic
components/pins/nets only; raw KiCad, geometry, routing, commands, and executable
text are rejected by the schema and compiler.

<code>plan_review</code> checks only facts that can be determined from the
reviewed topology and local symbol metadata: power-input coverage, declared
non-ground rail sources, applicable I2C pull-up/decoupling evidence, and the
board rules. Findings do not block an otherwise valid generation attempt.

Example:

    copperwright api <<'EOF'
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

## Legacy fixture methods

<code>requirements.compile</code> and <code>project.generate</code> are retained
for the old deterministic fixture corpus. They are explicitly compatibility
methods, not the general conversational generation route. Consumers building new
agent tooling should use <code>symbols.find</code>,
<code>agent.plan.compile</code>, and <code>agent.project.generate</code>.

## Stability and output truthfulness

<code>api_version</code> follows major compatibility. Semantic IR, generic
request/plan, manifest, validation, release, and evidence records have their own
schema/version fields. Write targets are create-only except for explicitly
transactional publication and lock-protected evidence indexes.

Domain labels do not trigger API rejection. The current native backend accepts
2- and 4-layer board requests; complex domains may produce diagnostic warnings.
Only tool results that actually ran are reported as validation evidence.
