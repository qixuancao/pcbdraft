# M1 输入契约对齐记录 · 2026-09-21

本记录只描述输入材料整理，不是模型运行、评分结果或完整 KiCad 验收报告。

## 范围

- 旧目录 `benchmarks/lq-eda/15th-province-p1/` 保持冻结；旧 prompt、contract、
  私有答案和旧 run 不改。
- 新目录 `benchmarks/lq-eda/15th-province-p1-m1-v2/` 使用独立 task ID
  `15th-province-p1-m1-v2`、`version: 1` 和 `input_revision: 2`。
- 新输入保留旧机器可执行的器件、网络、几何和规则门槛，但不与旧 M1 当作同题
  同输入直接比较。
- C13/C14 在 v2 明确采用非极性 `Device:C`；删除电容方向要求，R/C 两端按
  对称端点规则处理。这是新版输入建模选择，不是对旧 prompt 文字的逐句声称。
- BCON 符号和封装按原字节复制到新 input 路径并由 contract SHA-256 绑定；继续
  保留 `ai_reviewed_pilot`，不声称人工 sign-off 或厂家规格。

## 尚未闭合的输入/工程项

- 没有可靠来源的 BCON 电气语义、供电范围或额定/电流限制；不能由模型猜测。
- 没有可靠来源的 LED 型号、Vf 或 `current_limit` 参数；不能由模型补值。
- CN1 `ZX-XH2.54-2PZZ` 的 pitch、外形和机械适配依据仍需人工或可靠资料确认；
  stock JST 资源只是声明的适配方案。
- 当前自动边界对 GND zone 只确认目标层 presence、filled 和正面积，不等同于
  实际连通性/有效覆盖；pad-to-slot、丝印尺寸/可读性和制造性仍为 unknown。

## 验收边界与运行状态

acceptance matrix 将资源/元数据/私有 topology endpoint、已暴露规则、ERC/DRC
receipt、原理图/PCB parity 和 `candidate_ready=true` 分开；topology、ERC/DRC
及 engineering candidate 彼此不能替代，unknown 不判 pass。公开目录不含答案端点。

## 代码对齐概要

- runner 的现有安全边界覆盖安全 `task_id`、输入 manifest 和 worker 身份绑定；
  contract preflight 会校验 v2 的资源路径与 SHA-256，但这不等于已经执行一次
  M1。
- scorer 的公开/私有一致性边界覆盖 component mapping、rules、R/C 对称 pin
  swap 规则和 artifact/hash 一致性；私有 endpoint 仍只在 holdout 中使用。
- revision 2 的 public input-revision binding 已由 run manifest 的
  `contract.input_revision` 与 `contract_path` 强制实现；缺少该 binding 的 run
  必定不能判为 pass。旧 revision/旧目录的产物不重评分、不借用 v2 契约结论。

本轮主复验为 23 tests（含新包 5 个专属 case），0.030s，OK；5 个相关 Python
文件的 Ruff check/format 与 `git diff --check` 均通过。这些是材料/契约和代码
边界证据，不是模型或工程候选通过证据。

新私有 `answer-v1.json` 仅记录 basename/hash：
`3be89b833f206840daa959cb8595e63171ea42ab8e8417c28343f731c4c41987`。主核验它
与旧 key 的 components、nets、rules、pinchecks、swap 完全一致，且新的
public/private semantic checks 通过；公开文档不记录私有路径或 endpoint。

本轮未运行模型、未执行 live M1、未产生 M1 评分结果；未跑全量测试、full
KiCad 验收或 E2E。
