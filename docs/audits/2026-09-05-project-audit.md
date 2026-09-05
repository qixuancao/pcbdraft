# PCBDraft 项目审计与整改

当前状态：首轮 F01–F09 和整合时发现的 F10 已有修复及定向验证；
原生化重构已按阶段提交，但还不能通过完整 CI 或作为发布完成证据。
**F11（P1，未解决）：最近一次吸收后的全库 Ruff 检查有 11,498 项告警。**
告警主要来自复用代码的宽泛异常处理、旧注解、未使用导入及 subprocess
检查项，不能把告警数量直接解释为运行缺陷数量。未通过禁用规则或排除新包
来隐藏这些问题；需要后续按模块修复，再通过 CI 的类型/覆盖率/原生验收。

后续稳定化审查新增的 F12–F17 已完成整改和定向验证。F11 仍未清零；后续
小检查点只处理 deadline 与模型归一化模块的局部告警，没有重新运行或统计
全库检查，也不扩大为全量发布验收。

下文“已做的验证”第一节记录的是迁移前基线；各轮整改状态与检查结果
分别列在后面。原有实现的保留证据见[来源说明](../NATIVE_RUNTIME.md)和
[696 个模块的逐文件映射](../native-runtime-source-map.json)。

审计时间：2026-09-05。检出分支：`recovery/web-workbench-convergence-20260904`，提交：`9d7f1558af992e7c30f59a5198191bce63e00239`。本报告记录用户要求原生整合运行时之前的状态。已有未跟踪 `.hermes/` 保持原样。

结论：静态检查通过，但存在安全、验证可信度、双运行时和 Web 状态同步缺陷。以下 9 项均有代码证据；F01–F08 另有离线最小复现。复现只写临时目录，没有读取真实凭据或调用付费模型。

| 编号 | 优先级 | 问题 | 代码位置 |
| --- | --- | --- | --- |
| F01 | P1 | 静态资源查询参数可以读取应用进程有权读取的任意本地文件 | `src/pcbdraft/interfaces/gui.py:883`、`:665` |
| F02 | P1 | Web 默认进入旧固定流程，未使用终端的自主 Agent 循环 | `src/pcbdraft/services/gui_session.py:69`、`src/pcbdraft/model/tool_calls.py:100` |
| F03 | P1 | 畸形但合法的 JSON 检查报告可以被记录为 ERC/DRC 通过 | `src/pcbdraft/verification/validation.py:607`、`:248`、`src/pcbdraft/verification/rule_evidence.py:684` |
| F04 | P2 | SSE 收到外部 KiCad 修改事件后不刷新状态，导入提示不能及时出现 | `src/pcbdraft/web/app.js:420` |
| F05 | P2 | 快照与事件游标不是同一个读事务，可能永久跳过刚发生的更新 | `src/pcbdraft/interfaces/gui.py:928` |
| F06 | P2 | 切换项目时复用上一个项目尚未完成的快照请求，目标项目可能不加载 | `src/pcbdraft/web/app.js:467` |
| F07 | P2 | 前端仍使用旧的会话状态，排队和取消中的作业显示可发送、不可停止 | `src/pcbdraft/web/conversation.js:170` |
| F08 | P2 | 导入只绑定应用 revision，没有绑定用户所见的外部板文件 hash | `src/pcbdraft/services/application.py:5757`、`src/pcbdraft/interfaces/gui.py:956` |
| F09 | P2 | CI 要求 Python 3.14，但项目和锁文件明确排除 3.14 | `.github/workflows/ci.yml:25`、`pyproject.toml:10`、`uv.lock:3` |

## 问题证据与建议

### F01：任意文件读取

循环里定义的 `static_asset(name=..., content_type=...)` 被 FastAPI 解释为查询参数。处理函数直接把 `name` 传给 `importlib.resources.files(...).joinpath("web", name)`；绝对路径会绕过 Web 资源目录。

