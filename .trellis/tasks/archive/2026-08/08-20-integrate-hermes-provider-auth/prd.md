# Integrate Hermes provider authentication

## Goal

Let a newly installed PCBDraft user connect an existing model subscription or
API-key plan from the PCBDraft CLI and start using the PCB agent without
manually editing configuration files. Reuse the vendored Hermes provider,
authentication, token refresh, endpoint detection, and model-selection flows
where they already implement the required behavior.

## User Value

- A first-time user can launch PCBDraft, choose a supported provider, complete
  browser/device-code login or paste the plan key, choose a model, and
  immediately enter the PCB conversation.
- Users can choose every provider that the bundled Hermes version exposes,
  including subscription, API-key, cloud-identity, local, aggregator, and
  custom endpoint flows, without understanding Hermes internals.
- Reauthentication and provider switching use the same PCBDraft-facing flow.

## Confirmed Facts

- The bare `pcbdraft` command launches the vendored Hermes terminal through
  `src/pcbdraft/interfaces/cli.py` and
  `src/pcbdraft/interfaces/hermes_cli.py`.
- `/connect` is already part of the pruned PCBDraft slash-command surface, but
  it only reports the current OpenAI-compatible connection and tells an
  unconfigured user to edit TOML manually
  (`src/pcbdraft/interfaces/commands.py:136-155`).
- PCBDraft's current model schema requires a literal `api_key`, `base_url`, and
  model list for every connection. It has no authentication type or reference
  to renewable OAuth credentials (`src/pcbdraft/model/config.py:104-112`,
  `src/pcbdraft/model/config.py:292-342`).
- Every interactive launch rewrites Hermes `config.yaml` from the PCBDraft
  TOML config. A configured PCBDraft provider is emitted as generic
  `provider: custom`, so a provider selected only through Hermes would be lost
  or downgraded on the next launch
  (`src/pcbdraft/model/hermes_config.py:19-66`).
- The vendored Hermes runtime already includes reusable provider/auth code:
  - OpenAI Codex device-code/OAuth login, token persistence/refresh, Codex
    Responses transport, and live Codex model discovery
    (`vendor/hermes/hermes_cli/auth.py:259-264`,
    `vendor/hermes/hermes_cli/model_setup_flows.py:623-709`).
  - Z.AI/GLM API-key support with general, China, Coding Plan global, and
    Coding Plan China endpoint detection
    (`vendor/hermes/hermes_cli/auth.py:316-323`,
    `vendor/hermes/hermes_cli/auth.py:732-833`).
  - MiniMax global/China API-key plans plus MiniMax portal OAuth, PKCE login,
    persistent refresh tokens, and per-request access-token refresh
    (`vendor/hermes/hermes_cli/auth.py:374-391`,
    `vendor/hermes/hermes_cli/auth.py:8380-8900`).
  - A shared provider/model wizard used by both `hermes model` and setup
    (`vendor/hermes/hermes_cli/main.py:3519+`).
- The bundled Hermes version defines 38 static canonical picker entries. At
  runtime, automatic injection from its 36 bundled provider-profile plugin
  directories currently expands that picker to 46 base entries, including the
  manual custom-endpoint row; saved custom providers may add more. The
  authentication registry has 36 definitions. Because
  the runtime picker is dynamic, a copied static PCBDraft list would
  immediately become a second source of truth
  (`vendor/hermes/hermes_cli/models.py:1154-1213`).
- Hermes provider families include direct API keys, device-code/browser OAuth,
  external processes, AWS Bedrock, Google Vertex identity, Azure Entra ID,
  local servers, aggregators, and custom endpoints. Several providers rely on
  Hermes' existing lazy optional-dependency installation.
- Hermes is vendored under the MIT license, so its code may be reused while
  preserving the required copyright/license notice (`vendor/hermes/LICENSE`).
- The previous Hermes integration task intentionally left provider credential
  and protocol semantics out of scope. This task owns that deferred boundary.

## Requirements

### R1. PCBDraft-owned connection entry points

Provide one connection workflow reachable from both `pcbdraft connect` before
entering the terminal and `/connect` inside the terminal. Both entry points
must use the same service and produce the same persisted result.

When an interactive first launch has no usable provider, PCBDraft must offer
this connection workflow directly instead of sending the user to edit TOML.
Cancellation must leave the existing configuration unchanged and exit or
continue with an actionable message.

### R2. Reuse Hermes authentication behavior

Use the vendored Hermes Python provider/auth implementation as the behavioral
source for supported logins, refresh, endpoint detection, model discovery, and
provider-specific transport. Do not reimplement OAuth or copy a second fork of
the same algorithms into unrelated PCBDraft modules.

The PCBDraft integration layer may wrap or adapt Hermes functions so product
names, storage locations, error messages, and configuration ownership remain
PCBDraft-specific. This work uses only the Hermes snapshot already vendored in
this repository; it must not inspect, import, modify, uninstall, or depend on a
separately installed system/user Hermes. Vendor modifications, if unavoidable,
must be minimal, traceable to upstream, and covered by focused tests.

### R3. Complete Hermes provider catalog

The PCBDraft connection picker must expose the complete provider catalog of
the vendored Hermes version, including every canonical provider, grouped
provider variant, dynamically registered bundled provider profile, saved
custom provider, and the manual custom-endpoint flow. PCBDraft must not keep a
provider allow-list or retype provider metadata in its own catalog.

