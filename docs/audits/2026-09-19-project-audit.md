# 2026-09-19 项目状态审计

当前状态：原生运行时整合、审计整改与模块化重构均已提交到开发分支
`refactor/native-runtime-20260905`；**TypeScript 终端往返已完全回退，Python TUI
是唯一产品面**。分支领先 `main` 209 个提交（2026-08-28 起分叉），尚未合并回
`main`，也没有形成全绿发布门禁。本报告记录检出版本的完成度、确定性门禁阻断
和优先级建议。

审计时间：2026-09-19。检出分支：`refactor/native-runtime-20260905`，
提交：`5ef9f18`。工作区干净，与 origin 同步。版本声明 `0.1.0`
（`pyproject.toml`），`pcbdraft --version` 输出 `pcbdraft 0.1.0`。

## TypeScript 终端往返（已收尾）

- `7f0e502`（`refactor/tui-modularization-stage1`）曾把 bun/OpenTUI 的
  TypeScript 终端设为默认并合入分支；`693611d`（`spike/typescript-tui-bridge`）
  作为 spike 从未合入。
- `8ff98eb`（2026-09-18）恢复 Python TUI 并删除全部 TypeScript 客户端：
  `src/pcbdraft/terminal_client/`（约 40 个文件）、`prototypes/typescript-tui/`、
  MANIFEST 与 pyproject 条目。当前仓库无任何 `.ts`/`.tsx` 文件。
- 结论：终端产品面固定为 Python TUI（`interfaces/tui/` + OpenTUI chat，
  `8f1e7c6`）；不重建双运行时，后续新增交互能力一律进 Python TUI。

## 完成度小结

1. **原生运行时整合**（2026-09-05 起）：696 个 Hermes 模块吸收进
   `src/pcbdraft/{agent,interfaces/tui,model,tools,services,core}`，删除
   `vendor/hermes`、路径注入、启动补丁；终端与 Web 共用同一 `AIAgent`。
2. **审计整改闭环**：09-05 审计 F01–F10 与后续 F12–F17 全部修复并有定向
   验证；09-10 lint 清理将全库 Ruff 从 10565 降至 8078。
3. **模块化重构**（09-17/09-18 密集）：application/session-db/mcp/model/agent
   按职责拆分，各模块独立分支 + 文档 ledger，逐提交合入当前分支。

## 门禁状态：三个确定性阻断（本审计实跑复现）

CI 门禁（`ci.yml`：ruff/format/mypy/coverage/compileall + kicad-acceptance；
`platform.yml`：安装器矩阵）当前必红：

| 阻断 | 证据 | 修复方向 |
| --- | --- | --- |
| **mypy 全绿被阻断**：`model/provider_profiles/builtins/` 下连字符目录
  （`ai-gateway` 等）带 `__init__.py`，mypy 报 "not a valid Python package
  name" 并中止 | `.venv/bin/mypy` 只输出该错误，退出码 1 | 重命名目录为
  下划线；加载器 `_import_plugin_dir` 已用 `safe_name`（`-`→`_`）且经
  `spec_from_file_location` 直接吃路径，行为不变 |
| **Ruff 6335 条**（较 09-10 的 8078 继续下降；基线 10565） | `.venv/bin/ruff
  check src tests` 退出码 1；198 项可 `--fix` | 分模块小批次清理，重点
  BLE001/S110/S603/S607/S608 |
| **format 9 个文件**待格式化（09-10 记录为 6 个，集合有变化） | `.venv/bin/ruff
  format --check src tests` 退出码 1，1119 个已格式化 | 机械格式化 |

## 其他发现

