# Design — Integrate the complete Hermes provider system

## Context

PCBDraft already ships and launches the complete trimmed Hermes Python
runtime, but provider setup is split across two incompatible authorities.
Hermes knows how to authenticate and route its full provider catalog, while
PCBDraft rewrites Hermes config from a smaller literal API-key TOML schema.
The result is a `/connect` command that cannot connect anything and a PCB
planning service that cannot consume subscription or non-OpenAI transports.

## Goals

- Expose every provider delivered by the vendored Hermes version through
  first-run onboarding, `pcbdraft connect`, `/connect`, and `/model`.
- Use Hermes provider discovery, login, refresh, endpoint detection, model
  selection, transport routing, and optional dependency behavior directly.
- Make the active Hermes provider work for both the conversational agent and
  PCBDraft's internal requirement interpretation/circuit planning calls.
- Keep credentials private and keep provider additions/removals synchronized
  automatically with future Hermes vendor updates.

## Non-goals

- Maintaining a PCBDraft provider whitelist or forked OAuth implementation.
- Managing provider billing/subscriptions or guaranteeing account entitlements.
- Re-enabling unrelated Hermes commands or tools pruned by the prior task.

## Architecture

### Authority after this task

```text
Hermes canonical providers + provider plugins
                  │
                  ▼
Hermes setup/auth/model/runtime_provider
        │                         │
        ▼                         ▼
PCBDraft connect surfaces     Hermes auxiliary call boundary
(first launch, subcommand,     (all transports normalized for
 slash command, /model)         PCBDraft intent/plan requests)
        │                         │
        └──────────┬──────────────┘
                   ▼
       HERMES_HOME config/auth stores
```

The vendored runtime's `config.yaml`, `.env`, `auth.json`, credential pool,
provider-specific auth files, and provider plugins are the sole authority for
provider/model choice inside PCBDraft. They live under a PCBDraft-owned home,
not standalone Hermes' default `~/.hermes`. The PCBDraft TOML provider
catalog/schema is removed instead of read, migrated, or retained as a fallback.

### Provider catalog and connection service

Add a thin PCBDraft-owned adapter, tentatively
`src/pcbdraft/services/provider_connection.py`, responsible for product entry
points, not provider logic. It will:

1. install the vendored Hermes path and bind `HERMES_HOME`;
2. query Hermes' canonical/provider registries for status and picker entries;
3. call the existing `hermes_cli.main.select_provider_and_model(args)` wizard;
4. return a sanitized `ConnectionStatus` projection for PCBDraft UI/doctor;
5. distinguish configured, unavailable, expired/relogin-required, cancelled,
   and successfully changed outcomes without returning secrets.

The adapter imports Hermes lazily after `install_vendor_path()` so importing
ordinary PCBDraft modules does not depend on top-level Hermes imports.

The binding is process-local and always resolves to the PCBDraft configuration
tree (with a PCBDraft-specific test override). It never probes a `hermes`
executable, imports a separately installed Hermes package, or reads
`~/.hermes`. User-facing strings and recovery commands say `pcbdraft connect`,
not `hermes setup` or `hermes model`.

`pcbdraft connect` and `/connect` both call this adapter. `/model` remains the
retained Hermes command and therefore uses the exact same wizard. No provider
names are copied into `src/pcbdraft`.

### First-run behavior

`interfaces/hermes_cli.launch_cli()` performs a readiness check after the
Hermes home and PCBDraft-owned config defaults exist but before starting the
REPL:

- interactive TTY + no usable provider: explain that a model connection is
  required and launch the connection wizard;
- cancellation: do not change prior files; show `pcbdraft connect` and return
  without entering a broken conversation loop;
- non-interactive/headless launch: never block on prompts; return an actionable
  error directing the user to an interactive `pcbdraft connect` invocation;
- existing usable provider: launch immediately with no onboarding prompt.

Remote OAuth flows retain Hermes' URL/code output and `--no-browser` behavior.

### Configuration ownership

Replace `write_hermes_config()`'s destructive model rewrite with a small
atomic PCBDraft-defaults writer:

- PCBDraft owns only its required display/toolset/plugin/tool-search keys.
- Hermes owns `model`, `providers`, `custom_providers`, `auxiliary`, auth
  references, and provider-specific sections.
- Existing Hermes provider/model values are never translated through a
  PCBDraft model object.
- No old TOML detection, migration marker, dual read, compatibility shim, or
  credential fallback is added. An empty Hermes home starts the new wizard.
- Writes use Hermes' config helpers or PCBDraft atomic private-file helpers and
  retain mode `0o600`.