实际 HTTP 复现：`/assets/app.js` 和 `/pcbdraft/assets/board.js` 都返回了临时目录哨兵文件，状态为 200。默认服务仅监听 loopback，因此没有据此声称已发生互联网远程泄露。若服务可访问，固定资源白名单并不能保护该查询入口。

建议：用无客户端参数的闭包固定资源名和 MIME，在资源读取边界再次校验白名单与路径。

### F02：Web 与终端是不同的执行产品

`GuiSessionManager -> JobRunner -> AgentOrchestrator -> ConfiguredPCBCallProducer` 使用旧的固定 `plan_request/generate/validate/repair` 流程。正常配置返回 `HermesIntentProvider`，旧路由只识别 `OpenAICompatibleSettings`，因此选择 `local-policy`。对“只查看当前设计，不要修改”的离线输入，第一项提议仍为 `plan_request`，没有对话模型决策。

终端则另外启动复用的 Agent，并通过工具注册、环境变量、`sys.path` 和方法补丁连接 PCBDraft。这正是用户随后指出的生硬桥接问题。还存在会话文本来源不同：Web 从 `TurnRecord.assistant_texts` 取文字，固定流程的业务答复主要写应用 conversation，不能保证展示。

建议：将复用代码按 TUI、Agent、模型、工具职责吸收为原生模块；通过同一个应用会话服务驱动 Web 和终端，工具继续由 ApplicationService 管理 revision、权限及事务。

### F03：缺失检查结果被当作零违规

`_run_kicad_report` 只要 JSON 可解析且进程退出状态可接受，就标记 `completed`；随后缺失的 `sheets/violations/unconnected_items/schematic_parity` 被当作空列表。

实际复现：给 `run_individual_check` 的进程边界注入 `{}` 或 `[]`，`run_erc` 和 `run_drc` 四种组合全部返回 `completed/pass`。完整证据捕获也接受只有 `$schema` 和 `kicad_version`、缺失全部 DRC 结果段的对象，并记录 `complete=true/error_count=0`。

这是异常工具响应的拒绝策略缺陷；未声称真实 KiCad 当前正常输出此类报告，也未把最小复现夸大为已通过完整制造发布流程。建议校验实际支持的报告结构、各必需结果段及 severity，不可把缺失字段解释为通过。

### F04：外部修改通知未驱动刷新

后端发送 `external_revision.detected`，前端刷新白名单只有 `scene.committed/generation.complete/validation.complete/job.complete/job.failed`。健康 SSE 下备用轮询关闭，因此手动保存 KiCad 修改之后，旧画面和验证状态可能持续显示。

实际执行前端事件处理函数：收到该事件后活动记录增加 1，快照刷新为 0，连接仍 healthy。建议将外部修改作为状态失效事件处理。

### F05：快照与 SSE 游标竞态

HTTP 快照先调用 `runtime.snapshot`，再调用会再次读取工程的 `events.cursor`。两次读取之间发生提交，快照仍是旧 revision，游标已经包含新 revision 的事件。

受控复现返回 `snapshot_revision=7/current_revision=8/cursor=2`，用返回游标恢复事件流得到 `replayed_updates=0`。建议保证快照和游标在同一一致性边界，或允许重放并在客户端去重，不能吞掉未包含在快照中的更新。

### F06：快照请求去重没有绑定项目

`refreshSnapshot` 发现任意 `snapshotInFlight` 就返回旧 promise，不比较 `projectId`。A 读取期间切换 B，B 不发快照请求；A 返回后又因项目已变而被丢弃。

实际执行前端函数：当前项目是 B，请求记录只有 `board-a/snapshot`。建议按项目去重，切换时取消旧请求，并给所有异步会话/Inspector 读取增加项目身份检查。

### F07：新旧会话状态不兼容

服务返回 `queued/running/cancel_requested`，前端只把 `starting/running/stopping` 当作 active。复现 `queued` 和 `cancel_requested` 都得到 `sendDisabled=false/stopDisabled=true`，与后端“已有活动作业”的拒绝逻辑冲突。

建议统一状态协议，让视图直接使用原生作业的活动状态；补实际行为测试。

### F08：确认的外部版本与实际导入版本可以不同

