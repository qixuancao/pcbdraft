# Persisted verification artifacts

These artifacts are selected real acceptance evidence, not test mocks.

## `acceptance/release`

A KiCad 10.0.5 manufacturing candidate for the checked-in ATtiny402/TMP102
project. `release-manifest.json` inventories 31 stable content artifacts;
`receipt.json` links the manifest, deterministic ZIP, real tool runs,
pre-normalization hashes, and three raw audit artifacts. Verify it with:

```bash
pcb-agent release-verify artifacts/acceptance/release --json
```

Expected hashes:

- manifest: `64b779d1ea1ff744307a2faf803ea0c42b9b9710504b63b93a1742bf4f0cd778`
- ZIP: `d70f582c8f428df45e2c6a6aa56dc8ffeac368185849329b3d15f17df3c88d98`

`engineering_candidate=true` and `production=false`. Board-only STEP is used for
content reproducibility; component 3D model references remain in the source board.

## `acceptance/review`

A real bounded Codex review using `gpt-5.6-sol` with reasoning `max`. The final run
is `20260812T154551Z-8f8b4876`: schema and completion event validated, duration was
340.286 seconds, and real ERC/DRC were clean. The model still rated the design
medium risk, returned heuristic findings, and named unsupported review categories.
This is useful review evidence but is not qualified L6 sign-off.

## `benchmark`

The full 90-case/five-repetition deterministic result plus two blinded live-model
repetitions. See the root `BENCHMARK.md` for metrics and limitations.

Receipts may contain absolute local artifact paths and model-generated text. Review
privacy before redistributing new runs. Failed runs should be retained when they
are cited as evidence rather than rewritten as successes.
