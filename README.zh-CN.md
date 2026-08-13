<p align="center">
  <img src="docs/assets/brand/copperwright-mark-256.png" width="180" alt="CopperWright 铜色 PCB 走线 W 标志">
</p>

<h1 align="center">CopperWright</h1>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.zh-CN.md">简体中文</a> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.ko.md">한국어</a>
</p>

<p align="center"><strong>用自然语言设计 PCB，由确定性的 KiCad 工程流程落地。</strong></p>

CopperWright 是一款本地优先、采用 Apache-2.0 许可的应用，可将一次对话转化为
可审查、可验证、可逆的 KiCad 项目。你可以通过 `copperwright chat` 启动引导式
终端会话，也可以用 `copperwright app` 打开仅监听本机回环地址的浏览器工作台。
KiCad 仍负责原理图/PCB、几何计算、规则检查和制造后端；AI 可以解释设计意图，
而元件、拓扑、布局、布线、输出、验证和发布身份始终由确定性的 CopperWright
代码负责。

有边界的 v1 支持三种真实布线设计：ATtiny402/TMP102 I2C、ATtiny402/BME280
SPI，以及带 AP2112K 3.3 V LDO 的 5 V 输入 ATtiny402 UART 控制器。三者均通过
适用的真实 KiCad ERC/DRC 和 CopperWright 候选关卡。我们仍不会把它们称为生产
就绪：合格的人工审查、实时采购、制造、上电调试、EMC 和实测物理结果都属于
外部关卡。

> **产品状态：**CopperWright 1.0.0 是一款完整但范围有界的应用，并非通用 PCB
> 自动驾驶工具。共享服务、终端/浏览器旅程、持久化、语义修改、三种配置文件和
> 发布路径的实测证据见[产品验收记录](docs/PRODUCT_ACCEPTANCE.md)。较早的
> [R01–R44 报告](docs/FINAL_REPORT_ZH.md)作为历史运行时证据原样保留。

## 已实现功能

- `copperwright chat` 与 `copperwright app` 共用唯一权威应用服务；它管理私有的
  持久化项目、对话、决策、作业、结构化事件、重启恢复和逐项目并发锁。
- 在产生工程副作用前，先提出聚焦的澄清问题，并展示易读的设计简报、假设、BOM、
  接口、约束、范围判断，最后要求用户明确确认。
- 响应式、可用键盘操作的本地浏览器界面，包含进度/取消/重试状态、真实原理图/
  PCB/3D 预览、工件直接路径、L0–L7 发现、在 KiCad 中打开以及候选包导出。
- 支持已认证的本地 Codex、通过环境变量配置的 OpenAI 兼容端点和离线确定性
  提供方。浏览器不接收密钥，项目对话也不存储密钥。
- 对话式语义修改支持预览/应用/放弃/撤销；暂存设计通过候选验证且用户确认前，
  当前 KiCad 文件不会被改动。
- 严格的语义电路/PCB IR，涵盖类型化接口、电源域、需求、功能块、约束、分析、
  风险和来源信息。
- 确定性的规范 JSON 与内容哈希，不受输入顺序影响。
- 采用 CC0 的可信元件图，将制造商/MPN、引脚、符号、封装、额定值、生命周期/
  来源证据、采购状态、制造契约和可用模型绑定在一起。
- 版本化、经规则验证的可复用功能块；其声明的元件、端口、证据和测试引用都会
  对照确定性实现进行检查。
- 带前置条件、预览、语义差异、原子发布、幂等性、冲突检测、备份、撤销和崩溃
  恢复的语义变更集。
- 需求编译、有界布局优化、确定性多层 A* 布线、细间距逃逸布线、填充参考平面、
  确定性缝合过孔以及原生 KiCad 生成。
- 对已识别封装位姿编辑进行双向 KiCad 同步；拓扑、元件、走线、原理图或规则漂移
  都会以失败关闭。
- 真实反映 L0–L7 验证状态：`completed`、`not_applicable`、`unavailable`、
  `heuristic` 和 `human_required`。
- 集成真实 KiCad ERC/DRC、原理图一致性、BOM、布局、Gerber、钻孔、IPC-D-356、
  电路板统计、PDF、SVG、渲染以及仅电路板 STEP。
- 字节级可复现的内容发布；时间戳规范化时在审计回执中保留原始哈希，并提供
  确定性 ZIP 和离线验证器。
- 版本化 CLI、Python API，以及有界的换行分隔 JSON-RPC 2.0 API。
- 包含 90 个案例的独立 CC0 错误注入语料库，测量检出、误报、修复、回归、
  可重复性、延迟以及可选的盲测模型指标。
- 原有的审查器/安全补丁工作流仍可用于非托管项目，但原始文本替换只是旧版
  兼容路径，不是主要变更模型。

