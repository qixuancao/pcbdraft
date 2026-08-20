# Hermes Provider Integration

> Executable contract for PCBDraft-owned provider discovery, authentication,
> persistence, status, and PCB-planning requests through the vendored Hermes
> runtime.

## Scenario: One isolated provider authority

### 1. Scope / Trigger

Apply this contract whenever code changes provider onboarding, `/connect`,
`/model`, model status, authentication storage, Hermes environment binding, or
the provider used by `ApplicationService`.

PCBDraft uses only `vendor/hermes`. Hermes-native configuration and auth files
below the PCBDraft config directory are the sole provider/model authority. Do
not restore the removed PCBDraft provider TOML catalog, probe a `hermes`
executable, or use a standalone `~/.hermes` home.

### 2. Signatures

Public entry points and Python boundaries:

```text
pcbdraft connect [--no-browser] [--timeout SEC] [--region global|china]
                  [--refresh] [--reauthenticate]
/connect [--no-browser] [--refresh] [--reauthenticate]
/model [--refresh | <target> | --provider <provider> ...]
```

```python
activate_provider_runtime() -> None
provider_identities() -> tuple[str, ...]
connection_status(*, verify: bool = True) -> ConnectionStatus
connect(options: ConnectionOptions | None = None) -> ConnectionStatus
format_connection_status(status: ConnectionStatus) -> str
HermesIntentProvider.from_config() -> HermesIntentProvider | None
```

All retained `/model` forms persist the selected provider/model. Ephemeral
`--once` and `--session` selection is rejected because it would create a
second, session-only authority.

### 3. Contracts

`ConnectionOptions` contains only `no_browser: bool`, `timeout: float | None`,
`region: str | None`, `refresh: bool`, and `reauthenticate: bool`.

`ConnectionStatus.to_dict()` is secret-free and contains:

```text
configured, usable, provider, model, auth_kind, source, outcome, error, state
```

`state` is one of `ready`, `unconfigured`, `cancelled`, `expired`,
`invalid_credentials`, `unreachable`, `unsupported_endpoint`, or
`unavailable`. Raw exception text, endpoints containing credentials, API
keys, access tokens, refresh tokens, and credential paths are never returned.

Environment ownership:

| Key | Contract |
|-----|----------|
| `PCBDRAFT_HERMES_HOME` | Optional PCBDraft-specific override, primarily for isolated tests. |
| `HERMES_HOME` | Always overwritten with the resolved PCBDraft home before vendored imports/use. Never accepted as product input. |
| `HERMES_SHARED_AUTH_DIR` | Always overwritten with `<PCBDraft Hermes home>/shared`. |
| `HERMES_HOME_MODE` | Set to `0700`; auth/config files remain `0600`. |

Provider picker identities come directly from Hermes canonical/provider
discovery plus compatible saved custom providers and the manual custom row.
PCBDraft must not add a provider allow-list.

The connection wizard snapshots bounded PCBDraft-owned mutable credential
files before running. Cancellation, timeout, and failure restore or remove the
snapshotted files atomically. Reject symlinked homes, auth directories, and
credential files before vendor code can write. External provider-owned CLI
credentials are not read, copied, deleted, or rolled back.

After a successful `/connect` or `/model` switch, refresh any cached
`ApplicationService` provider so the Hermes conversation and PCB planning use
the same provider/model immediately and after restart.

Interactive `/connect` and bare `/model` never run an `input()`/curses wizard
inside `HermesCLI.process_command`. They record one process-local request,
return control so prompt_toolkit fully restores the terminal, then run the
wizard on the outer main thread. That handoff must not execute Hermes'
process-global cleanup or arm its exit watchdog; final normal exit still does.
Consume and clear the pending request at every launch/exit boundary so normal
quit cannot spuriously reopen the terminal. Cancellation and sanitized wizard
errors return to a fresh terminal instance without recursive launch calls.

### 4. Validation & Error Matrix

| Condition | Required behavior |
|-----------|-------------------|
| No provider/model configured | `state=unconfigured`; direct the user to `pcbdraft connect`. |
| User cancels or wizard exits without committing config | Restore snapshots; `outcome=cancelled`, `state=cancelled`. |
| Expired OAuth/login-required evidence | `state=expired`; recommend `--reauthenticate`. |
| HTTP 401/403 or stable invalid-credential evidence | `state=invalid_credentials`, except explicit plan/tier denial. |
| Plan, tier, model, or endpoint unsupported evidence | `state=unsupported_endpoint`. |
| Timeout, connection, or network failure | `state=unreachable`; do not expose raw vendor text. |
| Other provider resolution failure | `state=unavailable` with a sanitized recovery action. |
| Non-positive timeout | Raise sanitized `PCBDraftError` before starting the wizard. |
| Timed wizard outside a safe POSIX main-thread signal context | Reject before vendor code starts; never interrupt an unrelated thread. |
| Symlinked private state path | Raise sanitized `PCBDraftError` before mutation. |
| Model response is malformed, oversized, truncated, or schema-invalid | Raise `ValidationError`; never bypass PCB domain validators. |

### 5. Good / Base / Bad Cases

- Good: an empty isolated home runs `pcbdraft connect`, persists a Hermes
  provider/model, refreshes the planning provider, and survives restart.
- Base: `connection_status()` on an empty home returns a sanitized
  `unconfigured` projection without contacting a provider.
- Bad: a wizard writes `auth.json` and `.env` and is then cancelled; both files
  must be restored byte-for-byte (or removed if absent before the wizard).
- Bad: the process inherits `HERMES_HOME=~/.hermes`; PCBDraft must overwrite it
  and leave the standalone sentinel unchanged.

### 6. Tests Required

- Assert picker identity parity with Hermes canonical/discovered, grouped,
  saved-custom, and manual-custom rows.
- Run all auth tests with `HOME`, `XDG_CONFIG_HOME`,
  `PCBDRAFT_HERMES_HOME`, and standalone sentinels below an isolated temporary
  directory; assert standalone state is unchanged.
- Assert modes `0700` for private directories and `0600` for config/auth
  files; reject symlinks.
- Mock success, cancellation after partial writes, timeout, failure taxonomy,
  forced reauthentication, and cache restoration.
- Assert `pcbdraft connect`, `/connect`, bare/target/provider `/model`, restart,
  and `ApplicationService` resolve one persisted identity.
- Mock representative API-key, OAuth/device, refresh, external-process,
  Responses, Anthropic, Bedrock, Vertex, Azure, aggregator, local, and custom
  transport boundaries without real credentials or network.
- Assert status, errors, traces, receipts, and project files contain no secret
  material.
- Build a wheel and assert Hermes auth/setup modules, provider plugins, and
  `vendor/hermes/LICENSE` are present.

### 7. Wrong vs Correct

#### Wrong

```python
# Accepts an unrelated standalone Hermes home and creates split authority.
home = Path(os.environ["HERMES_HOME"])
provider = LEGACY_PCBDRAFT_PROVIDERS[user_choice]
```

#### Correct

```python
home = hermes_home()  # PCBDRAFT_HERMES_HOME or PCBDraft config/hermes
os.environ["HERMES_HOME"] = str(home)
os.environ["HERMES_SHARED_AUTH_DIR"] = str(home / "shared")
providers = provider_identities()  # derived from the vendored Hermes registry
```

The adapter remains thin: reuse Hermes authentication, endpoint discovery,
refresh, model selection, and transport routing; keep PCBDraft ownership to
paths, entry points, rollback, sanitized status, and domain validation.
