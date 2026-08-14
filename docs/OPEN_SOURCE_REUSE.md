# Open-source reuse and attribution record

This file records the upstream projects and public material examined while
building CopperWright's generic Agent-safe KiCad runtime. It is a design and
license record, not a claim that another project's code was copied.

## Runtime dependencies and external tools

| Item | Use | License / attribution treatment |
|---|---|---|
| [kicad-sch-api](https://github.com/circuit-synth/kicad-sch-api) 0.5.6 | native KiCad schematic construction adapter | separately distributed MIT dependency; named in NOTICE and package metadata |
| [KiCad](https://www.kicad.org/) | user-installed schematic/PCB editor, library data, geometry API, checks, and manufacturing backend | not bundled as an application dependency artifact; users install a compatible KiCad 10 distribution |
| [Official KiCad libraries](https://gitlab.com/kicad/libraries) | symbols, footprints, and models resolved from the user's local installation | not redistributed as a standalone collection; generated designs retain the KiCad library license/design exception |

## Public projects studied

| Project | Revision/license checked | What was taken from it | CopperWright implementation |
|---|---|---|---|
| [atopile](https://github.com/atopile/atopile) | commit [619eda7](https://github.com/atopile/atopile/tree/619eda7f777558a3e500dbad9cc2941712881495), [MIT](https://github.com/atopile/atopile/blob/619eda7f777558a3e500dbad9cc2941712881495/LICENSE) | declarative circuit intent, typed interfaces, reusable composition, and constraints as a layer above KiCad | generic request/plan schema and semantic IR keep design intent distinct from KiCad output |
| [tscircuit](https://github.com/tscircuit/tscircuit) | commit [5587ef5](https://github.com/tscircuit/tscircuit/tree/5587ef58af046200e7d534ac07eb9a873e452533), [MIT](https://github.com/tscircuit/tscircuit/blob/5587ef58af046200e7d534ac07eb9a873e452533/LICENSE) | separation between circuit description, PCB representation, rendering, and solver work | planner returns components/pins/nets only; deterministic runtime owns placement/routing and UI reads authoritative project state |
| [circuit-synth](https://github.com/circuit-synth/circuit-synth) | commit [3aaff18](https://github.com/circuit-synth/circuit-synth/tree/3aaff18c056de7cbe8f5b0a3e1e6e7e7895f544e), [MIT](https://github.com/circuit-synth/circuit-synth/blob/3aaff18c056de7cbe8f5b0a3e1e6e7e7895f544e/LICENSE) | Python circuit composition and KiCad integration direction | project staging/managed generation remains CopperWright code; the separately packaged kicad-sch-api dependency is used for schematic emission |
| [Pi / π](https://github.com/earendil-works/pi) | [MIT](https://github.com/earendil-works/pi/blob/main/LICENSE) | small agent core with clear provider/session/CLI boundaries | CopperWright keeps terminal/browser clients thin and routes all stateful engineering work through one application service |
| [KiCad MCP Pro](https://github.com/oaslananka/kicad-mcp-pro) | [MIT](https://github.com/oaslananka/kicad-mcp-pro/blob/main/LICENSE) | the need for guided workflows, diagnostics, and transactional behavior rather than a giant raw tool list | public interfaces expose high-level request/plan/validation operations; raw KiCad mutation is not model-controlled |
| [SKiDL](https://github.com/devbisme/skidl) | [MIT](https://github.com/devbisme/skidl/blob/master/LICENSE) | textual connectivity and reusable circuit composition | semantic connectivity remains authoritative before native KiCad emission |

The repositories above were inspected on 2026-08-13. No source files, tests,
fixtures, screenshots, logos, datasets, or design files from those projects are
vendored into CopperWright. The implementation uses compatible architectural
patterns, not copy-pasted source. Any future direct code reuse must name the
files and preserve every required license notice in this table and NOTICE.

## Design conclusions applied here

1. AI proposes a bounded semantic plan; it does not author raw KiCad or board
   geometry.
2. KiCad remains the editor, geometry, rule-checking, and manufacturing backend.
3. Component identity comes from an auditable project-local graph; a local
   library extraction is useful but provisional.
4. Generation is transactional: failures preserve useful artifacts and do not
   overwrite an authoritative project.
5. A browser or terminal is an interaction shell, not a second engineering
   implementation.
6. ERC/DRC results are evidence, not a functional or manufacturing sign-off.

## Public research and vendor material

The repository's design rationale follows the local research report
<code>ai_agent_pcb_design_analysis.md</code>. That local working document
discusses pcbGPT, OmniLayout, SchGen, KiCad's IPC/API work, and vendor
documentation as background. No paper text, vendor reference design file, or
third-party PCB layout has been copied into the runtime.

Curated factual part records must retain their own provenance links in the part
catalog. A provenance link is not permission to redistribute a datasheet, library,
or reference design.
