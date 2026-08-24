# Build verifiable convergent BoardBench execution

## Goal

在不更换 `openai-codex / gpt-5.6-luna`、不扩大 BoardBench Tier A 题库、
不放宽 KiCad ERC/DRC 规则、也不单纯提高模型迭代上限的前提下，为 PCBDraft
建立一套可验证、可收敛、可终止的工程执行协议。

下一轮固定 20×3 BoardBench 对照运行必须能够验证：仅通过强化 KiCad 修改事务、
Router 正确性与失败分类、Agent 进展控制、预算语义、工具上下文和评分校准，
是否能彻底消除已提交 revision 的语义—原生不一致，将 DRC 通过数从 20/60
提高到至少 40/60，并将模型请求中位数从约 79 降低至少 50%，同时不让
AI 审查的原理图功能正确数相对当前 50/60 明显回退。

## Background and Confirmed Facts

- 当前 60-run AI pilot 的漏斗为：AI 审查原理图功能正确 50/60、ERC 通过
  34/60、DRC 通过 20/60、自动最终通过 4/60。14 个 case 的三次原理图均被
  认为功能正确；11 个 case 三次 DRC 全部失败；4 个 case 三次 DRC 全部通过。
- AI 审查给出的首要失败阶段为：routing 30、layout 9、validation 9、
  circuit design 7、KiCad materialization 3、none 2。这些是 AI 判断而非人类签字，
  但足以定位下一轮工程优化方向。
- `ApplicationService.apply_pcb_operation()` 已在 transaction 目录中 staging、
  重新物化、交换 live design，并在发布记录失败时回滚
  （`src/pcbdraft/services/application.py:1353`）。
- `materialize_managed_design()` 已生成原生 snapshot 和 manifest
  （`src/pcbdraft/services/managed.py:215`），但
  `ManagedProject.assert_synchronized()` 主要检查文件 hash 与 IR semantic hash，
  不证明原生 net graph、铜线、端点连通性与 IR 意图一致
  （`src/pcbdraft/services/managed.py:140`）。
- `pcb_route_net` 会先生成 routing probe，再把生成铜线提升进 `native_intent` 并
  二次物化；它只检查目标 net 不在 `unrouted` 且多端点 net 至少有 segment，尚未
  建立完整的原生端点连通、错误 net merge 和 DRC 后置条件
  （`src/pcbdraft/services/application.py:1423`）。
- 60 次运行中 `pcb_route_net` 调用 417 次，185 次失败；至少 39 次是
  `seed segments must have non-zero length`。原始评审还发现成功状态下原生零走线、
  missing connection、错误 net merge 或陈旧铜线等问题。
- Agent 当前固定“一次 provider response 最多执行一个 `pcb_*` 工具”
  （`src/pcbdraft/interfaces/hermes_plugin.py:78`）；60 次运行有 234 个工具调用因此
  被阻止。18 次运行恰好达到 90 个 model request，其中 16 次 trace 明确记录
  `max_iterations_reached(90/90)`，18 次自动结果全部失败。
- Campaign manifest 记录 `tool_call_budget=500`，但实际先触发的常常是 Hermes 的
  90 次模型迭代上限。Runner 只要 worker return code 为 0、有 final response 且
  保留一个工程，就写入 `status=completed, termination_reason=agent_returned`
  （`src/pcbdraft/verification/boardbench_runner.py:917`），因此进程正常退出、PCB
  任务结果和终止原因目前没有分离。
- 模型工具面包含 57 个工具。工具结果中位大小约 3.8 KB，4,664 个结果累计约
  16.65 MB；60 runs 合计约 2.803 亿 token，其中约 96.2% 为 cache-read。
  `hermes_tools._model_summary()` 当前会重复附带工程路径、文件表、readiness 和近期
  events（`src/pcbdraft/agent/hermes_tools.py:220`）。