查询阶段返回 `board_sha256`，POST 只接受 `expected_revision`。KiCad 再次保存不增加 PCBDraft 应用 revision，导入阶段重新预览并导入新内容。

受控复现展示 hash 为 `bbbb...`，实际传给导入器的 hash 为 `dddd...`，返回 `imported`。建议把审阅的 board/manifest/tracked-file hashes 或不透明预览身份一并绑定到导入请求；变化时要求刷新审阅。

### F09：Python 支持范围和 CI 冲突

`SpecifierSet(">=3.11,<3.14").contains("3.14")` 为 false；`uv.lock` 同样限制 `<3.14`，而 CI 和 `scripts/python-matrix.sh` 仍包含 3.14。该分支的同步步骤无法按矩阵合同完成。没有运行整个版本矩阵。建议先统一真实支持范围，再验证对应单个安装场景。

## 已做的验证

- `git diff --check`：通过。
- `uv run --frozen --no-sync ruff check src tests`：通过。
- `uv run --frozen --no-sync ruff format --check src tests`：189 个文件通过。
- `uv run --frozen --no-sync mypy`：108 个源码文件通过；上次审查的 12 个类型错误已不再出现。
- 第一组 38 项定向测试：通过，0.628 秒；覆盖 GUI API、会话适配、外部导入、事务、权限和 gate JSON。
- 第二组 70 项定向测试：通过，其中 3 项跳过，4.465 秒；覆盖 native consistency、live view、rule evidence、工具绑定、安装器。跳过项见以下重现命令的 `-v` 输出；本次没有把 skip 计为通过。
- frozen runtime 导出与 `constraints/runtime.txt`：完全一致。
- `reproduce_audit.py`、`reproduce_frontend.mjs`：离线问题复现已成功；结果见相邻 JSONL 文件。

定向测试命令：

```sh
uv run --frozen --no-sync python -m unittest -q tests.interfaces.test_gui tests.services.test_gui_session tests.services.test_application_external tests.services.test_transactions tests.agent.test_permissions tests.verification.test_gates
uv run --frozen --no-sync python -m unittest -q tests.kicad.test_consistency tests.services.test_live_view tests.verification.test_rule_evidence tests.agent.test_hermes_tools tests.core.test_installers
uv run --frozen --no-sync python artifacts/audits/20260905-9d7f155/reproduce_audit.py
node artifacts/audits/20260905-9d7f155/reproduce_frontend.mjs
```

没有运行全量 unittest、coverage、完整 KiCad 验收、浏览器/TUI E2E、依赖漏洞审计、版本矩阵或 release-check。没有超时中止的检查。

## 审查覆盖与证据边界

检查了源码/测试目录、核心权限与事务、模型/工具路由、GUI 状态和资源边界、KiCad 同步与校验、发布逻辑、安装/打包/CI 及现存 BoardBench 报告。108 个 Python 源码模块、64 个测试模块以及 Web 资源构成项目自有代码；大文件按关键调用链深入，没有声称逐行证明所有代码安全。当前 `tests/integration/` 没有 `test_*.py`；Web 前端不少测试只检查源码字符串，不能替代运行行为测试。

现存 2026-08-24 BoardBench 报告 `artifacts/boardbench-local/vbe-m10-report-20260824/canonical-draft/report.md` 为 AI-reviewed、未封存 pilot：60 个计划运行、60 终结、自动通过 0、自动失败 60；DRC 5 通过、2 失败、53 未知，错误分类 60 个 unclassified，尚无硬件记录。它是历史证据，不代表本次检出版本的新跑结果。此次没有重新运行真实模型 campaign 或制造实验。

`services/application.py` 约 6904 行、验证/benchmark 也有多个大模块，增加修改成本；这属于维护风险，未混作已复现功能缺陷。后续原生整合应收敛职责，避免把复用运行时简单换一个目录名后继续桥接。

## 原生整合检查点

