# BoardBench v1 technical design

## 1. Design objective

BoardBench is an experiment and evidence system around the existing product,
not a second PCB generator. It must send one natural-language board request into
the same default Hermes Agent and flat PCBDraft tool surface a user receives,
then evaluate the retained project outside the Agent with a hidden reference
contract. Human corrections and physical results extend the same immutable run
record until the campaign can be sealed and published.

Fundamental invariants:

1. The tested Agent sees the prompt and normal PCBDraft tool results, never the
   evaluator or a prebuilt `CircuitPlan`.
2. Every planned run remains in the denominator, including model, environment,
   timeout, tool, and validation failures.
3. Raw run evidence is immutable; rescoring, human review, correction, and
   publication are new source-bound artifacts.
4. Automatic evidence, engineering judgment, and physical measurements remain
   separate. None can silently stand in for another.
5. The first campaign uses one frozen default provider/model/configuration.
   Configuration drift creates a new campaign rather than changing an active one.

## 2. System boundary and data flow

```text
sealed corpus (prompt + hidden evaluator)
              |
              v
      campaign coordinator -----> campaign.json (60 planned run ids)
              |
              | prompt only
              v
    isolated worker process
      - fresh repository/project/session
      - existing launch_cli one-shot path with a run-local usage receipt
      - default Hermes Agent + pcbdraft-only tools
      - real KiCad backend
              |
              v
  project tree + trace + stdout/stderr + terminal receipt
              |
       hidden evaluator
       /             \
 existing validation  reference topology/rating rules
       \             /
            score.json
              |
     engineer review + corrected snapshot + structural diff
              |
       one manufactured board per category
              |
       aggregate, seal, sanitize, publish
```

The coordinator and evaluator live under `pcbdraft.verification`. A thin
repository script exposes operator commands. A thin internal worker adapter
may live under `pcbdraft.interfaces` because it creates/binds the trusted
project and calls `interfaces.hermes_cli.launch_cli`; verification code does not
import upward into the interface layer.

No BoardBench operation is exported as a `pcb_*` model tool. No new model
provider, general file tool, shell tool, phase macro, or alternative Agent loop
is introduced.

## 3. Modules and ownership

Proposed cohesive modules (names may be adjusted during implementation without
changing their ownership):

- `verification/boardbench.py`: strict versioned corpus, campaign, run, review,
  correction, hardware, and result dataclasses; bounded loaders; IDs and hashes.
- `verification/boardbench_runner.py`: campaign planning, configuration
  fingerprinting, isolated child execution, terminal receipt publication,
  resumption of only unstarted runs, and trace reduction.
- `verification/boardbench_evaluator.py`: managed-project reopening, independent
  validation, reference matching, required-circuit/rating checks, completion
  claim detection, and automatic failure-stage suggestion.
- `verification/boardbench_diff.py`: normalized generated/corrected snapshots
  and semantic/native structural differences.
- `verification/boardbench_evidence.py`: immutable engineering-review,
  correction-selection, and attributed physical-evidence imports.
- `verification/boardbench_report.py`: completeness gate, aggregation, Markdown
  and JSON reports, sanitized publication bundle, and README inputs.
- `interfaces/boardbench_worker.py`: internal prompt-only worker that selects a
  fresh trusted project and invokes the existing Hermes one-shot path.
- `scripts/boardbench.py`: thin operator CLI; no business rules.

Tests mirror these modules under `tests/verification/` and `tests/interfaces/`.
The public corpus is bundled under `src/pcbdraft/data/boardbench/v1/` only after
the sealed baseline. Before sealing, the same strict loader accepts a private
external corpus path that is not committed or installed.

The existing error-injection `verification/benchmark.py` and its 90-case data
remain unchanged except for any shared, clearly generic artifact helper that is
proven useful to both.

## 4. Versioned artifact contracts

All JSON objects use a closed `schema` and integer `version`, strict
`from_dict(path=...)` / `to_dict()` validation, bounded reads, atomic writes,
`0o600` files, and `0o700` directories.

### 4.1 Corpus

`pcbdraft-boardbench-corpus` contains:

- corpus id/version/license/methodology and an explicit cohort
  (`sealed_holdout_baseline`, `public_corpus_rerun`, or the explicitly
  non-baseline `ai_reviewed_pilot`);
- exactly 20 unique cases and exactly 4 cases in each fixed category;
- per case: id, category, natural-language prompt, applicability, review rubric,
  allowed component-slot alternatives, net equivalence/inequality contracts,
  required support circuits, forbidden conditions, rating bounds, and
  manufacturing/assembly constraints;
- no executable expressions, Python snippets, arbitrary paths, or model prompts
  for a judge.

`ai_reviewed_pilot` is a reversible progress track when no independent human
engineer can review the corpus. It retains the same 20-case/60-run contracts and
automatic evidence, but it never satisfies the sealed-holdout human-review
gate. Its reports remain unsealed and both seal and publication fail closed.
Pilot prompts exposed to the tested model cannot later be relabeled as unseen
baseline prompts.

Component alternatives refer to stable identities and installed KiCad
symbol/footprint facts. The evaluator performs an injective slot match before
checking nets so an allowed equivalent cannot be counted twice. A missing fact
is `unknown`, never an inferred pass.

