# Verifiable BoardBench execution technical design

## 1. Objective and constraints

本设计把 PCBDraft 的修改执行从“工具没有报错”提升为“候选 revision 已在原生 KiCad
中被重新读取并证明满足操作后置条件”。实现必须保持 BoardBench Tier A、
`openai-codex / gpt-5.6-luna`、KiCad 10.0.5、ERC/DRC 规则和
`90 model turns / 500 PCB tool calls / 3600 s wall time` 不变，以便下一轮 20×3
campaign 能归因于工具契约和控制策略，而不是模型或预算变化。

现有 managed project 的 staging、materialize、live swap 和 rollback 继续作为文件事务
骨架。本任务不建立第二套 Agent 框架、不用完整 BoardBench campaign 代替确定性回归，
也不把 AI 审查提升为人类工程签字。

## 2. Fundamental invariants

1. Design IR 是设计意图的权威状态；原生 `.kicad_sch` / `.kicad_pcb` 是交付工件的
   权威状态。两者不能互相替代。
2. 修改只在临时 revision 上执行；候选 IR、原生文件和 transaction receipt 的相关
   投影全部通过后，才能交换为 live revision。
3. 任何 postcondition、receipt 或 event 发布失败都 fail closed；live design、revision
   和 project record 保持原值。
4. 早期编辑只检查与本次操作相关的不变量，不要求整板提前通过 DRC。route、move、
   rotate、via 等物理操作必须检查其影响范围内的原生连通和新增严重 DRC。
5. “进程正常退出”“PCB 任务完成”“为什么停止”是三个独立事实；只有发布门通过才可
   得到 `task_outcome=passed`。
6. 原始 BoardBench campaign/run/score/review 永不就地迁移。新 schema 和 evaluator
   以新版本、新 campaign id 与旧证据并存。

## 3. Current gap and target flow

当前 `ApplicationService.apply_pcb_operation()` 已能在 staging 目录中物化并在发布失败时
回滚，但 `ManagedProject.assert_synchronized()` 主要证明文件 hash 与 IR hash 没有漂移，
不能证明网络分区、pad-net、铜线和端点连通满足设计意图。Router 的几何搜索成功也不能
直接等价为原生 KiCad 已成功布线。

目标执行流为：

```text
bounded semantic transaction
        ↓
normalize to existing concrete operations
        ↓
candidate IR in temporary revision
        ↓
materialize native KiCad
        ↓
reopen and project native schematic/PCB state
        ↓
verify common + operation-scoped postconditions
        ↓
run scoped KiCad checks and compute progress delta
        ↓
commit live revision ───── or ───── rollback
        ↓
compact model receipt + full diagnostic artifact
```

## 4. Component ownership

### Native projection and consistency

新增 `src/pcbdraft/kicad/consistency.py`，集中拥有：

- typed native projections；
- IR endpoint 与 native endpoint 的稳定标识映射；
- net partition 的 merge/split 比较；
- route/native connectivity postconditions；
- `NativeConsistencyReport` 和稳定 mismatch codes。报告使用稳定排序即可，不新增独立的
  cryptographic digest。

`src/pcbdraft/kicad/schematic.py` 只扩充原生原理图读取能力，返回 symbol pin、label、
no-connect 与网络端点投影；`src/pcbdraft/kicad/pcbnew_worker.py` 只扩充原生 PCB 读取，
返回 footprint/pad net、track/via/zone、连接与 DRC 摘要。比较规则不能散落到两个 reader。

### Managed project transaction

`src/pcbdraft/services/managed.py` 继续拥有文件 staging、snapshot、manifest 与原子交换。
`src/pcbdraft/services/application.py` 继续拥有修改编排、事务提交和发布；它在交换 live
revision 前调用 consistency/postcondition engine。现有 `src/pcbdraft/services/transactions.py`
是 IR 级语义事务，不扩张为第二套 managed-project transaction owner。

### Progress and control

新增 `src/pcbdraft/services/progress.py`，拥有 `ProgressVector`、来源 revision、向量比较、
错误指纹、无进展计数和阶段投影。Hermes adapter 消费 receipt 中的状态，不复制业务
判定。阶段是由证据推导的状态，不允许模型直接设置。

### Router

`src/pcbdraft/kicad/routing.py` 拥有 typed `RoutingFailure` 和搜索诊断；几何、pad escape、
障碍物与 node expansion 原始数据由 PCB 层提供。人类可读 diagnostics 从 typed failure
派生，BoardBench 不再解析自由文本来分类。

### Agent and BoardBench

- `src/pcbdraft/agent/hermes_tools.py` 生成有界 model receipt；完整诊断通过 artifact handle
  按需读取。
- `src/pcbdraft/interfaces/hermes_plugin.py` 只在 postcondition 框架稳定后开放有界批量写入。
- `src/pcbdraft/verification/boardbench_runner.py` 和相邻 schema/report 模块拥有 v2 run、预算、
  outcome 与 campaign 比较，不把 product session receipt 的职责复制进 benchmark。