首轮已将 Agent、TUI、模型认证、工具和会话存储吸收为正式 Python 包，移除
`vendor/hermes`、路径注入、启动方法补丁和磁盘观察器插件。版权许可保留在
`NOTICE` 与 `data/licenses/Nous-Research-MIT.txt`。旧 PCBDraft 专用配置目录仍可读；
独立安装的 Hermes 状态不参与路径解析。

首轮验证：原生导入不改变 sys.path、不加载旧顶层命名空间；41 个提供商定义
可加载；终端/认证/工具/观察器 85 个定向测试通过；原项目维护模块与新边界
的 lint、110 个源文件的 mypy 通过。全库静态检查还包含原复用源代码继承的
大量宽泛异常捕获、类型注解和安全检查告警，不能把这次模块迁移当作这些
技术债已清零。没有运行全量测试或 KiCad/TUI/Browser E2E。

表中缺陷状态以各后续修复记录为准，本节不宣称 F01–F09 已全部修复。

打包验证：在临时源码副本中使用隔离构建生成 wheel；解包后在排除源码
路径的 Python 进程中验证 TUI、Agent 与 41 个提供商可导入。wheel 不含
`vendor/` 或 `hermes_cli/` 副本。导入仍不修改 sys.path，也不创建旧顶层别名。

## 第二轮：统一 Web 会话

F02 已修复：Web 默认通过 `ConversationOrchestrator` 使用原生 `AIAgent`，与
终端共用模型、认证和工具模块。工具逐次由模型选择，并经过持久化 dispatch、
revision 和审批边界；跨轮历史保存到工程会话库。并发会话用 ContextVar
隔离工程与权限，取消中断模型，审批续跑只执行一次尚未 dispatch 的调用。

整合时另发现 F10：`AgentTurnStore` 的 effect 校验遗漏标准 `read` 类型，
会拒绝记录只读工具。本轮改为直接取规范 `ToolEffect` 的成员，消除重复枚举。

验证包括真实原生 Agent 向本机模拟模型发送工具 schema、消费 SSE 工具调用、
执行一次检查、写入回复，以及第二轮读取历史。模拟服务只监听 loopback，
测试阻止所有非 loopback 连接；没有调用真实模型或操作真实 PCB。另有定向
并发工程、审批、取消及旧工具/终端回归。前期模拟器将能力探测当作对话请求、
未支持 SSE 的失败已修正，不作为产品失败结论。

## 第三轮：资源读取与检查报告边界

F01 已修复：静态路由不再暴露文件名/MIME 参数，读取层也拒绝非白名单
资源并限制读取字节数。两种挂载路径的真实 ASGI 请求不能返回临时哨兵文件。

F03 已修复：单项 ERC/DRC、确定性 gate 与完整证据共用受支持 v1 报告的
结构校验。必需结果段缺失、类型错误、未知 severity、明确截断，以及仅含
warning 的不完整检查，都不能产生 completed/pass 或 complete/zero-error。
对应模拟 KiCad 输出已同步到有效报告结构。

29 项相关 GUI、report contract、rule evidence、gate 与命令身份测试通过
（0.520 秒），4 个改动源文件的 mypy、改动文件 Ruff 和 diff 检查通过。
没有执行完整 KiCad 验收或全量测试。

## 第四轮：Web 状态与事件一致性

F04–F07 已修复：外部修改事件触发快照失效；服务先取事件游标再读快照，
确保竞态提交仍可重放；前端按项目与切换代次合并请求，旧事件流/旧会话请求
不再覆盖当前项目。读取中的新失效事件会追加一次刷新，合并请求保留游标
重置要求。queued/running/cancel_requested 都禁止重复发送，键盘入口同样检查。

34 项 GUI/API/前端定向测试通过（0.465 秒），其中包含用 Node 执行实际 JS
函数的离线行为测试；Python Ruff/mypy、JS 语法与 diff 检查通过。
没有运行 Browser E2E。

## 第五轮：外部导入绑定已审阅预览