The task therefore includes, without being limited to, Codex subscription
login, GLM/Z.AI plans, MiniMax plans/OAuth, Anthropic, OpenAI API, OpenRouter,
Nous, xAI, Qwen, Kimi, StepFun, DeepSeek, Gemini, Copilot, Bedrock, Vertex,
Azure Foundry, local endpoints, aggregators, and any additional provider the
same Hermes picker discovers from its bundled provider plugins.

When the vendored Hermes snapshot gains or loses a bundled provider, the
PCBDraft picker and provider-routing behavior must follow automatically after
the vendor update, without a parallel PCBDraft catalog edit.

### R4. One authoritative model selection

PCBDraft and Hermes must agree on the active provider, authentication mode,
endpoint, transport, and model across process restarts. Running `/connect`,
`pcbdraft connect`, or the retained `/model` command must not create competing
configurations. Hermes `config.yaml` and its auth stores are the only provider
authority; the old PCBDraft TOML provider catalog/schema and destructive
`write_hermes_config()` translation are removed from the normal product path,
not migrated or retained as a compatibility fallback.

OAuth access and refresh tokens must stay in the private Hermes/PCBDraft auth
store and must not be copied into ordinary project files, logs, debug traces,
or user-visible status output. API-key files must retain private permissions.
The store must live below the PCBDraft config directory and must not share the
standalone Hermes default home.

### R5. Status, switching, reauthentication, and failure recovery

The connection workflow must distinguish at least: not configured, connected,
expired/relogin required, provider unreachable, invalid key, unsupported plan
endpoint, and user cancellation. Users must be able to view the active provider
without revealing secrets, switch providers/models, and deliberately
reauthenticate.

Headless or remote terminals must show the verification URL/code and must not
depend on successfully opening a local browser.

### R6. Installable and testable behavior

The workflow must work from the packaged PCBDraft installation using the
already packaged vendored Hermes runtime, including all bundled provider
profiles and the provider-specific lazy dependency mechanism, not only from
the source checkout.
Focused tests must cover fresh-config round-trip, entry-point routing, provider
selection, secret redaction/permissions, cancellation, and mocked
Codex/GLM/MiniMax auth or endpoint flows without contacting real accounts.

## Acceptance Criteria

- [ ] With an empty config, a user can run bare `pcbdraft`, choose a supported
  connection, complete the guided flow, choose a model, and reach the terminal
  without manually editing a file.
- [ ] `pcbdraft connect` and `/connect` invoke the same connection service and
  persist one authoritative active provider/model.
- [ ] The displayed provider set is derived from Hermes
  `CANONICAL_PROVIDERS`/provider discovery and matches the vendored Hermes
  picker, including grouped variants, custom providers, and future bundled
  API-key provider plugins; PCBDraft contains no separate provider whitelist.
- [ ] A mocked Codex login completes, selects a discovered Codex model, survives
  restart, and resolves through the Codex Responses transport without exposing
  access or refresh tokens.
- [ ] A mocked GLM key can resolve the correct general/Coding Plan and
  global/China endpoint, persist the selected endpoint/model, and survive
  restart.
- [ ] Mocked MiniMax API-key and supported OAuth flows choose the correct
  global/China endpoint; OAuth access tokens refresh through the Hermes logic
  during a long-running session.
- [ ] Representative mocked tests cover every Hermes authentication/transport
  class: API key, OAuth/device code, external process, Anthropic messages,
  Codex Responses, AWS Bedrock, Vertex identity, Azure Entra ID, aggregator,
  local endpoint, and custom endpoint.
- [ ] `/connect` can display sanitized status, switch provider/model, request
  reauthentication, and cancel without corrupting the prior working config.
- [ ] `/model` and startup config generation cannot overwrite or silently
  downgrade a connection created through the new workflow.
- [ ] The old PCBDraft provider preset/TOML authority is absent from the normal
  runtime; there is no migration branch, dual read, or fallback to old
  provider credentials.
- [ ] Auth files remain private, secrets are redacted from status/errors/traces,
  and PCB project directories contain no provider credentials.
- [ ] Focused lint/type checks and the nearest unit/integration tests pass
  within the repository's fast-validation policy; live-account and full E2E
  checks, if not run, are explicitly deferred.

## Out of Scope

- Purchasing, reselling, or managing provider subscriptions/billing inside
  PCBDraft.
- Implementing a new OAuth server or changing the upstream provider protocols.
- Restoring unrelated Hermes gateway, messaging, voice, billing, or plugin
  commands pruned by the previous task.
- Requiring live user credentials in automated tests or promising that an
  external provider account/plan grants access to a particular model.
- Redesigning PCB generation, KiCad validation, or the project repository.
- Backward compatibility or automatic migration for the old PCBDraft provider
  TOML format; users configure a fresh Hermes provider connection.
- Reading, deleting, upgrading, uninstalling, or otherwise changing any
  independently installed Hermes program, `~/.hermes` data, credentials,
  sessions, memories, skills, or launcher.

## Product Decision

- The first release exposes the full provider catalog delivered by the
  vendored Hermes version. There is no Codex/GLM/MiniMax-only MVP and no
  PCBDraft-maintained provider allow-list.
- The old PCBDraft provider configuration is deleted rather than migrated.
  Implementation should prefer removal and one direct path over compatibility
  branches.
- PCBDraft is isolated from standalone Hermes. Hermes-derived code is adapted
  to PCBDraft-owned names and paths under the PCBDraft config directory.
