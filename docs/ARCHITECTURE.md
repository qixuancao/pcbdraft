# CopperWright architecture

CopperWright is a compiler/runtime around KiCad, not a replacement EDA GUI.
The semantic design is authoritative; native KiCad files are deterministic build
products with a deliberately narrow, fail-closed import path.

## Data flow

```text
strict requirements
        |
        v
scope policy -> verified block registry -> trusted part graph
        |                  |                       |
        +------------------+-----------------------+
                           v
                    semantic PCB IR
                           |
             +-------------+-------------+
             |                           |
             v                           v
    placement optimizer          semantic transactions
             |                           |
             v                           v
       bounded router              semantic diff
             |
             v
  native KiCad schematic/PCB/project
             |
             +--------> recognized pose import -> regenerated project
             |
             v
 L0-L7 validation -> candidate release -> offline verification
```

LLMs may interpret requirements or provide a heuristic review, but they do not
emit route geometry, approve evidence gates, or directly mutate managed KiCad
files. Deterministic code owns schemas, component identity, topology, geometry,
rule execution, file publication, and release identity.

## Semantic authority

`pcb_agent.ir` defines immutable records for:

- requirements and acceptance statements;
- provenance records with source, method, date, and confidence;
- scope and risk boundaries;
- functional blocks and typed component instances;
- power domains, typed interfaces, nets, and endpoint roles;
- electrical, placement, routing, manufacturing, and verification constraints;
- board rules and declared analysis results.

Parsing is strict: unknown fields, missing references, duplicate identities,
multiple-net pins, non-finite numbers, excessive nesting, and unsupported schema
versions fail. Collections with semantic identity are canonicalized by ID; the
serialized representation is compact UTF-8 JSON with stable key order and a
trailing newline. SHA-256 of those bytes is the design content identity.

## Trust model for parts and blocks

`PartGraph` binds a canonical part ID to the manufacturer/MPN variant, symbol,
footprint, exact pin-to-pad map, ratings, lifecycle/source evidence, sourcing
state, assembly constraints, and available SPICE/IBIS/3D models. Trust states are
separate: `unverified`, `extracted`, `rule_validated`, `human_verified`, and
`production_verified`.

The bundled records are only `rule_validated`. This means their schemas, local
KiCad library resolution, pin maps, electrical contracts, and runtime rules are
tested. It does not mean a human has signed them off, stock was checked live, or a
board using them was produced.

`BlockRegistry` accepts only blocks at a validated trust state and requires source
evidence and named tests. Instantiation checks exact equality between declared and
implemented part sets and ports, plus component ownership/identity.

## Deterministic physical compilation

`placement.py` performs bounded deterministic search over component rectangles,
fixed items, near constraints, functional groups, board edges, and net-length
proxies. Failure to satisfy hard constraints is an error.

`routing.py` implements bounded multilayer grid A* with deterministic ordering,
width/clearance/via constraints, keepouts, seeded fine-pitch escapes, through vias,
and explicit expansion/cell limits. The PCB backend adds a filled GND reference
plane and deterministic, clearance-checked stitching vias required by the routing
contract. An unrouted result is never renamed complete.

`kicad_schematic.py` compiles the IR through `kicad-sch-api`. `kicad_pcb.py`
inspects installed footprints, prepares a trusted job, and invokes
`pcbnew_worker.py` through system Python with `-I`. The worker imports no project
code, writes through KiCad's board API, assigns stable UUIDs, and returns a bounded
hash receipt. The parent reloads and semantically snapshots the result.
Release-blocking decoupling metrics use exact installed-footprint pad rectangles;
the metric name, geometry source, measured rail gaps, and limits are retained in
the managed generation receipt.

## Managed project contract

A managed project contains:

```text
requirements.pcbreq.json
design.pcbir.json
<design>.kicad_sch
<design>.kicad_pcb
<design>.kicad_pro
<design>.worker-result.json
project.pcb-agent.json
```

The manifest records hashes, generator results, semantic native snapshots, design
identity, and KiCad compatibility. Opening a project validates the tree, manifest,
paths, schemas, and KiCad major before exposing it. `drift()` names each tracked
hash mismatch.

## Transactions and synchronization

Semantic change sets are applied to immutable IR in memory. A transaction moves
through `preparing -> ready -> applying -> applied`; rejected and interrupted
states retain receipts and known recovery material. Base/source/staged hashes and
field preconditions prevent lost updates. Files are written atomically and whole
managed-project publication uses a same-filesystem directory rename under a
resource lock. Backup directories support undo and crash recovery.

Native KiCad import computes semantic snapshots. Only footprint x/y/rotation/side
changes are representable today. A pose edit becomes a normal typed change set,
is previewed, compiled from IR again, fully validated, and then published. Any
other native change is rejected to avoid silently discarding user work.

The legacy unmanaged `patch/apply` workflow is retained for compatibility. It
limits model output to unique bounded text replacements in private staging, runs
gates before and after, backs up source files, and rolls back regressions. It is
not used by managed generation.

The review workflow detects an intact managed manifest and strictly reparses its
requirements, IR, part and block contracts, synchronization hashes, and generation
receipts into a bounded context. Adjacent arbitrary JSON never enables that path.
Drift is exposed explicitly and disables native/intent authority. Codex receives
data only through stdin under a read-only, no-network policy; its conclusions stay
heuristic and never satisfy a validation gate.

## Validation semantics

Each check has both a state and an outcome. State answers how the evidence was
obtained; outcome is `pass`, `fail`, or `unknown`. Missing tools and external
evidence produce `unavailable`/`human_required`, never a synthetic pass.

- Candidate readiness is blocked by failed or unknown locally required checks.
- Production readiness also requires valid, attributed external L4 sourcing and
  selected-fabricator capability, L6 review, and L7 physical evidence.
- `production_claimed` remains false in runtime-generated reports and releases.
- Imported external evidence is hashed and attributed but explicitly not
  independently verified by the runtime.

L5 uses deterministic calculations for supported DC/current/power contracts.
There is no installed SPICE, field, thermal, or EMI solver in the acceptance
environment, so those adapter results are unavailable or not applicable rather
than inferred from ERC/DRC.

## Reproducible release split

Audit receipts contain wall-clock times, durations, raw KiCad reports, command
records, and pre-normalization hashes. The content manifest/archive exclude those
volatile records. Creation-time fields in KiCad manufacturing exports are
normalized to a documented epoch, with both original and normalized hashes kept
in the audit receipt. For identical managed input and tool version, the manifest
and ZIP are byte-identical.

`release-verify` treats the directory and ZIP as untrusted: it verifies bounded
member counts/sizes, safe relative paths, single-link regular files, exact
inventory, hashes, ZIP timestamps, encryption absence, expansion limits, and
receipt linkage.

## Extension points

New generation profiles should add, in order:

1. CC0 canonical part records with source evidence and local-library tests.
2. Block metadata plus deterministic builders and verification tests.
3. A strict requirements contract and explicit scope declaration.
4. Semantic rules for interface/function/manufacturing intent.
5. Real 2/4-layer KiCad ERC/DRC and release fixtures.
6. Independent fault and clean-control benchmark cases.

Do not broaden `runtime.capabilities` until that complete chain passes.