- **messaging 死引用（74 处、17 个文件，比初查的"约 10 处"严重得多）**：
  `pcbdraft.services.messaging` 包不存在，全部为惰性 import（函数内），模块导入
  不受影响，但调用即 `ImportError`。重灾区 `tools/send_message_tool.py`（约 40
  处）、`tools/yuanbao_tools.py`、`interfaces/tui/send_cmd.py`、`gateway_enroll.py`、
  `pairing.py`、`doctor.py`、`platforms.py`、`platform_actions.py`、
  `cli_commands_mixin.py`、`legacy_app.py`、`tools/process_registry.py`、
  `tools/skills_tool.py`、`agent/extensions/manager.py`、`agent/relay_runtime.py`、
  `agent/system_prompt.py`、`interfaces/tui/security_audit_startup.py`（有
  try/except 兜底）、`tools/toolsets.py`。网关已退役（NATIVE_RUNTIME.md），这些
  引用需要按退役政策删除或改为明确的不支持错误，列为后续批次。
- **cron 子系统整体缺失**：`cron.*`（jobs/scheduler/lifecycle_guard/
  blueprint_catalog/suggestions/notepad/executions）包不存在，约 20 处惰性
  import 分布在 agent/curator、monitoring/cron_health、model/configuration、
  tools/blueprints、interfaces/tui（cron.py、console_engine、blueprint_cmd、
  suggestions_cmd、legacy_app）。其中 `interfaces/tui/cron.py:21` 对
  `cron.lifecycle_guard` 的**模块级硬导入**使全包导入检查失败，`agent/monitoring/
  cron_health.py` 同样硬导入 `cron.jobs`；本审计已把这两处改为惰性/降级，全包
  导入 walk 恢复 0 失败。TUI 的 `/cron` 命令与网关健康导出仍会在调用时
  ImportError，需后续决策：重建、删除命令或明确不支持。
- **全包导入检查的两个合法例外**：`pcbdraft.__main__` 导入即执行 CLI（标准
  `python -m` 行为）；`pcbdraft.kicad.pcbnew_worker` 是设计上由 KiCad 子进程
  `python3 -I` 调用的独立 worker，宿主环境无 `pcbnew` 属正常。import-all 检查
  应显式排除这两者。
- **版本不一致**：CHANGELOG Unreleased 声称推进到 `1.1.0.dev0`，`pyproject.toml`
  仍为 `0.1.0`；需用户拍板统一。
- **I2C 示例超时**：2026-09-09 单次尝试案例中 I2C 唯一尝试 600 秒超时、无
  DRC/预览；LED、RC 的 ERC/DRC 通过。未重新定位。
- **空测试目录**：`tests/integration/`、`tests/hermes/` 无 `test_*.py`。
- **维护风险**：`interfaces/tui/legacy_app.py` 约 22.3k 行、`kanban_db.py`
  约 12.5k 行、`agent/conversation_loop.py` 约 9.1k 行等大文件仍是模块化重点。
- `kicad-sch-api==0.5.6` 已声明依赖并在环境中；`pcbdraft.kicad.schematic`、
  `pcbdraft.interfaces.tui` 在本机环境导入正常。历史提交消息中"缺少
  kicad_sch_api"的备注与当前环境不符，已核实。

## 测试规模与验证边界

- 1757 个 test 方法，204 个测试文件；每轮定向测试按仓库约 90 秒预算运行并通过。
- AP2112 真实板副本 smoke（2026-09-05）：ERC/DRC pass、预览正常（KiCad
  10.0.6）。
- 本审计只做了只读检查（mypy、ruff check/format、导入探针、git 历史核对），
  未运行全量 unittest/coverage、完整 KiCad 验收、TUI/Browser E2E、依赖审计、
  版本矩阵或 release-check。

## 优先级建议

1. **P0（合并回 main 的硬前置，成本低）**：修复 mypy 目录名阻断（已修，见后
   续检查点）；收口 `tui/cron.py` 与 `cron_health.py` 的硬导入（已修，全包
   import walk 0 失败）；format 9 文件；Ruff 按模块分批清零。**注意：mypy 目录
   名修复后暴露全库 3169 条类型错误（此前被崩溃掩盖），与 Ruff 同级的大额
   债务，需按模块分批推进**。
2. **P1**：复现定位 I2C 600 秒超时；统一版本声明与 CHANGELOG。
3. **P2**：全量 unittest/coverage 跑通 → CI 全绿 → `scripts/release-check.sh`
   通过 → 合并回 main 并打 tag。