需求、实现和测试之间的映射见[规格追踪表](docs/SPEC_TRACEABILITY.md)。产品的
精确验证结果和剩余关卡记录在 [v1 中文报告](docs/PRODUCT_REPORT_ZH.md)中；历史
运行时报告保持不变。

## 支持范围

内置配置文件有意保持狭窄且边界明确：

| 契约 | 当前支持 |
|---|---|
| I2C | `low_voltage_i2c_controller_v1`：稳压 3.3 V 输入、ATtiny402、TMP102、Qwiic、UPDI、LED |
| SPI | `low_voltage_spi_environment_v1`：稳压 3.3 V 输入、ATtiny402、板载 BME280、四线 SPI 模式 0、1 MHz、UPDI |
| UART/LDO | `low_voltage_uart_ldo_controller_v1`：稳压 5 V 输入、AP2112K 3.3 V LDO、ATtiny402、3.3 V CMOS UART、UPDI、LED |
| 铜层堆叠 | 2 层或 4 层 |
| 电路板外形 | 45 mm × 30 mm |
| 用途 | 原型或非安全关键的低压传感/控制 |
| KiCad | 主版本 10；精确验收版本为 10.0.5 |
| Python | 3.11+ |

USB 2.0 和 buck 转换已被识别，但 v1 尚无在本地完整验证过的电气/布局链，因此
仍不支持。RS-232 电平也不属于受支持的 3.3 V CMOS UART。其他电路板尺寸不会
被悄悄应用到固定且经过验证的布局/布线契约。DDR、PCIe、SerDes、
RF、市电、高功率、医疗、航空及安全关键工作会被明确拒绝，而不是被悄悄近似。

在测试主机上，KiCad 自身无法重新载入通过 KiCad 10 Python API 生成的奇数三铜层
电路板，因此原生契约采用分析目标 2–4 层中的常见 2/4 层子集。

## 环境要求

- Linux 和 `uv`
- Python 3.11 或更新版本
- KiCad 10.x CLI、符号库、封装库和系统 `pcbnew` Python 绑定
- 用于诊断和开发的 Git
- 可选：已认证的 Codex CLI，用于对话式意图、`review`、旧版 `patch` 和实时模型
  一致性基准测试
- 可选：OpenAI 兼容的 Chat Completions 端点，只能通过
  `COPPERWRIGHT_OPENAI_BASE_URL`、`COPPERWRIGHT_OPENAI_MODEL` 和 API 密钥环境变量配置

KiCad 10.0.5 是本地精确验收的版本。其他 10.x 版本会报告为主版本相同但未经
精确测试；其他主版本会以失败关闭。Ubuntu 用户可采用 KiCad 官方
`ppa:kicad/kicad-10.0-releases` 说明。

## 安装

从仓库检出安装：

```bash
scripts/deploy.sh
scripts/prepare-kicad-environment.sh
uv run copperwright doctor --json
```

从已构建 wheel 进行隔离安装：

```bash
uv build
uv venv /tmp/copperwright-venv
uv pip install --python /tmp/copperwright-venv/bin/python dist/*.whl
/tmp/copperwright-venv/bin/copperwright --version
```

`doctor.ok` 表示确定性核心可用。离线提供方、生成、验证、发布、校验和确定性
基准测试均不需要付费或私有凭据。

## 对话式快速上手

启动浏览器应用（按设计仅监听本机回环地址）：

```bash
copperwright app
# 如果浏览器没有自动打开，请访问 http://127.0.0.1:8765
```

新建项目，回答铜层问题，检查设计简报、BOM 和约束，再确认生成。随后可以说
“Change this board to 4 layers”，先查看经过验证的语义差异，再选择 Apply；Undo
会精确恢复此前的权威状态。Export candidate 会生成并离线校验制造候选包。

通过 SSH 也能完成同一套流程：

```bash
copperwright chat
# /new Greenhouse sensor
# Describe: Create a BME280 SPI environmental sensor controller
# Reply: 2 layers
# /confirm
# Change this board to 4 layers
# /confirm
# /undo
# /release
```

脚本化自动化可使用 `--new`、`--project`、`--message`、`--yes`、`--undo`、
`--validate`、`--release`、`--list` 和 `--json`；详见 `copperwright chat --help`。

![CopperWright 浏览器项目视图](artifacts/product-e2e/copperwright-app-visuals.png)

## 提供方与密钥

`--provider auto` 会依次选择已安装且完成认证的 Codex CLI、已配置的 OpenAI 兼容
端点和离线分类器。也可以用 `--provider codex`、`--provider openai-compatible`
或 `--provider builtin` 明确选择。