- evaluator 新版本拥有三层评分和电气等价规则；旧 evaluator 继续只读旧结果。

## 5. Native projection contract

### Schematic projection

原理图投影至少包含：

- `(component_ref, logical_pin)` 到原生 symbol pin 的映射；
- 每个可识别端点所属的 electrical net partition；
- label、power symbol 与 no-connect 的等价表达；
- 缺失、额外和无法映射端点。

比较以端点集合的网络分区为核心，而不是依赖生成顺序或网络显示名。两个原本不同的 IR
网络在原生文件中落入同一 partition 是 `unintended_net_merge`；一个应连通网络被拆成多个
partition 是 `native_net_split`。允许 evaluator 定义的对称器件和合法 slot permutation，
但 transaction consistency 只使用候选 IR 已明确表达的等价映射。

### PCB projection

PCB 投影至少包含：

- `(footprint_ref, pad_number)`、native net 和几何位置；
- 目标 net 上非零长度 track/via/zone；
- native connectivity/unresolved connection 摘要；
- fatal/error DRC 项的稳定指纹与影响对象。

route 成功必须同时证明：目标 net 产生真实非零铜线；预期端点处于同一原生连接分量；
没有错误 net merge；没有新增与该操作相关的 fatal DRC。只检查“目标 net 不在 unrouted
列表”或“存在 segment”不再足够。

### Consistency report

```json
{
  "schema_version": "native-consistency-v1",
  "candidate_revision": 32,
  "passed": false,
  "mismatch_count": 1,
  "mismatches": [
    {
      "code": "native_connectivity_failed",
      "subject": "NET_SCL",
      "expected": "U1.4 connected to J1.2",
      "observed": "2 native components"
    }
  ]
}
```

完整 item 集写入 artifact；model receipt 只返回计数、稳定码和短摘要。

## 6. Postcondition policy

所有写操作先验证：文件可解析、预期对象存在、预期 native delta 已发生、相关 IR/native
投影一致、无非预期 merge/split。现有 managed-project hash 检查继续工作，但 postcondition
框架不额外引入与功能判断无关的 hash 门禁。

| Operation family | Additional postconditions |
| --- | --- |
| add/connect/disconnect/remove | 相关端点分区与候选 IR 一致；允许整板仍有其他未连接项；不得出现无关 native delta |
| place/move/rotate | footprint transform 精确匹配；关联铜线被正确重锚或显式失效；影响范围内不新增 short、严重 overlap、edge/hole fatal 项 |
| route | 目标 net 有非零铜线；指定端点原生连通；net 未被合并；不新增相关 fatal DRC |
| via | 位置、net、层对和孔径匹配候选意图；不新增相关 fatal DRC |
| unroute | 指定铜线在原生文件中消失；其他 net 和无关几何不变化 |

检查失败时事务返回 `ok=false`、稳定 `error_code`、失败 postcondition、
`rollback_performed=true` 和 artifact handle。只有全部通过时才发布 candidate revision。

## 7. Receipt and artifact contract

完整 transaction artifact 使用版本化 schema，至少包含：

```json
{
  "schema_version": "pcb-transaction-v1",
  "ok": false,
  "operation": "route_net",
  "error_code": "native_connectivity_failed",
  "candidate_revision": 32,
  "committed_revision": null,
  "intended_delta": {"nets": ["NET_SCL"], "objects": 1},
  "native_delta": {"tracks_added": 0},
  "postconditions": [{"name": "native_endpoint_connectivity", "passed": false}],
  "rollback_performed": true,
  "progress_before": {},
  "progress_after": {},
  "artifact_id": "txn_..."
}
```

给模型的默认投影控制在 1,024 bytes 左右，只包含：stage、live revision、operation、
ok/error code、必要 delta、progress delta、下一步建议和不含绝对路径的 opaque artifact id。
只有首次工程绑定或显式 inspect 才返回路径、完整文件表或历史事件。

## 8. Structured routing failures

`RoutingFailure` 至少有：

```text
code, net, endpoints, expanded_nodes, blocking_region,
nearest_obstacle_class, state_revision, retry_key, recommendations
```

稳定 codes 为：

- `invalid_seed`
- `zero_length_seed`
- `pad_escape_blocked`
- `no_legal_channel`
- `congestion_exhausted`
- `search_budget_exhausted`
- `native_commit_failed`
- `native_connectivity_failed`
- `unintended_net_merge`

零长度 seed 在进入 A* 前完成标准化或以 typed failure fail closed。结构化 retry key 由
failure code、net、端点、revision 和相关 placement/layer/order 状态组成，不要求额外计算
加密 hash；只有这些相关状态改变后，重复 route 才被视为新策略，而不是相同失败的盲重试。

## 9. Progress, stages, and termination

`ProgressVector` 的每一项保存 `value`、`source_revision` 和 `status=known|unknown|stale`；
unknown 不能按 0 参与比较。比较优先级为：

1. semantic/native mismatch、short 等安全/一致性错误；
2. unresolved connection 与 ERC；
3. unplaced/严重布局错误；
4. unrouted、routing failure 与 DRC；
5. 发布门证据完整性。