- 当前 pilot corpus 是 BoardBench Tier A：prompt 已给出精确器件、part ID、封装、
  BOM 和主要连接。它不能证明自主选型、复杂电源或高速设计能力。本任务先修好 Tier A
  闭环，不扩展 Tier B/C/D。
- 已封存的旧 BoardBench campaign、run、score 和 AI review 是不可变历史证据；
  新 schema/evaluator 必须版本化，不能就地重写旧结果。

## Requirements

### VBE-R1 — 冻结范围与对照变量

- 本任务的唯一产品目标是可验证、可收敛、可终止的 PCB 工程执行协议；暂停新模型
  provider、模型横评、新 Agent 子框架、安装器/UI 横向功能、Tier B/C/D 题库扩展和
  专用模型训练。
- 软件改进完成后的对照 campaign 使用同一 `gpt-5.6-luna`、同一 20×3 Tier A corpus、
  同一 KiCad 10.0.5 环境、同一 ERC/DRC 规则和冻结预算。任何不可避免的环境漂移必须
  单独披露，不能混入因果结论。
- 旧 pilot 是只读基线；新结果使用新 campaign id 和新 evaluator/schema version。

### VBE-R2 — 带语义后置条件的原生写入事务

- 在现有 staging/swap/rollback 基础上，为所有修改型 PCB 工具建立统一的
  `execute temporary revision → reopen native KiCad → verify postconditions → commit or rollback`
  框架。
- 所有写操作始终验证以下通用不变量：原生文件可重新解析；预期原生变化真实发生；
  未出现非预期 net merge/split；原生投影与候选 IR 的相关意图一致；失败时 live design、
  project records 和 revision 均不改变。
- 后置条件按操作与阶段区分。早期原理图编辑允许工程仍不完整；不能把“整板 DRC 已
  通过”作为所有写操作的通用条件。route、move、rotate、via 等物理操作必须验证各自
  影响范围内的原生连通性和新增严重 DRC 差异。
- 成功 receipt 必须包含有界的 intended/native delta、后置条件结果和 revision；失败
  receipt 必须包含稳定 error code、rollback 状态和 artifact handle。重复错误判断可以保存
  受影响对象的结构化 retry key，但不新增与功能验证无关的加密 hash 审计体系。
- 完整原生 snapshot、路径、历史事件和调试详情留在 transaction artifact，不默认进入
  模型上下文。

### VBE-R3 — Router 正确性与结构化失败

- `pcb_route_net` 只有在原生 KiCad 中存在目标 net 的真实非零铜线、指定端点原生电气
  连通、无非预期 net merge 且 route postconditions 通过时，才能返回成功。
- 修复或 fail-closed 捕获当前已知类别：`invalid_seed`、`zero_length_seed`、
  `pad_escape_blocked`、`no_legal_channel`、`congestion_exhausted`、
  `search_budget_exhausted`、`native_commit_failed`、
  `native_connectivity_failed`、`unintended_net_merge`。
- Router 失败至少返回失败 net/端点、错误指纹、节点展开数、阻塞摘要，以及可用的
  策略建议（移动器件、换层、改变网络顺序或确定性 blocked）；不再只返回不可聚合的
  自由文本。
- 为零长度 seed、pad escape、移动 footprint 后陈旧铜线、网络合并、原生零走线和
  各结构化失败类别建立最小确定性回归，不使用 60 次真实模型 campaign 作为日常复现器。

### VBE-R4 — 进展函数与 Agent 收敛协议

- 每个 revision 计算并保留至少以下进展向量：
  `semantic_native_mismatch_count`、`unresolved_connection_count`、
  `fatal_drc_count`、`error_drc_count`、`erc_error_count`、
  `unplaced_component_count`、`routing_failure_count`。
- 每个修改 receipt 显示向量前后差异。工具调用成功但向量未改善或恶化时，不得被描述
  为工程进展。
