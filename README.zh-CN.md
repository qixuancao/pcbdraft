<p align="center">
  <img src="docs/assets/brand/copperwright-mark-256.png" width="180" alt="CopperWright 标志">
</p>

<h1 align="center">CopperWright</h1>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.zh-CN.md">简体中文</a> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.ko.md">한국어</a>
</p>

<p align="center"><strong>构建在 KiCad 之上的开源、本地、Agent-safe PCB 设计运行时。</strong></p>

CopperWright 不是重写 KiCad，也不是“给几个固定开发板套模板”的生成器。KiCad
继续负责原理图/PCB 文件、几何、规则检查和制造输出；CopperWright 负责 AI 真正
缺少的那一层：结构化设计意图、可审查的电路计划、本地器件解析、事务化生成、
失败证据保留和分层验证门禁。

## 现在实际做到什么

通用 Agent 路径已经接通，但它还<strong>不是“任意一句话直接量产 PCB”</strong>。
当前它能：

- 在对话中接受普通的 2 或 4 层板需求；
- 保留用户点名的每一个器件，不再偷偷换成某个演示板里的器件；
- 查询本机安装的 KiCad 库，把真实可用的符号和引脚号交给规划模型；
- 只接受受 schema 约束的语义电路计划：元件、引脚、网络、假设、备注；模型不能
  直接写 KiCad 文本、坐标、命令或走线；
- 将审查后的计划编译成语义 IR 和项目本地的、带来源状态的元件图；
- 在生成前做确定性的拓扑预检：供电引脚覆盖、供电源、I2C 上拉、去耦和板厂规则
  会被明确列为通过、失败或待人工核实；失败不会把普通器件变成“不支持”，仍可保留并尝试；
- 生成原生 KiCad 原理图，然后做一次有界的布局/布线尝试；
- 成功项目会把已审查的 <code>circuit-plan.json</code> 与请求、IR、项目本地元件图
  一起纳入 manifest 和哈希，后续 AI 审查不会只看到生成后的 KiCad 文件；
- 如果失败，保留需求、计划、IR、提取的器件记录、已有的原生文件和具体错误。

从本机 KiCad 符号/封装提取到的数据故意标为 <strong>provisional（暂定）</strong>。
它不等于已经证明精确 MPN、datasheet 约束、采购、布局质量或可制造性。因此，即使
生成了项目，也不能跳过 L0–L7 证据和人工评审去宣称可投产。

之前把 RP2040 + TMP117 写成专用产品路径，是错误的：它只是报告里的测试例子，
不是产品入口。该硬编码路径已经删除。

## “不支持”与“尝试失败”不是一回事

普通的器件需求，例如某颗 MCU、传感器、连接器或 LDO，不应该因为系统没有专门
写过它就被说成“不支持”。CopperWright 会保留名字，查询本机库，让模型给出
语义计划，并真正尝试编译/生成；找不到符号、引脚不匹配、计划不合法或布不通，
都作为这一次尝试的具体证据返回，绝不暗中换成另一块板。

市电、大功率、DDR/PCIe/SerDes、RF、医疗、航空和安全关键等词不会触发生成拒绝。系统照常按本机 KiCad 库与实际布线能力尝试；缺少的符号、引脚、规则或验证证据会如实返回，不把结果包装成工程签核。

## 快速开始

环境：Linux、Python 3.11+、含符号/封装库和 CLI 的 KiCad 10；要生成电路计划，
还需要已认证的 Codex CLI 或 OpenAI-compatible provider。

    uv sync --extra dev
    scripts/prepare-kicad-environment.sh
    uv run copperwright doctor --json

    # 浏览器客户端只监听 127.0.0.1。
    uv run copperwright app --provider codex

创建项目后，用正常语言描述，例如：

    做一块 2 层低压控制板，使用 STM32F405 和 SHT31，提供 I2C、SWD 与 3.3V 供电；
    板子大小是 60 mm × 40 mm。

系统先提取需求，再把本机 KiCad 中真实可用的候选符号交给规划模型。模型必须返回
可审查的电路计划。你审查假设、器件身份、网络和局限后，明确确认才会创建 KiCad
文件。

