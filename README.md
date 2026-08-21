# PCBDraft

![PCBDraft 标志](docs/assets/brand/pcbdraft-mark-256.png)

PCBDraft 是一个采用 Apache-2.0 许可证的独立 PCB 设计智能体。你用自然语言描述想做的
电路板，它负责整理需求、规划电路、生成原生 KiCad 工程，并把连接检查、
ERC、DRC 和每一步的证据留在本地。

它面向小型、低压、非安全关键的原型板。生成结果是工程候选，仍然需要
人工审查，不能替代电气、布局、热、EMC 或制造工程师的签字。

## 特性

- 类似编程智能体的交互终端界面，支持中文自然语言输入；
- 默认即自主 Agent：Hermes Agent 自己观察工程、选择工具、阅读真实结果并
  决定下一步，没有固定的 plan/generate/validate/repair/release 顺序；
- 不要求用户预先决定层数、尺寸或全部器件；
- 只使用本机安装的 KiCad 符号和封装；
- 模型负责工程思考与工具选择，确定性代码负责生成 KiCad 文件、权限、
  revision、事务与证据；
- 自动布局、布线、连接检查、ERC、DRC 和项目一致性检查；
- 所有模型服务都通过 PCBDraft 自己的配置文件接入，不依赖其他 CLI；
- 失败会保留计划、工程和错误信息，方便继续修改。

## 快速开始

PCBDraft 支持 Linux、macOS 和 Windows，兼容稳定版 KiCad
`>=10.0.0,<10.1.0`。当前发布的精确验收基线是 10.0.5；同一 10.0 系列的其他
稳定 patch 可以运行，但 `pcbdraft doctor` 会如实标注它不是本发布的精确基线。

### Linux / macOS 一键安装

只需系统自带的 shell 和 `curl`。安装器会检测 KiCad 与 uv；uv 缺失或过旧时使用
官方固定版本安装器，KiCad 缺失时在 Ubuntu、Debian、Fedora、Arch Linux 或装有
Homebrew 的 macOS 上调用对应包管理器。安装 KiCad 时可能要求输入管理员密码，
PCBDraft 本身仍安装在当前用户目录：

```bash
installer=$(mktemp /tmp/pcbdraft-install.XXXXXX)
curl --fail --location --proto '=https' --tlsv1.2 \
  --output "$installer" \
  https://raw.githubusercontent.com/qixuancao/pcbdraft/main/scripts/install.sh
bash "$installer"
rm -f -- "$installer"
```

安装器会先把公开 `main` 解析为完整提交 SHA，随后只从这个不可变提交读取源码和
锁定约束。记录最后输出的 SHA 即可复现；也可传入
`--ref 40位提交SHA`。不需要 Git、预装 Python 或 GitHub 凭据。

### Windows 一键安装

在普通 PowerShell 中运行；安装器使用 uv 官方安装器，并通过 WinGet（或已有的
Chocolatey）安装缺失的 KiCad：

```powershell
$installer = Join-Path $env:TEMP "pcbdraft-install.ps1"
Invoke-WebRequest `
  https://raw.githubusercontent.com/qixuancao/pcbdraft/main/scripts/install.ps1 `
  -OutFile $installer
