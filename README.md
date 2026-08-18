# PCBDraft

![PCBDraft 标志](docs/assets/brand/pcbdraft-mark-256.png)

PCBDraft 是一个采用 Apache-2.0 许可证的独立 PCB 设计智能体。你用自然语言描述想做的
电路板，它负责整理需求、规划电路、生成原生 KiCad 工程，并把连接检查、
ERC、DRC 和每一步的证据留在本地。

它面向小型、低压、非安全关键的原型板。生成结果是工程候选，仍然需要
人工审查，不能替代电气、布局、热、EMC 或制造工程师的签字。

## 特性

- 类似编程智能体的全屏终端界面，支持中文自然语言输入；
- 不要求用户预先决定层数、尺寸或全部器件；
- 只使用本机安装的 KiCad 符号和封装；
- 模型只负责受约束的需求理解和电路计划，确定性代码负责生成 KiCad 文件；
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

电路规划需要配置一个模型服务。运行 `pcbdraft` 进入 TUI 后输入 `/connect`，
选择 DeepSeek、MiniMax、Kimi、OpenAI、OpenRouter、本地 Ollama 或自定义
OpenAI 兼容服务并填写 API Key，之后直接在 TUI 中输入任意一句板卡需求即可。

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

模型服务和 API Key 保存在平台的用户配置目录（Linux 默认为
`~/.config/pcbdraft/config.toml`，macOS 默认为
`~/Library/Application Support/pcbdraft/config.toml`，Windows 默认为
`%APPDATA%\pcbdraft\config.toml`）。首次启动会创建并
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

启动 TUI 后输入 `/connect`，选择 DeepSeek、MiniMax、Kimi、OpenAI、
OpenRouter、本地 Ollama 或自定义 OpenAI 兼容服务，然后输入 API Key。
输入 `/models` 可以搜索并切换模型。

远程模型地址必须使用 HTTPS。只有字面量回环地址（`localhost`、`127.0.0.0/8`
或 `::1`）可以使用 HTTP，以支持本机 Ollama；模型调用不会跟随重定向，返回值还
会在本地再次执行 JSON Schema 校验。

模型接入层会按服务声明生成兼容请求：DeepSeek 使用 JSON Object，MiniMax M2
使用提示词约束并由本地 Schema 做最终裁决，Kimi、OpenAI、OpenRouter 和 Ollama
使用各自支持的结构化输出与令牌字段。网络瞬断、408、429 和 5xx 只会在同一个
总超时内进行至多三次带抖动的重试，并遵守有界 `Retry-After`；PCBDraft 不会在
失败后静默切换模型或提供商。

密钥由 PCBDraft 写入上述平台配置目录，且在 POSIX 系统使用文件权限 `600`；
它不会进入 PCB 工程、对话记录或运行收据。也可以手动创建同样的配置：

```toml
version = 1
active_provider = "deepseek"
active_model = "deepseek-v4-pro"

[providers.deepseek]
name = "DeepSeek"
base_url = "https://api.deepseek.com"
api_key = "在这里填写密钥"
models = ["deepseek-v4-pro", "deepseek-v4-flash"]
docs_url = "https://platform.deepseek.com/"
```

在 Linux 手动创建后执行；macOS 请对上面列出的实际配置目录应用相同权限：

```bash
chmod 700 ~/.config/pcbdraft
chmod 600 ~/.config/pcbdraft/config.toml
```

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

TUI 中的每条消息现在都是一个可恢复的 Agent 回合。规划、生成、检查和有限修复会以
对话内工具活动显示，而不是要求用户按固定四步逐页推进。每次工具调用都会在
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

当前使用混合路由，而不是把完整工程循环交给模型。只有同时选中内置
OpenAI preset、provider ID 为 `openai`，且 `base_url` 主机精确为
`api.openai.com` 时，每个自然语言 turn 才会在开始处最多调用一次
OpenAI Responses function tool，从当前工程状态允许的 `pcb_*` 工具中选择
首个动作。规划之后必须执行的生成、验证、证据驱动修复、审批和 revision
检查仍由本地确定性策略推进。所有其他预设、自定义 OpenAI 兼容地址和本地
模型都使用本地路由回退；它们仍可用于受约束的需求解释和电路规划，但不会被
当作原生 Agent 控制面。

原生路由请求会在发出前写入工程内的
`agent-turns/model-decisions/{turn_id}-router.json`。已完成的决策只会按原调用
复用；已 dispatch 但结果不明，或已明确失败的决策，都不会自动再向模型
POST，而是保守回到本地策略。该 journal 只记录工具选择边界，不能代替本地
KiCad 检查和工程证据。无论调用来自模型、MCP 还是本地策略，都必须通过
`PermissionBroker`、封闭工具目录、严格参数 Schema 和 revision 检查；模型不会因此
获得文件系统、shell 或原始 KiCad 写权限。

TUI 默认的 `--approval-mode workspace` 会继续执行用户要求的本地工程操作；希望在
每次 authoritative write 前人工确认时，可用
`uv run pcbdraft --approval-mode review`。`read_only` 会拒绝所有会留下持久状态的
PCB 工具。

TUI 中常用命令：

| 命令 | 作用 |
| --- | --- |
| `/connect` | 添加或更新模型服务和 API Key |
| `/models` | 搜索并选择当前模型 |
| `/project [路径]` | 查看当前项目仓库；提供路径时切换后续项目的统一存储位置 |
| `/new [名称]` | 可选：先创建一个有名称的空项目；直接输入需求也会自动建项目 |
| `/projects` | 打开已有项目 |
| `/review` | 查看计划、变更和检查证据 |
| `/confirm` | 仅批准当前精确绑定的工具调用，或生成已审查方案 |
| `/discard` | 拒绝待审批调用，或丢弃已暂存变更 |
| `/logs on` | 展开跨 turn 的工具参数、风险、revision 与有界结果收据 |
| `/stop` | 在安全边界停止当前任务 |
| `/retry` | 继续尚未 dispatch 的失败边界；绝不重放结果不明的写调用 |
| `/validate` | 重新运行检查 |
| `/release` | 生成制造候选证据包 |
| `/quit` | 退出 TUI |

`Ctrl+P` 打开命令面板。`Ctrl+X` 是快捷操作前缀：再按 `N` 新建项目、
`L` 打开项目列表、`M` 切换模型、`R` 工程审查、`D` 展开工具详情、
`S` 刷新项目状态、`C` 连接模型服务、`H` 打开帮助、`Q` 退出。
`Esc` 关闭菜单或中断当前任务，`Ctrl+C` 依次用于复制选区、停止任务、
清空草稿或退出，`F1` 也可查看完整帮助。

## 工程内核（不是界面步骤）

```text
自然语言需求 → 约束提取 → 电路计划 → KiCad 符号解析
→ 原理图与 PCB → 布局/布线 → 连接、ERC、DRC → 人工审查
```

电路计划 v2 会把层级功能块、电源域、接口、连接器完整引脚表、网络标签、
命名布局区域、锚点禁布区、差分对验收条件和可本地复算的断言送入同一套
语义 IR。模型只能选择受限名称和尺寸，不能直接写坐标、走线或 KiCad 文件
文本；布局、禁布和生成后几何指标都由本地确定性代码执行与记录。当前差分对
能力验证实际线宽、边到边间距、耦合长度比例和长度差，不代表阻抗仿真，也不
代表已有专用的耦合布线器。

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
| `pcbdraft/interfaces/` | TUI 界面 |

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
