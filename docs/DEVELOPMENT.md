# PCBDraft development guide

## Environment

Install Python 3.11, 3.12, or 3.13, Git, and a stable KiCad in the range
<code>&gt;=10.0.0,&lt;10.1.0</code>, including symbols, footprints, CLI, and bundled
Python bindings. KiCad 10.0.5 is the current exact acceptance baseline;
other stable 10.0 patch releases are compatible but reported as non-baseline.
<code>uv</code> is the recommended environment manager.

    uv sync --frozen --extra dev
    uv run pcbdraft setup
    uv run pcbdraft doctor --json

A configured model service is required for natural-language circuit planning.
PCBDraft does not bundle a standalone model or a deterministic offline
natural-language fallback. A local model endpoint can be configured, but not
every local model and endpoint combination has been validated.

The initializer copies only missing KiCad global library-table templates; it does
not overwrite a valid user configuration.

## Verification

For an ordinary change, keep local verification focused and within roughly 90
seconds. Run `git diff --check`, `uv lock --check` when dependency inputs may have
changed, the closest relevant `unittest` module or case, and the applicable lint,
format, or syntax check for the files you touched. Focused examples:

    uv run python -m unittest tests.agent.test_design -v
    uv run python -m unittest tests.services.test_application.ApplicationConversationTests -v
    uv run pcbdraft --help
    uv run pcbdraft doctor --json

The full suite, deterministic benchmark, Python matrix, real KiCad acceptance,
and release reproducibility checks are integration or release gates. Do not run
them for every local commit; CI and release maintainers run them when appropriate:

    scripts/test.sh
    uv run python -m unittest -v tests.verification.test_benchmark
    uv run python scripts/boardbench.py --help
    scripts/python-matrix.sh
    scripts/release-check.sh

BoardBench is the natural-language, real-model campaign workflow; it is separate
from the deterministic fault-injection benchmark above. See
[`BOARDBENCH.md`](BOARDBENCH.md) for its explicit-path operator lifecycle,
holdout boundary, evidence counting, and physical-test limits.

`scripts/clean.sh` removes only repository-local `build/`, `dist/`, and
`src/pcbdraft.egg-info/` products. Release checks call it before and after
packaging so a stale package from an earlier source layout cannot enter a wheel.
The release check also clean-installs that wheel, verifies the bundled 90-case
deterministic corpus, and runs a local fake-provider agent turn through the
current flat PCB tools and real KiCad ERC/DRC. Its retained fixture is release
smoke evidence, not a real-model benchmark result or production attestation.
The normal test command also enforces security/bugbear lint rules, the current
complexity ceiling, a typed trust-boundary module set, and at least 70% branch
coverage. CI verifies that `constraints/runtime.txt` is an exact `uv.lock` export
and audits every locked runtime dependency. Expand the mypy file set as older
modules are annotated; do not weaken it to make a change pass.

## Placing new code

Use the responsibility packages documented in
[`PROJECT_STRUCTURE.md`](PROJECT_STRUCTURE.md). Keep package roots free of new
implementation modules, use canonical `pcbdraft.<area>.<module>` imports, and
place focused tests under the matching `tests/<area>/` directory. Historical
flat module paths remain only so existing persisted records and imports can be
migrated; they are not a semantic-version compatibility promise and must not be
used by new source or tests.

The generic-path tests must prove all of the following:

- no fixed board/profile is selected from a named part;
- local KiCad candidates are resolved from this host;
- a circuit plan that drops a named part, names an unknown pin, or embeds concrete
  geometry is rejected;
- a valid plan produces semantic IR and a project-local part graph;
- the LED, passive RC, and I2C pull-up stock-KiCad examples produce native routed
  projects and reach the candidate gate under real KiCad ERC/DRC/parity checks;
- the incomplete fine-pitch STM32/SHT31 fixture routes but remains blocked by
  deterministic electrical evidence rather than being called usable;
- a native generation failure retains evidence rather than inventing success.

## Adding a generic capability

Do not add a special branch such as “if the request contains device X, compile
board Y.” A named part is input data, not a product mode.

Instead:

1. Extend the generic request or circuit-plan schema only when the fact belongs
   to all future plans, not one board.
2. Keep the circuit plan semantic: components, actual local symbols, pin
   endpoints, nets, constraints, assumptions, and notes. The runtime may expose
   bounded, schema-validated placement and routing tools with explicit geometry,
   but never raw model-controlled KiCad text, shell code, or unchecked writes.
3. Resolve selected symbols and footprints from the installed KiCad libraries and
   persist the resulting project-local <code>PartGraph</code>.
4. Add deterministic validation for any new electrical, layout, or manufacturing
   claim, and state exactly what was checked.
5. Add adversarial tests: dropped part identity, false pin number, unavailable
   stock symbol, complex-domain non-rejection, and retention after a failed attempt.
6. Update tests and user-facing documentation only with evidence that actually
   exists.

Use the high-level runtime APIs rather than expanding a raw file-mutation tool
surface. A plan should state intent. Concrete placement and routing calls must
remain revision-bound, locally validated, and committed through the normal
evidence boundary; the KiCad adapters still own native representation.

## Optional curated part knowledge

The normal generator needs only installed stock KiCad symbols and footprints.
Curated reusable data for legacy fixtures lives in
<code>src/pcbdraft/data/parts/catalog.json</code>. A record promoted beyond
local extraction must include:

- canonical identity, manufacturer, MPN/package variant, symbol, footprint, and
  pin-to-pad mapping;
- ratings and manufacturing facts used by code;
- dated provenance and lifecycle/sourcing state;
- a justified trust level; and
- tests against the relevant local library and deterministic checks.

Do not call a model guess, a scraped field, or a generic KiCad symbol
<code>rule_validated</code>. Attributed external evidence is still not authenticated
or independently verified by the runtime; it may complete an evidence checklist,
but it must never create a PCBDraft production attestation.

## Domain handling and truthful checks

Do not add named-board branches or pretend that recognizing a domain is technical
validation. Safety-critical, medical, aviation, mains-voltage, high-power, and
production-certification workflows are outside the current product scope. Other
specialized requests must disclose unavailable analysis and must never be
presented as safe or production ready.

Never weaken a validation or release gate merely to make a generated project look
complete. ERC/DRC do not prove functional, thermal, EMC, SI/PI, sourcing, or
manufacturing correctness.

## Open-source reuse

Before borrowing code or data from another project, verify its license and add
every required attribution or notice to <code>NOTICE</code>. Do not copy
third-party fixtures, logos, screenshots, or unknown-license design files into
the repository.

## Commit and artifact policy

Keep changes coherent and run focused tests before the full suite. Do not commit
virtual environments, caches, private workspaces, KiCad personal files, locks, or
arbitrary user projects. Preserve failed generic attempt receipts only when they
are independently authored, sanitized, and useful engineering evidence.