F08 已修复：预览返回绑定 board、manifest 和全部 tracked-file hashes 的
`review_token`，Web/API 导入必须同时携带该 token 与应用 revision。
后台在更改应用状态之前比较预览身份；原有事务在 staging 前、发布锁内
继续校验实际文件哈希。预览本身也先取哈希，再解析原生文件，最后复查，
避免解析期间再次保存使展示内容与 token 不一致。

39 项相关服务、哈希边界、GUI 和离线 JS 测试通过（0.551 秒）；三类预览
输入变化均在状态/native mutation 前拒绝，解析中修改也被拒绝。3 个改动
源文件的 mypy、相关 Ruff、JS 语法和 diff 检查通过。未运行完整原生导入验收。

## 第六轮：Python 支持声明

F09 已修复：CI、默认 matrix 脚本和包 classifier 都统一为 3.11–3.13，
与 `requires-python` 和锁文件保持一致。验证使用 TOML/SpecifierSet 与文本
矩阵比较，并通过 `bash -n`、diff 检查；没有运行 Python 版本矩阵。

## 第七轮：原生目录收尾与来源核对

PCB 终端命令处理归入 `interfaces/tui/project_commands.py`，删除已无生产
调用的命令注册表重写辅助函数。其余启动辅助模块的 sys.path 注入也已移除。
用户入口的旧桥接描述/配置来源标签改为原生描述；模型系统提示的身份也统一
为 PCBDraft，并在实际发出的模型请求中验证不再自称 Hermes Agent。兼容配置
与上游归属保留。

696 个复用 Python 模块均核对到当前文件；MIT 许可与两个语音唤醒模型
逐字节匹配迁移前版本。原生代码继续维护在正常职责目录中。

本轮 58 项终端、提供商、隔离 worker 和真实原生导入/本机模型循环测试
通过（3.609 秒），6 个维护边界源文件的 mypy、完整 Ruff 通过；涉及的原
复用 TUI 模块完成导入/未定义名称检查，已有的全规则告警列入 F11。
补充的 2 项原生启动/出站提示回归也通过。重新隔离构建 wheel 并从解包
目录导入成功：904 个打包文件，41 个提供商；
未导入源码副本、未改变 sys.path、无旧顶层模块别名。

各轮均保留本地 Git 检查点，未 push。没有运行全量 unittest/coverage、
完整 KiCad/TUI/Browser E2E、依赖审计、Python 版本矩阵或 release-check。
本次没有因耗时中止的检查；过程中出现的定向检查失败已修正并重跑，
F11 的全库静态检查失败明确保留，不计为通过。历史 BoardBench 结果没有重跑。

## 第八轮：原生会话恢复稳定化

本轮继续审查原生 `ConversationOrchestrator` 与应用作业之间的故障恢复边界。
F12–F14 均有本地复现、实现修复和独立主审。

- **F12 已修复：完成 receipt 后不再恢复旧模型。** FAILED、INTERRUPTED、
  CANCELLED 旧回合已有 durable COMPLETED receipt 时拒绝重试；恢复到旧
  RUNNING 状态时，无 active、遗留 PROPOSED 或 receipt reconcile 后得到的
  COMPLETED 调用也会关闭旧 aggregate，并要求创建新回合。精确匹配、已批准
  且尚未 dispatch 的调用只执行一次，直接把真实本地 receipt 交付给用户后
  结束回合，不再次启动旧模型。正常运行中的相同只读调用仍可重复执行，避免
  把安全屏障误作全局去重。检查点：`bd2a75d`、`269f5ee`。
- **F13 已修复：恢复作业绑定创建它的控制器。** 作业 policy v2 持久化稳定
  `controller_id`，恢复、retry 和 dispatch 均严格比较 native 与 legacy
  控制器；同控制器可恢复，跨控制器和没有该标识的旧 v1 作业可读取但失败
  关闭并要求新回合。检查点：`5809882`。
