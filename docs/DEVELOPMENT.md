# CopperWright development and release guide

## Environment

Install `uv`, Python 3.11+, Git, and KiCad 10 with symbols, footprints, 3D models,
CLI, and system Python bindings. Then run:

```bash
uv sync --frozen --extra dev
uv run copperwright doctor --json
```

Codex is optional unless exercising the authenticated conversational provider,
changing the AI reviewer, or running live model metrics. The builtin provider
supports deterministic offline product development. Firefox plus geckodriver are
required for the real browser acceptance journey.

On a fresh noninteractive Linux account, initialize KiCad's user library tables
after installing the KiCad 10 packages:

```bash
scripts/prepare-kicad-environment.sh
```

The initializer copies only missing vendor `sym-lib-table` and `fp-lib-table`
templates. It preserves valid existing user tables and never disables ERC or DRC.

## Verification commands

```bash
scripts/test.sh
scripts/benchmark.sh
scripts/smoke.sh
scripts/compatibility.sh
scripts/generate-brand-assets.sh --check
scripts/chat-e2e.sh /tmp/copperwright-chat-e2e
uv run python scripts/browser-e2e.py --output /tmp/copperwright-browser-e2e
uv run python scripts/generate-product-examples.py --output-root /tmp/copperwright-profiles
scripts/release-check.sh
```

Focused tests can use standard unittest module names, for example:

```bash
uv run python -m unittest tests.test_transactions -v
```

`scripts/test.sh` runs Ruff lint/format checks, byte compilation, the complete
unittest suite, and whitespace validation. Compatible local KiCad enables native
generation/sync/release cases; otherwise those cases are explicit skips.

`scripts/release-check.sh` additionally builds wheel/sdist, inspects their members,
installs the wheel into a fresh environment, runs the complete terminal and real
Firefox journeys against that installed wheel, runs a deterministic benchmark,
generates and validates the acceptance project, creates two releases, compares
content hashes, and verifies the bundle offline. Browser acceptance uses a private
clean HOME and initializes only the missing KiCad global library tables there.

`scripts/generate-brand-assets.sh --check` regenerates every derived icon and the
social preview in a private temporary directory and byte-compares them with the
tracked assets.

## Adding a part

Part data is CC0 and lives in `src/pcb_agent/data/parts/catalog.json`. A record must
identify an exact orderable variant and include:

- canonical ID, manufacturer, MPN, package, symbol, and footprint;
- every symbol pin and exact footprint pad mapping;
- electrical ratings used by runtime decisions;
- lifecycle status with dated source and honest stock/price state;
- manufacturing contract and available model references;
- trust state and provenance records with method/confidence.

Add tests that resolve the real installed symbol/footprint and exercise rating and
pin contracts. Do not label scraped or model-extracted data `rule_validated` until
those checks exist; do not label anything human/production verified without
external evidence.

## Adding a generation profile

A profile is not complete when it merely parses. It needs verified blocks,
deterministic compilation, semantic rules, native KiCad generation, 2/4-layer
tests as applicable, ERC/DRC/parity, manufacturing export, honest L5/L6/L7 states,
and independent fault/clean benchmark cases. Only then extend
`runtime.capabilities`.

High-risk domains remain rejected even if a caller supplies syntactically valid
IR. New risk policy requires explicit review and tests.

After a profile's full native chain passes, regenerate a reviewable copy outside
the tracked tree first:

```bash
uv run python scripts/generate-product-examples.py \
  --output-root /tmp/copperwright-product-profiles
```

Promoting output into `examples/product_profiles` requires reviewing the native
project, real ERC/DRC reports, L0–L7 states, preview receipt, source/license
contract, and all machine-specific paths. A new profile is not advertised merely
because the provider can classify its name.

## Application and provider changes

Keep business logic in `ApplicationService`; terminal and browser layers should
only translate input/output and never create a second confirmation, transaction,
or validation implementation. Persist a job before starting it, publish only at
an atomic safe boundary, and make restart behavior explicit.

Provider output is untrusted. Extend the shared strict interpretation schema and
normalizer before adding fields. Never accept credentials in browser forms,
command arguments, project files, transcripts, or receipts. An OpenAI-compatible
contract test should use a private local test server and scan the resulting
workspace for its sentinel secret.

## Benchmark corpus

The bundled corpus is independently authored CC0 data. Each case mutates one clean
semantic fixture or is a clean control. Expected codes are used only by the
deterministic evaluator; the optional model prompt is blinded.

Contributions should include negative controls and avoid copying competitor-owned
fixtures or annotations. Document limitations; do not tune a rule only to an ID or
fixture-specific string. A repair case must use the public semantic operation path
and be checked for new findings.

## Commit and artifact policy

Keep commits coherent and run focused tests before the full suite. Preserve failed
experiment receipts when they explain an engineering decision, but never replace a
failed run with invented success output. Generated acceptance artifacts must name
their tool versions and readiness limitations.

Do not commit `.venv`, caches, transient locks, KiCad personal `.kicad_prl` files,
or arbitrary user projects. Do commit independent corpus data, the canonical
acceptance project, and selected evidence bundles used by traceability claims.