```bash
# 在 CopperWright 之外完成认证；令牌不会复制到项目中。
codex login
copperwright app --provider codex

# 或使用 OpenAI 兼容端点启动。不要把这些配置写入项目文件。
COPPERWRIGHT_OPENAI_BASE_URL=https://provider.example/v1 \
COPPERWRIGHT_OPENAI_MODEL=model-id \
OPENAI_API_KEY='<secret>' \
copperwright app --provider openai-compatible
```

浏览器不提供凭据输入框。模型输出受到 schema、大小和范围约束，并会规范化和
范围检查；用户确认前不能产生工程副作用。提供方逻辑从不选择元件或编辑 KiCad。

## 确定性运行时快速上手

所有输出路径都只能新建。请使用新路径，或自行移除之前的一次性输出。

```bash
copperwright compile \
  examples/attiny_sensor_controller/requirements.json \
  --output /tmp/controller.pcbir.json --json

copperwright generate \
  examples/attiny_sensor_controller/requirements.json \
  /tmp/controller --json

copperwright inspect /tmp/controller --json
copperwright validate /tmp/controller --output /tmp/controller-validation --json
copperwright release /tmp/controller /tmp/controller-release --json
copperwright release-verify /tmp/controller-release --json
```

生成的项目包含源需求、语义 IR、原生 `.kicad_sch/.kicad_pcb/.kicad_pro`、
隔离工作进程回执、语义快照、原生焊盘边缘约束测量、布线/参考平面证据以及
哈希清单。发布包包含交叉核对的制造文件、规范化验证证据、执行回执、内容清单
和确定性 ZIP。

已提交的参考输出位于：

- [`examples/product_profiles`](examples/product_profiles) — 当前三种 v1 配置文件的
  原生项目、验证和预览
- [`examples/attiny_sensor_controller`](examples/attiny_sensor_controller)
- [`artifacts/product-e2e`](artifacts/product-e2e) — clean-HOME 浏览器/终端产品流程
  证据和截图
- [`artifacts/acceptance/release`](artifacts/acceptance/release)
- [`artifacts/acceptance/review`](artifacts/acceptance/review)
- [`artifacts/benchmark/benchmark-20260812.json`](artifacts/benchmark/benchmark-20260812.json)

## 语义事务

Agent 应输出类型化的 `copperwright-change-set`，然后使用事务命令，而不是编辑
KiCad 文本：

```bash
copperwright semantic-preview design.pcbir.json change-set.json --output /tmp/tx
copperwright semantic-apply /tmp/tx
copperwright semantic-undo /tmp/tx
copperwright semantic-recover /tmp/tx
```

操作覆盖需求、元件、网络/端点、约束、电路板规则和元数据。每项操作都包含原因，
也可携带字段级预期。运行时会验证基础哈希，在内存中应用全部操作，验证生成的
IR，写入语义差异，然后才创建暂存区。发布时会在资源锁保护下重新检查源哈希和
暂存哈希。

要导入经审查的原生 KiCad 封装移动：

```bash
copperwright sync /tmp/controller --json
copperwright sync /tmp/controller --apply --json
copperwright sync-undo /tmp/.copperwright-transactions/sync-...
```

仅导入位姿变更。未知的电路板字节、封装变更、新增或移除元件、走线、网络映射、
原理图变更和项目规则变更都会被拒绝，而不会被悄然丢弃。

## 验证与证据

验证阶梯按检查项和层级分别报告：

| 层级 | 运行时证据 |
|---|---|
| L0 | 清单/哈希完整性、语义解析、原生 KiCad 解析 |
| L1 | 规范元件、引脚、封装/焊盘、连通性、原理图/PCB 一致性 |
| L2 | 真实 KiCad ERC 和 DRC 报告 |
| L3 | 接口、去耦、上拉、电流、布局、布线和意图规则 |
| L4 | 生命周期/BOM/制造契约、DFM 代理、发布交叉检查及外部采购/制造厂证据 |
| L5 | 适用时的确定性直流/功率检查；除非存在证据，否则 SI/PI/热/EMI 不可用 |
| L6 | 以外部证据形式导入、带归属信息的合格人工审查 |
| L7 | 以外部证据形式导入、带归属信息的电路板序列号/测试计划/结果工件 |

候选就绪要求所有可在本地实现的阻断关卡通过。生产就绪还要求有效的 L4 采购/
制造厂证据、L6 审查和 L7 物理证据。运行时会复制并哈希所提供的证据，但将其
标记为 `externally_supplied_not_independently_verified`；运行时绝不会自行签署。

I2C 配置文件将总线限定为 200 pF、使用 4.7 kOhm 上拉且不允许外部上拉。SPI
配置文件将单个板载 BME280 固定为四线模式 0、1 MHz，并验证 CS 上拉。UART/LDO
配置文件会检查 AP2112K 输入/输出、负载、旁路、稳定性、使能，以及 3.3 V CMOS
8-N-1（不是 RS-232）契约。去耦距离在相关原生铜焊盘矩形之间测量；所有布线契约
都要求填充的 GND 参考平面和确定性 GND 缝合过孔。

