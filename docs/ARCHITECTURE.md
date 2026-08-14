# CopperWright architecture

CopperWright is an agent-safe runtime around KiCad, not a replacement EDA GUI.
It exists to turn a user-approved, semantic circuit plan into reviewable KiCad
artifacts while retaining enough evidence to explain either success or failure.
The product is model-agnostic: a model can interpret requirements and propose
topology, but it never owns raw KiCad text, geometry, filesystem writes, command
execution, validation outcomes, or release identity.

## Product path

    compact terminal agent / terminal chat / local browser / JSON-RPC
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
             - components, symbols, pin endpoints, nets, notes
             - no coordinates, KiCad syntax, code, commands, or routing
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

The application service is the only business write authority. The compact
terminal agent, terminal chat, and browser render the same persisted project,
conversation, attempt, event, and decision records. The compact agent derives a
local project name from the first normal-language request, while slash commands
handle project selection and explicit engineering actions. A restart marks an
incomplete job interrupted rather than replaying its side effects.

## Generic request and plan

<code>AgentDesignRequest</code> is a durable statement of the user's request:
board envelope, scope, named parts, functions, power assumptions, and source
context. It does not encode a board profile.

<code>CircuitPlan</code> is a compact plan proposed by a configured planner. It
contains only:

- component identity, KiCad symbol, optional footprint, role, and exact user name;
- nets whose endpoints use real component IDs and symbol pin numbers;
- assumptions, summary, and review notes.

The runtime looks up actual local symbol candidates before planning. It rejects
unknown symbols, invalid pins, duplicate pin-to-net assignments, raw geometry,
and a plan that drops an explicitly requested part. This is not a guarantee that
the plan is electrically correct; it is a controlled boundary between model text
and engineering data.

After compile, the runtime produces deterministic <code>plan_review</code>
evidence from the selected local symbols and topology. It flags missing
power-input coverage, undeclared rail sources, and applicable I2C pull-up or
decoupling evidence. These are not a fixed part template or a general-part
denylist: a finding explains what an attempted project still needs, while the
user may choose to preserve and generate that attempt.

## Stock KiCad component resolution

<code>PartGraph</code> owns canonical component records. For the generic path,
the resolver extracts a record from the locally installed KiCad symbol and
footprint libraries. Such records have:

- trust state <code>extracted</code>;
- lifecycle state <code>unknown</code>;
- a local-library provenance record; and
- no claimed manufacturer verification, sourcing status, rating, simulation, or
  layout qualification.

These internal records let the existing KiCad adapters work without requiring a
manufacturer, MPN, datasheet, supplier, import, or non-stock library from the
user. They do not turn a library symbol into an authoritative electrical model.

## KiCad generation and retention

The semantic IR is compiled through the existing KiCad adapters:

- <code>kicad_schematic.py</code> emits a native schematic through
  <code>kicad-sch-api</code>;
- <code>kicad_pcb.py</code> uses deterministic placement seeds plus the bounded
  placement/routing backend and KiCad's PCB API;
- <code>managed.py</code> stages all files in a sibling directory and publishes
  only on success.

For a generic success, the exact reviewed <code>circuit-plan.json</code> is a
tracked managed member alongside the request, IR, and project-local part graph.
Its hash must match the IR provenance before publication; the parsed plan is
therefore available to later review without trusting arbitrary adjacent JSON.

For an application generation failure, the attempt directory retains the approved
request, circuit plan, IR, project-local part graph, error, and every native
artifact that had already been produced. A router failure is therefore a useful,
inspectable result, not a hidden substitution or a false success.

## Validation and release

L0–L7 reports distinguish completed, unavailable, heuristic, human-required, and
not-applicable evidence. In particular:

- L0 checks project/file/IR coherence;
- L1 checks symbols, pins, footprint mapping, and trust state;
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

The current PCB backend implements 2- and 4-layer boards. Domain classification
does not gate the request: mains, high power, DDR, PCIe, SerDes, RF, medical,
aviation, safety-critical, and unfamiliar domains use the same plan and
generation path. Diagnostics state which domain-specific electrical, regulatory,
RF, thermal, or safety checks were not performed. Generation, routing, ERC/DRC,
and export failures are reported from the actual attempted operation.

## Existing deterministic fixtures

The repository still contains a deterministic requirements compiler, blocks, and
sample designs. They are useful regression fixtures for IR, KiCad generation,
validation, and the error-injection corpus. They are not the conversational
product routing and must not be presented as a list of boards that the AI can
only generate. New product features extend the generic plan and high-level
runtime above.

## Upstream design influence

The architecture takes compatible ideas, not copied implementation, from the
declarative/component patterns of atopile, the circuit representation and
separation of concerns in tscircuit, the Python-to-KiCad composition direction of
circuit-synth, and the small interaction-shell shape of Pi/π. The precise
license/source record is in [OPEN_SOURCE_REUSE.md](OPEN_SOURCE_REUSE.md).
