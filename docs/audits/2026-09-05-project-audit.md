# PCBDraft 审计基线

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