powershell -ExecutionPolicy Bypass -File $installer
Remove-Item $installer
```

## 第一次启动

电路规划需要连接一个模型服务。首次在交互终端启动 `pcbdraft`
时会直接打开提供商、登录/API Key 和模型选择向导；也可先单独运行：

```bash
pcbdraft connect
```

完成后再启动 `pcbdraft`，直接输入任意一句板卡需求即可。

环境检测和非破坏性修复统一由下面两个命令完成：

```bash
pcbdraft setup
pcbdraft doctor --json
```

PCBDraft 自身始终按用户级安装，不修改系统 Python，也不覆盖已有的 KiCad 配置；
只有安装缺失的系统 KiCad 包时才会通过包管理器请求管理员权限。它会：

- 检测稳定版 KiCad 10.0.x、`kicad-cli` 和 KiCad 自带的 `pcbnew` Python；
- 在已支持的平台包管理器上安装缺失的 KiCad；
- 检测或安装 uv，并由 uv 管理 PCBDraft 所需的 Python；
- 用提交内锁定的运行时约束和 `uv tool install` 安装独立命令；
- 按操作系统找到 KiCad 数据与配置目录，只初始化缺失的符号和封装库表。

安装器最后会打印命令的实际位置。Linux 和 macOS 通常位于
`~/.local/bin/pcbdraft`；若首次运行提示找不到命令，执行：

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

模型选择和认证信息保存在平台的 PCBDraft 用户配置目录下的
私有 `hermes/` 子目录。该目录与独立安装的 Hermes 及其 `~/.hermes`
数据隔离。首次启动还会创建并
记录统一的 PCB 项目仓库 `~/PCBDraft/`；以后无论从哪个目录执行 `pcbdraft`，
新项目、KiCad 原理图、`.kicad_pcb`、检查记录和发布文件都会位于这个仓库的
`projects/` 下。要更新或修复安装，重新运行上面的安装命令即可。

## 从源码运行（开发）

需要 Python 3.11 或更高版本、[uv](https://docs.astral.sh/uv/)，以及稳定版
KiCad 10.0.x。Linux、macOS 和 Windows 的包导入、安装路径、锁与子进程边界会在
CI 中检查，并分别运行真实 KiCad 的首板生成验收。

```bash
git clone https://github.com/qixuancao/pcbdraft.git
cd pcbdraft
uv sync --extra dev
uv run pcbdraft setup
uv run pcbdraft doctor --json
```

## 配置模型

运行 `pcbdraft connect`，或在交互终端中输入 `/connect`、`/model`，
可以连接、切换或重新认证当前内置 Hermes 版本发现的提供商。向导支持
API Key、浏览器/设备代码登录、云身份、本地端点、聚合服务和自定义端点；
无浏览器的远程终端可使用 `pcbdraft connect --no-browser`。

提供商认证、端点检测、令牌刷新和传输路由随 PCBDraft 打包的 Hermes
运行时统一处理。PCBDraft 仍会对用于板卡规划的返回值执行本地 JSON Schema
和领域校验。密钥和刷新令牌只保存在私有认证存储中，不会写入 PCB
工程、对话记录、调试跟踪或模型运行收据。

## 启动

```bash
uv run pcbdraft
```

默认项目仓库是首次启动时创建的 `~/PCBDraft/`。如要放在另一块磁盘或已有的
工程目录中，只需设置一次：

```bash
pcbdraft repository /path/to/my-pcb-repository
pcbdraft repository --json  # 查看当前位置
```

该位置记录在与模型配置相同的平台配置目录下的 `repository.json`。切换仓库只影响
之后打开和创建的 PCBDraft 项目；原仓库中的文件不会被移动或删除。

直接描述电路板即可。用户没有指定层数时，PCBDraft 会根据小型原型的约束
自动选择保守的初始方案，不要求用户理解叠层设计。

## 默认工作方式：自主 Agent + 工具

PCBDraft 默认不再按固定顺序驱动“规划→生成→验证→修复→发布”。默认模式是
一个类似 Coding Agent 的循环：Hermes Agent 拿到一个持续存在的 PCB 目标
（standing goal），自己观察当前工程、选择下一个工具、阅读真实结果，再决定
继续、修改、检查、回退还是结束。

```text
用户持续目标
      ↓
  Hermes Agent
  ↙    ↓    ↘
查看   设计   修改
  ↘    ↓    ↙
 PCB 工具注册表（具体扁平操作）
      ↓
 ApplicationService（权限、revision、事务）
      ↓
 语义设计图 / KiCad
      ↓
 事实与证据
      ↓
  Hermes Agent
      ↓
