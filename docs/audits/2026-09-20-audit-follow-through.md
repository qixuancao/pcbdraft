# 2026-09-20 LQ-EDA M1 审计跟进

## 结论

M1 已完成一次 clean 单跑，worker 技术上正常结束并生成可编辑 KiCad 工程；v2 独立复评分为 **fail**，候选工程不通过。真实设计缺口包括只形成了 `B.Cu` GND 铜区而没有物化要求的 `F.Cu` GND 铜区；此外，derived validation 还记录了 KiCad library table 中未注册 `LQEDA` 导致的 ERC/DRC warnings，以及未满足的语义/制造约束。不能据此声称真实 PCB 能力已通过。

M2 停止。后续优先修复产品侧铺铜能力与约束构造，另以新版本输入契约消歧；不修改本次 raw artifact，不为消除失败而重跑模型。

## M1 执行证据

- 试跑源提交/已完成 CI 对应提交：`7771876`，source clean；source tree 为 `b9e93daba10a46d403392f489e56d7ee07a8300d`。后续 scorer 修复提交为 `9d91358`。
- 运行目录只以 basename 记录：`pcbdraft-lq-m1-run-20260919`。
- worker 状态为 completed，return code 0；1 次 attempt，769.206154 秒，60 次 model request，97 次 tool call。
- 生成项目 basename：`lq-eda-pilot-e1a91c91`；工程 revision 58，design revision 40。
- raw `result.json` SHA-256：`a28588a5ba80fc0e8c739900754cbed1d1e0cfd3f1c912d32f942656916235ca`。
- raw `manifest.json` SHA-256：`61c8e9f18bc28d30baa8e894d5f5e2a86270df0f360baed3f130c0b18f2a4fbe`。
- 最终 native artifact SHA-256：PCB `7a3a7794af5305339983c245a2ef5cd0637083413d8fa26845bfbd03657e1ff1`；原理图 `127c80e5d1c7568e7d43a2be9865e34d7af57a5baac430074add41a90a3e9760`；KiCad project `9b2b27fcfc6315e4b87fb4418ca68b95ccbfde32acfc18d8ade7b82a7de2cf8a`。
- 最终 board preview SHA-256：`31a1e5d747a8a28efb39779a3e6a71bdee23f35b829a42226bb50e18c106f53c`。

模型运行自述最终连通性为零未连接项、DRC 为 0 error/1 warning、ERC 为 0 error/2 warnings；v2 也实际执行了独立 ERC/DRC 集成复评。derived validation 的 `design_content_hash` 仍绑定 `5afaea2697a9f4000f16049364f90ed8fb00b4a554e7cd880ff4bd0a93678bc2`，但 `candidate_ready: false`，不能替代人工工程复核或被解释为生产就绪。

## v1 首评分历史记录

首个 score artifact SHA-256 为 `e1ebf9025fcefd6e2d8be1101b109b0384e127a531bf5e7512b3eee7b6ea4659`，整体状态为 `fail`，human review 为 `not_reviewed`，production ready 为 false。该结果保留为历史证据，已由 v2 复评分取代，不再作为当前待修复状态。

已确认的真实失败：

- `ground_copper_zones` 只观察到 `B.Cu`，未观察到要求的 `F.Cu`；这是本次 run 的明确设计产物缺口。

v1 中识别出的评分边界：

- 私有 `LQEDA` overlay 接入与 CN1 `Pin_N` 规范化已在 v2 评分实现中纠正；private-key CN1 erratum 另在仓库外单列，这些评价边界不应解释为电路连接错误。
- `pad_to_slot_clearance_mil` 在 v2 仍无 native rule 覆盖，保留为 unknown。
- C13/C14 的 `Device:C_Polarized` 对 `Device:C` strict metadata fail 在 v2 仍保留。题干只给出 C13 正极侧描述，未显式要求 `Device:C`；这是当前输入与 key 的一致性欠缺，不宜直接归因于模型错选或反接。旧 key 不放宽，仅在本报告披露该歧义。

首评分的 raw receipt、公开输入 binding 和 manifest copy 校验已通过；首评分文件保留，不覆盖、不伪装为最终结果。

## v2 独立复评分

- v2 score schema/version 为 `pcbdraft-lq-eda-score` / `1`；score SHA-256：`b9418d95176b90a40f0d8601c45d2379f0eb17bb337354ecca7cb2fba022b332`。
- v2 score output 只以 basename 记录：`pcbdraft-lq-m1-score-20260920-v2`。
- v2 private answer provenance SHA-256：`89768e140c7e2841f1d1458ed8568f61c93d93013ed3e3a562f7e1a0b1b98bc3`；公开 contract/prompt/resource binding 与本次 run 一致。
- 结果为 10 pass、3 fail、1 unknown；netlist endpoint check 为 pass，runtime 独立复核确认真实拓扑正确，仅有 R18 对称引脚交换。C metadata fail 不应解释为反接。
- `managed_validation` 已为 complete 且 bound=true，但 candidate=false；不再是 v1 的 validation unknown。derived `validation.json` SHA-256：`8cd8299e0220d3a4be6bb4f07feb209d277c8fcd90a453b12c068a5b8ed5202d`。

derived validation 的阻断项：

