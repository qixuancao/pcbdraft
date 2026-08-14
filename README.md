# PCBDraft

![PCBDraft 标志](docs/assets/brand/pcbdraft-mark-256.png)

PCBDraft 是一个独立的开源 PCB 设计智能体。你用自然语言描述想做的
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

## 快速开始（推荐安装）

这个仓库目前是私有仓库。已通过 `gh auth login` 登录 GitHub 的 Ubuntu 或其他
Linux 用户，以普通用户运行下面这一条命令即可：

```bash
gh api 'repos/qixuancao/pcbdraft/contents/scripts/install.sh?ref=main' --jq .content | base64 --decode | bash
```

如尚未登录，先执行 `gh auth login`。安装器通过已授权的 Git 访问仓库；它不会
把 GitHub Token 写入配置文件或工程目录。

安装器遵循用户级安装：不使用 `sudo`，不修改系统 Python，也不覆盖已有的
KiCad 配置。它会：

- 检查已安装的 KiCad 10 和 `kicad-cli`；
- 检查 Git；
- 必要时把 `uv` 安装到当前用户目录；
- 用 `uv tool install` 把 `pcbdraft` 安装为独立命令；
- 只在缺失时初始化 `~/.config/kicad/10.0/` 中的符号和封装库表。

安装后的命令位于 `~/.local/bin/pcbdraft`。若首次运行提示找不到命令，执行：

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

然后启动并检查运行环境：

```bash
pcbdraft
pcbdraft doctor --json
```

模型服务和 API Key 保存在 `~/.config/pcbdraft/config.toml`。首次启动会创建并
记录统一的 PCB 项目仓库 `~/PCBDraft/`；以后无论从哪个目录执行 `pcbdraft`，
新项目、KiCad 原理图、`.kicad_pcb`、检查记录和发布文件都会位于这个仓库的
`projects/` 下。要更新或修复安装，重新运行上面的安装命令即可。

KiCad 是生成原理图和 PCB 所需的系统运行时，安装器不会替你以管理员权限安装
它；请先安装 KiCad 10，再运行此命令。

## 从源码运行（开发）

需要 Linux、Python 3.11 或更高版本、[uv](https://docs.astral.sh/uv/)，以及
KiCad 10 和 `kicad-cli`。

```bash
git clone https://github.com/qixuancao/pcbdraft.git
cd pcbdraft
uv sync --extra dev
scripts/prepare-kicad-environment.sh
uv run pcbdraft doctor --json
```

## 配置模型

启动 TUI 后输入 `/connect`，选择 DeepSeek、MiniMax、Kimi、OpenAI、
OpenRouter、本地 Ollama 或自定义 OpenAI 兼容服务，然后输入 API Key。
输入 `/models` 可以搜索并切换模型。

密钥由 PCBDraft 写入 `~/.config/pcbdraft/config.toml`，文件权限为 `600`，
不会进入 PCB 工程、对话记录或运行收据。也可以手动创建同样的配置：

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

手动创建后执行：

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

该位置记录在 `~/.config/pcbdraft/repository.json`。切换仓库只影响之后打开和
创建的 PCBDraft 项目；原仓库中的文件不会被移动或删除。

直接描述电路板即可。用户没有指定层数时，PCBDraft 会根据小型原型的约束
自动选择保守的初始方案，不要求用户理解叠层设计。

TUI 中常用命令：

| 命令 | 作用 |
| --- | --- |
| `/connect` | 添加或更新模型服务和 API Key |
| `/models` | 搜索并选择当前模型 |
| `/new [名称]` | 创建新项目 |
| `/projects` | 打开已有项目 |
| `/review` | 查看计划、变更和检查证据 |
| `/logs on` | 展开工具执行详情 |
| `/stop` | 在安全边界停止当前任务 |
| `/retry` | 重试最近一次失败的任务 |
| `/validate` | 重新运行检查 |
| `/release` | 生成制造候选证据包 |
| `/quit` | 退出 TUI |

`Ctrl+P` 打开命令面板。`Ctrl+X` 是快捷操作前缀：再按 `N` 新建项目、
`L` 打开项目列表、`M` 切换模型、`R` 工程审查、`D` 展开工具详情、
`S` 刷新项目状态、`C` 连接模型服务、`H` 打开帮助、`Q` 退出。
`Esc` 关闭菜单或中断当前任务，`F1` 也可查看完整帮助。

## 离线模式

没有配置模型时也可以使用：

```bash
uv run pcbdraft --provider builtin
```

离线模式可以整理需求，但不会凭空编造未知电路拓扑。要从自由描述生成
完整电路计划，需要在 `/connect` 中配置一个模型服务。

## 工程流程

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
| `pcbdraft/interfaces/` | CLI、JSON-RPC、聊天、TUI 和本地 Web 界面 |

`tests/` 使用相同的职责目录，能够直接找到每层对应的测试。详细边界和
新增代码的放置规则见 [项目结构说明](docs/PROJECT_STRUCTURE.md)。1.0 版本
暴露过的旧 Python 模块路径仍由惰性兼容层支持，新代码应使用上表中的规范路径。

## 开发

```bash
scripts/test.sh
```

如需清理可能污染 wheel 的本地构建缓存，运行 `scripts/clean.sh` 或
`make clean`。发布检查会在构建前后自动执行这一步。

欢迎提交 Issue 和 Pull Request。需要脚本化生成时，也可以使用
`agent-generate` 命令调用同一套受约束的电路计划和 KiCad 生成流程。

## 许可证

PCBDraft 使用 Apache License 2.0，详见 [LICENSE](LICENSE)。