- **F14 已修复：factory 返回后同步重检取消和截止。** 本地 fake 曾复现取消
  在 `agent_factory` 尚未返回时到达，factory 返回后仍调用模型，最终才标为
  cancelled；0.10 秒截止后到 0.302 秒，调用仍阻塞在 factory 且回合保持
  running。现在普通模型分支在 factory 返回后、读取 history 或调用模型之前
  同步重检；取消转为 CANCELLED，超时转为 FAILED，均不进入 model/history/
  tool，并关闭已创建的 Agent 和会话库。审批分支直接执行精确匹配的已批准
  调用，不创建模型。检查点：`269f5ee`。

会话、原生导入与本机模拟模型、作业控制器三组分别运行：

```sh
uv run --frozen --no-sync python -m unittest -q tests.agent.test_conversations
uv run --frozen --no-sync python -m unittest -q tests.agent.test_runtime_import
uv run --frozen --no-sync python -m unittest -q tests.services.test_job_controller_recovery tests.services.test_application.ApplicationConversationTests.test_recovery_never_widens_mcp_job_permission_mode tests.services.test_application.ApplicationConversationTests.test_startup_fails_closed_for_legacy_queued_mutations tests.services.test_application.ApplicationConversationTests.test_legacy_agent_job_with_a_turn_is_cancelled_before_dispatch tests.services.test_application.ApplicationConversationTests.test_recovery_requires_exact_registry_and_tool_call_bounds tests.services.test_application.ApplicationConversationTests.test_malformed_active_job_envelope_blocks_startup_dispatch
```

三组依次为 15 项通过（0.400 秒）、2 项通过（3.053 秒）、10 项通过
（0.240 秒）。F12–F14 对应改动文件的 Ruff、格式与 diff 检查通过；会话
两文件的 mypy 也通过。以上组数分别记录，不把重复覆盖累计成额外测试。

F14 只修复 factory **返回后**的误调度。同步 factory 本身没有可强杀的墙钟
截止；真实模型 HTTP 卡住时能否在所有传输上可靠退出，以及 `agent.close()`
或 watcher 中断阻塞时的清理时限，仍需独立验证。常用 HTTP 路径已有中断
标记、轮询和 socket shutdown，但这不等于全部提供商和清理路径已经获得
有界执行保证；本轮没有使用无法回收的后台 factory 线程伪装强制取消。

## 第九轮：deadline 超大数值边界

F15 已修复：`clamp_timeout(10**1000)`、`clamp_timeout(-(10**1000))` 以及
来自 YAML 的同类超大整数此前会在 `float()` 转换时抛出 `OverflowError`。
现在正超大整数收敛到平台安全上限 `31536000.0`，负超大整数遵循既有非正数
无界语义返回 `None`；NaN、无效配置、环境变量和默认值的回退顺序保持不变。
检查点：`9fd4513`。

`tests.agent.test_deadline` 的 6 项边界测试通过（0.015 秒），定向 mypy、格式
和 diff 检查通过。该模块的 Ruff 告警由 13 项降至 6 项；剩余项全部位于本轮
未修改的 `kill_process_tree`，因此没有把整个改动文件宣称为全规则通过。

最终组合回归运行：

```sh
uv run --frozen --no-sync python -m unittest -v tests.agent.test_deadline tests.agent.test_conversations tests.services.test_job_controller_recovery tests.agent.test_runtime_import
```

共 28 项通过（3.677 秒）。这是对上述模块的组合重跑，不作为额外 28 项累加。

## 第十轮：模型名称归一化质量检查点

`model_normalize.py` 的 7 项 Ruff 告警已清理：删除未使用的 `Optional` 导入
和重复且同值的 Trinity 映射键；4 个宽异常分支逐项审查后，删除一层由内部
helper 已完整保护的重复捕获，其余 3 个保留公开入口“best-effort、永不因目录
查询失败而中断”的兼容契约，并增加带堆栈的 debug 记录。日志只包含提供商名
或固定说明，不记录凭据和完整配置。Copilot 目录失败时仍使用原有通用回退，
NVIDIA 目录修复仍只在后缀唯一匹配时添加 vendor 前缀。检查点：`a45e990`。

