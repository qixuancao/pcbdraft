# PCBDraft product acceptance record

This is the acceptance record for the current generic Agent-safe runtime. It is
not a marketing claim that PCBDraft can autonomously create any production
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
| Component qualification evidence | every generic project retains a versioned report that checks installed symbols and exact native footprint pad numbers while classifying KiCad datasheet locators and planner identity claims as unverified | implemented, managed-hash checked, and real-KiCad regression-tested |
| Deterministic topology preflight | compile results expose power coverage/polarity, plausible sources, output contention, passive shorts, LED polarity, per-line I2C pull-ups, decoupling, and board-rule evidence | implemented and regression-tested; findings do not deny a generation attempt |
| Generic managed review context | later <code>review</code> receives the parsed request/plan, project-local part trust states, IR, and native evidence; it does not invent verified blocks | implemented and regression-tested |
| Durable non-blocking terminal turn | <code>AgentRuntime</code> submits persisted jobs, rejects overlapping turns, and lets the TUI continue polling input and activity | implemented and regression-tested |
| UI-neutral tool activity | project events map to requirement, planning, generation, preview, and validation activities outside curses | implemented and regression-tested |
| Terminal recovery and review | a non-secret last-project pointer restores persisted history without replay; review/log overlays expose the retained plan, semantic diff, validation and activity, and retry is explicit | unit-tested and exercised by the real PTY restart flow |
| Safe-boundary stop | Esc or <code>/stop</code> requests cancellation before the next stateful PCB operation | implemented and regression-tested |
| Transactional generation boundary | application stores a reviewable plan before any native-generation side effect; the default terminal advances it automatically and manual clients may confirm explicitly | implemented and regression-tested |
| Bounded automatic repair | generation errors and completed deterministic L1-L3 failures can produce at most two full replacement plans through the same resolver/compiler; unknown/reference-only/human evidence does not trigger repair; existing projects use staged validation, atomic apply, and exact undo | implemented and regression-tested |
| Replaceable DeepSeek Harness planner | native Python clients can select <code>deepseek-harness</code>; a versioned stdin/stdout bridge bounds time/output, keeps prompts out of argv, validates correlation/errors, and sends results through the normal PCBDraft schemas | contract-tested with all three provider operations, malformed output, timeout, and provider errors |
| DeepSeek Harness host plugin | DSH can host the conversation with only prepare/search/generate tools; it checks API capabilities, confines output, retains failed attempts, and exposes repair attempts 0–2 | standalone Node tests, real DSH tool-runtime contract, real PCBDraft/KiCad smoke test |
| Real terminal product flow | PTY-driven curses session submits natural language, renders requirement/plan/generation/validation/release tools, opens review/expanded logs, generates and releases a real KiCad project, exits cleanly, and resumes the released project after restart | <code>scripts/tui-e2e.py</code> |
| Real browser product flow | clean-HOME Firefox session uses a local OpenAI-compatible planner, reviews a generic plan, loads real previews, validates/releases, reopens after server restart, and shows complex-domain warnings without rejection | <code>scripts/browser-e2e.py</code> |
| Real parameterized chat flow | local OpenAI-compatible planner produces a generic reviewed plan with internal layer selection; native generation, L0–L7 evidence, release verification, and reopen are checked | <code>scripts/chat-e2e.sh</code> |
| Native generic fine-pitch attempt | generic STM32F405/SHT31 plan creates a native KiCad schematic and completes its four declared routes; electrical preflight/ERC/parity still reject the intentionally incomplete topology | verified locally by <code>tests.test_agent_design</code> |
| Stock-KiCad candidate examples | LED indicator, passive RC filter, and I2C pull-up adapter produce routed native projects and reach the candidate gate while remaining non-production | real KiCad 10 ERC/DRC/parity and L0–L7 regression-tested |
| Bounded generic PCB attempt | exact-geometry fine-pitch escape feeds a bounded grid router; failures report unrouted nets, search count, and pad-level causes before any reference-plane work | implemented; success remains plan-dependent |
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

1. Request a board in the full-screen terminal, parameterized chat, or local browser.
2. Check that every named part is present in the reviewed plan and BOM identity.
3. Check the local symbol candidates, selected pins, nets, assumptions, board
   envelope, warnings, and every <code>plan_review</code> finding. A
   preflight failure means missing topology evidence to fix or knowingly test,
   not that a normal named part is unsupported.
4. In the default terminal, observe the streamed tool activity while generation
   proceeds automatically. Use Esc or <code>/stop</code> before the next tool if
   the plan must not proceed. In a manually staged client, confirm explicitly.
5. If it fails, inspect the retained attempt/transaction evidence. The autonomous
   runtime may try at most two schema-constrained replacement plans; it must not
   replace the request with an unrelated demo board or overwrite an existing design
   before the candidate gates complete.
6. If it generates, run validation and add independent L4–L7 evidence before
   considering any release statement.

## Historical deterministic fixtures

The repository's older requirements compiler and sample projects remain useful
for regression tests of KiCad generation, routing, validation, synchronization,
and the error-injection corpus. They are not product profiles and are not the
generic conversational path. Their historical reports may remain in the tree,
but they must not be used to claim that a fixed RP2040/TMP117 or other
hard-coded composition is a current product feature.
