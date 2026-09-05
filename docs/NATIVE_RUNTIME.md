# 原生运行时的代码来源与维护方式

PCBDraft 直接维护复用的 Hermes 实现。核心会话循环、TUI、工具执行、认证、
模型适配与会话存储保留原有实现，再调整包路径及应用边界。没有另写一套同名
功能来替代这些代码。

本次核对了 **696 个原有 Python 模块**，每个都有当前源码位置，详见
[逐文件来源映射](native-runtime-source-map.json)。迁移前的完整版本可在
`9d7f1558af992e7c30f59a5198191bce63e00239:vendor/hermes/` 查阅。

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
`interfaces/tui/project_commands.py`。终端和 Web 使用同一个 `AIAgent`；
Web 的持久化调度负责记录工具、绑定工程和权限、处理审批与取消。

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

同步初始化本身仍无法强杀；真实提供商 HTTP 卡住能否在全部传输上可靠退出，
以及 `agent.close()` 或 watcher 中断阻塞时的清理上限，尚未完成验证。这些
边界不改变上述来源结论：696 个原有 Python 模块仍按职责直接复用和维护，
没有销毁后另写替代实现。

统一 deadline 层也已修复超大整数转换的溢出：正值收敛到平台安全上限，
负值保留既有无界语义。该模块仍有 6 项位于未改进程树终止代码的 Ruff 告警，
全库静态债没有据此清零。
