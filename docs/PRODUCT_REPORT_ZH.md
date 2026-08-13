# CopperWright 1.0 产品完成报告

日期：2026-08-13

## 结论

CopperWright 已从有边界的 PCB 工程运行时扩展为一款可由普通用户使用的、本地优先
的对话式 PCB 应用。`copperwright chat` 和仅监听 `127.0.0.1` 的
`copperwright app` 共用一个权威应用服务。用户无需手写 JSON：可以用自然语言建项、
回答聚焦问题、检查简报/BOM/约束、明确确认、生成真实 KiCad 工程、查看预览与
L0–L7 验证、通过语义事务修改/撤销，并导出和离线校验制造候选包。

这里的“完成”严格限定为 v1 已验证的低压、非安全关键 MCU 传感/控制范围。报告不把
候选包称为生产就绪，也不以 ERC/DRC、模型回答或软件状态替代实时采购、制造厂能力、
合格人工审查、制造、上电、EMC 或物理测试。

历史 [`FINAL_REPORT_ZH.md`](FINAL_REPORT_ZH.md)及其 R01–R44 哈希没有改写。本报告
只记录在该工程运行时之上新增的产品层。

## 已交付产品

- 单一 `ApplicationService`：私有持久化项目、对话、决策、作业、结构化事件、
  逐项目锁、重启恢复、0.2.0 托管项目迁移、预览、验证、事务和发布。
- 终端产品：交互式 `/new`、`/open`、`/projects`、`/status`、`/confirm`、
  `/discard`、`/validate`、`/undo`、`/release`，以及完整的非交互 JSON 参数面。
- 浏览器产品：首次诊断/安全提供方指引、项目列表、对话、澄清、确认、结构化进度、
  取消/重试、简报/BOM/约束、真实 KiCad 原理图/PCB/3D 预览、L0–L7、可操作发现、
  工件路径、在 KiCad 中打开和候选包导出。
- 提供方契约：已认证 Codex CLI、环境变量配置的 OpenAI 兼容 Chat Completions、
  无账户离线提供方。模型输出使用严格有界 schema，经规范化和范围检查，不能直接
  选择元件、生成几何、修改 KiCad 或批准副作用。
- 三条完整且互不等价的工程链：
  `low_voltage_i2c_controller_v1`、`low_voltage_spi_environment_v1`、
  `low_voltage_uart_ldo_controller_v1`。
- USB 2.0、buck、RS-232 电平以及 DDR/PCIe/SerDes/RF/市电/高功率/医疗/航空/
  安全关键请求明确失败，不会映射到已知夹具。三个配置文件的已验证外形明确限定为
  45 mm × 30 mm；其他尺寸在确认前显示为不支持。
- 1.0.0 包、依赖锁、四语 README、中文对话式快速上手、架构/API/开发/安全/复用记录、
  三个原生示例和真实浏览器截图。

完整逐项映射见 [`PRODUCT_ACCEPTANCE.md`](PRODUCT_ACCEPTANCE.md) 的 PA01–PA35。

## 真实验收结果

### 三个原生 KiCad 配置文件

| 配置文件 | 层数 | 语义内容哈希 | 本地关卡 | 候选/生产 |
|---|---:|---|---|---|
| `low_voltage_i2c_controller_v1` | 2 | `7febeb85cd376b1ef20112fc038983edc0d7f64206c2f2e38021bd8a851393a1` | L0–L3 pass；真实 ERC/DRC 无违规 | `true` / `false` |
| `low_voltage_spi_environment_v1` | 2 | `963c80da0bca2e37c08aa3f2262cf13664777815f7998a45521cd4ff8bd732b7` | L0–L3 pass；真实 ERC/DRC 无违规 | `true` / `false` |
| `low_voltage_uart_ldo_controller_v1` | 4 | `f992d7be1466fa17942957ecefe7fc005f5e99ff935a7683514376d346ee624d` | L0–L3 pass；真实 ERC/DRC 无违规 | `true` / `false` |

三个配置文件均如实报告 L4/L5 `unavailable`、L6/L7 `human_required`，且
`production_claimed=false`。

### 浏览器完整旅程

命令：

```bash
uv run python scripts/browser-e2e.py \
  --executable .venv/bin/copperwright \
  --geckodriver /snap/bin/geckodriver \
  --output artifacts/product-e2e
```

