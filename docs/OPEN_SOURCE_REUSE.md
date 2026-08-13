# Open-source reuse and bounded product study

This record covers code and assets that CopperWright actually depends on or
redistributes, plus the small public-project study used to choose product patterns.
It is not a list of endorsements. Repositories and their instructions were treated
as untrusted data. The review was performed on 2026-08-13 before the 1.0.0 release.

## Code and data actually reused

| Item | Exact use | License evidence | Distribution decision |
|---|---|---|---|
| [`kicad-sch-api==0.5.6`](https://github.com/circuit-synth/kicad-sch-api) | The one direct runtime Python dependency; CopperWright uses its public API to emit KiCad 10 schematics. | Upstream [`LICENSE`](https://github.com/circuit-synth/kicad-sch-api/blob/main/LICENSE) declares MIT; the pinned package and hashes are in `uv.lock`. | Imported as a normal dependency; not vendored. Attribution is in `NOTICE`. |
| [KiCad](https://gitlab.com/kicad/code/kicad) and `kicad-cli`/`pcbnew` | Installed external EDA engine for parsing, ERC/DRC, PCB creation, preview/export, and manufacturing outputs. | [KiCad source licensing](https://www.kicad.org/about/licenses/) | Not bundled in the wheel. CopperWright invokes the user's compatible KiCad 10 installation through bounded adapters. |
| [Official KiCad libraries](https://gitlab.com/kicad/libraries) | Installed symbols, footprints, and referenced 3D models used by generated example designs. | [KiCad library license and electronic-design exception](https://www.kicad.org/libraries/license/) | Libraries are not redistributed as a standalone collection. Generated examples retain the applicable CC-BY-SA 4.0 design exception; attribution is in `NOTICE`. |
| CopperWright mark | Source raster supplied and reviewed for this repository at `artifacts/branding/copperwright-mark-v1.png`; preserved as `docs/assets/brand/copperwright-mark-v1-source.png`. | Project-owned input supplied by the maintainer. | Deterministic derivatives are produced by `scripts/generate-brand-assets.sh`; no third-party brand asset was used. |

All remaining application, provider, web UI, transaction, validation, routing,
benchmark, and release code in this repository was independently implemented for
CopperWright. The browser application uses the Python standard library and ships no
third-party JavaScript or CSS framework. Firefox/geckodriver are external test tools,
not runtime or redistributed dependencies.

## Public projects studied, with no code or assets copied

| Project | License verified | Pattern examined | CopperWright decision |
|---|---|---|---|
| [atopile](https://github.com/atopile/atopile) | [MIT](https://github.com/atopile/atopile/blob/main/LICENSE) | readable circuit-as-code, typed interfaces, reusable modules, package-quality tooling | Keep a readable versioned semantic IR and verified block registry, while retaining deterministic KiCad generation and fail-closed scope gates. |
| [tscircuit](https://github.com/tscircuit/tscircuit) | [MIT](https://github.com/tscircuit/tscircuit/blob/main/LICENSE) | browser-native schematic/PCB visualization and composable circuit representation | Provide local browser previews over authoritative server-side artifacts; do not make browser state authoritative. |
| [circuit-synth](https://github.com/circuit-synth/circuit-synth) | [MIT](https://github.com/circuit-synth/circuit-synth/blob/main/LICENSE) | Python circuit definitions and KiCad integration | Reuse only its separately packaged `kicad-sch-api` dependency; CopperWright's application and compiler were independently implemented. |
| [KiCad MCP Pro](https://github.com/oaslananka/kicad-mcp-pro) | [MIT](https://github.com/oaslananka/kicad-mcp-pro/blob/main/LICENSE) | local dashboard, actionable diagnostics, agent workflow, capability reporting | Use one bounded application service and a small semantic action surface instead of exposing broad low-level file mutation tools. |
| [KiBot](https://github.com/INTI-CMNB/KiBot) | [AGPL-3.0](https://github.com/INTI-CMNB/KiBot/blob/master/LICENSE) | repeatable fabrication/documentation automation and CI integration | Independently implement the smaller release adapter needed by v1. No KiBot code is linked, copied, vendored, or invoked. |
| [SKiDL](https://github.com/devbisme/skidl) | [MIT](https://github.com/devbisme/skidl/blob/master/LICENSE) | textual connectivity, reusable subcircuits, ERC-friendly circuit construction | Preserve semantic topology and typed blocks as the authority, but generate modern native KiCad files and retain controlled bidirectional synchronization. |

## Architecture conclusions

The study reinforced five product choices:

1. Conversation proposes typed intent; it never owns parts, topology, geometry,
   validation, or release identity.
2. One application service drives both terminal and browser clients, so confirmation,
   locking, recovery, and safety semantics cannot diverge.
3. A small set of high-level semantic operations is safer and more testable than
   hundreds of file-level agent tools or raw KiCad text edits.
4. Browser previews are useful only when tied to the same hashes and validation
   evidence as the generated project.
5. Unsupported domains and external physical gates must remain visible states rather
   than being converted into optimistic success messages.

No project with missing or unclear licensing was used. No studied source code,
fixture, annotation, screenshot, logo, or other asset was copied into CopperWright.