托管项目审查会接收经过严格解析的需求、IR、可信元件和功能块记录、生成回执及
原生语义导出。如果任何受跟踪文件发生漂移，这些意图记录会被标记为非权威，而
不会被悄然采用。模型响应仍属于启发式审查，不能满足 L6。

## CLI 与 Agent API

运行 `copperwright --help` 和 `copperwright COMMAND --help` 查看权威 CLI。
主要命令组包括：

- 产品：`chat`、`app`
- 设计：`compile`、`generate`、`inspect`、`parts`
- 验证/发布：`validate`、`release`、`release-verify`、`evidence-record`
- 同步/事务：`sync`、`sync-undo`、`sync-recover`、`semantic-preview`、
  `semantic-apply`、`semantic-undo`、`semantic-recover`
- 评估：`benchmark`
- 非托管兼容：`review`、`patch`、`apply`
- 自动化：`api`

API 从 stdin 每行读取一个 JSON-RPC 请求，并向 stdout 每行写入一个响应。
请从 `runtime.capabilities` 开始；它是范围和方法支持情况的机器可读来源。

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"runtime.capabilities","params":{}}' \
  | copperwright api
```

进程最多接受 10,000 个请求，每个请求上限为 4 MiB。参数集必须精确，路径和数值
边界会被验证，协议错误也会保持 JSON-RPC 帧结构。参见 [API 参考](docs/API.md)。

## 基准测试

无需模型或网络即可运行确定性语料库：

```bash
scripts/benchmark.sh
# or
copperwright benchmark /tmp/copperwright-benchmark.json --repetitions 5 --json
```

显式请求的实时模型一致性测试会执行两次或更多次隔离盲测：

```bash
MODEL_RUNS=2 scripts/benchmark.sh
```

当前测量结果和限制见 [BENCHMARK.md](BENCHMARK.md)。该基准测试是回归语料库，
并不声称覆盖所有 PCB 故障。

## 产品标识

CopperWright 的分发包、Python 导入和执行命令均为 `copperwright`，这是唯一的
公开 CLI 和 Python 包。磁盘与协议标识符使用同一命名空间，包括
`copperwright-*` schema、`project.copperwright.json`、`.copperwright-*`
事务/锁目录和 `COPPERWRIGHT_*` 测试/配置命名空间。已提交的工程回执和基准工件
也全部使用 CopperWright 标识符。

## 开发与发布检查

```bash
scripts/test.sh
scripts/smoke.sh                 # real KiCad demo; no model by default
scripts/python-matrix.sh         # Python 3.11–3.14 core matrix
scripts/chat-e2e.sh              # scriptable terminal product journey
uv run python scripts/browser-e2e.py  # real Firefox journey and restart
uv run python scripts/generate-product-examples.py
scripts/release-check.sh         # full clean-install product/release hard gate
```

当本地存在兼容工具链时，`scripts/test.sh` 会自动运行真实 KiCad 测试；否则通过
`unittest` 记录跳过。CI 定义位于
[`.github/workflows/ci.yml`](.github/workflows/ci.yml)。语料库和配置文件贡献规则
见[开发指南](docs/DEVELOPMENT.md)。

## 安全模型

项目内容、模型输出、元数据、归档和文件名均不可信。运行时使用严格 schema、
字节/成员/深度限制、拒绝非有限数值、非登录子进程、时间/输出限制、只能新建的
输出、规范路径、符号链接/硬链接/特殊文件拒绝、文件清单、资源锁、原子写入和
写后验证。

隔离的 `pcbnew` 工作进程接收内部生成的有界 JSON 作业，并以 `-I` 运行系统
Python；它不会导入项目代码。Codex 审查采用只读工具策略，禁用项目配置、hooks、
multi-agent、网络和特权工具，并通过 stdin 传入提示。该策略并非操作系统沙箱：
请在容器/VM 中运行不可信项目，并且只发送您有权披露的数据。参见
[SECURITY.md](SECURITY.md)。

## 许可

运行时源代码和文档采用 Apache-2.0；参见 [LICENSE](LICENSE)。内置元件/功能块
目录和独立基准数据采用 CC0-1.0，详见
[`src/copperwright/data/LICENSE.md`](src/copperwright/data/LICENSE.md)。生成的示例设计
使用 KiCad 官方库材料，适用 KiCad 库的 CC-BY-SA 4.0 design exception。依赖和
归属说明见 [NOTICE](NOTICE)。
对公开项目的有界研究及实际复用决策记录在
[`docs/OPEN_SOURCE_REUSE.md`](docs/OPEN_SOURCE_REUSE.md)中；没有复制所研究项目的
代码或资源。

本项目不提供任何担保或工程认证。请始终根据产品、司法辖区和风险开展合格审查。
