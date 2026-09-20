# 2026-09-20 M1 产品缺口收口记录

本轮只收口三项范围：约束写入契约、自定义 KiCad 资源注册、以及双面铺铜产品功能。冻结的 M1 raw、benchmark inputs 和私有答案均未修改；没有再次调用模型。

## 1. 约束写入契约

- `4d84fa9d05a07fd0cf80899f9bf4935be0187d17`（`fix(constraints): reject unsupported writes before project mutation`）在项目变更前拒绝不支持的 assertion、fabrication 和 LED `current_limit` 写入，保留 legacy loading。
- 6 个定向测试与 scoped Ruff 检查通过。该提交只证明写入边界和失败前不变更，不证明 M1 的具体板级约束已经满足。

## 2. 自定义 KiCad 资源注册

- 新生成的 managed project 只打包实际引用且不同于 stock 的 custom symbol/footprint 资源；项目表使用 `${KIPRJMOD}`，保留既有 entries，并将表与资源纳入 manifest/hash/drift 校验。
- 独立搬移副本 basename：`pcbdraft-library-portable-evidence-20260920-FS8qsA`；收据 basename：`receipt.json`；收据 SHA-256：`e3dc918eab988a345f811293ea978c8e21c570824fc4fd6fedef2236da95923c`。
- KiCad 10 独立 ERC/DRC 均为 0 error、0 warning、0 LQEDA missing-library match；ERC 用时 1.772542 s，DRC 用时 2.196956 s。副本 native 三文件保持源 SHA：PCB `7a3a7794af5305339983c245a2ef5cd0637083413d8fa26845bfbd03657e1ff1`、SCH `127c80e5d1c7568e7d43a2be9865e34d7af57a5baac430074add41a90a3e9760`、PRO `9b2b27fcfc6315e4b87fb4418ca68b95ccbfde32acfc18d8ade7b82a7de2cf8a`。
- 4 个 library 专属测试和 4 个 KiCad runtime 测试通过；managed reproducibility native case 也通过（1 case，27.120 s）。library 改动提交并推送：`f907050a8c715e1f1b56b6e3a942383a241fa90f`。native 验证仍依赖已安装的 stock KiCad 10 runtime；项目表只注册打包的 custom resources，不能替代 stock 库或 generator runtime。PCBDraft 后续迁移/再生成仍依赖调用方运行时可见的 custom library；本轮不切换或修改 global environment，也不把 project-local 资源隐式当作 native generator 的 overlay。

## 3. 双面铺铜功能与 M1 停止边界

- M1 仍是“单跑产出但候选不通过”：raw 不重跑、不修写。此前独立复评分确认 `F.Cu` GND 铜区缺失，另有语义/制造约束和规则覆盖缺口。
- 产品侧 zone 修复已由 `ApplicationService` flat normalizer → `ChangeSet` → `generate_pcb` → native inspect 流程验证双面正面积铺铜；提交并推送：`1205027e02dc099dc65d48f2cb261c800cc3a4e8`。真实 1 case 用时 10.787 s，80 个定向 case 用时 6.426 s，layers-only exact-GND、wrong-layer rejection、null fallback recovery 和 route protection 均包含在定向测试中。该结果不回写 M1 raw，也不宣称完整 native planner/route 成功率或 M1 通过。
- GND zone、slot clearance、CN1 机械等效性、BCON 几何/电气语义以及生产可制造性仍需主 agent 逐项确认；本记录不把 library-table 修复解释成 zone 或生产 readiness 修复。
- 当前证据不支持生产就绪、真实硬件运行或人工 review 完成的结论；全库 CI 尚未 green，不得据此 release。M2 继续停止，后续应以新版本输入契约和产品侧铺铜/约束修复为边界。
