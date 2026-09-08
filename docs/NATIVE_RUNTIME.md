# 原生运行时的代码来源与维护方式

PCBDraft 直接维护复用的 Hermes 实现。核心会话循环、TUI、工具执行、认证、
模型适配与会话存储保留原有实现，再调整包路径及应用边界。没有另写一套同名
功能来替代这些代码。

迁移基线核对了 **696 个原有 Python 模块**，当时的落位记录见
[逐文件来源映射](native-runtime-source-map.json)。这份映射是历史来源台账，
不等同于当前文件清单；后续移除项记录在其中的 `retired` 段。迁移前的完整
版本可在 `9d7f1558af992e7c30f59a5198191bce63e00239:vendor/hermes/` 查阅。

| 原有实现 | 当前维护位置 |
| --- | --- |
| `run_agent.py` 与 Agent 辅助模块 | `src/pcbdraft/agent/` |
| `cli.py` 与终端界面、命令、渲染 | `src/pcbdraft/interfaces/tui/` |
| 工具注册、执行、结果处理 | `src/pcbdraft/tools/` |
| 模型、认证、传输、提供商定义 | `src/pcbdraft/model/` |
| SQLite 会话数据库 | `src/pcbdraft/services/session_db*.py` |
| 时间、日志、运行环境等公用功能 | `src/pcbdraft/core/` |

原有的两个包初始化文件由项目现有包或原生会话上下文代替；独立程序的
`setup.py` 和 `nemo_relay.py` 不作为 PCBDraft 的运行入口。原始文件仍留在
上述 Git 版本中。MIT 许可完整保存在 `data/licenses/Nous-Research-MIT.txt`，
`NOTICE` 明确保留上游归属。两个唤醒词模型文件也与迁移前字节一致；名称
代表实际模型资源，不能仅为去掉字样而更改其训练语义。

旧的路径注入、启动方法补丁、磁盘观察器插件，以及运行时重写命令注册表的
辅助函数已移除。TUI 命令直接声明于本项目，PCB 命令处理位于
`interfaces/tui/project_commands.py`。2026-09-08 又移除了没有 PCBDraft 入口的
Hermes dashboard/Web 服务、路由、PTY 桥接及其旧命令注册；PCBDraft 的本地 GUI
仍由 `pcbdraft.interfaces.gui` 和 `src/pcbdraft/web/` 提供。终端和 Web 使用同一个
`AIAgent`；Web 的持久化调度负责记录工具、绑定工程和权限、处理审批与取消。

现有 PCBDraft 专用配置目录仍可读取；独立安装的 Hermes 配置不参与解析。
配置兼容字段、模型资源名和版权归属保留必要的历史名称，不表示另有一层
Hermes 应用在运行。

这次吸收也使原有复用代码进入统一静态检查范围。全库 Ruff 尚未通过，
不能把路径迁移和定向运行测试当作这些代码的完整质量认证。详细缺陷、
本轮验证和保留的集成验证见[项目审计报告](audits/2026-09-05-project-audit.md)。

原生会话稳定化的 F12–F14 已完成定向整改：旧回合有完成 receipt 时不再恢复
模型，恢复作业绑定创建它的 native/legacy 控制器，普通模型分支也会在 Agent
初始化返回后、读取 history 和发出模型请求前同步重检取消与截止。审批分支
直接执行精确匹配的已批准调用并交付本地 receipt，无需创建模型。

同步初始化本身仍无法强杀。正式 OpenAI-compatible HTTP/SSE 路径已经用
本机服务验证：请求等待 response headers，或收到首个 SSE 帧后停止响应时，
取消均会关闭连接并让回合以 cancelled 结束，且不执行工具或重试。该证据只
覆盖这一传输的两个窗口；其他 provider 和 watcher 中断阻塞时的清理上限仍
未完成验证。Anthropic native transport 的 shared SDK client 已明确纳入
Agent hard close 所有权，并验证重复关闭和 SDK 关闭异常不妨碍其余自有资源
清理；跨线程 soft release 和真实外网 socket 仍不在这项证据内。
iteration-limit summary 的首次及空响应重试现均进入同一可中断请求边界：
预先存在的取消不发请求，请求中取消传播为 interrupted，而不是预算耗尽或
普通失败；只按对象身份删除本轮追加的合成总结提示，随后仍执行会话持久化、
任务资源清理和 Agent close。7 项离线用例覆盖上述边界和 chat、Codex
Responses、Anthropic Messages 的正常请求形状。首次验收时受限沙箱禁止创建
loopback listener（`PermissionError: [Errno 1]`），探针因此跳过；随后打开一次
技术上允许外网的网络开关，但用户授权和本次测试行为仅限
`127.0.0.1`/`localhost`，23 项定向测试于 4.065 秒全部通过、零跳过。直接运行
native loopback fixture 也退出 0：取消耗时 0.347 秒，状态为 cancelled，且
连接关闭、worker 退出、资源清理、会话持久化、Agent close 以及“不记为预算
耗尽”的硬断言全部通过。没有访问真实模型或公网。首次沙箱阶段曾尝试精确
暂存六个收尾文件，但因 `.git` 只读而失败；本轮 Git checkpoint 由宿主普通
用户在用户明确授权下对这六个文件创建，最终提交状态以 Git 实际记录为准。
这些边界不改变上述来源结论：696 是迁移基线的来源核对数；当前仍按职责维护
其中被 PCBDraft 使用的实现，已退役文件可由来源台账和基线提交追溯。

统一 deadline 层也已修复超大整数转换的溢出：正值收敛到平台安全上限，
负值保留既有无界语义。该模块仍有 6 项位于未改进程树终止代码的 Ruff 告警，
全库静态债没有据此清零。

模型名称归一化模块完成了一个独立质量检查点：删除无效导入和重复映射，
为必须保留的 provider/catalog best-effort 降级补充 debug 诊断，并用离线测试
固定 aggregator、原生提供商、custom、目录唯一性和目录异常时的既有输出。
该模块的 7 项 Ruff 告警已清理；没有更改提供商名称、默认模型或外部协议，
也没有重新统计全库静态债。

另以既有 AP2112 项目的只读副本做了一次真实板级 smoke。隔离根目录
`/tmp/pcbdraft-real-board-smoke-20260905-Q8zIoy`，KiCad 为 10.0.6；当前
`ApplicationService -> PCBToolExecutor` 顺序执行 `run_erc`、`run_drc`、
`render_board`，总命令退出 0、约 4.8 秒，副本 revision 从 59 到 62。ERC 与
DRC 均为 pass、0 error/0 warning，preview 的 `board.svg` 为 13,378 bytes；
生产参数对应的三条 `kicad-cli` 命令也分别退出 0。该结果只验证现有板副本的
ERC/DRC/预览链，不是新模型生成、Smoke-10、BoardBench、长流、完整 CI、发布、
物理硬件、订单或生产验证，也不构成新增真实生成能力通过证据。精确命令与
产物位置记录在[项目审计报告](audits/2026-09-05-project-audit.md)。