continue / done / blocked
```

要点：

- **工具结果只报告事实**。每次调用返回执行了什么、是否成功、改变了什么、
  当前状态、发现了什么、有什么限制，以及证据引用。结果不包含
  `next_step` 这类流程指令；下一步永远由 Agent 自己判断。
- **每次结果之后 Agent 重新选择工具**。没有“模型只选第一个工具，之后由
  本地固定流程接管”的限制；同一次模型响应中即使生成多个 PCB 调用，也只
  执行第一个并逐一返回结果，避免在看见事实前连续修改工程。
- **项目状态只是工程事实**（`draft`、`generated`、`validation_failed`、
  `validated` 等），不唯一决定下一个工具。
- **模型只看到具体扁平工具**。工程、检查、符号/封装库、语义编辑、摆放、
  布线、验证、渲染和导出分别使用独立名称，例如 `pcb_inspect_design`、
  `pcb_search_footprints`、`pcb_add_component`、`pcb_connect_pin`、
  `pcb_place_footprint`、`pcb_route_net`、`pcb_run_drc` 和
  `pcb_export_gerbers`。没有再通过 `operation` 选择第二层动作的 router，
  也不向模型暴露一次执行整个阶段的宏。
- **一次调用只做一个动作**。成功的写操作在同一事务里更新语义 IR、重新
  物化 KiCad、检查同步关系并返回 revision、前后内容哈希和事实差异；任何
  阶段失败都不会发布部分结果。
- **会话绑定一个当前工程**。模型不能列出或打开其他工程；`/new`、`/open`
  和 `--project` 是可信的工程选择边界。已安装的符号/封装可在选工程前查询，
  当前工程的器件目录可用 `pcb_search_parts` / `pcb_describe_part` 查询；本机
  KiCad 已有但目录未收录的组合可用 `pcb_register_kicad_part` 原子登记。
  成功切换工程后，终端会静默开始一个新的 Hermes 对话，旧工程的工具结果
  不会进入新工程的下一次模型请求。

### Goal Mode（持续目标）

用 `/goal <目标>` 设立一个持续目标后，Agent 每轮结束由独立 judge 判断
`done` / `continue` / `wait`：`continue` 时自动向同一会话追加一条简单的
continuation 消息继续推进；`wait` 时暂停等待；达到通用 turn/tool 预算时
如实暂停，不假装完成。用户的新消息随时可以暂停、修改或替换当前目标。
Continuation 消息只重申目标并要求“检查当前工程状态、做你判断最有用的下一
个具体工程动作”，不规定必须执行哪个阶段。

### 语义设计图（Design Graph）

CircuitPlan 和语义/原生意图 IR 保留为可查看、可逐步演化的设计表示。Agent
使用 `pcb_inspect_design`、`pcb_inspect_component` 和 `pcb_inspect_net` 查看
组件、功能块、网络、电源域、接口及保留的板级几何；再通过独立的 add、
remove、update、connect、place、route 或 via 工具逐项修改。旧版 IR 在只读
打开时保持原字节和哈希，第一次成功写入才原子升级为 IR v2。

### 安全边界保持不变

自由的是工程决策，硬约束的是权限、数据完整性和真实执行：

- 所有写操作仍经过封闭工具目录、严格 Schema 校验、
  `PermissionBroker`、baseline revision 检查、事务/工程锁和
  ApplicationService——它是唯一工程写入权威；
- 模型不能任意执行 Python/shell、不能任意写文件系统、不能直接手写原始
  KiCad 文本，也不能引用 ApplicationService 内部方法；
- ERC/DRC 等检查结果只能来自真实执行，不能由模型伪造；
- durable dispatch 之后结果不明时照旧 fail closed。

### Legacy 模式（durable job 路径）

Legacy durable Agent 回合仍由 `AgentOrchestrator`/`JobRunner` 驱动，
其历史上的确定性后续工具策略（先由模型选一次工具、之后本地策略接管）保留
为 legacy 兼容模式和显式快捷方式（`/validate`、`/confirm` 等）。它不再是
默认 Hermes Agent 的控制器。持久化、恢复、预算和审批仍由
`AgentOrchestrator`/`JobRunner` 负责；它们不决定 PCB 工程下一步。

Legacy durable 路径中的每条消息都是一个可恢复的 Agent 回合。每次工具调用都会在
执行前持久化，并绑定当前工程 revision；进程中断后，显式 `/retry` 会从原 turn
中尚未 dispatch 的边界继续，不会重新解释已经完成的那条需求。若进程在一个写工具
dispatch 后、精确结果收据落盘前退出，运行时会保守停止并要求检查工程后提交新 turn，
不会用 `/retry` 猜测并重放可能已经发生的副作用。同样，工具在 durable dispatch
之后抛错、但本地 revision/receipt 又不能证明精确结果时，会记为不可重放的
interrupted/outcome-unknown；模型选择的直接动作若未完成，也不会根据当前状态推导成
另一个动作（例如把失败的“丢弃候选”变成“应用候选”），而是要求提交新 turn。

持久化 Agent Job 使用版本化的执行策略快照，精确绑定 permission mode、工具目录
指纹和单回合工具上限。启动恢复和 `/retry` 只有在当前运行时与该快照完全一致时才会
继续尚未 dispatch 的调用；旧版无策略 Job、历史 direct action、缺失 durable turn，
以及任何绑定不明的记录都会 fail closed，只保留可见的 cancelled/interrupted/failed
审计结果，不会因为重启而获得更宽权限。

原生路由请求会在发出前写入工程内的
`agent-turns/model-decisions/{turn_id}-router.json`。已完成的决策只会按原调用
复用；已 dispatch 但结果不明，或已明确失败的决策，都不会自动再向模型
POST，而是保守回到本地策略。该 journal 只记录工具选择边界，不能代替本地
KiCad 检查和工程证据。无论调用来自模型、MCP 还是本地策略，都必须通过
`PermissionBroker`、封闭工具目录、严格参数 Schema 和 revision 检查；模型不会因此
获得文件系统、shell 或原始 KiCad 写权限。

默认的 `--approval-mode workspace` 会继续执行用户要求的本地工程操作；希望在
每次 authoritative write 前人工确认时，可用
`uv run pcbdraft --approval-mode review`。`read_only` 会拒绝所有会留下持久状态的
PCB 工具。

交互终端中常用命令：

| 命令 | 作用 |
| --- | --- |
| `/connect` | 连接、切换或重新认证模型提供商 |
| `/goal <目标>` | 设立持续目标；Agent 循环推进直到完成、阻塞或预算暂停 |
| `/goal status` / `pause` / `resume` / `clear` | 管理当前持续目标 |
| `/model` | 打开同一提供商与模型向导 |
| `/project [路径]` | 查看当前项目仓库；提供路径时切换后续项目的统一存储位置 |
| `/new <名称>` | 在项目仓库中创建新 PCB 项目并设为当前上下文（不传名称显示用法） |
| `/projects` | 列出项目仓库中的全部项目（空仓库给出可操作的提示） |
| `/open <id>` | 打开指定项目 |
| `/review` | 查看计划、变更和检查证据 |
| `/confirm` | 仅批准当前精确绑定的工具调用，或生成已审查方案 |
| `/discard` | 拒绝待审批调用，或丢弃已暂存变更 |
| `/logs [id]` | 显示最近工程事件 |
| `/stop` | 在安全边界停止当前任务 |
| `/retry` | 继续尚未 dispatch 的失败边界；绝不重放结果不明的写调用 |
| `/validate` | 重新运行检查 |
| `/release` | 生成制造候选证据包 |
| `/quit` | 退出交互终端 |

`/help` 显示全部可用命令。与 PCB 无关的 Hermes 内置命令
（消息网关、语音、看板、计费、技能市场等）已从帮助、自动补全和分发中裁剪，
交互终端只暴露 PCBDraft 需要的命令面。

## 工程内核（能力，不是界面步骤）

下面列出的是内核具备的工程能力。它们是 Agent 可以按任意顺序使用的活动，
不是必须逐个经过的阶段：

```text
约束提取 · 电路计划 · KiCad 符号解析
· 原理图与 PCB 生成 · 布局/布线 · 连接检查、ERC、DRC · 人工审查
```

语义设计图（CircuitPlan v2 / 语义 IR）会表达层级功能块、电源域、接口、
连接器完整引脚表、网络标签、命名布局区域、锚点禁布区、差分对验收条件和
可本地复算的断言。它是可查看、可逐步演化的设计表示，不要求一次性产出完整
JSON 才能开始其他工作。模型只能选择受限名称和尺寸，不能直接写坐标、走线
或 KiCad 文件文本；布局、禁布和生成后几何指标都由本地确定性代码执行与记录。
当前差分对能力验证实际线宽、边到边间距、耦合长度比例和长度差，不代表阻抗
仿真，也不代表已有专用的耦合布线器。

生成的工程可以直接用 KiCad 打开和继续编辑。PCBDraft 不锁定文件格式，
也不把模型服务绑定到某一家供应商。

L4/L6/L7 外部记录会被复制、哈希并校验结构，但不会被当作已认证的签字。
`production_evidence_complete` 只表示声明的证据槽位齐全；PCBDraft 始终保持
`production_ready=false`，生产放行必须在本工具之外由有权限的工程流程完成。

## 源码结构

实现代码不再平铺在包根目录，而是按职责分层：

| 目录 | 职责 |
| --- | --- |
| `pcbdraft/core/` | 错误、文件安全、锁、进程和项目基础设施 |
| `pcbdraft/domain/` | PCB IR、需求、器件、规则和变更模型 |
| `pcbdraft/agent/` | Agent 计划、事件、运行时、修复和工具边界 |
| `pcbdraft/model/` | 模型配置、结构化调用和供应商适配 |
| `pcbdraft/kicad/` | KiCad 原理图、PCB、布局、布线、预览与同步 |
| `pcbdraft/services/` | 应用服务、任务、托管工程、事务和工作流 |
| `pcbdraft/verification/` | 证据、验证、评审、基准和发布门禁 |
| `pcbdraft/interfaces/` | CLI 与 Hermes 交互终端（命令面、终端启动、调试插件） |

`tests/` 使用相同的职责目录，能够直接找到每层对应的测试。详细边界和
新增代码的放置规则见 [项目结构说明](docs/PROJECT_STRUCTURE.md)。1.0 版本
暴露过的旧 Python 模块路径仍由惰性兼容层支持，新代码应使用上表中的规范路径。

## 开发

```bash
scripts/test.sh
```

如需清理可能污染 wheel 的本地构建缓存，运行 `scripts/clean.sh` 或
`make clean`。发布检查会在构建前后自动执行这一步。

欢迎提交 Issue 和 Pull Request。

## 许可证

PCBDraft 使用 Apache License 2.0，详见 [LICENSE](LICENSE)。
