# Implementation plan: verifiable BoardBench execution

## Working rules

- 依赖顺序不可反转：native projection/transaction → Router → progress/termination → semantic
  batch/context → evaluator → comparison campaign。
- 每个开发里程碑只运行最接近改动的测试、lint/format 和 `git diff --check`，目标预算约
  90 秒。完整测试、release check 和真实模型 campaign 不属于普通提交门禁。
- 历史 BoardBench artifact 只读。回归测试使用最小、去敏、确定性 fixture，不复制整个
  本地 benchmark corpus 进 Git。
- 每个 schema 先有 typed model、version 和兼容读取测试，再接入 producer/consumer。
- 不通过换模型、增加 90-turn 上限、放宽 DRC 或删除困难 run 改善结果。

## Milestone 0 — Freeze contracts and regression fixtures

### Changes

- 将 `design.md` 中的 native projection、transaction receipt、routing failure、progress、
  session outcome 与 budget dimension 定义为 typed schemas。
- 从旧 60-run 证据提炼最小 deterministic fixtures：zero-length seed、原生零铜线、端点未连通、
  unintended net merge、移动 footprint 后陈旧铜线、receipt/event 发布失败。
- fixture 仅表达复现所需 KiCad/IR 状态，并记录来源 case/run 的本地 research 注释；不提交
  原始私有 prompt、模型 trace 或完整 campaign。

### Validation

- schema round-trip/version tests；
- fixture 可由 KiCad reader 稳定解析；
- v2 loader/serializer tests 证明旧 artifact 只读，不依赖额外 hash 审计。

## Milestone 1 — Native projections and consistency report

### Changes

- 新增 `src/pcbdraft/kicad/consistency.py` 与 typed `NativeConsistencyReport`。
- 扩充 schematic reader，使其投影 symbol pin、label、no-connect 和 electrical partitions。
- 扩充 pcbnew worker inspection，使其投影 pad-net、非零 track/via、连接分量与稳定 DRC item。
- 实现 IR/native endpoint 映射、net merge/split、缺失/额外对象和稳定排序报告。
- 保持 `ManagedProject.assert_synchronized()` 的文件职责；新增显式 native consistency API，
  不把高成本解析隐藏到普通 hash assertion 中。

### Validation

- 同步、merge、split、missing endpoint、等价 no-connect fixture tests；
- board route/no-route、错误 pad-net 和连接分量 tests；
- native report 输出顺序与 stable mismatch codes 跨重复运行稳定。

## Milestone 2 — Integrate postconditions with managed transactions

### Changes

- 为 `ApplicationService.apply_pcb_operation()` 增加公共 transaction executor：候选 IR、临时
  materialize、native re-open、common/operation postconditions、commit/rollback。
- 按 add/connect/remove、place/move/rotate、route、via、unroute 建立 policy matrix。
- 写入 versioned full artifact 和不超过约 1 KB 的 compact receipt。
- postcondition、receipt 或 event 发布失败时恢复 live design、revision 和 records。
- 在 native checks 成为硬门前使用短期 shadow mode 对现有 fixture 比较；正式路径不允许
  绕过 hard gate。

### Validation

- 每个 operation family 至少一个 commit 与 rollback test；
- 故意注入 parse、consistency、receipt/event 失败，断言 live revision 和原生内容不变；
- 断言没有“成功但原生零铜线/未连通/net merge”的 receipt。

## Milestone 3 — Router typed failures and correctness

### Changes

- 引入 `RoutingFailure` 与九个稳定 codes，保留派生的人类 diagnostics。
- A* 前规范化 seed，消除可修复的零长度输入；无法规范化时返回 `zero_length_seed`。
- 为 pad escape、channel、congestion、node budget、commit/connectivity/merge 分类并记录
  endpoint、node expansion、阻塞摘要、state revision、retry key 与 recommendations。
- route 成功依赖 Milestone 2 的 native route postconditions。
- footprint transform 后明确重锚或失效关联铜线，禁止陈旧 route 继续被计为成功。

### Validation

