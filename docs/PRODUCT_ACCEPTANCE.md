# CopperWright product acceptance record

This is the acceptance record for the current generic Agent-safe runtime. It is
not a marketing claim that CopperWright can autonomously create any production
PCB. “Verified” below means a repository test or a real local KiCad check exists;
it does not mean a board has been manufactured or approved for production.

## Current product boundary

| Capability | Evidence | Status |
|---|---|---|
| Generic conversational request record | <code>AgentDesignRequest</code>, application conversation tests | implemented and regression-tested |
| Named-part preservation | plan compilation rejects a plan that drops a user-named part | implemented and regression-tested |
| Local KiCad component resolution | resolver scans installed <code>.kicad_sym</code> files and probes real symbols/pins/default footprints | implemented and regression-tested |
| Model-safe circuit planning | strict plan schema accepts components, symbols, pins, nets, assumptions, and notes; rejects raw geometry/KiCad/code | implemented and regression-tested |
| Project-local component graph | extracted parts, provenance, and trust state are written beside the request/IR | implemented and regression-tested |
| Generic-plan provenance | a successful generic managed project tracks the reviewed <code>circuit-plan.json</code>; its hash must match the request/IR provenance | implemented and regression-tested |
| Deterministic topology preflight | compile results expose power-input, rail-source, applicable I2C pull-up, decoupling, and board-rule evidence | implemented and regression-tested; findings do not deny a generation attempt |
| Generic managed review context | later <code>review</code> receives the parsed request/plan, project-local part trust states, IR, and native evidence; it does not invent verified blocks | implemented and regression-tested |
| Explicit confirmation | application stores a reviewed plan before any native-generation side effect | implemented and regression-tested |
| Native generic schematic | generic STM32F405/SHT31 plan creates a native KiCad schematic in the test environment | verified locally by <code>tests.test_agent_design</code> |
| Stock-KiCad basic board | connector/capacitor/resistor/LED example produces a native routed project | real KiCad 10 ERC/DRC regression-tested |
| Bounded generic PCB attempt | compiler/placement/router runs after schematic generation; failures are concrete errors, not substitutions | implemented; success remains plan-dependent |
| Failure retention | application attempt stores request, plan, IR, part graph, available native staging, phase, and sanitized error | implemented and regression-tested |
| Detailed evidence state | local-library records remain explicitly unverified in detailed reports without blocking generation | implemented and regression-tested |
| ERC/DRC | runs when native schematic/PCB generation succeeds and KiCad CLI is available | implemented; not proof of functional correctness |
| Complex-domain requests | domain classification produces warnings but does not block planning or generation | implemented and regression-tested |

## Deliberately not claimed

- automatic success for arbitrary components or arbitrary board topologies;
- manufacturer-verified MPN, sourcing, footprint suitability, or layout
  constraints from a generic KiCad library symbol;
- electrical functional correctness from ERC/DRC alone;
- SI/PI, thermal, EMI/EMC, tolerance, startup, or manufacturing qualification;
- production or safety sign-off; or
- domain-specific electrical, regulatory, RF, thermal, medical, or aviation
  validation unless a named tool actually produced that evidence.

## Acceptance workflow

1. Request a board in chat or the local browser.
2. Check that every named part is present in the reviewed plan and BOM identity.
3. Check the local symbol candidates, selected pins, nets, assumptions, board
   envelope, warnings, and every <code>plan_review</code> finding. A
   preflight failure means missing topology evidence to fix or knowingly test,
   not that a normal named part is unsupported.
4. Confirm generation explicitly.
5. If it fails, inspect the project attempt directory; do not replace the plan
   with an unrelated demo board.
6. If it generates, run validation and add independent L4–L7 evidence before
   considering any release statement.

## Historical deterministic fixtures

The repository's older requirements compiler and sample projects remain useful
for regression tests of KiCad generation, routing, validation, synchronization,
and the error-injection corpus. They are not product profiles and are not the
generic conversational path. Their historical reports may remain in the tree,
but they must not be used to claim that a fixed RP2040/TMP117 or other
hard-coded composition is a current product feature.
