# PCBDraft development guide

## Environment

Install Python 3.11+, Git, and KiCad 10 with symbols, footprints, CLI, and system
Python bindings. <code>uv</code> is the recommended environment manager.

    uv sync --frozen --extra dev
    scripts/prepare-kicad-environment.sh
    uv run pcbdraft doctor --json

model API is optional unless exercising the model-backed planner. The builtin provider
is useful for offline requirement extraction but deliberately does not invent a
circuit. Browser E2E tests additionally need Firefox and geckodriver.

The initializer copies only missing KiCad global library-table templates; it does
not overwrite a valid user configuration.

## Verification

    scripts/test.sh
    scripts/benchmark.sh
    scripts/smoke.sh
    scripts/python-matrix.sh
    scripts/release-check.sh

`scripts/clean.sh` removes only repository-local `build/`, `dist/`, and
`src/pcbdraft.egg-info/` products. Release checks call it before and after
packaging so a stale package from an earlier source layout cannot enter a wheel.

Focused examples:

    uv run python -m unittest tests.agent.test_design -v
    uv run python -m unittest tests.services.test_application.ApplicationConversationTests -v
    uv run pcbdraft symbols SHT31 --json
    uv run pcbdraft doctor --json

## Placing new code

Use the responsibility packages documented in
[`PROJECT_STRUCTURE.md`](PROJECT_STRUCTURE.md). Keep package roots free of new
implementation modules, use canonical `pcbdraft.<area>.<module>` imports, and
place focused tests under the matching `tests/<area>/` directory. Historical
flat module paths exist only for downstream 1.0 compatibility and must not be
used by new source or tests.

The generic-path tests must prove all of the following:

- no fixed board/profile is selected from a named part;
- local KiCad candidates are resolved from this host;
- a plan that drops a named part, names an unknown pin, or contains geometry is
  rejected;
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
2. Keep model output semantic: components, actual local symbols, pin endpoints,
   nets, constraints, assumptions, and notes. Never add model-controlled KiCad
   text, coordinates, shell code, or routes.
3. Resolve selected symbols and footprints from the installed KiCad libraries and
   persist the resulting project-local <code>PartGraph</code>.
4. Add deterministic validation for any new electrical, layout, or manufacturing
   claim, and state exactly what was checked.
5. Add adversarial tests: dropped part identity, false pin number, unavailable
   stock symbol, complex-domain non-rejection, and retention after a failed attempt.
6. Update tests and user-facing documentation only with evidence that actually
   exists.

Use the high-level runtime APIs rather than expanding a raw file-mutation tool
surface. A plan should state intent; the compiler, solver, and KiCad adapters
should decide representation and geometry.

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
<code>rule_validated</code>. Do not claim human or production verification without
attributed external evidence.

## Domain handling and truthful checks

Domain names are not generation gates. Requests involving mains, high power,
DDR/PCIe/SerDes, RF, medical, aviation, safety-critical, or unfamiliar work are
attempted through the normal stock-KiCad path. Diagnostics may warn that relevant
specialized analysis is unavailable.

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