- Agent 使用有硬门的阶段状态：需求冻结、原理图语义、原生原理图确认、封装/网络同步、
  布局、布线、原生连通确认、ERC/DRC、发布门。阶段约束完成声明，不强制死板的单一路径。
- 加入稳定错误指纹、无进展停止、重复错误停止和策略切换。重复 route 失败不能只触发
  相同参数重试；允许转向相关器件重摆、层/顺序调整或明确 blocked。
- 一次模型决策最终可执行一个有边界、可回滚、可验证的语义事务；只读查询可批量。
  批量写入只能在 VBE-R2 的后置条件框架稳定后开放。

### VBE-R5 — 预算、终止和任务结果语义

- BoardBench receipt 分离 `process_status`、`task_outcome`、
  `termination_reason`、`stage_reached` 和 `release_gate_passed`；正常退出但未通过发布门的
  run 不能被解释为 PCB 任务完成。
- 预算变为有名字的向量，至少记录 model turns、PCB tool calls、route attempts、route
  node expansions、tokens 和 wall time 的 limit/consumed。
- 任一预算耗尽必须产生 `budget_exhausted:<dimension>`；无进展和主动提前返回必须分别
  产生 `no_progress` 与 `agent_returned_before_gate`。
- 新 loader/report 兼容读取旧 v1 evidence，但不就地迁移或重写旧 immutable artifacts。

### VBE-R6 — 精简模型工具上下文

- 默认工具结果目标中位大小不超过约 1 KB；只返回当前阶段、revision、必要状态差异、
  稳定错误码、简短摘要和 artifact handle。
- 文件路径只在首次绑定或明确 inspect 时返回；历史 events、完整文件表和完整 snapshot
  不随每个工具结果重复发送。
- 支持按阶段暴露相关工具或等价的 schema 缩减机制，避免每轮无条件携带全部 57 个工具
  的完整 schema；不得因此削弱权限和 closed-toolbox 边界。
- 分别记录成本指标与上下文质量指标，包括非缓存输入/输出、实际成本状态、活动上下文
  长度、重复比例和每轮新增 token。

### VBE-R7 — BoardBench 三层评分与等价关系

- 将单一总结果拆成：`design_intent`、`native_artifact`、`delivery_readiness`。每层保留
  pass/fail/unknown 和来源；缺少人类/实物证据时 delivery readiness 保持 unknown。
- 自动 topology evaluator 支持对称二端器件端点交换、串联拓扑等价、同型号连接器的
  合法 slot permutation、合法 no-connect 表达；prompt 未规定的 pin order 不得成为
  隐藏硬条件。
- 失败记录同时保存 `first_blocking_stage`、`terminal_stage`、多个 `root_causes` 和
  多个 `symptoms`，避免把最终 DRC 症状误当作唯一根因。
- 校准 evaluator 不得放宽真实 KiCad ERC/DRC，也不得把 AI review 冒充人类工程签字。

### VBE-R8 — 固定对照 campaign 与发布判据

- 在实现和确定性回归通过后，经操作者再次明确授权，运行完整 20×3 真实模型对照
  campaign；所有计划 run 保留在分母，禁止静默替换失败或超时。
- 对照报告必须直接比较旧、新 campaign 的 DRC、ERC、三层评分、模型请求、工具调用、
  token、终止原因、错误类别和 task-level 三次稳定性。
- 必须公开区分“因 evaluator 校准改变的指标”和“由工具/Agent 改进改变的原生 KiCad
  指标”；核心因果判据以原生 DRC、原生一致性和固定预算效率为主。

### VBE-R9 — 实施与验证纪律

- 优先顺序固定为：原生事务/postconditions → Router 正确性与错误分类 → 进展/预算/终止
  协议 → 有界语义事务与上下文压缩 → evaluator 校准 → 固定对照 campaign。
- 不先取消单动作保护后补一致性；不通过提高迭代上限、换模型、删除困难题、修改答案或
  放宽 DRC 来提高数字。
