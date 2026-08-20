# Implement — Integrate the complete Hermes provider system

## 1. Provider adapter and configuration ownership

- [ ] Add a typed provider-connection service that lazily loads the vendored
  Hermes runtime, invokes its canonical setup/model wizard, and returns only a
  sanitized status projection.
- [ ] Bind the vendored runtime to a PCBDraft-owned home and product wording;
  never discover or touch a standalone Hermes executable, package, or
  `~/.hermes` state.
- [ ] Derive the provider list exclusively from Hermes canonical/provider
  discovery; do not introduce a PCBDraft allow-list.
- [ ] Change Hermes config generation to an atomic ownership-aware merge that
  preserves Hermes model/provider/auth/auxiliary sections while ensuring the
  PCBDraft display, toolset, and debug-plugin settings.
- [ ] Delete the old PCBDraft provider presets/TOML authority and update its
  callers/tests; add no migration, dual-read, or compatibility fallback.
- [ ] Preserve private modes and redact all status/error output.

## 2. User entry points and onboarding

- [ ] Add `pcbdraft connect` with the Hermes wizard's relevant browser,
  timeout, region, and refresh options.
- [ ] Replace `/connect`'s read-only TOML status handler with the same
  interactive connection service; keep `/model` routed to Hermes.
- [ ] Before the interactive REPL starts, prompt on a TTY only when no usable
  Hermes provider exists; cancellation leaves config unchanged and exits with
  a clear next command.
- [ ] Make headless/non-TTY behavior fail fast with a non-secret actionable
  message rather than hanging for input.
- [ ] Update `doctor` and help text to report the Hermes-backed connection.

## 3. Make every Hermes provider usable by PCB planning

- [ ] Add `HermesIntentProvider` using Hermes runtime provider resolution and
  `agent.auxiliary_client.call_llm()` while retaining PCBDraft prompts,
  bounded artifacts, strict parsing, and domain validators.
- [ ] Route normal `ApplicationService(provider_name="auto")` through the
  Hermes-backed provider; preserve only protocol-based direct provider
  injection for tests.
- [ ] Support interpret, plan, and revise-plan calls across the normalized
  chat, Codex Responses, Anthropic Messages, Bedrock, Vertex/Gemini, ACP,
  aggregator, local, and custom transport classes.
- [ ] Convert Hermes/provider failures at the PCBDraft boundary into sanitized
  `PCBDraftError` instances without losing chained diagnostics.

## 4. Packaging and provider dependency behavior

- [ ] Assert the built wheel contains `hermes_cli/auth.py`,
  `hermes_cli/model_setup_flows.py`, `providers/`, and all bundled
  `plugins/model-providers/*` directories.
- [ ] Preserve Hermes lazy optional dependency installation for Anthropic,
  Bedrock, Vertex, Azure identity, and ACP; do not inflate PCBDraft base deps.
- [ ] Add a vendor-contract test that fails clearly if a future Hermes sync
  removes or changes the registries/functions used by the adapter.
- [ ] Preserve `vendor/hermes/LICENSE` in wheel/sdist output.
- [ ] Add an isolation test using only a test-runner temporary directory:
  point the test process's `HOME`, `XDG_CONFIG_HOME`, and
  `PCBDRAFT_HERMES_DIR` at separate paths under `tmp_path`; put sentinel files
  in the temporary `HOME/.hermes`; then prove PCBDraft neither reads nor
  changes those sentinels. Never inspect or create the developer's real
  `~/.hermes`.

## 5. Focused tests

- [ ] Test that PCBDraft's displayed provider identities equal Hermes'
  canonical/discovered picker identities, including grouped and custom rows.
- [ ] Test `pcbdraft connect`, `/connect`, `/model`, first-run TTY onboarding,
  non-TTY behavior, cancellation, switching, and restart persistence with an
  isolated fresh PCBDraft Hermes directory under the test runner's
  `tmp_path`; never use the process owner's real home.
- [ ] Mock representative flows for API key, Codex/device-code OAuth, MiniMax
  refresh, external process, Anthropic Messages, Codex Responses, Bedrock,
  Vertex, Azure identity, aggregator, local, and custom transports.
- [ ] Verify the same selected provider is used by the Hermes conversation and
  `ApplicationService` PCB planning path.
- [ ] Assert secrets never appear in status, errors, trace records, project
  files, repr output, or world/group-readable files.

## Validation

Stay within the repository's normal ~90-second fast budget:

```bash
git diff --check
uv run ruff check <changed Python files and focused tests>
uv run ruff format --check <changed Python files and focused tests>
uv run mypy <changed PCBDraft modules>
uv run python -m unittest tests.model.test_config tests.interfaces.test_hermes_cli <new focused provider tests>
uv build
python3 -c "import pathlib, zipfile; p=sorted(pathlib.Path('dist').glob('*.whl'))[-1]; names=zipfile.ZipFile(p).namelist(); assert any('plugins/model-providers/openai-codex' in n for n in names); assert any(n.endswith('vendor/hermes/LICENSE') for n in names)"
```

Do not run live provider logins, the full test suite, release check, dependency
audit, KiCad acceptance, or E2E matrix during normal iteration. Leave those to
explicit release/integration validation and report them as deferred.

## Rollback points

- Keep config-ownership deletion isolated from entry-point changes so a failed
  onboarding rollout can be reverted cleanly in Git.
- Do not mutate `vendor/hermes` unless a focused vendor-contract test proves a
  wrapper is impossible; any unavoidable vendor patch must be isolated and
  documented against the vendored commit.
- Never delete or modify a real standalone Hermes installation or home during
  implementation, validation, or rollback.
