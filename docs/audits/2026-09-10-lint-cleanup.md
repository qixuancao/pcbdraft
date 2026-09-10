# 2026-09-10 lint 清理与 `.hermes` 调查记录

## 当前结论与计数

**lint 尚未全绿：8078 条诊断，较基线净减少 2487 条。**
本轮 lint 修复未修改规则或忽略配置；原有脏文件及未跟踪资料保留。
Ruff **0.16.2** 按现行配置检查 `src tests` 的全部启用规则，JSON 聚合复核命令：

```sh
.venv/bin/ruff check src tests --output-format json --no-cache
```

JSON 数组长度与规则 `Counter` 聚合结果为 `src = 8078`、`tests = 0`，退出码 1。

| 检查点 | 剩余诊断 | 变化 |
| --- | ---: | --- |
| 基线 | 10565 | — |
| 第一阶段 | 8809 | 减少 1756 |
| 第二阶段 | 8077 | 再减少 732，两轮共修掉 2488 |
| helper 补齐后 | **8078** | 新增 1 条未抑制 S603，净减少 **2487** |

`src/pcbdraft/core/runtime_process.py` 的 helper 使用固定命令并校验 PID，仍有 1 条未抑制 S603。
两轮清理的模块累计变化如下；helper 随后使 core/services 从 115 增至 116：

| 模块 | 基线 → 两轮清理后 | 两轮减少 |
| --- | --- | ---: |
| core/services | 372 → 115 | 257 |
| model | 1233 → 919 | 314 |
| agent | 2727 → 2076 | 651 |
| tools | 2448 → 1785 | 663 |
| TUI | 3785 → 3182 | 603 |

### 主要剩余规则

| 规则 | 数量 | 规则 | 数量 |
| --- | ---: | --- | ---: |
| BLE001 | 4324 | S110 | 1266 |
| S603 | 415 | S607 | 158 |
| S608 | 148 | F401 | 111 |
| SIM102 | 107 | SIM103 | 85 |
| S310 | 79 | S112 | 78 |

上述 Top 10 合计 6771，其余规则 1307；重点仍为异常边界、静默吞错与进程调用。

## 已提交成果与验证

以下四个提交均已推送核验，本记录复核时远端当前分支 SHA 与本地 HEAD 一致：
`a4e89e2be75dc51d576e875826326f092f2e4fe3`。

| 提交 | 内容 |
| --- | --- |
| `57f56d1` | 数据库与 runtime 加固，新增 17 个定向回归测试 |
| `074c730` | 第一批机械修复：导入与可选类型清理 |
| `1ea1456` | 第二批：明确子进程退出策略及剩余 agent 类型 |
| `a4e89e2` | 补齐 `terminate_pid` helper，7 个平台定向测试通过 |

各批遵循仓库约 **90 秒**快速验证预算，下列定向行为测试通过；全规则 lint 仍未通过：
core 新增 17、model 17、agent 4 + 2 个 import unittest、TUI 分批 48 + 12、
tools 10 个故障注入测试；helper 7 个测试见 `tests/core/test_runtime_process_compat.py`。
批次覆盖可能重复，不汇总为唯一测试总数。

**gateway 尚未修好：** `terminate_pid` 已补齐，但完整导入仍被旧缺失
`pcbdraft.services.messaging` 阻断；本轮不重建旧 messaging，完整导入保留为待收口项。

本次复核：Ruff lint 退出码 1；`git diff --check` 通过。
`.venv/bin/ruff format --check --no-cache src tests` 退出码 1：
**901 个文件已格式化，6 个待格式化**。以下均为原有脏文件，保留原样：

- `src/pcbdraft/agent/tooling.py`
- `src/pcbdraft/domain/task_contract.py`
- `src/pcbdraft/services/application.py`
- `src/pcbdraft/services/gui_session.py`
- `src/pcbdraft/services/progress.py`
- `tests/services/test_audit_contract_gates.py`

本次未重跑上述批次测试，无超时中止；未扩大全量测试、coverage、KiCad 验收、
TUI/Browser E2E 或发布门禁，全量验证留待 CI/集成阶段。

## `.hermes` 为何仍然存在

根 `.hermes/plans` 仅有三份 **2026-08-30** 历史计划，未跟踪，非正常 runtime 必需。
[旧审计](2026-09-05-project-audit.md) 已明确记录保留；本轮询问原因，历史资料未移动或删除。
正常 runtime 位于配置目录下的 `runtime`，见 `src/pcbdraft/core/runtime_paths.py`。
`.hermes` 仍有以下实际使用，不能宣称已全部移除（源码路径以 `src/pcbdraft/` 为根）：

| 位置 | 当前行为 |
| --- | --- |
| `agent/verify/environment.py` | 保存时创建项目 `.hermes` 并写入 `.hermes/environment.json` |
| `agent/skill_utils.py` | 兼容读取受信任项目的 `.hermes/skills` |
| `agent/extensions/manager.py`、`agent/memory_backends/__init__.py` | 显式启用时兼容读取项目 `.hermes/plugins` |
| `tools/environments/ssh.py` | 实际创建 SSH 远端 `.hermes` 目录树并用于同步 |

## 分模块下一步

| 模块 | 重构与定向验证 |
| --- | --- |
| core/services（116） | 收紧文件、数据库异常边界及查询构造；验证 runtime、事务失败行为 |
| model（919） | 拆分配置读取、转换与持久化，减少静默回退；运行对应模型测试 |
| agent（2076） | 整理 provider、执行器与扩展的异常和导出契约；验证 import、异常传播与清理 |
| tools（1785） | 统一进程参数、可执行文件解析及失败语义；保留定向故障注入 |
| TUI（3182） | 拆分命令与服务管理复杂流程，明确降级行为；验证命令和生命周期 |

后续按约 90 秒小批次推进，核对导入副作用与公共导出，持续更新计数及 gateway 阻断状态。