新增 6 项离线行为测试，覆盖 aggregator 前缀、Anthropic/Copilot 点号规则、
custom 透传，目录唯一/歧义/不存在，以及 alias、延迟目录导入和 Copilot 查询
抛出运行时异常时的兼容回退。测试通过（0.001 秒）；两个改动文件的 Ruff、
格式和 diff 检查通过。此处清理的是 7 项静态质量告警，不代表发现或修复了
7 个功能缺陷，也没有据此更新 F11 的全库剩余数量。

`model_metadata.py` 的 `TC004` 对应刻意的 `requests` 延迟导入，各运行路径先
调用 `_ensure_requests`；不能机械改成顶层导入而破坏启动和导入性能约定。

## 第十一轮：OpenAI-compatible HTTP 取消验收

新增本机传输集成探针，使用正式 `ConversationOrchestrator`、`AIAgent` 和
OpenAI-compatible HTTP/SSE 路径覆盖两个卡住窗口：主请求已经发出但服务端
尚未返回 response headers，以及 `stream=true` 且收到首个 SSE 帧后服务端
不再发送事件。两种情况下，发出取消后非 daemon 的 `run_turn` 线程均约
0.2 秒退出；服务端观察到 peer EOF 并结束 handler，持久化回合为 cancelled，
没有 tool run、工具执行或模型重试。检查点：`f9a61bc`。

2 项完整定向测试通过（约 4.18 秒）；补充 `stream=true` 明确断言后只复验
其中 mid-SSE 一项（2.177 秒），不重复计为第 3 项。改动测试文件的 Ruff、
格式与 diff 检查通过。探针只证明 loopback OpenAI-compatible HTTP/SSE
传输在这两个窗口能被取消，不能外推到其他 provider 或资源关闭路径。

## 第十二轮：原生 Anthropic client 关闭所有权

F16 已修复：完整生产 `AIAgent` 使用 Anthropic native transport 构造共享
SDK client 后，旧的 `agent.close()` 只清理 request client 缓存，shared
primary client 仍被 `_anthropic_client` 持有且 `is_closed=false`。这证明
hard close 没有明确释放它拥有的该实例；没有据此声称每个回合都会永久泄漏
真实 provider socket。

现在 hard close 先取得并清除 shared client 引用，再调用 SDK `close()`；
关闭异常写入 debug 诊断，但不会阻止后续会话消息清理和 Agent 自有
SessionDB 清理。2 项离线测试使用完整生产 Agent 与真实 Anthropic SDK 构造
（不发网络请求），覆盖释放、清引用、重复关闭，以及 SDK close 抛错后仍继续
其他资源清理；全部通过（0.888 秒）。测试、格式、diff 与定向 Ruff 检查通过；
loop 全规则 Ruff 的既有 298 项未增加。检查点：`497a8ee`。

本轮没有改变跨线程 `release_clients` 的软淘汰语义，也没有验证真实外网 socket
或全部 provider。

## 第十三轮：iteration-limit summary 取消收尾

F17 已修复：iteration-limit summary 原先的 Codex Responses 及普通
OpenAI-compatible 分支绕过统一可中断请求边界，Anthropic 分支虽有 Relay
包装也仍直接调用 provider；回合在达到上限后等待总结时，应用 watcher 的取消
不能可靠终止该请求。现在首次总结及空响应重试都经 `_interruptible_api_call`
按现有 api mode 分派。进入总结前和每次请求前均重检取消；`InterruptedError`
不转成总结失败文本，Relay logical call 记录 cancelled，finalizer 把回合标为
interrupted、`failed=false`，且不会调用 kanban 的预算耗尽记录。

取消时只按对象身份删除本轮追加的 `MAX_ITERATIONS_SUMMARY_REQUEST`；收尾复核
还修正了一个确定性缺口：当请求边界同时追加另一条同文本用户消息时，旧的
“仅当合成提示仍是末项才 pop”会残留合成提示。现在删除精确对象，保留其他
同文本消息。资源清理、会话持久化和 Agent close 继续执行；持久化内容保留原
用户消息及已完成 tool result，不包含合成总结提示。正常总结及空响应重试、
Codex Responses 和 Anthropic Messages 的既有 request shape 均有离线用例固定。

