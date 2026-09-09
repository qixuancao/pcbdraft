# PCBDraft 用户指南

PCBDraft 的基本单位是 **PCB 项目**，不是一段聊天。项目保存设计、原生 KiCad 文件、
revision、检查记录和候选产物；终端对话只是操作当前项目的一种方式。

## 创建和迭代项目

完成[安装](INSTALLATION.md)后：

```bash
pcbdraft connect
pcbdraft setup
pcbdraft doctor --json
pcbdraft
```

在 TUI 中先创建工程，再描述需求：

```text
/new led-demo
设计一块 3.3V 指示灯原型板：绿色 5mm LED、330Ω 0805 电阻和 2 针电源接口。
请先说明假设，再生成可在 KiCad 中检查的候选工程。
```

这是输入示例，不代表成功率。遇到器件身份、封装或电气条件不明确时，应补充精确要求，
不要默认接受模型猜测。随后可以直接用自然语言要求查看网络、移动器件或重新检查。

## 检查候选

```text
/review
/logs
/validate
```

- `/review` 汇总当前计划、revision、检查和预览状态；
- `/logs` 显示最近的结构化项目事件；
- `/validate` 运行当前聚合验证快捷操作。

`/confirm` 批准**当前工程候选用于生成**，不是任意模型工具的通用授权，也不代表人工
设计审核已经通过。`/release` 尝试生成制造候选证据包；即使成功也不表示生产就绪。
ERC/DRC 不能证明功能、热、EMC、SI/PI、可采购性、可制造性或硬件实测通过。

## 恢复已有项目

```text
/resume
/projects <筛选词>
/open <项目名、完整 ID 或唯一 ID 前缀>
```

`/resume`、`/projects` 和别名 `/pr` 共用最近项目入口。选择器允许继续输入筛选，使用
`↑` / `↓`、`Enter`、`Esc` 操作。切换到不同工程会载入其状态和审计记录，并开始
新的模型会话；已选中同一工程时保留当前会话。它不会恢复旧聊天逐字内容，也不会
自动重放中断或结果不明的写操作。

`/retry` 与项目恢复无关。它会移除当前对话最后一次用户/助手交换，并重新发送最后一条
真实用户消息。写操作结果不明时，先 `/review` 并检查项目日志，再提交明确的新指令。

## 项目仓库与产物

```bash
pcbdraft repository --json
pcbdraft repository /path/to/my-pcb-repository
```

工程位于仓库的 `projects/` 目录。改变仓库指针不会移动或删除旧项目。一个项目可能
包含 PCBDraft 状态、KiCad 工程/原理图/PCB、revision 绑定的检查与预览，以及明确
执行后生成的 BOM、Gerber/Drill、PnP、STEP 或 release 归档；以实际文件和收据为准。

## 常用 TUI 命令

| 命令 | 当前语义 |
| --- | --- |
| `/help` | 显示实际可用命令 |
| `/new <名称>` | 创建 PCB 项目并设为当前工程 |
| `/resume [筛选词]` | 浏览或筛选最近 PCB 项目 |
| `/projects [筛选词]`、`/pr` | 与 `/resume` 共用项目入口 |
| `/open [名称或 ID]` | 打开唯一匹配项目；无参数时打开选择器 |
| `/project [路径]` | 查看或切换项目仓库 |
| `/connect`、`/model` | 连接提供商或选择模型 |
| `/review`、`/logs` | 查看项目摘要和事件 |
| `/confirm`、`/discard` | 批准当前候选用于生成，或丢弃暂存修改 |
| `/validate`、`/release` | 运行候选检查或构建制造候选包 |
| `/goal <目标>` | 建立跨回合持续目标 |
| `/stop` | 请求停止当前工作 |
| `/retry` | 重发当前对话最后一条用户消息 |
| `/quit` | 退出 TUI |

以 `/help` 为运行时权威；开发分支和公开 `main` 的命令可能暂时不同。

## 本地 Web 工作台

```bash
pcbdraft gui
pcbdraft gui --project <项目 ID> --host 127.0.0.1 --port 9130
```

浏览器不会自动打开。默认只监听 `127.0.0.1`；改为对外监听或使用反向代理时，你需要
自行负责访问控制、TLS 和网络边界。Web 工作台不是完整 EDA 编辑器，精细编辑请用 KiCad。

## 人工审核

至少检查器件型号与 pin-to-pad、封装与极性、电源和额定值、连接器与网络、板框和
安装孔、布局布线与间距、ERC/DRC 的 revision、BOM 与制造商规则，以及制板后的装配、
上电、功能和必要测量。不要仅凭聊天文字、预览图、绿色状态或 release ZIP 下单。

自然语言任务会把对话、工具返回的工程信息，以及使用视觉功能时的板图图像发送给所选
模型提供商。费用、日志保留、训练和地域策略由提供商与账户决定。PCBDraft 不内置
模型；可以配置本地端点，但本文不承诺具体本地模型兼容性。不要上传 API Key、令牌
或不应离开本机的设计。
