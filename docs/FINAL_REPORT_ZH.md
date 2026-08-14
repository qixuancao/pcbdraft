# PCBDraft 工程基线：最终完成报告

> **历史记录，已不代表当前产品状态。** 本文保留的是固定确定性夹具时代的
> 2026-08-13 验收背景，不应被理解为 PCBDraft 当前的通用 AI PCB 产品说明。
> 当前架构、能力边界和证据状态以
> [ARCHITECTURE.md](ARCHITECTURE.md)、
> [PRODUCT_ACCEPTANCE.md](PRODUCT_ACCEPTANCE.md) 和
> [SPEC_TRACEABILITY.md](SPEC_TRACEABILITY.md) 为准。

> 本报告保留 2026-08-13 工程验收背景。当前产品名、主命令、schema、工件路径和
> 记录的运行名称均使用 PCBDraft / `pcbdraft`。

完成日期：2026-08-13（Asia/Shanghai）

## 结论

仓库已从 reviewer/safe-patcher MVP 完成到规范所要求的、有明确支持边界的
PCBDraft 0.2.0。需求追踪表中的本地可实现项 R01–R44 均有代码和
自动验证路径；R45 仅保留必须来自供应商、合格工程师、制造和实物测试的
外部门禁。当前 ATtiny402/TMP102 验收设计为 `engineering_candidate=true`，
并且诚实保持 `production=false`、`production_claimed=false`。

逐项代码/测试映射见 [`SPEC_TRACEABILITY.md`](SPEC_TRACEABILITY.md)。

## 已完成的软件

- 严格、可版本化、确定性序列化的电路/PCB 语义 IR，包含需求、验收条件、
  意图、接口、电源域、约束、风险、分析声明和来源证据。
- CC0 可信元件图和已验证功能块，绑定精确 MPN、符号、封装、引脚/焊盘、
  额定值、生命周期、来源、BOM、制造契约和可用模型。
- 以语义操作为主的预览、差异、前置条件、原子提交、锁、备份、撤销、
  幂等、冲突检测和崩溃恢复；原始文本替换仅保留为非托管兼容路径。
- 严格需求编译、确定性布局求解、有界多层 A* 布线、细间距逃线、地平面、
  拼接过孔，以及通过 KiCad API 生成原生原理图和 PCB。
- 托管工程、哈希清单、原生语义快照和双向封装姿态同步；拓扑、器件、
  规则、布线或未知字节漂移均失败关闭。
- L0–L7 证据模型及 `completed`、`not_applicable`、`unavailable`、
  `heuristic`、`human_required` 状态；外部 L4/L6/L7 证据可归属、复制、
  哈希和防篡改，但不会被运行时自签名。
- 真实 KiCad ERC/DRC、连接性、BOM、位置、Gerber、钻孔、IPC-D-356、
  PDF、SVG、PNG、STEP 和板统计集成；内容发布可逐字节复现并可离线验证。
- 稳定 CLI、Python API 和有界 NDJSON JSON-RPC 2.0 API；高风险和未实现
  领域均在能力发现/编译阶段显式拒绝。
- 有界进程、输出、JSON、目录、归档和模型调用，路径/链接/特殊文件防护，
  并发资源锁，收据、回滚和恶意输入测试。
- Apache-2.0 发行许可、CC0 数据许可、依赖声明、wheel/sdist、安装脚本、
  Makefile、GitHub Actions、开发/安全/API/架构/基准文档和真实示例。

## 验收设计与真实工具结果

验收工程位于 `examples/attiny_sensor_controller/project/`，使用 KiCad
10.0.5（运行时标记 `10.0.5-10.0.5~ubuntu26.04.1`）。

- 设计内容 SHA-256：
  `b75625e0bbd759c42f20e24d6ca91bcfca64fa9b8efb1dde6b6f5cefe54d8a0f`
- 原理图 SHA-256：
  `5ddcf62791d69e5014a8fbc876a716863233aa98ba51ece6bae8ac510dbab570`
- PCB SHA-256：
  `941461c59c57f753bfab946b3e38c047c6bac45f7ae9bec8eef753d66436f61c`
- 真实 ERC：0 error、0 warning；真实 DRC：0 error、0 warning。
- 布线：176 段、15 个过孔、`unrouted=[]`；其中 GND 拼接过孔 2 个。
- B.Cu GND 热焊盘填充面积：1215.059312 mm²。
- MCU 3V3/GND 去耦焊盘边缘距离：1.414490 mm，限值 3.0 mm。
- TMP102 3V3/GND 去耦焊盘边缘距离：1.836166 mm，限值 2.0 mm。
- 供电包络：3.0–3.465 V、0.1 A、0.3465 W；3.465 V 是 3.3 V +5%，
  低于 TMP102 的 3.6 V 工作上限。
- I²C：100 kHz、4.7 kΩ、总线电容上限 200 pF、计算上升时间
  796.462 ns、最大电压下拉电流 0.737234 mA，并禁止额外外部上拉。

持久化验证报告 SHA-256 为
`86a79ed3eed04da7d8287e368991e67426c927ec81195ecfc616e5b60148ac36`。
L0–L3 全部 `completed/pass`；本地 L4 BOM/生命周期/DFM 检查通过，但实时
库存和指定制造商能力不可用；L5 的 LED 直流计算通过，固件相关数字功能和
完整最坏功耗没有足够证据；L6 需要合格人审；L7 需要实物制造和测试。