首次验收时，8 项定向模块测试中 7 项离线用例通过；native loopback 探针因
受限沙箱禁止创建 listener 而跳过，单独执行在 server 创建阶段得到
`PermissionError: [Errno 1] Operation not permitted`，尚未进入模型 dispatch。
此限制保留为历史记录。

随后打开一次技术上允许外网的网络开关，但用户授权和本次测试行为仅限
`127.0.0.1`/`localhost`，并执行：

```sh
NO_PROXY=127.0.0.1,localhost timeout 90s .venv/bin/python -m unittest -v tests.agent.test_iteration_summary tests.agent.test_conversations
```

命令退出 0；23 项于 4.065 秒全部通过、零跳过，native loopback 用例实际执行为
`ok`。再直接运行 fixture，进程于 2.092 秒退出 0，并输出
`NATIVE_ITERATION_SUMMARY_CANCEL_OK:elapsed=0.347:status=cancelled`。该标记只在
正式 `ConversationOrchestrator` 回合已取消、请求连接观察到 peer EOF、server
handler 与 conversation worker 均退出、预算耗尽记录未触发、任务资源清理、
SessionDB 持久化及 Agent close 全部断言通过后产生。测试 stub 仅监听回环地址，
没有访问真实模型或公网。本轮没有新增其他 provider、CI 或发布证据；第十一轮
的两项 HTTP/SSE 结果仍是通用请求中断路径的现有证据。

首次沙箱阶段曾尝试精确暂存六个收尾文件，但因 `.git` 只读而失败；本轮 Git
checkpoint 由宿主普通用户在用户明确授权下对这六个文件创建，最终提交状态
以 Git 实际记录为准，不在本文中编造 commit。

## 第十四轮：既有 AP2112 板副本 smoke

根会话在隔离目录完成真实 KiCad 工具链 smoke：
`SMOKE_ROOT=/tmp/pcbdraft-real-board-smoke-20260905-Q8zIoy`，输入是现有 AP2112
项目的只读复制，KiCad CLI 版本 10.0.6。当前
`ApplicationService -> PCBToolExecutor` 顺序执行 `run_erc`、`run_drc`、
`render_board`，总命令退出 0、约 4.8 秒，副本 revision 59 -> 62。

- ERC：`validation/20260905T060318Z-95dedbcc/check.json`，outcome pass，
  0 error/0 warning；原始报告为同目录 `erc.raw.json`。
- DRC：`validation/20260905T060320Z-28b23e93/check.json`，outcome pass，
  0 error/0 warning；原始报告为同目录 `drc.raw.json`。
- 预览：`previews/20260905T060323Z-70c591dd/receipt.json`，其 `board.svg`
  为 13,378 bytes，KiCad subprocess exit 0。

用生产参数直接复验的实际命令如下，三者退出码均为 0：

```sh
kicad-cli sch erc --format json --severity-error --severity-warning --output $SMOKE_ROOT/direct-erc.json $SMOKE_ROOT/projects/ap2112-3v3-mvp-e8ec0d92/design/ap2112-3v3-mvp-e8ec0d92.kicad_sch
kicad-cli pcb drc --format json --severity-error --severity-warning --output $SMOKE_ROOT/direct-drc.json --schematic-parity $SMOKE_ROOT/projects/ap2112-3v3-mvp-e8ec0d92/design/ap2112-3v3-mvp-e8ec0d92.kicad_pcb
kicad-cli pcb export svg --output $SMOKE_ROOT/direct-board.svg --layers F.Cu,F.Mask,F.SilkS,Edge.Cuts --mode-single --fit-page-to-board --exclude-drawing-sheet $SMOKE_ROOT/projects/ap2112-3v3-mvp-e8ec0d92/design/ap2112-3v3-mvp-e8ec0d92.kicad_pcb
```

这是既有板副本的 ERC/DRC/预览 smoke，不是新模型生成、Smoke-10、BoardBench、
长流、完整 CI、发布、物理硬件或订单/生产验证；不据此声称真实生成能力新增
通过。