### 4.2 Campaign manifest

`pcbdraft-boardbench-campaign` is created before the first model call and fixes:

- campaign id, cohort, corpus hash, PCBDraft commit and dirty-state digest;
- provider/model and a redacted configuration fingerprint;
- KiCad/Python/platform identity, tool registry fingerprint, time/tool budgets,
  repetition count, and evaluator version;
- the complete ordered matrix of 60 `(case_id, repetition, run_id)` entries.

Each worker preflight must match the frozen fingerprint. A mismatch terminates
that run as `configuration_drift`; it does not silently update the campaign.

### 4.3 Per-run evidence

Each `runs/<case-id>/<repetition>-<run-id>/` contains:

- prompt-only `request.json` and a `run.json` terminal receipt;
- redacted stdout/stderr and every trace member in sequence order;
- the fresh PCBDraft repository/project, including failed/intermediate artifacts;
- an inventory of relative path, size, and SHA-256 for every retained member;
- `score.json` written by a separate evaluator step.

Run status is one of `planned`, `running`, `completed`, `failed`, `timed_out`,
`interrupted`, or `configuration_drift`. A terminal run is immutable. Retrying
creates a new supplemental run id; the original planned run remains in the
denominator. Campaign resume executes only still-`planned` entries.

### 4.4 Human, correction, and hardware evidence

- `pcbdraft-boardbench-review`: source run hash, reviewer attribution, outcome
  (`pass_without_schematic_change`, `pass_after_changes`, `fail`, or
  `not_applicable`), independent modification decisions, active engineer
  minutes, non-ERC/DRC findings, and model/knowledge/compiler/layout/router
  attribution.
- `pcbdraft-boardbench-correction`: generated snapshot hash, corrected snapshot
  hash, normalized semantic/native diff, manufacturing-candidate hash, and the
  review decisions that motivated each change.
- `pcbdraft-boardbench-hardware`: source correction/release hash, category,
  board revision/serial, fabricator acceptance, solderability, first-power
  short result, measured rails with units/tolerance, firmware download result,
  core-function result, revision count, operator/date, and hashed attachments.

Where a project remains a valid managed project, the structured review/hardware
JSON and attachments are also imported through the existing attributed L6/L7
external-evidence API. The BoardBench artifacts remain the campaign source of
truth; the import is a source-hashed project linkage, not a second mutable copy.

## 5. Runner protocol

1. Validate the private/public corpus and create all 60 planned run ids.
2. Snapshot the default connection and environment without credentials.
3. For each run, create a private directory and write only the prompt-only
   request visible to the child.
4. Start a new child Python process with:
   - a run-local repository configuration path and repository;
   - a run-local debug trace path;
   - the normal PCBDraft credential/config authority left read-only and shared;
   - no evaluator path or reference data in argv, environment, project, or model
     context.
5. The worker creates a blank trusted project, binds it to a fresh session, reads
   the prompt, and calls the existing `launch_cli` one-shot path with
   `--usage-file` pointing inside the run artifacts. The prompt is still the
   only model input; the usage path is output-only evidence and contains no
   evaluator data. The outer coordinator supplies only a wall-clock watchdog;
   it does not replace the Agent's default tool budget.
6. `core.process.run_command` owns process-group timeout/output bounds. On every
   exit it retains child output, trace, repository state, and a sanitized reason.
7. The coordinator inventories and hashes the directory, then runs evaluation
   in its own process with the hidden reference case.

BoardBench v1 runs sequentially. Parallelism is deferred because provider rate
limits, shared KiCad library state, and campaign configuration would add noise
to the first baseline.

An operator may select an explicit bounded set of immutable run ids for a pilot.
The coordinator still creates and returns all 60 receipts, executes only the
selected still-planned entries in frozen order, and leaves unselected receipts
unchanged (newly initialized receipts remain planned). Existing terminal
evidence across the full denominator is verified before new selected work
starts. With no selector, the original run/resume-all behavior is unchanged.

## 6. Automatic evaluation

The evaluator never trusts the Agent's stated result.

1. **Complete project**: locate exactly one project; require matching
   `.kicad_pro`, `.kicad_sch`, `.kicad_pcb`, managed manifest, IR, project-local
   graph, and provenance; reopen it with `open_managed_project()`.
2. **Real parts/footprints**: rerun graph/library and pin-to-pad validation
   against the recorded KiCad environment. Extracted local-library identity is
   distinguished from manufacturer-qualified identity.
3. **Reference topology**: injectively match component slots and evaluate
   same-net, different-net, required endpoint, forbidden endpoint, and allowed
   equivalence contracts against semantic IR.
4. **Independent ERC/DRC**: call `validate_managed_project()` in a new evidence
   directory even if the Agent already ran checks. Record both whether the Agent
   invoked checks and whether independent reruns passed.
5. **Support circuits**: evaluate case-declared decoupling, pull-up, protection,
   power-source/return, reset/boot/debug, and interface requirements using shared
   graph/net helpers plus narrowly scoped BoardBench predicates.
