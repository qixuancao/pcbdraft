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

<p align="center"><strong>面向 KiCad、以证据为基础的 PCB 自动化。</strong></p>

CopperWright 是一个采用 Apache-2.0 许可、与模型无关的运行时，可将边界明确的
电子设计需求转化为可审查、可验证、可逆的 KiCad 项目。KiCad 仍负责原理图/PCB、
几何计算、规则检查和制造后端；CopperWright 在此基础上加入语义意图、可信元件
契约、事务、确定性算法、证据关卡以及面向 Agent 的 API。

仓库内提交的验收设计是一块真实布线的 ATtiny402/TMP102 控制器。其 KiCad ERC
和 DRC 均无违规，制造候选包也可复现，但我们有意不将其称为生产就绪：合格的
人工审查、实时采购、制造、上电调试、EMC 以及实测物理结果仍属于外部关卡。

> **产品状态：**历史 R01–R44 报告证明的是下述有边界的工程运行时；仅凭该报告
> 不能证明端到端用户应用已经完成。只有共享应用服务、对话式 `chat` 工作流、
> 本地浏览器 `app`、持久化项目、安全的对话式修改、预览和多配置文件验收均已实现
> 并完成实测，CopperWright v1 才算完整。参见[产品验收](docs/PRODUCT_ACCEPTANCE.md)。

## 已实现功能

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

需求、实现和测试之间的映射见[规格追踪表](docs/SPEC_TRACEABILITY.md)。实际交付
的验证结果及剩余外部关卡记录在[最终中文报告](docs/FINAL_REPORT_ZH.md)中。

## 支持范围

内置生成器配置文件有意保持狭窄且边界明确：

| 契约 | 当前支持 |
|---|---|
| 配置文件 | `low_voltage_i2c_controller_v1` |
| 电路 | 外部稳压 3.3 V ATtiny402 + TMP102 + I2C/Qwiic + UPDI + LED |
| 铜层堆叠 | 2 层或 4 层 |
| 用途 | 原型或非安全关键的低压传感/控制 |
| KiCad | 主版本 10；精确验收版本为 10.0.5 |
| Python | 3.11+ |

SPI、UART、基础 USB 2.0、LDO 和简单 buck 已被识别为策略域，但尚无内置生成
配置文件。包含这些名称的请求会在生成前被拒绝，而不会被悄悄映射到 I2C
夹具。DDR、PCIe、SerDes、RF、市电、高功率、医疗、航空及安全关键工作会被
自动范围关卡明确拒绝。

在测试主机上，KiCad 自身无法重新载入通过 KiCad 10 Python API 生成的奇数三铜层
电路板，因此原生契约采用分析目标 2–4 层中的常见 2/4 层子集。

## 环境要求

- Linux 和 `uv`
- Python 3.11 或更新版本
- KiCad 10.x CLI、符号库、封装库和系统 `pcbnew` Python 绑定
- 用于诊断和开发的 Git
- 可选：已认证的 Codex CLI，用于 `review`、旧版 `patch` 和实时模型一致性基准测试

KiCad 10.0.5 是本地精确验收的版本。其他 10.x 版本会报告为主版本相同但未经
精确测试；其他主版本会以失败关闭。Ubuntu 用户可采用 KiCad 官方
`ppa:kicad/kicad-10.0-releases` 说明。

## 安装

从仓库检出安装：

```bash
scripts/deploy.sh
uv run copperwright doctor --json
```

从已构建 wheel 进行隔离安装：

```bash
uv build
uv venv /tmp/copperwright-venv
uv pip install --python /tmp/copperwright-venv/bin/python dist/*.whl
/tmp/copperwright-venv/bin/copperwright --version
```

`doctor.ok` 表示确定性核心可用。Codex 可用性会单独报告为
`ai_review_available`；生成、验证、发布、校验或确定性基准测试均不需要付费或
私有凭据。

## 端到端快速上手

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

- [`examples/attiny_sensor_controller`](examples/attiny_sensor_controller)
- [`artifacts/acceptance/release`](artifacts/acceptance/release)
- [`artifacts/acceptance/review`](artifacts/acceptance/review)
- [`artifacts/benchmark/benchmark-20260812.json`](artifacts/benchmark/benchmark-20260812.json)

## 语义事务

Agent 应输出类型化的 `pcb-agent-change-set`，然后使用事务命令，而不是编辑
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
copperwright sync-undo /tmp/.pcb-agent-transactions/sync-...
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

对于内置配置文件，功率包络采用单一同时最大值契约
（`3.465 V × 0.1 A = 0.3465 W`，电源上限增加 +5%，且留有低于传感器 3.6 V
工作上限的余量）；I2C 限定为 200 pF、使用 4.7 kOhm 上拉且不允许外部上拉；
UPDI VTREF 仅用于感测；去耦距离在相关原生铜焊盘矩形之间测量。I2C 布线契约
要求填充的 GND 参考平面和至少两个确定性 GND 缝合过孔。

托管项目审查会接收经过严格解析的需求、IR、可信元件和功能块记录、生成回执及
原生语义导出。如果任何受跟踪文件发生漂移，这些意图记录会被标记为非权威，而
不会被悄然采用。模型响应仍属于启发式审查，不能满足 L6。

## CLI 与 Agent API

运行 `copperwright --help` 和 `copperwright COMMAND --help` 查看权威 CLI。
主要命令组包括：

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

## 兼容名称

分发包和主命令名为 `copperwright`。安装后仍提供等效的 `pcb-agent` 兼容别名。
内部 Python 模块继续使用 `pcb_agent`，以避免风险高且没有实际价值的模块迁移。

稳定的磁盘和协议标识符也保持不变，包括 `pcb-agent-*` schema、
`project.pcb-agent.json`、`.pcb-agent-*` 事务/锁目录以及 `PCB_AGENT_*`
测试/配置命名空间。品牌切换前创建并提交的工程回执和基准工件仍保留当时记录的
`pcb-agent-runtime`；CopperWright 不会重写历史证据来制造更新的假象。

## 开发与发布检查

```bash
scripts/test.sh
scripts/smoke.sh                 # real KiCad demo; no model by default
scripts/compatibility.sh         # Python 3.11–3.14 core matrix
scripts/release-check.sh         # tests, wheel/sdist, clean install, E2E release
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
[`src/pcb_agent/data/LICENSE.md`](src/pcb_agent/data/LICENSE.md)。生成的示例设计
使用 KiCad 官方库材料，适用 KiCad 库的 CC-BY-SA 4.0 design exception。依赖和
归属说明见 [NOTICE](NOTICE)。

本项目不提供任何担保或工程认证。请始终根据产品、司法辖区和风险开展合格审查。