Delete the now-unused PCBDraft provider presets/config writer and update their
callers/tests rather than preserving deprecated types solely for compatibility.
This removes the current startup race where `/model` succeeds during one
session but the next PCBDraft launch overwrites it as `provider: custom`.

### PCB planning calls use the Hermes runtime provider

Add a `HermesIntentProvider` in the PCBDraft model layer. It reuses the
existing PCBDraft prompts, strict JSON validation, artifact recording, and
error hierarchy, but sends requests through Hermes'
`agent.auxiliary_client.call_llm()`/runtime provider projection. That boundary
already adapts:

- OpenAI-compatible chat completions;
- Codex/xAI Responses;
- native Anthropic Messages and compatible endpoints;
- AWS Bedrock Converse;
- Gemini/Vertex;
- Copilot ACP/external processes;
- aggregators, local servers, and custom endpoints.

The provider asks for strict JSON and feeds the returned text through
PCBDraft's existing bounded strict decoder and domain validators. No provider
response can bypass existing `validate_interpretation()` or
`CircuitPlan.from_dict()` validation.

`ApplicationService(provider_name="auto")` resolves this provider when Hermes
status is usable. Tests inject the `IntentProvider` protocol directly; they do
not require a deprecated product configuration path.

### Optional provider dependencies

Preserve Hermes' on-demand installation path for provider-specific packages
(`anthropic`, `boto3`, `google-auth`, `azure-identity`, ACP). PCBDraft must not
eagerly add every optional cloud SDK to its base install. The connection flow
must surface installation failure as a sanitized, actionable provider error.
Tests mock the lazy installer and never install packages from the network.

### Status and doctor

`/connect` with an existing connection opens the same Hermes picker, whose
current provider is preselected and offers unchanged/cancel behavior.
Sanitized status helpers may be used by `pcbdraft doctor`; they show provider,
model, auth kind/source, and readiness only—never keys, access tokens, refresh
tokens, credential file contents, or unredacted provider responses.

## Data flow

```text
First launch / pcbdraft connect / /connect / /model
  → provider_connection.connect()
    → Hermes select_provider_and_model()
      → CANONICAL_PROVIDERS + discovered provider profiles
      → provider-specific auth/setup flow
      → Hermes private config/auth stores
    → sanitized status

PCB tool calls ApplicationService.send_message()
  → HermesIntentProvider.interpret()/plan()/revise_plan()
    → Hermes runtime provider + auxiliary_client.call_llm()
      → correct provider transport and renewable credential
    → strict PCBDraft JSON/domain validation
    → normal PCB proposal/plan pipeline
```

## Clean replacement

- Existing provider state inside PCBDraft's own Hermes home remains ordinary
  Hermes-native state and needs no special compatibility path or prompt.
- Test/automation callers can still inject an `IntentProvider` directly.
- `PCBDRAFT_HERMES_DIR` and packaged runtime resolution remain supported. A
  PCBDraft-specific home override is used for tests; the generic standalone
  `HERMES_HOME` is not accepted as an authority for product state.
- Isolation tests replace `HOME`, `XDG_CONFIG_HOME`, and
  `PCBDRAFT_HERMES_DIR` only inside the test process with distinct paths below
  the test runner's temporary directory. A temporary `HOME/.hermes` may hold
  sentinel bytes solely to prove non-access; the developer's actual home is
  never inspected, created, or mutated by tests.
- Future bundled Hermes provider plugins appear automatically because all
  listing and dispatch remains registry-derived.
- Old PCBDraft provider TOML, presets, translation code, and tests are removed;
  a user with only that old config reconnects through Hermes.
- A separately installed Hermes program and its home are outside every runtime,
  test, setup, cleanup, and rollback path.

## Risks and mitigations

- Hermes internals are not a stable public API: isolate imports in one adapter
  and add a vendor-contract test around the functions/registries used.
- Provider setup can mutate several private files: snapshot status before the
  wizard, rely on Hermes atomic/locked writers, and verify post-state before
  reporting success.
- A provider may authenticate but not support reliable strict JSON: keep
  PCBDraft's bounded decoder/domain validators and report an actionable model
  compatibility error rather than accepting malformed engineering data.
- Lazy dependency installation can fail offline: preserve the chosen config,
  report the missing package, and allow retry after installation.
- The vendored tree is large: no second source copy is added; existing wheel
  packaging and MIT notice remain the delivery mechanism.

## Rollback

- Code rollback is a normal Git revert of the provider replacement. No runtime
  compatibility layer is carried in the new implementation.
- No local standalone Hermes cleanup exists in this task. Rollback and tests
  touch only isolated PCBDraft-owned paths.
