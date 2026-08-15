# PCBDraft architecture

PCBDraft is an agent-safe runtime around KiCad, not a replacement EDA GUI.
It exists to turn a schema-constrained semantic circuit plan into reviewable KiCad
artifacts while retaining enough evidence to explain either success or failure.
The product is model-agnostic: a model can interpret requirements and propose
topology, but it never owns raw KiCad text, geometry, filesystem writes, command
execution, validation outcomes, or release identity.

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
      interfaces/    CLI, JSON-RPC, chat, browser, and Textual TUI

The dependency direction starts with `core` and `domain`. KiCad and model
adapters implement external boundaries. Services orchestrate those capabilities,
verification evaluates their persisted results, and interfaces translate user
input without becoming a second business-logic layer. See
[`PROJECT_STRUCTURE.md`](PROJECT_STRUCTURE.md) for placement rules and the
compatibility policy for historical module paths.

## Product path

    full-screen terminal / parameterized chat / local browser / JSON-RPC
                       |
                       v
             AgentRuntime / durable JobRunner / project event stream
             (the terminal uses this asynchronous path)
                       |
                       v
             ApplicationService: projects, events, locks,
             confirmation, recovery, retained attempts
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
                 bounded semantic repair (max 2)
                 - sanitized generation evidence
                 - completed deterministic L1-L3 failures only
                 - replacement plan through the same compiler
                 - staged validation before atomic apply

The application service is the only business write authority. `AgentRuntime`
and `JobRunner` turn its synchronous, transactional operations into durable
background turns and UI-neutral activity events; the Textual client owns
rendering and input only. The default full-screen terminal and browser render the same
persisted project, conversation, attempt, event, and decision records. The
terminal derives a local project name from the first normal-language request,
while its slash palette handles project selection and explicit engineering
actions. It automatically advances a completed pending plan across the existing
confirmation transaction boundary. Manual clients can retain the staged plan
and confirm it explicitly. The parameterized <code>chat</code> command is for
automation and does not start a line-oriented session. A restart marks an
incomplete job interrupted rather than replaying its side effects.

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

The autonomous terminal may make at most two repair attempts. A repair provider
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