- L2 ERC：0 error、2 warnings；L2 DRC：0 error、1 warning。warning 均来自 KiCad symbol/footprint library table 未注册 `LQEDA`，不是 scorer 文件 overlay 找不到；native reports 已实际执行并保留该 warning。derived ERC report SHA-256 为 `22e90b8eed5f3510460900bf80351e86ef99904dbc557d163559f7fb267f001d`，derived DRC report SHA-256 为 `d232015d736380c4ae470c0a5bdbfb8b6530182c6f6f7dab7816d7a414a9a2a5`。
- `ground_copper_zones` fail：只观察到 `B.Cu`，缺少 `F.Cu`。
- `l3.constraint.cn1_declared_pitch_equivalence_review`：CN1 assertion predicate 缺失/unsupported。
- `l3.constraint.m1_fabrication_and_copper_rules`：`edge_clearance_mm`、`min_clearance_mm`、`min_drill_mm` 与 board contract 不匹配。
- semantic intent registry 缺少 LED `current_limit` constraint。
- `l4.manufacturability` fail：制造证据不完整或失败；L4 sourcing/lifecycle 仍不作现货或生产能力声明。
- scorer `native_board_rules` 仍有 `pad_to_slot_clearance_mil` unknown，作为当前覆盖不足。

因此 managed validation 的 candidate=false 由 L2/L3/L4 阻断共同决定：native KiCad library-table warnings、语义/制造契约阻断、LED 约束缺失和制造证据不足。F.Cu GND 是独立 scorer check 的 fail，slot 规则是独立 scorer check 的 unknown，不能归因到 managed candidate 状态。评分通过也不代表生产就绪，当前 human review 仍为 not reviewed。

## 题面与输入复核边界

已复核官方图与最终公开输入的一致性，但不在公开审计文档复制私有答案端点表：

- figure 3 的 LED4/R19、LED5/R20 对应关系与最终 prompt 一致；
- figure 4 的 C13/C14 封装为 C1206；
- figure 2 的 BCON 焊盘为 oval，pad 1 为原点；
- prompt SHA-256：`80cc431867953a0e1964ca87831fb80cb8e560242050b20e668b5e40d4506843`；contract SHA-256：`95c352c6e24749e2e8eb3dda14e01dc439751db87561db6f51e4e3fdb186d319`；BCON symbol SHA-256：`9113d26f4204bc3245af32543d1d33d7c518be4eee92f4066128e5eceafef8ff`；BCON footprint SHA-256：`c73839c209c478cc693d7a22eda943e01f1baa8c3d94d4fbcbba47f1e32fc03f`。

BCON 仍是 `ai_reviewed_pilot` 给定资源，五个 pin 的 passive 建模不能证明充电芯片的供电、驱动或开漏语义。CN1 仍为 pitch-equivalent stock JST 适配，机械外形和插接适配需要人工复核。

## 相关提交与验证边界

- `b5afb11`：显式安装 KiCad stock library packages，解决 CI 缺库根因；native M1 validation 仍有独立的 LQEDA library-table 注册缺口。
- `ef9830a`：建立 LQ-EDA 实战套件和首题输入包。
- `640f055`：冻结已复核的 M1 输入、符号和封装资源。
- `cfd1a73`：保护 late-bound agent tool contracts；对应 runtime 定向测试 16 项。
- `dfab388`：加入隔离单次 M1 runner。
- `19ea3fe`：增加 prompt hash drift 执行前拒绝。
- `7771876`：加入私有 LQ pilot artifact 独立评分。
- `9d91358`：修正 scorer 的私有 overlay 接入与 CN1 `Pin_N` 规范化；不包含 private answer-key，相关 erratum 在仓库外单列。
- 当前记录的 targeted tests 为 runtime 16、runner 最终 6、scorer 初版 4；`9d91358` 后 scorer 最终 7 项、文件范围 Ruff 通过；没有把它们扩大解释为全库通过或 M1 成功。
- `7771876` 是试跑源/已完成 CI 对应提交，不作为当前 head。对应的 [platform CI run 35431258373](https://github.com/qixuancao/pcbdraft/actions/runs/35431258373) 成功（7/7）；[primary CI run 35431258348](https://github.com/qixuancao/pcbdraft/actions/runs/35431258348) 在 Ruff 6073 条处失败，后续 mypy/coverage 按门禁跳过。
- `9d91358` 的 [primary CI run 35474060216](https://github.com/qixuancao/pcbdraft/actions/runs/35474060216) 仍因 Ruff 6073 条失败；对应的 [platform CI run 35474060215](https://github.com/qixuancao/pcbdraft/actions/runs/35474060215) 在本记录时未作为通过证据。
- 最新 6073 Ruff 阻断仍未闭环；不因本次 M1 run 完成而视为解决。

## 旧 I2C trace 的阶段归因

旧 trace 覆盖 `06:16:16.403–06:26:16.570`。最后一次 `pcb_run_drc` 在 `06:26:11` 开始、没有结束记录，约 5 秒后被 wall kill。共有 53 次 completed tool call、总计约 223.918 秒；`route_net` 7 次约 93.472 秒，`unroute` 3 次约 19.548 秒。存在 register、route、place 错误，其中 place 是 retain routed copper 冲突。该证据不能证明 DRC 自身卡死 600 秒。

## 下一步与停止边界

1. 修复产品侧 F.Cu/B.Cu GND 铺铜能力与约束构造；native KiCad library table 的 LQEDA 注册也需独立闭环，不能与 scorer overlay 混为一谈。
2. 未来以新版本输入契约消歧 C13/C14 元件类型、CN1 等价性 assertion、制造字段和 LED `current_limit` 约束；本次冻结的 input/prompt/contract 不回改。
3. 保留 `pad_to_slot_clearance_mil` unknown，作为当前评分覆盖不足；由人工复核 F.Cu/B.Cu GND 铜区、BCON 几何、CN1 机械等效性、极性与可制造性。
4. M1 只记为“单跑产出但候选不通过”；M2 停止，不发布生产就绪或硬件运行结论。

本轮没有运行全库测试、全库 Ruff 或全套发布 KiCad 验收，也没有再次调用模型；M1 v2 复评分实际执行了独立 ERC/DRC 集成检查。
