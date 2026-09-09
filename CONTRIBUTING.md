# Contributing to PCBDraft

PCBDraft is an alpha project. The most useful contributions make it easier for
someone new to install the application, produce a small native KiCad project,
understand a failure, and continue the same project later.

## Choose a contribution path

- **Report an onboarding bug.** Open an
  [issue](https://github.com/qixuancao/pcbdraft/issues) with the shortest
  reproduction you can provide.
- **Improve the user path.** Fix an unclear command, error message, example, or
  documentation link that blocked you.
- **Reproduce a small board.** Record both successes and failures for a small,
  non-safety-critical circuit. Do not present generated files as production
  ready.
- **Change code.** Keep the change scoped to one observable problem and add the
  closest focused test.

For vulnerabilities, do not open a public issue; follow [SECURITY.md](SECURITY.md).

## Set up a checkout

Install Git, `uv`, and Python 3.11, 3.12, or 3.13. A compatible KiCad installation
is needed for native generation and KiCad checks.

```console
git clone https://github.com/qixuancao/pcbdraft.git
cd pcbdraft
git switch --track origin/refactor/native-runtime-20260905
uv sync --frozen --extra dev
uv run pcbdraft setup
uv run pcbdraft doctor --json
```

Until the next coherent public release is cut, the active alpha contribution
target is `refactor/native-runtime-20260905`; the public default branch does not
yet contain all behavior described here. When reviewing an existing pull request,
check out that pull request's branch instead.

Natural-language planning also requires a configured model provider. See the
[development guide](docs/DEVELOPMENT.md) for the current KiCad baseline and
architecture rules.

To exercise the first-board path, start the TUI:

```console
uv run pcbdraft
```

Then create a project with `/new contributor-led-smoke` and try a deliberately
small request such as:

> Design a non-safety-critical 3.3 V LED indicator using one green LED, one
> 330 ohm resistor, and a two-pin power connector.

Follow the approvals displayed by the application, then use `/review` and
`/logs` to inspect what actually happened. This is a reproduction scenario, not
a promise that every environment or model will produce a passing board.

When reporting a failure, include the PCBDraft commit, operating system, Python
and KiCad versions, the provider/model name, the minimal prompt, expected versus
actual behavior, and relevant diagnostics. Review logs and `doctor --json`
output before sharing them. Remove API keys, tokens, private endpoints, personal
paths, and confidential design content.

## Make and verify a change

Read [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) before changing schemas,
generation, validation, evidence, benchmark data, or package structure. Do not
add a named-board shortcut in place of a generic capability.

Local checks are a fast development filter, not a duplicate of the release
gate. Keep the normal pre-push pass to roughly 90 seconds:

1. Run `git diff --check`.
2. Run `uv lock --check` when dependencies or the lock file could have changed.
3. Run the smallest relevant `unittest` module or case, for example
   `uv run python -m unittest tests.agent.test_design -v`.
4. Run the applicable formatter, linter, or syntax check for changed code or
   configuration. A documentation-only change does not need an unrelated Python
   test.

Do not routinely run `scripts/test.sh`, the full benchmark, a Python version
matrix, KiCad end-to-end acceptance, or `scripts/release-check.sh` for an ordinary
commit. Required CI checks are the integration gate; release maintainers run the
additional release checks. If a required check was not run locally, say so in
the pull request.

Open a draft pull request early. Describe the user-visible problem, the chosen
scope, the focused evidence you ran, and anything left unverified. Keep generated
claims honest: a fixture cannot stand in for sourcing, engineering review,
fabrication, assembly, or physical measurement.

## Data and licensing

Code and documentation contributions are licensed under Apache-2.0. Records
under `src/pcbdraft/data/` use the CC0-1.0 dedication documented there. Do not
contribute proprietary, confidential, copied-competitor, or license-unclear board
data, screenshots, footprints, symbols, or datasheet extracts.
