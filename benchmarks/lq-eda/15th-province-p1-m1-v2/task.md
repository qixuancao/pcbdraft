# 首题：第 15 届省赛 P1 · 充电控制板（M1 v2）

| 项 | 值 |
| --- | --- |
| 来源 | 蓝桥杯 EDA 设计与开发 · 第十五届省赛 P1 |
| 题面 | [嘉立创 EDA 文档中心](https://wiki.lceda.cn/zh-hans/contest/lq-contests/true-question/15th-province-p1.html) |
| 独立任务 ID | `15th-province-p1-m1-v2` |
| 输入修订 | `2` |
| 本包状态 | 仅完成输入契约整理；未运行模型，未产生评分结果 |

## 目的与冻结边界

这是旧 M1 输入的独立 v2 包。旧目录 `15th-province-p1/` 保持冻结，v2
不覆盖旧 prompt、contract、私有答案或旧 run。v2 只消歧输入契约，不改变
原 M1 的器件、网络、几何、板层或设计规则门槛。

v2 是新的输入修订，不能把它与旧 M1 当作同题同输入直接比较评分；旧单跑
结果只属于旧输入。保持不变的是机器可执行的器件/网络/几何/规则门槛，不是
旧 prompt 的每一句文字。

本版唯一的电容契约消歧是：C13、C14 使用非极性的 `Device:C`，因此题干不
再要求电容极性或方向。两端网络连接仍必须正确；
R18/R19/R20/C13/C14 的 pin-1/pin-2 交换按公开契约的对称两端规则处理。
这是一项输入建模约定，不是对原题电容极性或硬件行为的额外断言。

## M1 输入

1. 题面图 3 的充电控制区及其文本化连接描述：U13 BCON、R18/R19/R20、
   LED4/LED5、C13/C14、CN1，网络名为 `VBUS`、`VBAT`、`PROG`、`STAT`、
   `GND`、`LED4_A`、`LED5_A`。
2. C13/C14 均为 10uF、1206、非极性 `Device:C`；电阻和 LED 为 0805；
   CN1 使用 stock JST XH 2-pin vertical 2.50 mm footprint 作为声明的
   stock-footprint adaptation，其 pitch、外形和机械适配仍需人工确认。
3. U13 的 `LQEDA:BCON` 符号和 `LQEDA:SOP_BCON` 封装作为给定 AI-reviewed
   pilot 资源，原字节及 SHA-256 在 `contract.json` 绑定。资源来源于旧 M1
   输入包的冻结副本，不声称有厂家 datasheet 或独立人审。
4. 双层板、最小线宽 10 mil、焊盘到焊盘 7.5 mil、焊盘到挖槽 7 mil、其它
   间距 8 mil、过孔外径至少 25 mil、钻孔至少 15 mil；器件全在 F.Cu，
   F.SilkS，F.Cu/B.Cu GND zones，0 未连接项。
5. 若工具提供受限的 `ground_plane_layers` 结构化操作，可请求逻辑层
   `[0, 1]` 生成双面 GND zone；该说明只帮助使用公开工具，不提供私有答案
   端点，也不替代独立拓扑评分。

## 验收边界

验收按 `contract.json` 的 `acceptance_matrix` 分开执行：自动检查负责可机读
的资源、原理图 netlist 元数据、完整 endpoint 集合、已暴露的规则字段、原理图/
PCB 一致性和 receipt 绑定；当前 GND 检查边界是目标层 presence、filled 和正
面积，不证明实际铺铜连通性或有效覆盖。人工或未知项负责极性/机械适配/BCON 资源、
pad-to-slot 和可制造性。未知项不得视为通过。

ERC/DRC receipt、零未连接、可编辑工程和 `candidate_ready=true` 是独立工程
证据门槛；它们不能替代私有参考网表的逐网络 topology 比对，topology 通过也
不能单独宣称完整 engineering candidate 已通过。当前 runner/scorer 未覆盖的
pad-to-slot 和制造性字段必须记录为 unknown，不能从静态规则文本推断为通过。

私有参考网表和答案键不放入本目录；公开契约只保留规则、资源哈希和评分边界。
本包不运行模型，不提供运行结果，不声称完整 KiCad 验收或人工工程签字。

## 已知限制

- BCON 五个引脚的电气类型仍统一建模为 passive，因为题面只给引脚编号和名称；
  ERC 不能证明充电芯片的 power-input、驱动或 open-drain 语义。
- 题面没有可引用的 BCON 供电/电流限制、LED Vf 或 LED current_limit 参数；
  不要求模型编造这些数值，也不把其未知状态判为通过。
- 后续若要闭合这些工程未知项，需要用户或可靠规格提供 BCON 电气语义与额定
  条件、LED 型号/Vf/限流依据，以及 CN1 `ZX-XH2.54-2PZZ` 的 pitch、外形和
  机械适配依据；本包不默认补值。
- BCON 封装是从官方图形重建的 AI-reviewed pilot 资源，需独立人工复核。
- CN1 的 stock JST footprint 只是声明的 stock-footprint adaptation，连接器
  pitch、本体和机械适配需人工确认；不得将其自动当作 `ZX-XH2.54-2PZZ` 的
  精确封装身份。
- 没有硬件测试，不宣称生产准备度。