- 九类 failure code 的最小回归；
- 旧自由文本 API 的兼容投影测试；
- route success fixture 同时断言 native non-zero copper、endpoint connectivity、无 merge、
  无新增相关 fatal DRC。

## Milestone 4 — Progress, stages, convergence, and terminal receipt

### Changes

- 新增 `ProgressVector`，包含 known/unknown/stale、source revision 与 deterministic comparison。
- 每个修改 receipt 记录 before/after/delta 和 improved/neutral/regressed。
- 由 native evidence 推导阶段和 release gate；模型不能直接写阶段。
- 实现相同 revision/相关状态下的重复 retry key 检测、无进展停止与策略切换要求。
- 定义 process status、task outcome、termination reason 的单一 product session terminal receipt。

### Validation

- unknown 不按 0、stale 不误判改善、严重错误优先级 tests；
- unchanged state 重复 route 被停止，placement/layer/order 改变后允许新策略；
- agent 提前返回、无进展、发布门通过与失败的状态机 tests。

## Milestone 5 — Bounded semantic transactions and compact context

### Changes

- native hard gate 稳定后加入 `pcb_connect_group`、`pcb_place_group` 和必要的只读批量描述；
  每个写工具仍是一笔有上限的原子事务。
- 修改 Hermes one-action policy，使“一次 decision 一个有边界语义事务”成为规则；禁止同一
  response 组合多个独立写事务。
- `_model_summary()` 默认只发 stage、revision、必要 delta、error、progress 和 artifact id；
  路径、文件表、events、snapshot 改为显式 inspect。
- 在唯一工具 registry 上增加 stage tags 并由 adapter 过滤 schema；若 provider 不能动态
  过滤，则从同一 typed registry 生成按阶段的 schema projection，不能维护第二份手写定义。
- 分开记录非缓存输入/输出/费用与活动上下文长度/重复比例/每轮新增 token。

### Validation

- group 操作全成功或全回滚，越界 payload 被稳定拒绝；
- one-decision policy 接受单个 batch、拒绝多个独立 writes；
- 代表性 trace 的 tool result median ≤ 1,024 bytes，默认不重复路径/文件表/events；
- stage filter 不扩大工具权限，执行 registry 与 schema registry 不分叉。

## Milestone 6 — BoardBench run schema v2 and budget attribution

### Changes

- 引入 v2 `BoardBenchRun`/session projection，分离 process status、task outcome、termination
  reason、stage reached 和 release gate。
- 引入 named budget dimensions；冻结 90 model turns、500 PCB tool calls、3600 s wall time，
  同时记录 route attempts、node expansions 和 tokens 的已有有效限制/消耗。
- Runner 从 terminal receipt 和 trace 归一化结束原因；max iteration 映射为
  `budget_exhausted:model_turns`，正常退出但未过 gate 映射为 incomplete。
- v1 loader 兼容投影 unknown 字段；新 serializer 不写旧 artifact。

### Validation

- v1/v2 loader、round-trip、unknown preservation tests；
- max iterations、tool budget、wall timeout、agent early return、crash、cancel、release pass tests；
- 计划 run 分母不可被 retry/replacement 静默改变。

## Milestone 7 — Evaluator v5 and causal reporting

### Changes

- 输出 design intent、native artifact、delivery readiness 三层结果和 evidence source。
- 实现对称二端器件、串联拓扑、connector slot permutation、合法 no-connect 与未规定 pin
  order 的 graph-equivalence fixtures。
- 保存 first blocking stage、terminal stage、root causes 与 symptoms。
- 报告分别显示原生 KiCad 指标、同口径 evaluator v5 重评和 evaluator delta。

### Validation

- 所有合法等价 fixture pass，真实断路/短路/错误 net fixture fail；
- 无人类或实物证据时 delivery readiness 必为 unknown；
- ERC/DRC command/rules 与旧 baseline 一致；
- v5 重评不修改旧 score/review 文件。

## Milestone 8 — Deterministic integration and campaign preflight

### Changes

- 建立一个小型端到端 fixture：自然语言 session → semantic operations → native KiCad →
  progress → release/terminal receipt → BoardBench score。