结果：fresh HOME、KiCad 全局库表初始化、loopback-only、本地 builtin 提供方、
BME280/SPI 自然语言请求、2 层澄清、确认前无设计副作用、真实生成/验证/图片加载、
2→4 层语义预览和应用、原哈希
`0c7b198169c3438fb9b9cbcda50e62b6dd93d4c9f285db80c4117aa23289c118`、应用哈希
`adcf7f68584ed50be31fb1953c11a620f903e727e32cbf7809404e9625169a80`、撤销精确
恢复、L0–L7 共 8 层、候选就绪、生产未就绪、196,452 字节发布 ZIP 离线验证成功。
界面还明确显示并拒绝了 USB-C/市电/医疗请求。服务重启后从持久项目列表选择原项目，
状态为 `released`，11 条对话消息、设计和发布仍在，活动作业为零。该独立探针耗时
70.68 秒。

### 终端完整旅程

命令：

```bash
scripts/chat-e2e.sh /tmp/copperwright-chat-e2e-v1
```

结果：自然语言建项、澄清、确认前无副作用、生成、验证、语义预览保持权威哈希、
应用改变哈希、撤销恢复原哈希、候选发布、离线验证和重开全部为 `true`；
`production_ready=false`。

### 真实 Codex 提供方

已认证的 `codex-cli 0.147.0` 使用 `gpt-5.6-sol`、`max`、`default`、只读 sandbox、
Fast/hooks/multi-agent/network tools 关闭、approval `never`，通过 stdin 接收提示并
用严格 schema 返回。它将请求分类为 `low_voltage_spi_environment_v1`、2 层、
`supported`，项目停在 `awaiting_confirmation`，确认前没有设计副作用。净化后的
回执位于 `artifacts/product-e2e/codex-provider.json`。

### 自动化检查

- `scripts/test.sh`：115 tests，175.541 s，`OK`；包含单元、集成、安全、迁移、
  打包契约、真实 KiCad、三配置文件和候选发布链。
- `scripts/compatibility.sh`：Python 3.11/3.12/3.13/3.14 各 44 tests，分别
  19.263/17.956/17.933/17.761 s，全部 `OK`；总耗时 79.22 s。
- 四语 README：15 个章节、12 个 bash 块、20 个相对链接、20 个表格行保持对齐；
  命令字面量与链接目标由 `tests.test_branding` 校验。
- `scripts/release-check.sh`：355.23 s，完成锁定依赖测试、wheel/sdist 构建和归档
  校验、隔离 Python 3.11 安装、`copperwright`/`pcb-agent` 1.0.0 启动、终端 E2E、
  clean-HOME Firefox E2E、90 例 benchmark、真实生成/验证、两次逐字节可复现发布及
  离线校验，最终输出 `release check passed`。
- 当前五次重复 benchmark：70/70 故障检出、20/20 clean 判定、0 假阳性、0 假
  阴性、65/65 修复成功、0 引入回归、90/90 重复稳定；450 个样本 mean
  3.119323 ms、median 2.851686 ms、p95 4.671333 ms。当前确定性运行没有把未请求
  的模型运行伪装成结果，故 `model_consistency=unavailable`；保留的独立历史模型
  工件则真实记录 2 次、48/48 正确、24/24 一致，且本次 1.0 又单独实跑了已认证
  Codex 提供方契约。

验收工具版本：CopperWright 1.0.0、KiCad/kicad-cli 10.0.5、Firefox 153.0.3、
geckodriver 0.37.0、uv 0.12.1。

## 工件路径

- 产品旅程与截图：`artifacts/product-e2e/`
- 当前独立 benchmark 原始逐例结果：`artifacts/product-e2e/benchmark.json`
- 三配置文件项目/验证/预览：`examples/product_profiles/`
- 产品追踪表：`docs/PRODUCT_ACCEPTANCE.md`
- 工程运行时历史追踪表：`docs/SPEC_TRACEABILITY.md`
- 开源复用与许可判断：`docs/OPEN_SOURCE_REUSE.md`
- 历史 R01–R44 完成报告：`docs/FINAL_REPORT_ZH.md`

## 剩余的真实外部门槛

以下不是软件 TODO，而是本次本地运行无法诚实产生的工程事实：

1. 授权经销商实时库存/生命周期和所选 PCB/PCBA 厂的实际能力；
2. 有资质工程师对具体设计、应用场景、法规和风险进行的 L6 签核；
3. 实际采购、制造、装配、上电、固件联调、EMC/环境测试及带板序列号的 L7 结果；
4. USB 2.0 与 buck 的完整元件—电气—布局—规则—真实板链，因此 v1 明确不支持；
5. SI/PI/热/EMI 等没有本地可信求解器或测量证据的分析状态保持 `unavailable`。

## 提交范围

产品层起点为 `1c8b78d`（共享对话式应用基础）。1.0.0 实现终点提交将在本报告的
最终证据提交中固定；交付时工作树必须干净，且没有执行 push 或修改 GitHub 私有性。
