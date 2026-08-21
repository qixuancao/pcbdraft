# Flat Agent Blocker Remediation Design

## Summary

Keep the flat PCB operating-system model: the model chooses one concrete action,
the runtime executes at most one PCB action, returns its factual result, and the
model then chooses again. Fix the observed LED failure by adding explicit
project-local generic KiCad part registration, isolating each Hermes session to
one user-selected project, and removing hidden identifiers from model payloads.

The MVP produces a normally usable KiCad project from installed library
resources.

## Model-Facing Tool Surface

### Project authority

- Remove `pcb_list_projects` and `pcb_open_project` from the model export. Human
  `/projects`, `/open <id>`, `/new <name>`, `--project`, and repository commands
  remain the trusted project-selection surface.
- Keep `pcb_create_project` only for a session with no bound project. A model
  cannot create a replacement project after one is bound.
- `pcb_inspect_project` and all other project-scoped tools operate only on the
  bound project and do not accept a model-supplied project ID.

This is the concrete implementation of explicit user authorization: the human
command boundary selects or changes a project; model prose cannot grant itself
cross-project authority.

### Canonical and generic part actions

Add three flat tools:

- `pcb_search_parts`: search the bound project's canonical and project-local
  catalog and return stable IDs plus concise identity/symbol/footprint/trust
  facts.
- `pcb_describe_part`: return one complete bounded part contract by stable ID.
- `pcb_register_kicad_part`: atomically register one generic project-local part
  from an installed KiCad symbol, installed footprint, and explicit pin-to-pad
  mappings.

`pcb_register_kicad_part` accepts only fields the model can establish from
installed-library facts:

```text
id, kind, description, symbol, footprint, bom,
pins[] = {number, name, electrical_type, functions, required, footprint_pad}
```

The service constructs the minimum schema-valid project-local record, derives
the compatibility fields required by the existing catalog schema, and retains
the installed-library facts used to validate it. The model supplies only facts
it can establish through the symbol and footprint inspection tools.

Installed `pcb_search_symbols`, `pcb_describe_symbol`,
`pcb_search_footprints`, and `pcb_describe_footprint` become global factual reads
and never read, create, or switch project state.

## Session Project Binding

Replace the single model-writable `_current_project_id` cursor with a small
PCBDraft-owned context store:

```text
trusted human selection -> default project + selection epoch
Hermes handler(session_id) -> project binding captured at that epoch
```

- Tool handlers consume Hermes' supplied `session_id`; model arguments cannot
  spoof it.
- A human `/open`, `/new`, `--project`, or repository switch updates the trusted
  selection and invalidates prior session bindings.
- The next call in that session binds to the trusted selection. If none exists,
  only project creation and global library reads are allowed.
- Model-created projects bind the current session immediately and cannot be
  replaced by a later model call.
- Missing session identity fails closed for model project selection, while
  direct unit/service APIs continue to accept explicit isolated repositories.

The durable project records remain unchanged and separate; the context store is
only an in-process authority cursor.

## One PCB Action Per Model Decision

Use the existing Hermes `tool_execution` middleware from the installed
PCBDraft plugin. Track the first `pcb_*` dispatch for each trusted
`(session_id, turn_id)` pair:

1. The first PCB call invokes `next_call` normally.
2. Any later PCB call emitted in the same model response returns a bounded
   policy result and performs no dispatch or mutation.
3. Hermes appends a result for every provider tool-call ID, preserving provider
   protocol pairing, then asks the model to decide again.
4. The tracker is bounded and cleared as turns/sessions complete.

This avoids modifying vendored Hermes execution code and works for sequential,
parallel, and segmented batches because all real dispatches pass through the
same middleware.

## Atomic Project-Local Part Publication

Part registration uses the same authoritative transaction shape as semantic
writes:

```text
read synchronized baseline
  -> resolve installed symbol and footprint
  -> validate every symbol pin and footprint pad mapping
  -> construct project-local PartRecord
  -> stage catalog + design + complete native materialization
  -> verify manifest/hash synchronization
  -> lock + revision/content/catalog hash comparison
  -> atomic directory swap + state/event/receipt write
  -> rollback design and records on any failure
```

The catalog remains `pcbdraft-part-catalog` v1 because registration adds a valid
existing `PartRecord`; no new persisted record shape is needed. The transaction
receipt identifies `register_kicad_part`, before/after catalog hashes, design
hashes, revisions, and local-library evidence.

Duplicate IDs fail unless the complete canonical record is byte-identical, in
which case the operation may return an explicit no-op. An ID collision with
different facts never silently overwrites a part already used by components.

## Provenance and Legacy Power-Domain Fixes

- Remove model-authored provenance arrays from ordinary flat add schemas. The
  service supplies empty IR provenance or derives a concrete operation source;
  existing durable provenance records remain readable.
- For legacy circuit-plan power-domain binding, locate the source net by
  `(component, pin)` rather than the complete endpoint including descriptive
  role. Retain role in the plan and report a focused conflict diagnostic where
  relevant. Do not weaken exact endpoint semantics for connect/disconnect tools.

## Factual Results

- Every project-scoped result includes the bound `project_id`.
- Part search reports total matched/available counts and each part's stable ID.
- Generic registration reports the exact local symbol, footprint, and mapping it
  retained.
- No summary may infer that a catalog is empty from a failed lookup.
- Errors from another project cannot appear unless a trusted human switch made
  that project current.

## Compatibility and Rollback

- Existing project directories, part catalogs, IR v1/v2 files, receipts, and
  legacy planning failures remain readable.
- Removed model project-list/open exports remain available as human CLI commands;
  historical tool calls stay audit-only and fail closed on replay.
- A failed catalog/native/state/event publication restores the old design
  directory and records byte-for-byte.

## Validation Strategy

Focused tests cover registry/fingerprint, session authority, middleware batch
blocking, global library reads, part schemas and local mapping, atomic rollback,
provenance, physical endpoint binding, and factual summaries.

The user explicitly requires a real CLI acceptance beyond the normal fast-test
budget. Reinstall the checkout with UV, run `pcbdraft --workspace <temporary
repository>` in a PTY, seed an old project, request the LED board in a new
project, and retain the trace plus synchronized KiCad/ERC/DRC receipts as the
acceptance evidence.