## 自动化验证结果

- `scripts/test.sh`：Ruff、格式、编译和 93/93 测试通过，141.125 秒。
- `scripts/python-matrix.sh`：Python 3.11、3.12、3.13、3.14 各 42/42
  核心测试通过；用时分别为 17.654、16.760、16.848、16.560 秒。
- `scripts/release-check.sh`：再次通过 93/93 测试（140.108 秒）；成功构建
  wheel 和 sdist、校验包、在全新 Python 3.11 环境安装 0.2.0，并完成
  基准、真实 KiCad 生成、验证、两次发布比较和离线校验。
- `scripts/deploy.sh`：`doctor.ok=true`、`core_ok=true`，KiCad 10.0.5 为
  精确验收版本；Codex 和 Git 可用。
- `REAL_CODEX=0 scripts/smoke.sh`：在临时副本上完成真实 KiCad ERC/DRC
  命令；模型步骤按参数明确跳过。

## 独立基准结果

持久化结果：`artifacts/benchmark/benchmark-20260812.json`，SHA-256：
`2ef52959b171651e67b5be59eef5394ed77b125293f95095ca996073c22a4bf5`。

- 独立 CC0 语料：90 例、28 类；70 个故障注入、20 个干净对照。
- 5 次重复，共 450 次确定性评估：70 TP、0 FN、0 FP、20 TN。
- precision、recall、specificity、目标规则码召回率均为 1.0；误报率 0。
- 65/65 可修复案例成功；引入回归 0；90/90 案例重复稳定。
- 平均/中位/p95 规则延迟：3.487032/2.913661/5.328245 ms；确定性墙钟
  2.406665 秒。
- 两次盲化真实 Codex 运行在 24 个分层样本上完成 48 次判断：48/48
  正确，成对一致率 1.0，用时 115.079 和 48.005 秒。

这些数字仅是此有界、同一基础设计衍生语料的回归结果，不是任意 PCB 的
总体准确率，也不表示模型可以替代确定性规则或工程审核。

## 制造候选与真实 Codex 审查

制造候选位于 `artifacts/acceptance/release/`：

- 31 个内容工件和 3 个单独保留的原始审计工件；离线验证通过。
- manifest SHA-256：
  `1f500b0787db8dde9c94e48c275726d702fc3aa670a959baa319da5c82e94b3e`
- ZIP SHA-256：
  `9711a5921ef410bbf7ac1525b628df5f49ec969e850529d99c525406f2d8dc2a`
- 两个独立临时目录生成的 manifest 和 ZIP 逐字节相同。

最终模型审查位于
`artifacts/acceptance/review/20260812T154551Z-8f8b4876/`。它使用
`gpt-5.6-sol`、reasoning `max`、service tier `default`、Fast off、hooks
off、multi-agent off、禁网和只读沙箱；340.286 秒完成，未超时，JSON
schema 有效。模型确认本地确定性检查无缺陷，并把制造商选择、外部总线、
程序器行为、固件功耗和实物环境保留为风险。该结果是启发式审查，不是 L6。

## 仍需外部完成的门禁

- L4：在下单时验证授权渠道库存/生命周期，并让选定板厂和装配厂确认其
  实际叠层、最小工艺、表面处理、钢网、拼板和 DFM/DFA 能力。
- L5/L6：定义固件所有工作状态、外部线缆/设备/程序器契约和环境要求，
  完成最坏功耗、启动/负载阶跃、I²C 波形、保护和工程审核。
- L7：制造并装配带序列号的实板，执行 bring-up、功能、边界、功耗、热、
  ESD/EMC（若产品要求）和长期测试，导入可归属的测量工件。

这些是缺少真实供应商、合格人员、实验室或物理板的外部事实，不是可以用
更多本地代码诚实补造的测试。系统已经实现其记录、哈希、防篡改和放行逻辑。

## Git 与工件索引

- 初始 MVP 基线：
  `022212dade6610f26e08bea97fcecea1820507d8`
- 最终软件/发行工件提交：
  `7ec1ed27c242b72d0c0c2e567b28927e421f6170`
- 完整可审计实现范围：
  `022212dade6610f26e08bea97fcecea1820507d8^..7ec1ed27c242b72d0c0c2e567b28927e421f6170`
- 本报告位于上述实现之后的独立文档提交；交付仓库的完整范围可用
  `022212dade6610f26e08bea97fcecea1820507d8^..HEAD` 查看。

主要工件：

- 需求追踪：`docs/SPEC_TRACEABILITY.md`
- 架构/API/开发/安全：`docs/ARCHITECTURE.md`、`docs/API.md`、
  `docs/DEVELOPMENT.md`、`SECURITY.md`
- 真实托管 KiCad 示例：`examples/attiny_sensor_controller/project/`
- 制造候选：`artifacts/acceptance/release/`
- 最终真实审查：
  `artifacts/acceptance/review/20260812T154551Z-8f8b4876/`
- 确定性/模型基准：`artifacts/benchmark/benchmark-20260812.json`

## 可复现命令

```bash
scripts/deploy.sh
scripts/test.sh
scripts/python-matrix.sh
scripts/benchmark.sh
scripts/release-check.sh
pcbdraft release-verify artifacts/acceptance/release --json
```

需要现有已认证 Codex CLI 才运行模型重复；生成、验证、制造发布、离线校验
和确定性基准不需要付费或私有凭据。
