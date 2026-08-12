# 竞品烟测与三例缺陷基准

时间：2026-08-12

## 结论

当前 `pcb-agent-runtime` 是可运行的 **KiCad AI Reviewer + Safe Patcher MVP**，不是完整自动画板软件。它不能在“从代码/自然语言生成原理图与 PCB”这个任务上与 atopile、tscircuit、circuit-synth 直接竞争。

在与其最接近的 KiCad MCP Pro 只读审查基准中，当前版本没有胜出：三例目标缺陷检出 2/3，对方 3/3；平均墙钟时间 67.82 秒，对方 16.73 秒。样本太小，不能推广为总体准确率，但足以否定“当前已经优于竞品”的说法。

## 固定版本

- pcb-agent-runtime: 0.1.0；Codex CLI 0.147.0；KiCad CLI 10.0.5
- atopile repo: `619eda7f7775`；经典 CLI 0.15.8
- tscircuit repo: `8a3add95b64f`；CLI 0.0.2305
- circuit-synth repo: `3aaff18c056d`；PyPI 0.12.1
- KiCad MCP Pro repo: `2d12119ccdd5`；PyPI 3.30.1

## 功能烟测

| 工具 | 实际结果 |
|---|---|
| pcb-agent review | 真实 Codex 审查成功，生成结构化证据和 Markdown 报告 |
| pcb-agent patch/apply | 真实完成 R1 `10k -> 11k`；源文件先不变、staging 通过门禁、apply 有 backup、ERC/DRC 无新增错误 |
| tscircuit | 2.74 秒导出含 `.kicad_sch/.kicad_pcb/.kicad_pro/STEP` 的 KiCad ZIP；该最小板随后 ERC=2 错误/2 警告，DRC=0 错误/3 警告 |
| circuit-synth | 0.56 秒建项目并真实生成原理图、BOM、PDF；当前 PyPI 包明确报告 PCB generation 未包含，Gerber 失败；命令仍以 0 退出 |
| atopile | CLI help 可用；最小 quickstart build 600 秒未完成；validate 在当前安装上触发 `front_end` ImportError 后仍挂住，未产出板文件 |
| KiCad MCP Pro | review profile 可用；三例均给出 ERC/DRC、视觉 QA、放置/DFM/质量门禁；无 KiCad GUI IPC 时部分检查降级但仍产出结果 |

## 同任务基准

数据来自 KiCad MCP Pro 自带的三个标注缺陷 fixture。两边都在复制件上运行，目标只看 fixture 名称声明的主缺陷是否在结果中明确检出。

| Fixture / 目标缺陷 | pcb-agent-runtime | KiCad MCP Pro | 墙钟时间（本工具 / MCP） |
|---|---:|---:|---:|
| footprint overlap | 命中 | 命中 | 61.30s / 22.92s |
| bad decoupling placement | 命中 | 命中 | 68.80s / 14.43s |
| sensor cluster spread | 未明确命中 | 命中 | 73.37s / 12.84s |
| 汇总 | 2/3 | 3/3 | 平均 67.82s / 16.73s |

限制：fixture 由竞品仓库提供，天然可能偏向它；仅三例；未测误报率、重复性、修复成功率和大工程扩展性。因此这是定向烟测，不是公正排行榜。

## 当前能力边界

已完成：

- `doctor/review/patch/apply` CLI
- 本地 KiCad ERC/DRC、netlist、board stats、IPC-D-356 证据
- Codex 结构化审查
- 只在 staging 修改、唯一文本替换、路径/符号链接限制
- hash 漂移检查、备份、apply 后重跑门禁、失败回滚
- 25 个离线测试；四个真实 review；一个真实 patch/apply；Schema 修复经独立审查通过（无安全或逻辑问题）

未完成：

- 从需求生成原理图/PCB
- 语义 IR 和语义级修改（当前仅文本替换）
- 元件选型/库存/生命周期/可信 part graph
- 层次原理图、多板、多版本兼容矩阵
- 自动布局、布线、约束求解
- BOM/DFM/SI/PI/热/EMI/仿真/制造发布闭环
- GUI、MCP/API、多模型配置、持续基准与误报统计

## 下一轮优先级

1. 建立独立错误注入 corpus，至少覆盖 50–100 个正负样本，统计检出率、误报率、重复性和修复成功率。
2. 增加确定性 PCB 几何/意图规则：去耦距离、功能分组、器件重叠、边缘间距、回流路径代理；减少把所有判断交给 Codex。
3. 用 KiCad 语义 AST/IPC 操作替代原始 `replace_text`，保留事务、hash、门禁和 rollback。
4. 将 Codex 的 model/reasoning 设为参数；增加快速审查档、缓存和成本/延迟 receipt。当前 `max` 推理是主要延迟来源。
5. 支持层次原理图、多项目选择、BOM/footprint contract、DFM 和制造输出验证。
6. 再开始“需求 -> 结构化原理图 -> 布局/布线”的生成链；否则不应称自动写板软件。

## 可重跑入口

```bash
cd /mnt/2T/pcb-agent-runtime
scripts/test.sh
FIXTURE_DIR=/path/to/single-kicad-project scripts/benchmark.sh
```

竞品功能依据：[1][2][3][4]

## Sources

[1] https://github.com/atopile/atopile — atopile
[2] https://github.com/tscircuit/tscircuit — tscircuit
[3] https://github.com/circuit-synth/circuit-synth — circuit-synth
[4] https://github.com/oaslananka/kicad-mcp-pro — KiCad MCP Pro