某个普通写操作可以合法地不改善整板向量，但 receipt 必须如实标为 `neutral` 或
`regressed`，不能描述为全局进展。连续无改善阈值、同一 fingerprint 重复阈值和允许的
策略切换由单一 policy 配置控制，并在 session receipt 中记录触发依据。

阶段由证据推导：

```text
requirements_frozen → schematic_semantic → native_schematic_confirmed
→ footprint_net_sync → placement → routing → native_connectivity_confirmed
→ erc_drc → release_gate
```

不强制模型只能线性操作，但任何后续阶段不能补写前序 gate；发布门只能由 native checks
产生。结束状态分别记录：

```json
{
  "process_status": "exited",
  "task_outcome": "incomplete",
  "termination_reason": "budget_exhausted:model_turns",
  "stage_reached": "routing",
  "release_gate_passed": false
}
```

## 10. Bounded semantic transactions and tool surface

P0/P1 期间保留“一次 provider response 最多一个修改工具”的保护。只有 native
postconditions 和回滚回归稳定后，才加入少量、具体、可审计的批量能力：

- `pcb_connect_group`：一笔事务连接有限端点集合；
- `pcb_place_group`：按绝对或相对约束放置有限器件集合；
- 现有只读查询支持有界批量描述。

不新增任意 macro、自由 operation discriminator 或第二套工具 registry。工具定义在唯一
registry 中附加 stage tags，Hermes adapter 根据已证明的当前阶段发送相关 schema；阶段
切换时重新暴露所需工具。若 provider adapter 不能动态过滤完整工具列表，则从同一 typed
registry 生成按阶段的有界 schema projection；执行时仍由完整 registry 做权限和参数校验。
本任务必须至少交付一种可测的 schema 缩减路径，不能把第二份手写 registry 当作替代。

## 11. Budget and BoardBench run v2

预算使用命名维度：

```json
{
  "name": "model_turns",
  "limit": 90,
  "consumed": 90,
  "unit": "turn",
  "source": "hermes_runtime",
  "status": "exhausted"
}
```

固定对照硬限制仍为 90 model turns、500 PCB tool calls、3600 s wall time。route attempts、
node expansions 和 tokens 记录各层已有的有效 limit/consumed；没有独立硬限制时显式记录
`limit=null` 和实际 consumption，本轮不引入改变可比性的额外 campaign 级硬限制。Runner
优先从 trace/session terminal receipt 归一化结束原因；
`max_iterations_reached(90/90)` 必须得到 `budget_exhausted:model_turns`，不能落为
`agent_returned`。

v2 loader 可把旧 v1 evidence 投影成 normalized read model，并把缺失字段标为 unknown；
serializer 只写新 artifact，绝不回写旧目录。

## 12. Evaluator v5 and comparison

新评分输出：

- `design_intent`：需求、电路拓扑、额定值和必需保护；
- `native_artifact`：KiCad 可解析、symbol/footprint/pad、native netlist、ERC/DRC 与布线；
- `delivery_readiness`：工程师、制造、装配、上电和功能证据；无人类/实物时为 unknown。

拓扑匹配以 electrical graph 为基础，支持对称二端器件端点交换、串联等价、同型号连接器
slot permutation、合法 no-connect 和 prompt 未规定 pin order。旧 `overall_state` 只作为
legacy projection，不再是新报告的唯一结论。失败保存 first blocking stage、terminal stage、
多个 root causes 与 symptoms。

对照报告同时给出：

1. 未改口径的原生 ERC/DRC、consistency、预算和效率；
2. evaluator v5 下旧/新 artifact 的同口径重评结果（不改写原 artifact）；
3. evaluator 口径变化带来的差异。

这样不会把评分修正误报为工具链改善。

## 13. Rollout and compatibility

1. 先用最小 fixture 固定当前成功与已知失败，不依赖模型。
2. 上线 native projections 和 report，但先以 shadow assertion 观察既有工具。
3. 将修改工具切换为 postcondition fail-closed，保留 feature flag 只用于本地回滚，不用于
   正式 campaign 绕过检查。
4. 修 Router 与收敛控制，再开放最小语义批量事务和 compact context。
5. 升级 BoardBench v2/evaluator v5，跑小规模预检。
6. 只有操作者再次明确授权才运行完整 60-run 对照。

兼容性风险主要是 native projection 无法识别合法 KiCad 表达、局部 DRC 增加运行时间、
旧 run 缺少精确预算来源，以及 tool schema 动态过滤受 provider 限制。所有风险均应表现为
unknown/fail closed 或显式 deferred，不能静默视为 pass。

## 14. Task shape

本任务不拆成多个可并行 child task。native projection、transaction receipt、Router failure、
progress、BoardBench v2 和 evaluator 共享 revision、stage 与错误码契约；过早并行会
放大 schema 漂移。实现阶段可以按下述里程碑顺序交给 Trellis implement/check Agent，
每个里程碑合入同一工作树后再进入下一层。
