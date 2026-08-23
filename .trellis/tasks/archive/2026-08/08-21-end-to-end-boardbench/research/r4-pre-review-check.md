# BoardBench review v3 + private R4 pre-review quality check

Date: 2026-08-22 (Asia/Shanghai)

## Outcome

**PASS for dispatching two fresh, non-author Codex reviewers.** No deterministic
production-review-v3 or private-R4 protocol bypass was found in the requested
scope. This is only a pre-review quality gate: it is not either technical
review, an approval, a frozen corpus, a campaign, a human engineering review,
or evidence from a model-under-test run.

The private R4 corpus raw SHA-256 remained exactly:

`12704ad321c4f375a09a0ec5acaac43327ba2e341252668cfb5c545633ee0fbb`

## Findings (fixed)

No production source defect was found, so no production file was changed.

The Python protocol probes created interpreter cache files under R4. Those
reviewer-created `__pycache__` files were removed immediately, and the exact
authored R4 file set and manifest hashes were rechecked afterward. No authored
R4 byte was changed.

## Findings (not fixed)

No blocking issue was found.

Known scope boundaries remain deliberate and are not treated as defects:

- R4 reviewer identity and independence are structured reviewer attestations,
  not cryptographic proof of a person or process. The protocol rejects the
  declared R4 author identities and rejects use of the same declared identity
  for both roles.
- Orderability evidence is dated, source-attributed reviewer evidence. It does
  not query or guarantee live inventory, price, lead time, lifecycle, or future
  availability. `docs/BOARDBENCH.md` states this boundary explicitly.
- Passing this check does not make the AI-reviewed pilot satisfy the independent
  human-engineering gate. Production seal/publication continues to reject the
  `ai_reviewed_pilot` cohort.

## Production review v3 checks

- Review schema versions 1 and 2 fail closed; the writer/reader use version 3.
- A review binds the canonical campaign, full corpus, complete hidden case,
  terminal run, and automatic score hashes. Template creation, import, report
  loading, selection import, seal, and publication re-enter the same centralized
  source validator instead of trusting a stored outcome.
- Case checklist IDs are case-, kind-, index-, and content-bound. Import/report/
  seal require the exact source item set, kind, and text; missing, foreign,
  duplicate, or rewritten obligations fail closed.
- Applicable reviews cannot mark individual rubric or assembly obligations
  `not_applicable`. Whole-review `not_applicable` is accepted only when the
  terminal inventory has no nonempty `.kicad_sch`; the inverse rule is enforced.
- Every applicable review must cover every source component slot exactly once
  with nonempty MPN, typed status, strict UTC observation time, typed source,
  nonempty source name/note, and credential-free HTTPS URL. The aggregate state
  is derived from the item statuses. A passing review requires derived
  orderability pass, functional pass, and every checklist item pass.
- A non-passing automatic score requires the reviewer's `final_failure` before a
  completed review can enter reporting. The evaluator's `failure_suggestion` is
  retained as raw evidence and is not substituted into final classifications.
  The prior contradiction guard also remains: automatic pass plus no-change
  engineering pass cannot carry a final failure classification.

## Private R4 checks

- Corpus shape: 20 cases, exactly four in each of the five categories.
- Prompt fairness: all 107 component slots disclose the sole allowed bundled
  part ID, manufacturer MPN, nominal description/value, package, and KiCad
  footprint. All 154 review-rubric items and all 60 assembly items occur in the
  corresponding natural-language prompt.
- Test-point wording is narrow and aligned: populated test-point components are
  excluded by exact-BOM policy; ordinary routing vias remain allowed; only
  explicitly unused ATtiny/TMP102 pins prohibit copper/via/test-point attachment.
- R3 to R4 comparison found zero per-case non-prompt contract changes.
- The bundled `ATtiny402-SSN` fact used by R4 is `-40..+105 °C`.
- Both pending review templates exactly match the generator, bind the same R4
  raw corpus hash, contain all 20 cases and every source rubric/assembly item,
  and use distinct electrical/evaluator role and review IDs.
- Completed-review validation rejects missing/foreign/duplicate cases or items,
  case/text/text-hash/source-corpus drift, `N/A`, empty evidence, declared author
  self-review, reversed/equal times, report name/location/hash drift, same
  reviewer identity, disagreements, and any non-all-PASS approval attempt.
- The successful finalize path was exercised only with synthetic all-PASS input
  inside `TemporaryDirectory`; it reproduced the exact corpus bytes, wrote
  owner-only output, and disappeared when the probe exited. No real R4 review,
  report, approval receipt, or frozen corpus was created.
- R4 regenerated deterministically in a private temporary copy; every declared
  generated-artifact hash and size matched the existing manifest.
- Historical corpus bytes remained at their recorded identities:
  - R1: `b6eb79f3e3cab5d9f935531004854369f6cbc278e09f0749b6d62a12706c8067`
  - R2: `4f774ea99a18755c65601f61dd270d9a1a8833b52d801d68ca8013aef149c1b6`
  - R3: `1bffb09b6cbb24f40526a1ed919ab1f401bdb76320ec1a323bbe505cb91bfe3d`
  R2/R3 generated files also match their own saved manifests, and the two R3
  audit reports match the hashes bound into the R4 manifest.
- R4 directory mode is `0700`; authored data/docs are `0600`; executable author/
  protocol/self-test files are `0700`. The directory contains no completed
  review, approval, frozen corpus, campaign, run, or model-output evidence.

## Verification

- Tests: **pass** — `uv run python -m unittest -v
  tests.verification.test_boardbench
  tests.verification.test_boardbench_evidence
  tests.verification.test_boardbench_report` ran 74 tests in 13.976 seconds.
- Lint: **pass** — `uv run ruff check` on the three production modules and their
  three focused test modules.
- Format: **pass** — `uv run ruff format --check` on the same six files.
- TypeCheck: **pass** — `uv run mypy
  src/pcbdraft/verification/boardbench.py
  src/pcbdraft/verification/boardbench_evidence.py
  src/pcbdraft/verification/boardbench_report.py` reported no issues.
- R4 templates/protocol: **pass** — both `corpus_review.py validate-template`
  commands succeeded; `review_protocol_selftest.py` passed 26 positive and
  adversarial validations without persistent output.
- R4 independent probe: **pass** — 20 cases, 107 slots, 154 rubric items, 60
  assembly items, zero R3 non-prompt drift, two exact templates, temporary-only
  positive finalize, and deterministic regeneration.
- JSON/hash/evidence scan: **pass** — all R4 JSON parsed strictly enough for the
  production loader/protocol, every manifest entry matched byte size and SHA-256,
  and no prohibited evidence file was present.
- Repository hygiene: **pass** — `git diff --check`; `uv lock --check` resolved
  133 packages with no lock drift.
- Full suite/release gate: **not run**, per the repository's focused ~90-second
  iteration policy.

## Dispatch recommendation

Dispatch two fresh Codex reviewers with no R4 authoring participation, one for
electrical/source correctness and one for evaluator/prompt validity. Bind both
assignments explicitly to the R4 raw hash above. Do not freeze or start a model
campaign unless both completed reviews independently give all 20 cases and all
214 source items PASS, the protocol reports zero disagreement, and finalize is
separately authorized.