- 选取少量旧失败拓扑做本地 preflight，验证 Router 分类和 terminal semantics；不把它们
  计入正式 60-run 结果。
- 生成冻结 campaign manifest，记录模型、环境、corpus 版本、evaluator、预算和 DRC 命令；
  复用已有完整性字段，不新增无测试用途的 hash。

### Validation

- 针对性集成用例在普通开发预算内完成；
- `git diff --check` 和改动文件 lint/format；
- 只有准备合并/发布时才运行全量测试和 release check，并单独记录结果。

## Milestone 9 — Operator-authorized 60-run comparison

### Entry gate

- Milestones 0–8 的确定性回归全部通过；
- campaign manifest 已冻结并由操作者检查；
- 操作者再次明确授权真实模型成本和最长 60 小时时间窗。

### Execution

- 使用同一 20×3 Tier A corpus、Luna、KiCad/DRC 规则和 90/500/3600 预算运行；
- 所有计划 run 保留在分母；失败、timeout 和 incomplete 不替换；
- 保留 raw traces、native projects、session/run receipts、scores 与不可变 manifest。

### Validation

- planned=started=60；每个 artifact 可从 manifest 定位；
- 已提交 revision semantic/native mismatch=0；
- 汇总 DRC、ERC、三层评分、请求/工具/token、预算、失败码和 task-level 三次稳定性。

## Milestone 10 — Review, report, and spec capture

### Changes

- 使用冻结 AI rubric 复核 60 个原理图功能结果；无人类工程师时明确标注 AI-reviewed。
- 输出旧/新因果对照，单独列 evaluator 口径变化和未达门槛原因。
- 将稳定的 transaction、Router、session outcome 和 BoardBench v2 契约写回
  `.trellis/spec/backend/`；不把一次性实现细节写成规范。
- Trellis task 只有在事实性证据完成后归档；task 状态不替代产品发布门。

### Exit criteria

- VBE-AC1–AC7、AC10 有确定性或报告证据；
- 正式对照达到 DRC ≥ 40/60、90-turn exhaustion ≤ 3/60、模型请求中位数较约 79 下降
  ≥ 50%、AI functional ≥ 50/60、committed mismatch=0，或如实记录未达标并保持 task
  outcome 与发布门未通过；
- delivery readiness 在缺少工程师/实物证据时保持 unknown。

## High-risk files and rollback boundaries

- `src/pcbdraft/services/application.py`：公共修改路径，必须按 operation family 渐进接入；
  回滚到旧行为只能用于本地诊断，正式 campaign 不得绕过 postconditions。
- `src/pcbdraft/kicad/schematic.py`、`pcbnew_worker.py`、`routing.py`：KiCad 版本和几何语义风险；
  reader 扩展与比较器分离，fixture 锁定原生行为。
- `src/pcbdraft/interfaces/hermes_plugin.py`、`agent/hermes_tools.py`：上下文和工具策略可能改变
  Agent 行为；在同模型小 preflight 后才进入正式 campaign。
- BoardBench schema/evaluator：必须版本化并保持 v1 read-only 兼容；任何迁移器默认只输出
  新目录。

## Definition of done mapping

| Acceptance | Primary milestones |
| --- | --- |
| VBE-AC1–AC2 | 0–3 |
| VBE-AC3 | 3、9 |
| VBE-AC4 | 4–5 |
| VBE-AC5 | 4、6 |
| VBE-AC6 | 5、9 |
| VBE-AC7 | 7 |
| VBE-AC8–AC9 | 8–10 |
| VBE-AC10 | 7、9–10 |

## Requirement traceability

| Requirement | Design sections | Implementation milestones |
| --- | --- | --- |
| VBE-R1 | 1、13 | 8–10 |
| VBE-R2 | 2–7 | 0–2 |
| VBE-R3 | 5、8 | 0、3 |
| VBE-R4 | 7、9–10 | 4–5 |
| VBE-R5 | 9、11 | 4、6 |
| VBE-R6 | 7、10 | 5 |
| VBE-R7 | 5、12 | 7、10 |
| VBE-R8 | 1、11–13 | 8–10 |
| VBE-R9 | 13–14 | 0–10 |
