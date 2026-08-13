# Contributing to CopperWright

Contributions are welcome within the runtime's evidence-first scope. Read
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) before changing parts, verified
blocks, scope policy, generation profiles, validation rules, or benchmark data.

Before submitting a change, run:

```bash
scripts/test.sh
scripts/benchmark.sh
```

Changes to native generation, synchronization, validation, or release should also
run `scripts/release-check.sh` with KiCad 10.0.5. Keep generated claims honest:
unavailable physical tools, human review, sourcing, fabrication, and test evidence
must remain unavailable/external rather than being represented by fixtures.

By contributing code or documentation, you agree to license it under Apache-2.0.
By contributing records under `src/copperwright/data/`, you agree to the CC0-1.0
dedication documented there. Do not contribute proprietary, confidential, copied
competitor, or license-unclear board data.
