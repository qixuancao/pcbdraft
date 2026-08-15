# Contributing to PCBDraft

Contributions are welcome within the runtime's evidence-first scope. Read
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) before changing parts, verified
blocks, scope policy, generation profiles, validation rules, or benchmark data.

## Pull request verification workflow

Use a PR-first workflow. GitHub Actions is the authoritative full-suite gate;
local checks are a fast pre-push filter and should not duplicate the complete CI
run.

Before each push:

1. Run `git diff --check` and `uv lock --check`.
2. Run focused tests for behavior changed by the commit.
3. Run the applicable lightweight syntax or lint check for changed Python, shell,
   workflow, or configuration files.
4. Open a draft pull request early, then push corrections to the same branch and
   PR rather than opening a new PR for every failure.

Do not routinely run `scripts/test.sh`, `scripts/benchmark.sh`, or
`scripts/release-check.sh` locally before every push. The PR CI must run the full
Python matrix, lint and type checks, dependency audits, package/install checks,
KiCad acceptance tests, end-to-end tests, benchmarks, and release reproducibility
checks. A PR must not be marked ready or merged while a required check is pending
or failing, or while actionable review feedback remains unresolved.

Run the closest relevant full check locally when changing CI or release tooling,
when CI is unavailable, or when reproducing a failure is faster locally. Keep
generated claims honest: unavailable physical tools, human review, sourcing,
fabrication, and test evidence must remain unavailable/external rather than being
represented by fixtures.

By contributing code or documentation, you agree to license it under Apache-2.0.
By contributing records under `src/pcbdraft/data/`, you agree to the CC0-1.0
dedication documented there. Do not contribute proprietary, confidential, copied
competitor, or license-unclear board data.