6. **Ratings**: compare case bounds with attributed part facts and circuit
   operating values. Missing or unauthenticated evidence is `unknown`; an
   incompatible known bound is `fail`.
7. **False completion**: conservatively classify the final reply as
   `claims_complete`, `claims_blocked_or_incomplete`, or `ambiguous` using a
   tested Chinese/English phrase set. `false_completion=true` only when an
   explicit completion claim conflicts with a missing/failing mandatory metric;
   ambiguous text remains visible rather than being guessed.
8. **Efficiency**: reduce trace events into model request count, canonical token
   buckets, cost amount/status/source, PCB tool calls by name/status, provider
   retries/errors, tool time, API time, and wall time. Missing trace segments
   make affected values `unknown`.
9. **Failure suggestion**: deterministically suggest the earliest primary stage
   (requirements understanding, component knowledge, circuit design, KiCad
   materialization, layout, routing, or validation) and orthogonal cause/owner.
   Human review confirms or overrides it without deleting the original suggestion.

Every metric is tri-state (`pass`, `fail`, `unknown`) or explicitly
`not_applicable`; unknown is never added to pass. Overall automatic success
requires every case-mandatory metric to pass.

## 7. Human review and correction flow

Every one of the 60 planned runs receives a review record. Runs without an
inspectable schematic use `not_applicable` with a concrete retained cause.

For inspectable runs, the engineer reviews the generated snapshot before any
editing, records functional correctness and findings, starts/stops an active
work timer, and records one modification decision per independent engineering
change. Pure presentation/layout cleanup is distinct from functional change.

The correction importer copies the corrected tree instead of mutating the raw
run. It builds a normalized view from semantic IR when synchronized and from
native schematic/board inspection when manual KiCad edits caused managed drift.
The structural diff covers components, identities, footprints, nets/endpoints,
power/rules, board geometry, placement, routes, and file hashes. Screenshots may
supplement but never replace the machine-readable diff.

Before the baseline is sealed, every failed run has one engineer-confirmed
primary stage and cause/owner; there are no remaining `not_reviewed` or
`unclassified` values.

## 8. Physical selection and evidence

After human correction and manufacturing-candidate validation, a selection
record chooses at least one board from each of the five categories. Selection
uses engineering risk and representativeness within each category, not the
highest automatic score alone.

Ordering, assembly, safe current-limited power-up, firmware preparation, and
functional measurement are explicit human/external steps. Software provides
templates, hashes imports, and validates completeness; it never claims to have
performed them. The BoardBench parent task remains incomplete until five real
hardware records (one per category) and their attachments are present.

## 9. Aggregation, sealing, and publication

The report generator publishes counts and denominators by run, case, category,
and overall. It separately reports:

- complete-project, library, topology, ERC, DRC, support-circuit, rating, and
  false-completion outcomes;
- no-schematic-change engineer approval by run and by case;
- modification count and active minutes to orderability;
- the seven failure stages and orthogonal causes;
- model requests, token/cost status, tool calls, time, and failure reasons;
- physical first-power and core-function outcomes by category.

`seal` fails closed unless the campaign has 60 terminal planned runs, 60 review
records, no unclassified failure, correction data for every modified candidate,
and five physical records covering all categories. It writes a hash manifest;
later rescoring creates a new evaluator result/report version.

Before sealing, only schemas, runner, evaluator, and methodology are public.
After sealing, a sanitized publication step produces:

- the frozen CC0-compatible corpus and evaluator contracts;
- machine-readable summary and all run/review/hardware metadata;
- success and representative failure case material for `docs/boardbench/v1/`;
- a hash-indexed archive for large raw KiCad/trace artifacts;
- README text that labels the first campaign `sealed_holdout_baseline` and
  later runs `public_corpus_rerun`.

Publication scans for credentials and absolute/private paths and never bundles
KiCad library files or unauthorised third-party data. Uploading a release asset
or placing an order is an external action performed only with explicit user
authorization.

## 10. Compatibility, operations, and rollback

- Existing managed projects and the 90-case benchmark need no migration.
- New BoardBench readers accept only known versions. Schema changes bump the
  version; old campaign artifacts remain readable when required for comparison.
- CI uses fake providers and deterministic fixtures to test runner/evaluator
  behavior. It never performs paid model calls or reports fixture output as a
  real BoardBench baseline.
- Mainline release repair replaces obsolete Textual/TUI assumptions with a real
  Hermes/KiCad smoke and removes or restores every stale documented entrypoint.
- BoardBench code and data are additive. Rollback is removal of the new modules
  and scripts; immutable campaign directories remain standalone evidence.
- A model outage, timeout, trace gap, missing KiCad environment, or filesystem
  failure produces an explicit terminal artifact and remains in the denominator.

## 11. Task topology decision

The work remains one parent task with independently verifiable milestone gates
rather than separate Trellis child tasks. The corpus schema, campaign id, raw
run hashes, review records, hardware records, and publication gate form one
strictly sequential evidence chain; splitting completion authority would make it
easy to declare the software child complete while the baseline or hardware
evidence is still absent. Implementation/check agents may own disjoint modules,
but the parent acceptance criteria remain the only completion gate.
