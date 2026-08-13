# CopperWright application and agent API reference

## Conversational application interface

`copperwright chat` is the stable product interface for terminals and automation.
Interactive commands cover `/new`, `/open`, `/projects`, `/status`, `/confirm`,
`/discard`, `/validate`, `/undo`, `/release`, and `/quit`. Noninteractive callers
can combine `--new` or `--project` with `--message`, `--yes`, `--undo`,
`--validate`, `--release`, `--list`, and `--json`. The JSON result is the same
public project projection used by the browser and includes proposal, authoritative
design identity, current transaction, artifacts, and structured events.

```bash
copperwright chat --workspace /tmp/copperwright-work \
  --provider builtin --new "Environment sensor" \
  --message "Create a BME280 SPI controller" --json
```

`copperwright app` serves the same `ApplicationService` on `127.0.0.1` by default.
Its `/api/*` routes and SSE stream are a versioned internal browser transport, not
a general remote API. The server refuses non-loopback binds and validates Host,
Origin, CSRF tokens, request sizes, project IDs, and artifact paths. It must not be
placed behind a public reverse proxy. Use the terminal JSON interface or the
newline JSON-RPC API below for external automation.

The supported Python seam is `pcb_agent.application.ApplicationService`. Methods
return plain JSON-compatible projections and enforce the same confirmation,
locking, provider, and transaction semantics. Callers must not edit the private
application workspace directly.

## Deterministic JSON-RPC interface

`copperwright api` serves newline-delimited JSON-RPC 2.0 over stdin/stdout. The
legacy `pcb-agent api` spelling is equivalent. The service has no
network listener and performs no authentication. The caller is responsible for
process isolation and filesystem permissions.

## Framing and errors

Each input line must be one JSON object with exactly `jsonrpc`, `id`, `method`,
and optional `params`. `jsonrpc` must be `"2.0"`; `params` must be an object.
Responses are one compact JSON line and preserve the request ID.

Limits are 4 MiB per request and 10,000 requests per process. Standard errors are
used for parse (`-32700`), invalid request (`-32600`), method (`-32601`), params
(`-32602`), and internal failures (`-32603`). Runtime domain errors use negative
codes below `-32000` and include the exception type, not a traceback.

Start every integration with:

```json
{"jsonrpc":"2.0","id":1,"method":"runtime.capabilities","params":{}}
```

The response declares API/runtime versions, methods, evidence states, exact
generation profiles, recognized-but-unimplemented domains, and rejected domains.

## Methods

| Method | Required params | Optional params | Effect |
|---|---|---|---|
| `runtime.capabilities` | none | none | read-only capability discovery |
| `parts.find` | none | `kind`, `function`, `min_voltage_v`, `active_only`, `trusted_only` | query canonical parts |
| `requirements.compile` | `requirements` object | none | compile IR without filesystem writes |
| `project.generate` | `requirements` object, `output` | none | create a managed KiCad project |
| `project.inspect` | `project` | none | return identity, files, and drift |
| `project.validate` | `project`, `output` | `timeout` | create an L0–L7 evidence run |
| `project.release` | `project`, `output` | `timeout` | create a candidate release |
| `release.verify` | `release` | none | offline release integrity verification |
| `sync.preview` | `project` | none | preview recognized native KiCad edits |
| `sync.apply` | `project` | `timeout` | preview, regenerate, validate, publish |
| `evidence.record` | `project`, `level`, `outcome`, `actor`, `role`, `performed_at`, `statement`, `artifacts`, `metadata` | none | copy/hash attributed L4 sourcing/fabricator, L6 review, or L7 physical evidence |
| `benchmark.run` | `output` | `repetitions`, `corpus`, `model_runs`, `model_timeout` | run the independent corpus |

Unknown parameters are errors. Write methods use create-only output paths except
for intentional transaction publication and external-evidence indexes, which are
lock-protected and hash checked.

## Example session

```bash
copperwright api <<'EOF'
{"jsonrpc":"2.0","id":"caps","method":"runtime.capabilities","params":{}}
{"jsonrpc":"2.0","id":"parts","method":"parts.find","params":{"function":"i2c_sda"}}
{"jsonrpc":"2.0","id":"inspect","method":"project.inspect","params":{"project":"/tmp/controller"}}
EOF
```

For a requirements compile request, pass the parsed JSON object, not a path. This
keeps the API transport deterministic and lets the caller own source retrieval.

## Stability

`api_version` follows major compatibility. New optional response fields or methods
may be added within major version 1. Request schemas remain exact so misspellings
cannot be silently ignored. IR, requirements, corpus, manifest, transaction,
evidence, validation, and release documents each carry their own schema/version.
