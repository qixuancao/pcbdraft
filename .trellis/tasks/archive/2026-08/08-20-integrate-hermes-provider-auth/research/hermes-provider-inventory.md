# Hermes provider integration inventory

## Current PCBDraft boundary

- `src/pcbdraft/interfaces/commands.py:136-155` implements `/connect` as a
  read-only status command backed by PCBDraft TOML.
- `src/pcbdraft/model/config.py` persists only literal OpenAI-compatible
  `api_key`/`base_url`/model records.
- `src/pcbdraft/model/hermes_config.py:19-66` rewrites Hermes `config.yaml` on
  every launch and collapses a PCBDraft connection to `provider: custom`.
- `src/pcbdraft/services/application.py:215-230` resolves the PCB planning
  provider from `pcbdraft.model.providers`, so connecting Hermes alone would
  leave PCB tool calls unconfigured.

## Hermes sources of truth

- `vendor/hermes/hermes_cli/models.py:1154-1213` owns the canonical picker:
  38 static providers plus automatic injection of bundled API-key provider
  plugins. An isolated runtime probe of this vendored snapshot produces 46
  base picker entries, including the manual `custom` row; saved custom
  providers may add more. It also owns display grouping and custom provider
  entries.
- `vendor/hermes/hermes_cli/main.py:3519-3900` owns the shared provider/model
  wizard used by setup and model switching.
- `vendor/hermes/hermes_cli/auth.py:249-550` has 36 registered authentication
  definitions covering API keys, OAuth/device code, external processes,
  Bedrock, Vertex, and Azure-specific behavior.
- `vendor/hermes/plugins/model-providers/` has 36 bundled provider profile
  directories; `vendor/hermes/providers/__init__.py` discovers them and lets
  future bundled/user plugins extend the registry.
- `vendor/hermes/hermes_cli/runtime_provider.py:1724+` resolves active provider,
  credential, endpoint, model, and transport for runtime use.
- `vendor/hermes/agent/auxiliary_client.py:6075+` normalizes provider clients
  behind an OpenAI-shaped `chat.completions` interface, including Codex
  Responses, Anthropic Messages, Bedrock, Gemini, Copilot ACP, and custom
  endpoints; `call_llm()` is the provider-aware request chokepoint.
- `vendor/hermes/tools/lazy_deps.py` and provider adapters preserve Hermes'
  lazy optional dependency behavior for Anthropic, Bedrock, Vertex, and Azure.

## Packaging and license

- `setup.py` copies the complete trimmed `vendor/hermes` runtime, including
  provider plugins, into `pcbdraft/data/vendor/hermes` during wheel builds.
- `MANIFEST.in` includes the vendored Python/YAML/JSON assets in the sdist.
- Hermes is MIT licensed (`vendor/hermes/LICENSE`); the notice must remain in
  the shipped vendored tree.

## Planning conclusion

PCBDraft must adapt the Hermes registries and request boundary directly. A
second copied provider catalog or a second OAuth implementation would violate
the user's "Hermes has it, PCBDraft gets it" requirement because it would
immediately drift when the vendored snapshot changes.

The user explicitly rejected migration and compatibility code. The product
replacement deletes the old PCBDraft provider TOML/preset authority and starts
from a fresh Hermes configuration path.

The user also clarified that any independently installed Hermes on the
workstation is unrelated and strictly out of scope. PCBDraft uses only
`vendor/hermes`, with PCBDraft-owned configuration paths and wording.
