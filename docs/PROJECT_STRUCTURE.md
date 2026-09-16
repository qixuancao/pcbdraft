# PCBDraft project structure

This document is the placement guide for maintainers. The repository keeps a
small stable root, groups implementation by responsibility, mirrors those areas
in the test suite, and leaves executable developer workflows behind stable
scripts.

## Repository map

```text
pcbdraft/
├── src/pcbdraft/
│   ├── agent/          planning contracts, tool policy, ports, and durable turn orchestration
│   ├── core/           errors, safe I/O, redaction, locks, processes, runs, and project paths
│   ├── domain/         immutable PCB data and deterministic domain rules
│   ├── interfaces/     CLI, local Web API, launchers, and compatibility facades
│   │   ├── cli.py       subcommands, legacy bare launch, and `terminal` dispatch
│   │   ├── terminal.py    legacy terminal startup and model-wizard lifecycle
│   │   ├── terminal_launcher.py  source TypeScript client and loopback GUI lifecycle
│   │   ├── tui/           legacy Python terminal compatibility surface
│   │   ├── commands.py  pruned slash-command surface (PCB project commands)
│   │   └── gui.py         local Web API
│   ├── kicad/          native KiCad adapters and geometry algorithms
│   ├── model/          configuration, authentication, transports, and provider profiles
│   ├── tools/          tool registry, dispatch, and reusable tool implementations
│   ├── services/       application use cases and transactional orchestration
│   ├── verification/   evidence, gates, validation, BoardBench, benchmark, and release
│   └── data/           immutable bundled catalogs and benchmark corpus
├── clients/
│   └── terminal/       supported TypeScript terminal client
├── tests/              responsibility-mirrored unit and integration tests
├── scripts/            stable development, cleanup, E2E, BoardBench, and release entrypoints
└── docs/               architecture, API, development, and roadmap documentation
```

The Python package root contains only identity and bootstrap concerns:
`__init__.py`, `__main__.py`, and `_compat.py`. New implementation modules do
not belong there.

## Dependency responsibilities

### Core

`core` contains generic safety and runtime primitives, including text redaction
at every durable or presentation boundary. It must not depend on PCB domain, UI,
model, KiCad, service, or verification code.

### Domain

`domain` owns semantic PCB data and deterministic rules. It may depend on
`core`, but it must not perform model calls or render user interfaces.

### Agent, model, and KiCad adapters

`agent` owns constrained planning contracts and turn orchestration. `model`
owns configuration and structured external model calls. `kicad` owns native EDA
translation, inspection, layout, routing, preview, and synchronization. Raw
model output never crosses directly into the KiCad adapter.

The deterministic next-tool state machine lives in `agent.policy`; durable
records, permission checks, and fixed tool dispatch live in their own agent
modules. `agent.ports` defines the narrow structural contracts those modules and
the optional model router need from the application layer. They must not import
the concrete `ApplicationService`. This keeps workflow policy testable and
prevents the agent/model/service import cycle from returning when a new tool or
provider is added.

The circuit-plan boundary is split by role: `agent.plan` parses and versions
untrusted planner output, `agent.part_resolver` reads installed KiCad libraries,
`agent.review` evaluates engineering evidence, and `agent.compiler` lowers an
accepted plan to semantic IR. `agent.design` is a compatibility facade only;
new production code must import the narrow owner module. Provider validation and
configuration-path rules similarly live in `model.contracts`, below both the
model catalog and HTTP transport.

### Services and verification

`services` owns application use cases, write authority, jobs, managed projects,
and transactions. `ApplicationService`, `JobRunner`,
`ConversationOrchestrator`, and `AgentTurnStore` remain the authoritative
Python runtime for project state, execution, conversation turns, and durable
tool records. `services.gui_session` adapts that state for clients but persists
nothing itself; `services.gui_session_contract` defines the typed, versioned
reconnect payload shared by the local Web and TypeScript terminal clients.
`verification` evaluates persisted project evidence and owns candidate/release
decisions. Neither layer should contain presentation code.

### Interfaces

`interfaces` owns the `pcbdraft` CLI and the loopback GUI HTTP/SSE API. The GUI
API plus `services.gui_session_contract` is the sole supported protocol for
presentation clients; interfaces may format and validate requests, but they
must not duplicate engineering decisions or become an independent project,
job, or transcript store.

`clients/terminal` is the supported TypeScript TUI. It resolves the compact
slash-command surface, renders the trusted project transcript, and follows GUI
SSE lifecycle events. The event stream does not carry model token deltas. On a
terminal job event the client fetches the session contract again and displays
the saved assistant response.

`pcbdraft terminal` is a source-checkout launcher for that client. It starts a
GUI API bound to `127.0.0.1` when needed, or reuses a healthy PCBDraft GUI on the
selected loopback port. It rejects installed-only operation without
`clients/terminal` and rejects an occupied port that does not expose the exact
PCBDraft health document.

The former Python `prompt_toolkit` terminal remains a compatibility boundary.
`interfaces.tui.app` is the stable legacy facade and delegates to the isolated
implementation in `interfaces.tui.legacy_app`; a bare `pcbdraft` launch still
uses that compatibility path. New terminal features belong in
`clients/terminal` and use the GUI protocol rather than importing the legacy
implementation.

## Compatibility policy

PCBDraft 1.0 exposed implementation modules such as `pcbdraft.agent_design`,
`pcbdraft.kicad_pcb`, and `pcbdraft.validation`. `_compat.py` resolves those
historical names lazily to the exact canonical module objects, so imports and
monkeypatching continue to behave consistently. New code must import canonical
paths such as `pcbdraft.agent.design`, `pcbdraft.kicad.pcb`, and
`pcbdraft.verification.validation`.

The package-structure test enforces the allowed root files, canonical internal
imports, compatibility aliases, and the slim ``interfaces`` module set.

## Adding a module

1. Choose the directory that owns the behavior, not the caller that happens to
   need it first.
2. Keep reusable domain data out of interface and provider modules.
3. Add tests under the matching `tests/<area>/` directory.
4. Update this document only when a responsibility boundary changes.
5. Run `scripts/test.sh`; packaging changes should also run
   `scripts/release-check.sh`.

`scripts/clean.sh` is the single cleanup entrypoint for generated Python build
products. Package discovery is explicitly limited to `pcbdraft*`, and release
checks clean setuptools' persistent build directory before and after building.

## Runtime source ownership

`agent.loop` owns the reused conversation engine; `agent.tool_bindings` binds its
tools to PCB authority; `agent.observability` contains built-in trace and write
constraints. `agent.extensions` manages optional extensions. `model` owns provider
authentication and wire protocols, and `services.session_db` owns transcripts.
They are ordinary packaged modules, imported by their `pcbdraft.*` names in both
editable installs and wheels. There is no separately injected runtime source tree.
Third-party provenance and license text remain in `NOTICE` and `data/licenses`.
