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

## 快速开始

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

`Ctrl+P` 打开项目列表，`Ctrl+R` 打开工程审查，`Ctrl+L` 展开工具详情，
`F1` 查看快捷帮助。

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

生成的工程可以直接用 KiCad 打开和继续编辑。PCBDraft 不锁定文件格式，
也不把模型服务绑定到某一家供应商。

## 开发

```bash
scripts/test.sh
```

主要模块位于 `src/pcbdraft/`：模型配置、模型传输、TUI、需求解析、KiCad
生成、验证和事务应用彼此分离，方便替换任意一层。欢迎提交 Issue 和
Pull Request。需要脚本化生成时，也可以使用 `agent-generate` 命令调用同一套
受约束的电路计划和 KiCad 生成流程。

## 许可证

PCBDraft 使用 MIT 许可证，详见 [LICENSE](LICENSE)。
