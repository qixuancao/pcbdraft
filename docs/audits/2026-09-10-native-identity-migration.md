# 原生运行身份迁移审计（2026-09-10）

## 范围与状态

本轮把已吸收实现的运行身份统一到 PCBDraft，覆盖 core 路径与启动迁移、model
协议身份、agent/plugin namespace、tools 资源所有权和离线默认值、TUI 文案与
旧生命周期功能退役。代码由各 owner 完成，最终主审已完成；代码提交
`f162af03b557ea1806f0c10ce178190606a6ef18` 已推送，远端 SHA 已由主审核验。
本文记录主审交接的最终结果；本次文档收尾不重新执行代码验证。
操作说明见[原生运行时](../NATIVE_RUNTIME.md)。

公共 CLI 命令面不变；来源基线仍为 696 个原有 Python 模块，
[source map](../native-runtime-source-map.json) 的历史 `sources` 不重写。
本轮仅追加运行功能退役说明，不将仍用于导入兼容或读取的模块标为已删除。

## 迁移契约

| 子系统 | 本轮边界 |
| --- | --- |
| core | 正常路径纯解析，仅 `PCBDRAFT_RUNTIME_HOME` 为 runtime 环境覆盖；启动显式调用 `legacy_migration` 处理 PCBDraft 自有配置目录的 `hermes` → `runtime` rename |
| 迁移保护 | 内部绝对链接和已知配置字段指向旧根时拒绝并保源；旧环境变量报升级提示；双目录冲突保留两者并写记录，低层返回 native 路径及 `conflict`，启动层明确报错、停止绑定 |
| 项目 | 显式 `python -m pcbdraft.core.legacy_migration --project PATH`；只复制 `environment.json`、`skills`、`plugins`；保源、冲突阻止复制且不覆盖，拒绝选定根及所选源、目标树内被迁移节点的 symlink |
| runtime helpers / model | native helper 命名；工具 MCP 身份为 `pcbdraft-tools`；协议兼容项按下节保留 |
| agent / plugins | entry points 为 `pcbdraft.plugins`、`pcbdraft.memory_providers`；模块 namespace 为 `pcbdraft_plugins` |
| tools / remote | root sandbox 使用 `/root/.pcbdraft/runtime`，其他远端使用 `<remote_home>/.pcbdraft/runtime`；Docker 不复用或清理旧 owner 的资源 |
| 默认外部内容 | skills/catalog 默认离线；第三方来源需显式配置；Tirith 启用扫描与允许下载分离，下载须 opt-in |
| TUI / lifecycle | 无公共入口的 gateway、update、uninstall、desktop service 和 profile 管理写操作退役；保留 profiles 读取、有限的内部描述元数据编辑及连接向导 |

运行时目录 rename 与项目复制是两种独立操作。显式设置新的 runtime 环境变量
跳过前者；项目命令不会发现 home、迁移计划或执行 runtime 迁移。独立 Hermes
安装不作为默认配置、容器所有者或生命周期管理目标。

迁移允许可信父路径 canonicalization，例如 macOS 的 `/var` 别名；选定配置根、
项目根和迁移根本身的 symlink 仍拒绝，不能通过先解析根链接绕过边界。项目复制
拒绝所选树内节点的 symlink；runtime rename 中的外部链接则原样保留、不跟随，
指向旧 runtime 根的危险内部绝对引用仍阻止迁移。

## 必要的历史与协议例外

- 版权、许可、上游归属、历史 source map 和归档历史继续使用原始名称。
- 真实第三方模型 ID 与原始唤醒词 binary 保持资源语义；原始唤醒模型不默认启用。
- 已注册 OAuth 客户端/服务 ID、第三方协议要求和旧加密格式保留兼容语义，
  不凭名称替换破坏登录或已有密文读取。
- 集中 legacy 数据读取保留旧技能元数据、记忆数据及其他必要历史数据兼容。
  旧 namespace ID 数据保持 effective IDs，即使再次保存也不改写其身份；
  新写入的 source/provenance 使用 native 标识，不能概括为所有持久化值均改名。
  PCBDraft 自有 checkpoint 旧引用由专门迁移逻辑保留，不能据此接管另一应用的数据。
- 安全扫描和文件保护仍识别其他应用的敏感目录及攻击模式。

因此验收目标是运行身份、所有权和副作用边界正确；不以全仓库 Hermes 文本
检索零命中为标准，也不宣称全量 lint 已通过。

## 历史计划资料处理

三份 2026-08-30 原始计划含私人环境资料，使用 `apply_patch` 的 `Move to`
完整移动至本地 `.agents/archive/2026-08-30-plans/`，保留原文件名和内容。
该目录受根 `.gitignore` 的 `/.agents/` 规则保护，不在现行打包范围内。
源目录为空后仅用 `rmdir` 删除 `.hermes/plans` 和 `.hermes`。
公开[历史摘要](../archive/plans/2026-08-30/README.md)只记录三个方案主题，
不复制私人路径、端点或真实项目标识；原计划不进入公开文档或 Git 暂存。

## 验证证据与边界

以下为各 owner 早期已报告通过的定向批次，保留为阶段证据：

| 批次 | 已报告通过数 |
| --- | ---: |
| core | 70 |
| model | 23 |
| agent | 34 |
| tools | 44 |
| lifecycle | 24 |
| TUI identity | 21 |

最终主审交接的验证结果如下，**不是本次文档编辑重新执行的结果**：

| 验证范围 | 结果 |
| --- | --- |
| 最终分批集成 | 75 + 69 + 60，共 204 个不同用例通过 |
| 同进程重复复验 | 32 个用例通过，属于重复覆盖 |
| 主 Agent 独立快照验证 | 仅含暂存 `src` / `tests` 的快照，69 个用例通过，7.805 秒 |
| 后续新增修复：core | 35 个用例通过 |
| 后续新增修复：managed 缓存 | 11 个用例通过 |
| 后续新增修复：history EOF | 2 个用例通过 |
| 后续新增修复：Tirith | 5 个用例通过 |
| 改动 Python 文件 format | 479 个文件全部通过 |
| 新增 Python 文件 F821 / F822 / F823 | 19 个文件通过 |
| NativeBoundary | 3 个用例通过 |
| 改动文件全规则检查 | 选定改动范围仍有 6,412 项历史报告；不是全库统计，也不表示全量 lint 通过 |

主 Agent 独立快照验证的 unittest 选择参数（按执行顺序）为：

```text
tests.agent.test_runtime_import tests.services.test_provider_connection tests.interfaces.test_terminal.RuntimePathsTests tests.interfaces.test_native_lifecycle_migration tests.interfaces.test_tui_command_completion
```

204 仅是最终分批集成中的不同用例数；早期 owner 批次、同进程复验、独立快照
及后续针对性检查存在重复覆盖，不混算为新的唯一用例总数。验证无中止项；
补丁准备耗时超过 90 秒，但执行检查预算保持短时。未执行全量测试、TUI/Browser
E2E、真实外网或真实 KiCad 验收。

前一轮文档与归档检查已通过：本地链接、JSON 解析、历史映射不变、定向 diff
检查、归档前后 SHA-256 一致，以及 Git ignore / 打包配置的静态核对。
本次最终文档收尾仅检查两份 Markdown 的本地链接和 diff，不新增代码测试或提交。
