# 安装 PCBDraft

PCBDraft 0.1.0 是实验性 Alpha。需要 Python `>=3.11,<3.14` 和稳定版 KiCad
`>=10.0.0,<10.1.0`；当前精确验收基线为 KiCad 10.0.5。自然语言规划还需要
一个可用的模型服务。PCBDraft 不内置模型；可以配置本地端点，但本文不承诺具体本地
模型兼容性。

## 默认安装公开 main

下面的默认命令先读取公开 `main` 的精确提交 SHA，随后从该不可变提交安装。安装器
支持用 `--ref` / `-Ref` 指定其他完整 SHA，但不会自动选择当前开发分支。

Linux / macOS：

```bash
(installer="$(mktemp "${TMPDIR:-/tmp}/pcbdraft-install.XXXXXX")" && trap 'rm -f -- "$installer"' EXIT && curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 --output "$installer" https://raw.githubusercontent.com/qixuancao/pcbdraft/main/scripts/install.sh && bash "$installer")
```

Windows PowerShell：

```powershell
& ([scriptblock]::Create((Invoke-RestMethod -Uri 'https://raw.githubusercontent.com/qixuancao/pcbdraft/main/scripts/install.ps1')))
```

Linux 安装器支持 Ubuntu、Linux Mint、Debian、Fedora 和 Arch 的已知包管理器路径；
macOS 在需要安装 KiCad 时要求已有 Homebrew。Windows 优先使用已有 WinGet，找不到
时再使用已有 Chocolatey。系统 KiCad 的安装可能请求 `sudo` 或 UAC，PCBDraft 自身
安装到当前用户的 uv tool 环境。

## 只检查，不修改

Linux / macOS：

```bash
(installer="$(mktemp "${TMPDIR:-/tmp}/pcbdraft-install.XXXXXX")" && trap 'rm -f -- "$installer"' EXIT && curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 --output "$installer" https://raw.githubusercontent.com/qixuancao/pcbdraft/main/scripts/install.sh && bash "$installer" --check)
```

Windows PowerShell：

```powershell
& ([scriptblock]::Create((Invoke-RestMethod -Uri 'https://raw.githubusercontent.com/qixuancao/pcbdraft/main/scripts/install.ps1'))) -Check
```

退出码：环境已就绪为 `0`；存在支持的待执行动作时为 `10`；不受支持或不安全的状态
为普通失败码。

| Linux / macOS | Windows | 作用 |
| --- | --- | --- |
| `--check` | `-Check` | 只显示计划 |
| `--yes` | `-Yes` | 跳过 PCBDraft 确认，不绕过系统权限提示 |
| `--ref <40位SHA>` | `-Ref <40位SHA>` | 安装指定不可变提交 |
| `--no-install-kicad` | `-NoInstallKiCad` | 不安装或升级 KiCad |
| `--no-install-uv` | `-NoInstallUv` | 不安装或升级 uv |

重新运行会重新检查机器状态，并复用兼容的 uv、KiCad 和相同提交的 PCBDraft。

## 从源码运行

```bash
git clone https://github.com/qixuancao/pcbdraft.git
cd pcbdraft
uv sync --frozen --extra dev
uv run pcbdraft setup
uv run pcbdraft doctor --json
uv run pcbdraft
```

要评估尚未进入 `main` 的 TUI 项目恢复功能，请在同步依赖前明确切换分支：

```bash
git clone https://github.com/qixuancao/pcbdraft.git
cd pcbdraft
git switch refactor/native-runtime-20260905
uv sync --frozen --extra dev
uv run pcbdraft setup
uv run pcbdraft doctor --json
uv run pcbdraft
```

不要把开发分支文档或测试结果理解为一键安装的公开 `main` 已经具备。

## 第一次配置与诊断

```bash
pcbdraft connect
pcbdraft setup
pcbdraft doctor --json
pcbdraft repository --json
```

无浏览器的远程终端可使用 `pcbdraft connect --no-browser`。`setup` 初始化缺失的
KiCad 用户库表，不应覆盖有效配置。核心安装就绪与模型已经登录是两个不同条件。

安装结束会打印可执行文件绝对路径。Linux/macOS 常见位置是
`~/.local/bin/pcbdraft`；若新 shell 找不到命令，可将 `~/.local/bin` 加入 PATH。
Windows 安装结束会打印适用于当前用户 PATH 的 PowerShell 命令。

报告安装问题时，请附操作系统、`pcbdraft --version`、`kicad-cli --version` 和
`pcbdraft doctor --json` 的脱敏输出，以及安装器原始错误。不要附 API Key、令牌或
私有工程。