- 普通实现迭代遵守仓库约 90 秒快速验证规则；完整 BoardBench campaign、全量测试和
  release check 只在对应集成/发布门执行。

## Acceptance Criteria

- [ ] VBE-AC1：所有已提交的修改型工具 revision 均有原生 postcondition receipt；故意
  注入原生解析、连通性、net merge、receipt/event 写入失败时，live design 和 revision
  保持不变。（VBE-R2）
- [ ] VBE-AC2：确定性回归中不存在“工具返回 routed 但原生零走线、目标端点未连通或
  网络被错误合并”的成功状态；已知 mismatch fixture 全部 fail closed。（VBE-R2、R3）
- [ ] VBE-AC3：Router 的九类失败拥有稳定机器码和最小回归；60-run 报告能够按失败码、
  端点和阶段聚合，而不依赖自由文本解析。（VBE-R3）
- [ ] VBE-AC4：每个修改 receipt 都包含进展向量 delta；连续无进展、重复错误和策略切换
  有确定性状态机测试，Agent 不能在发布门前声明 task passed。（VBE-R4）
- [ ] VBE-AC5：run schema 能区分进程、任务结果和终止原因；max-iteration trace 被记录为
  `task_outcome=incomplete` 与 `budget_exhausted:model_turns`，旧 v1 evidence 仍可读取且
  不被改写。（VBE-R5）
- [ ] VBE-AC6：代表性 BoardBench 工具 trace 的结果中位大小不超过约 1 KB；默认不重复
  发送完整路径/文件表/events；上下文与成本指标分开汇总。（VBE-R6）
- [ ] VBE-AC7：评分器分别输出 design intent、native artifact、delivery readiness；对称
  电阻、等价串联、连接器 permutation、no-connect 与未规定 pin order 的回归通过，且
  KiCad ERC/DRC 规则未被放宽。（VBE-R7）
- [ ] VBE-AC8：在冻结对照条件下完成新的 60-run campaign，DRC 至少 40/60，达到 90 次
  model-turn 上限不超过 3/60，模型请求中位数相对约 79 至少下降 50%，已提交 revision
  的语义—原生 mismatch 为 0。（VBE-R1、R8）
- [ ] VBE-AC9：使用冻结的同一 AI 审查 rubric 时，新 campaign 的原理图功能正确数至少
  保持 50/60；如果仍无人类工程师，结果明确保持 AI-reviewed pilot，不能声称
  order-ready 或实物成功。
  （VBE-R7、R8）
- [ ] VBE-AC10：对照报告能够把 evaluator 口径变化与真实原生工件改善分开，并逐项说明
  未达到门槛的原因；不以 Trellis task 完成状态代替产品发布门。（VBE-R8、R9）

## Out of Scope

- 新模型 provider、多模型排行、通过换模型提高本轮数字。
- BoardBench Tier B/C/D、自主器件选型、USB-C/Buck/电机/RS-485 等新题型。
- 大规模替换 Hermes、引入第二套 Agent 框架或新增 phase macro 工具。
- UI、安装器、外部发布和与主分支可运行性无关的新功能。
- 将 AI review 当作人类工程师签字，或自动声称可制造、可下单、已上电。
- 在本任务软件修改阶段实际下单或制造新 PCB；实物验证保留为独立人工工作流。

## Key Decisions

- 对照 campaign 冻结旧基线实际生效的比较预算：`90 model turns / 500 PCB tool
  calls / 3600 s wall time`。新增加的 route attempt、route node expansion 和 token
  维度先准确记录各自已有的有效限制与消耗；不得为了本次对照引入会削弱可比性的额外
  campaign 级硬上限。
- 任务保持为一个按依赖顺序执行的复杂任务，不拆成可并行实现的子任务。原生投影、事务
  后置条件、Router、进展控制、run schema 和 evaluator 共享同一组跨层契约，过早拆分会
  增加 schema 漂移与重复实现风险；实现与检查仍可按里程碑依次交给 Trellis 子 Agent。