<code>--provider builtin</code> 可以离线整理需求，但它不会凭空编造电路拓扑；要
生成计划，请用 <code>--provider codex</code> 或配置
<code>--provider openai-compatible</code>。

终端和网页使用同一个应用服务。终端默认入口是全屏单窗格对话：启动后直接描述要做的板子，首条消息会自动创建本地项目；普通文字继续当前对话，只有项目和工程动作使用斜杠命令。

    uv run copperwright --provider codex
    # 做一块带 STM32F405 和 SHT31、含 I2C 与 SWD 的 2 层板
    # /confirm

输入 `/` 会显示全部命令和简短说明；上下键选择，Tab 补全，Enter 补全或执行，Esc
关闭命令面板。默认终端入口也支持 `--workspace`、`--project` 和 `--timeout`。例如
`/model` 会显示当前规划 provider/model，`/model builtin` 等受支持的 provider 只会
切换当前进程中的 provider，不会写入凭据或配置。

命令面板包含 `/help`、`/new [name]`、`/projects`、`/open ID`、`/status`、
`/model [auto|codex|openai-compatible|builtin]`、`/confirm`、`/validate`、
`/undo`、`/discard`、`/release` 和 `/quit`。

`chat` 仍保留完整的非交互参数，适合 `--json`、`--new`、`--project` 和自动化脚本，
例如 `uv run copperwright chat --new 名称 --message "需求" --json`；未给出明确动作时它
不会回退到行式 REPL。

## 面向脚本的入口

新的通用入口和旧测试夹具明确分开：

    # 看本机 KiCad 能实际解析到哪些符号。
    uv run copperwright symbols SHT31 --json

    # 将已经审查的通用请求和电路计划编译成 IR 与项目本地元件图。
    uv run copperwright agent-compile REQUEST.json PLAN.json \
      --ir-output design.pcbir.json --parts-output parts.copperwright.json --json

    # 根据该计划创建一次原生 KiCad 生成尝试。
    uv run copperwright agent-generate REQUEST.json PLAN.json OUTPUT_DIR --json

仓库附带
<a href="examples/agent_plan_stm32_sht31">examples/agent_plan_stm32_sht31</a>
这个故意不完整的通用夹具，用于演示真实本地解析和诚实的失败证据保留。

JSON-RPC 提供 <code>symbols.find</code>、<code>agent.plan.compile</code> 和
<code>agent.project.generate</code>；详见 <a href="docs/API.md">docs/API.md</a>。
其中编译/生成结果均包含 <code>plan_review</code>；它是“先告诉你缺了什么证据”的
预检，不是“AI 已经证明电路正确”或“拒绝尝试”的开关。

## 架构

    对话 / CLI / 浏览器
            |
            v
    需求解释器 —— 保留器件名和假设
            |
            v
    受约束的电路规划器 —— 元件 + 引脚 + 网络，不给几何权力
            |
            v
    本地 KiCad 解析器 —— 项目级元件图 + 来源
            |
            v
    确定性拓扑预检 —— 明示缺失证据，仍可尝试
            |
            v
    语义 IR —— 事务、快照、失败保留
            |
            +--> KiCad 原理图与有界 PCB 尝试
            |
            +--> L0–L7 证据门禁和人工评审

规划模型与具体模型供应商无关，也没有写 KiCad S-expression、修改文件、执行代码、
指定坐标或批准工程结果的权限。

详见：<a href="docs/ARCHITECTURE.md">架构</a>、
<a href="docs/PRODUCT_ACCEPTANCE.md">当前验收/状态</a>、
<a href="docs/OPEN_SOURCE_REUSE.md">开源复用</a> 和
<a href="docs/DEVELOPMENT.md">开发指南</a>。

## 旧夹具

<code>compile</code>、<code>generate</code>、<code>requirements.compile</code>
和 <code>project.generate</code> 仍保留为历史确定性回归夹具，服务已有测试语料和
兼容性；它们不是对话产品路径，也不表示“项目只会生成三种板”。新功能必须进入
上面的“通用请求 → 电路计划 → 本地解析 → IR”路径。

## 许可证

运行时和文档采用 Apache-2.0；独立编写的元件目录和语料采用 CC0-1.0。见
<a href="NOTICE">NOTICE</a>。
