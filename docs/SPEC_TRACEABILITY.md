# Specification traceability

This table maps the local design analysis
<code>ai_agent_pcb_design_analysis.md</code> to the current implementation. The
report is intentionally broader than the present runtime; rows say when a
requirement is only a direction rather than a completed capability.

| Report requirement | Current implementation | Evidence | Status |
|---|---|---|---|
| KiCad remains the EDA/geometry/manufacturing backend | native schematic/PCB adapters and managed KiCad projects | KiCad integration tests and managed-project validation | implemented |
| Semantic IR rather than model-authored KiCad text | immutable <code>Design</code> IR plus generic request/plan schemas | IR, API, and agent-plan tests | implemented |
| No fixed board profile as the AI product interface | generic <code>AgentDesignRequest</code> and <code>CircuitPlan</code>; application no longer selects an RP2040/TMP117 path | application and agent-plan tests | implemented |
| Preserve named component identity | exact requested names must match a plan component; no substitution code path | negative plan-identity test | implemented |
| Trusted component identity/provenance | <code>PartGraph</code> with curated and project-local extracted records, trust state and provenance | part graph/resolver tests | implemented; generic records remain provisional |
| Local library mapping | installed KiCad symbol scan/probe supplies symbols, pins, and default footprints | <code>symbols.find</code> and resolver tests | implemented |
| Transaction-safe generation | staging, atomic publish, locks, snapshots, recovery, and explicit confirmation | managed/application tests | implemented |
| High-level agent API | request/plan compile/generate APIs and symbol discovery; model cannot write raw KiCad/geometry | JSON-RPC and schema tests | implemented |
| Placement/routing by deterministic code | existing bounded placement/router backend, not model output | KiCad-generation tests | implemented; generic routing success is not guaranteed |
| L0–L7 evidence gates | validation report with completed/unavailable/heuristic/human-required states | validation tests | implemented |
| ERC/DRC are not a functional sign-off | release gates and provisional generic guard | validation/release tests and acceptance record | implemented |
| Retain failure evidence | application attempt record preserves request/plan/IR/part graph/native staging/error | application failure-retention test | implemented |
| Reviewer and safe patcher MVP | review/patch/transaction commands remain available | workflow and transaction tests | retained |
| Full functional rule library | only generic structural checks plus existing fixture rules | no universal electrical-rule claim | partial |
| Datasheet/MPN/footprint qualification | curated records can carry evidence; generic local extraction does not claim it | trust model and L0 provisional guard | partial |
| SI/PI, thermal, EMI, tolerance and startup analysis | external evidence model; no universal solver | honest unavailable/human-required states | not implemented as automated generic capability |
| Full autonomous layout/routing for arbitrary boards | bounded router only | failures retained; no general success claim | not implemented |
| L6 human review / L7 physical feedback | attributed external evidence recording | evidence-record API | implemented as evidence capture, not supplied evidence |
| Open error-injection benchmark | independent CC0 corpus and benchmark runner | benchmark tests/scripts | implemented |

## Reading this table

“Implemented” means the code and a matching test exist. “Partial” means the
data model/transaction/gate exists but cannot honestly make the universal
engineering claim in the report. “Not implemented” is a deliberate statement of
scope, not a conversion of a failure into a success label.

The correct roadmap is:

    reviewer/safe patcher
      → generic requirements to reviewed circuit plan
      → local part resolution and semantic IR
      → native schematic/PCB attempts with retention
      → richer functional rules, solver feedback, and independently verified
        part/block knowledge
      → expanded evidence and physical feedback
