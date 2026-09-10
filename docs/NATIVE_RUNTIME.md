# 原生运行时的代码来源与维护方式

PCBDraft 直接维护吸收自 Hermes 的实现。核心会话循环、TUI、工具执行、认证、
模型适配与会话存储在本项目内按职责演进；运行时路径、内部标识和资源所有权
使用 PCBDraft 原生命名。上游来源与现行运行身份分别记录。

迁移基线核对了 **696 个原有 Python 模块**，当时的落位记录见
[逐文件来源映射](native-runtime-source-map.json)。这份映射是历史来源台账，
不等同于当前文件清单；后续移除项及运行功能退役记录在其中的 `retired` 段。迁移前的完整
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

## 原生身份与迁移操作（2026-09-10）

core 正常路径解析是纯路径计算，仅以 `PCBDRAFT_RUNTIME_HOME` 覆盖平台默认的
PCBDraft 配置目录下 `runtime` 子目录，不探测或迁移旧目录。原生 helper 使用
`runtime_home()`、`get_runtime_home()`、`get_pcbdraft_dir()` 等名称；进程内任务
隔离继续使用 context-local runtime override。

启动层显式调用集中迁移模块，只处理 **PCBDraft 自有配置目录**下的旧 `hermes`
子目录。独立安装的 Hermes home 不参与发现、解析或接管：

- 只有旧目录存在且预检通过时，原子 rename 为同级 `runtime`，写入
  `runtime-migration.json`；记录写入失败时尝试回滚并报告错误。
- 旧、新目录同时存在时保留两者，写入 `runtime-migration-conflict.json`。
  低层 resolution 返回 native 路径及 `conflict` 状态；启动层明确报错并停止绑定，
  不会使用 native 继续启动，也不合并或覆盖。核对两份数据后，可显式设置
  `PCBDRAFT_RUNTIME_HOME` 选择要使用的目录。
- 允许可信父路径 canonicalization（如 macOS 的 `/var` 别名），但拒绝选定配置
  根及 `hermes`、`runtime` 迁移根本身为 symlink；不通过解析根链接绕过检查。
- 可能被 rename 破坏的内部绝对符号链接、已知配置路径字段指向旧根时拒绝迁移，
  保留原目录。修复引用后重试，或显式设置 `PCBDRAFT_RUNTIME_HOME` 指向现有目录。
  runtime 中的外部链接原样保留，不跟随目标。预检不扫描凭据和数据库内容，
  也不做全文替换。
- `PCBDRAFT_HERMES_HOME` 已停止支持；启动会明确提示升级。把原值改设为
  `PCBDRAFT_RUNTIME_HOME` 并移除旧变量后重启。新变量的显式覆盖跳过目录迁移。

项目元数据必须单独指定项目执行（将 `PATH` 替换为项目目录）：

```sh
python -m pcbdraft.core.legacy_migration --project PATH
```

该命令仅将项目 `.hermes` 中的 `environment.json`、`skills/`、`plugins/`
复制到 `.pcbdraft`，始终保留源。相同文件可重复运行；任一内容或类型冲突阻止
本次复制，不覆盖目标。允许可信父路径 canonicalization；拒绝选定项目根、
元数据根及所选源、目标树内被迁移节点的 symlink，不跟随这些链接。
JSON 结果提供 `copied`、`unchanged`、`conflicts` 和 `source_retained`；冲突或
错误退出 1，否则退出 0。先核对冲突内容、人工处理后再运行。`plans/` 不在迁移
范围内；本仓库旧计划的公开历史仅见[归档摘要](archive/plans/2026-08-30/README.md)。

| 运行边界 | 现行约定 |
| --- | --- |
| 模型工具 MCP 身份 | `pcbdraft-tools` |
| 插件 entry-point groups | `pcbdraft.plugins`、`pcbdraft.memory_providers` |
| 插件 Python namespace | `pcbdraft_plugins.<slug>` |
| root sandbox / 远端 runtime | `/root/.pcbdraft/runtime` / `<remote_home>/.pcbdraft/runtime` |
| Docker 所有权 | 只复用、清理 native owner；不认可旧 Hermes owner |
| 默认 skills/catalog | 离线本地内容；外部索引、来源和同步需显式配置 |
| Tirith | 可离线使用已安装 binary；下载另需显式 opt-in |

Tirith 下载可由 `security.tirith_allow_download: true`、
`TIRITH_ALLOW_DOWNLOAD=1` 或内部显式 `allow_download=True` 开启；
仅设置 `tirith_enabled` 不授权下载。更多工具边界见
[工具迁移说明](../src/pcbdraft/tools/MIGRATION.md)。

没有公共入口的旧 gateway、源码 updater、uninstaller、desktop backend service
和 profile 管理写操作已退役，内部调用明确返回不支持，公共 CLI 命令面不变。
已有 profiles 的读取和连接向导继续保留；profile 内部描述元数据编辑仍是有限的
保留能力，不代表恢复 profile 管理入口。安装维护使用原安装工具，支持的命令
以 `pcbdraft --help` 为准，模型连接使用 `pcbdraft connect`。

保留例外包括版权与历史 source map、真实第三方模型 ID、未默认启用的原始唤醒词
binary、已注册 OAuth 服务标识和旧加密格式，以及集中 legacy 数据读取和保护其他
应用的安全扫描规则。旧 namespace ID 数据保留 effective IDs，即使再次保存也
不改写其身份；新写入的 source/provenance 使用 native 标识，并非所有持久化值
都改名。这些兼容数据不构成旧运行身份的别名。变更范围和最终主审验证证据见
[原生身份迁移审计](audits/2026-09-10-native-identity-migration.md)。

## 历史验证与质量边界

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
