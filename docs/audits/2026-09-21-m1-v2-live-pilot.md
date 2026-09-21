# 2026-09-21 LQ-EDA M1 v2 live 试跑记录

## 结论

新版 M1 v2 已完成一次正式 live 试跑，但 raw run 因 900 秒 wall timeout
标记为 `failed`；独立评分为 **overall fail**（12 pass、2 fail、1 unknown）。
运行确实生成了一个可编辑 KiCad 工程，并验证了器件、网表、双面 GND
zone、零 native unconnected、ERC 和 schematic/PCB parity 等基础能力；这
不能解释为完整工程候选通过、生产就绪或硬件可运行。

旧目录 `15th-province-p1`、旧 raw、旧 score 和冻结的 v2 输入包均未修改。
由于 v2 是 `input_revision=2` 的新输入，不能与旧 M1 当作同题同输入做分数
或能力提升比较。

## 运行与绑定证据

- 首次启动的公开 basename 为 `pcbdraft-lq-m1-v2-run-20260921`；worker
  因 invalid run id 在 0.082742 秒退出，未调用模型。启动修复提交为
  `4453389484814492be14ad4b98fb9a681e4c8c1c`；对应 8 个 runner tests 和
  Ruff 已通过并已推送。
- 正式运行 basename 为 `pcbdraft-lq-m1-v2-run-20260921-r2`，1 attempt，
  900.245174 秒，84 次 model request、108 次 tool call，产出 1 个工程；
  raw result 状态为 `failed`，原因是 wall timeout。
- 运行 `result.json` SHA-256：
  `2d7d13395de1ca54306e605f0041b2999522fbaa1821c6d35eaecb7002135b1b`。
- 运行 `manifest.json` SHA-256：
  `fedf66235316f4f119d2550a57814da337b8de3c41dee8bb69eb0d3e052f2c2e`。
- 评分输出 basename 为 `pcbdraft-lq-m1-v2-score-20260921-r2`；`score.json`
  SHA-256：
  `642b8300c2f17c0bbcf6dd74a544217da2aa6fd94d6e77ff17f8ded9f8203ce1`。
- derived validation report 的 `validation.json` SHA-256：
  `57097c5141fb4b7e9670fdd62e311dca7dea57a032159b4da98357ab0de9c644`。
- derived validation receipt `receipt.json` SHA-256：
  `61c8d0158f6e6efb9c098dea116530c7df5aec857844c0a48299d4c6c3680bb3`。
- 生成工程 basename 为 `project-0fab3299`。最终 PCB、原理图和 KiCad
  project 的 SHA-256 分别为：
  `1620d212ac64863c8edace4087de4c7e8f7c7d9111e55a71dc2922b4b88ce027`、
  `e058378682a4fcbd4535fcbb81d08dab0b649883a327c489564bdde3cf975eb0`、
  `eb5ebeb3084136e7e85dc66e9d1040d5ec112fe53fe87a6ed0aed1a4d7fcf448`。
- v2 contract SHA-256 为
  `88ec4aed77cc94192824564857aee72f7b2b5f33523578d809e70d1fd9e735b9`；
  manifest copy 和 public/private consistency 均通过。最终工程实际包含
  绑定到冻结资源字节的 `LQEDA` symbol/footprint 及项目本地 library tables；
  这只证明资源打包与绑定，不证明 BCON 电气语义或器件额定值。
- 主 agent 对 raw result inventory 的 273 个文件逐一核对了 bytes 与 SHA-256；
  score 派生出的 PCB、原理图和 project 三个 native 文件与 raw 最终三文件
  字节一致。该独立 inventory/native 核验不改变 raw run 的 timeout 状态，
  也不把评分的 overall fail 改写为通过。

## 评分和 native 验收边界

通过项包括：

- components exact、netlist endpoint 检查、manifest/input contract binding；
- F.Cu 与 B.Cu 的 GND zone 均已填充且为正面积；
- native unconnected items 为 0；
- ERC 为 0 error / 0 warning；
- schematic/PCB parity、顶层器件、线宽和过孔尺寸检查通过；丝印这里只是
  layer presence 检查。

阻断项和未知项包括：

- raw run receipt 因 timeout fail；managed validation 虽为 complete/bound，
  但 `candidate_ready=false`，因此候选 fail。
- DRC 保留 1 个 error：F.Cu 上 U13 pad 2 的 GND thermal relief 要求至少
  2 个 spokes，实际只有 1 个。工程记录另有 LED4 的 `silk_overlap` warning；
  这不是可以忽略的完整 DRC 通过证据。
- L3 的 `routing` 与 `placement_region` 约束参数未满足当前 validator
  契约：routing 记录了 clearance/via 字段但缺少 validator 所需 routing
  字段，placement 只记录 top/side/listed-components 而非 region 契约。
  本轮不是把它们改名为 pass。
- `current_limit` 缺失。该缺口是已知输入资料不足：没有可靠的 BCON
  供电/电流限制或 LED Vf，未编造参数；v2 的通用 scope 上限也不是器件
  额定值证明。
- L4 manufacturability fail；器件生命周期、现货和制造能力没有被本轮
  伪造为已验证。pad-to-slot native rule evidence 仍为 unknown。

## 下一步

先做产品侧的定向收口：让 routing/placement 写入边界与 validator 使用同一
输入契约，并修复 native 热焊盘 spokes 生成/检查；随后只需针对这些边界做
最小验证，再决定是否值得下一次 live。没有 BCON/LED 的可靠额定资料前，
`current_limit` 仍保持缺失，不能用重跑模型或放宽评分来替代。

本记录没有修改历史输入或旧证据；正式 run 超时后未追加模型重试，也没有
运行全量测试、全量 Ruff 或发布门禁。
