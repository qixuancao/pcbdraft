<#
.SYNOPSIS
为当前 Windows 用户安装并验证 PCBDraft。

.DESCRIPTION
先执行非破坏性预检，再按需安装 uv、稳定版 KiCad 10.0.x 和指定的
PCBDraft 不可变提交。-Check 只显示计划；-Yes 跳过 PCBDraft 的一次确认，
但不会绕过 WinGet 或 Chocolatey 自己的 UAC 策略。
#>
[CmdletBinding()]
param(
    [string]$Ref = $env:PCBDRAFT_INSTALL_REF,
    [switch]$NoInstallKiCad,
    [switch]$NoInstallUv,
    [switch]$Check,
    [switch]$Yes
)

$RepositoryUrl = "https://github.com/qixuancao/pcbdraft"
$ExpectedVersion = "0.1.0"
$UvVersion = "0.12.1"
$KiCadInstallVersion = "10.0.5"
$CheckActionsRequired = 10
$script:PCBDraftPhase = "preflight"
$script:PCBDraftTemporary = $null

function Write-Info([string]$Message) {
    Write-Host "PCBDraft [$script:PCBDraftPhase]: $Message"
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal `
        -ArgumentList $identity
    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Find-Uv {
    $candidates = @()
    if ($HOME) { $candidates += Join-Path $HOME ".local\bin\uv.exe" }
    if ($env:LOCALAPPDATA) {
        $candidates += Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\uv.exe"
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    $command = Get-Command uv.exe -ErrorAction SilentlyContinue
    if (-not $command) { $command = Get-Command uv -ErrorAction SilentlyContinue }
    if ($command) { return $command.Source }
    return $null
}

function Test-UvCompatibility([string]$Path) {
    if (-not $Path) { return $false }
    $output = (& $Path tool install --help 2>$null | Out-String)
    if ($LASTEXITCODE -ne 0 -or -not $output.Contains("--build-constraints")) {
        return $false
    }
    $toolBin = (& $Path tool dir --bin 2>$null | Select-Object -First 1)
    return $LASTEXITCODE -eq 0 -and [bool]$toolBin
}

function Find-KiCadCli {
    if ($env:KICAD_CLI -and (Test-Path -LiteralPath $env:KICAD_CLI -PathType Leaf)) {
        return $env:KICAD_CLI
    }
    $command = Get-Command kicad-cli.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $roots = @($env:ProgramFiles, $env:ProgramW6432, ${env:ProgramFiles(x86)}) |
        Where-Object { $_ } | Select-Object -Unique
    foreach ($root in $roots) {
        $candidate = Join-Path $root "KiCad\10.0\bin\kicad-cli.exe"
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}

function Find-PCBDraft([string]$UvPath) {
    if ($env:PCBDRAFT_CLI -and (
        Test-Path -LiteralPath $env:PCBDRAFT_CLI -PathType Leaf
    )) {
        return $env:PCBDRAFT_CLI
    }
    if ($HOME) {
        $candidate = Join-Path $HOME ".local\bin\pcbdraft.exe"
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    $command = Get-Command pcbdraft.exe -ErrorAction SilentlyContinue
    if (-not $command) {
        $command = Get-Command pcbdraft -ErrorAction SilentlyContinue
    }
    if ($command) { return $command.Source }
    if ($UvPath) {
        $toolBin = (& $UvPath tool dir --bin 2>$null | Select-Object -First 1)
        if ($LASTEXITCODE -eq 0 -and $toolBin) {
            $candidate = Join-Path ([string]$toolBin).Trim() "pcbdraft.exe"
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                return $candidate
            }
        }
    }
    return $null
}

function Get-KiCadVersion([string]$Path) {
    if (-not $Path) { return $null }
    $version = (& $Path --version 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -ne 0 -or -not $version) { return $null }
    return ([string]$version).Trim()
}

function Test-KiCadCompatibility([string]$Version) {
    if (-not $Version) { return $false }
    if ($Version -notmatch '(?<!\d)10\.0\.\d+(?!\d)') { return $false }
    return $Version -notmatch '(?i)(?:rc|alpha|beta|nightly|dev)'
}

function Find-KiCadPackageManager {
    $command = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($command) {
        return [pscustomobject]@{ Name = "WinGet"; Path = $command.Source }
    }
    $command = Get-Command choco.exe -ErrorAction SilentlyContinue
    if ($command) {
        return [pscustomobject]@{ Name = "Chocolatey"; Path = $command.Source }
    }
    return $null
}

function Test-KiCadPackageCandidate($PackageManager) {
    if ($PackageManager.Name -eq "WinGet") {
        $output = & $PackageManager.Path show --id KiCad.KiCad --exact `
            --version $KiCadInstallVersion --accept-source-agreements 2>&1
    } else {
        $output = & $PackageManager.Path search kicad --exact `
            --all-versions --limit-output 2>&1
    }
    if ($LASTEXITCODE -ne 0) {
        throw "$($PackageManager.Name) 找不到 KiCad $KiCadInstallVersion 安装候选；请检查软件源或从 https://www.kicad.org/download/windows/ 手动安装。"
    }
    $candidateFound = $output | Where-Object {
        ([string]$_).Trim() -eq "kicad|$KiCadInstallVersion"
    }
    if ($PackageManager.Name -eq "Chocolatey" -and -not $candidateFound) {
        throw "Chocolatey 软件源中没有 KiCad $KiCadInstallVersion；请检查软件源或从 https://www.kicad.org/download/windows/ 手动安装。"
    }
}

function Resolve-InstallRef([string]$RequestedRef) {
    if ($RequestedRef) {
        if ($RequestedRef -notmatch '^[0-9a-f]{40}$') {
            throw "-Ref/PCBDRAFT_INSTALL_REF 必须是完整的 40 位小写提交 SHA。"
        }
        return $RequestedRef
    }
    Write-Info "把公开 main 分支解析为不可变提交。"
    $reference = Invoke-RestMethod `
        -Uri "https://api.github.com/repos/qixuancao/pcbdraft/git/ref/heads/main"
    $resolved = [string]$reference.object.sha
    if ($resolved -notmatch '^[0-9a-f]{40}$') {
        throw "无法把公开 main 分支解析为不可变提交；可通过 -Ref 指定完整 SHA。"
    }
    return $resolved
}

function Get-PCBDraftDoctorReport([string]$Executable) {
    if (-not $Executable) { return $null }
    $json = (& $Executable doctor --json 2>$null | Out-String).Trim()
    if (-not $json) { return $null }
    try {
        return $json | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Test-DoctorHasMissingLibraryData($Doctor) {
    if (-not $Doctor -or -not $Doctor.library_data) { return $false }
    foreach ($property in $Doctor.library_data.PSObject.Properties) {
        $available = $property.Value.PSObject.Properties["available"]
        if ($available -and -not [bool]$available.Value) { return $true }
    }
    return $false
}

function Test-KiCadStockLibraryData([string]$KiCadPath) {
    if (-not $KiCadPath) { return $false }
    $binDirectory = Split-Path -Parent $KiCadPath
    $installRoot = Split-Path -Parent $binDirectory
    $dataRoot = Join-Path $installRoot "share\kicad"
    $requirements = @(
        [pscustomobject]@{
            Kind = "symbols"
            Variables = @("KICAD_SYMBOL_DIR", "KICAD10_SYMBOL_DIR")
        }
        [pscustomobject]@{
            Kind = "footprints"
            Variables = @("KICAD_FOOTPRINT_DIR", "KICAD10_FOOTPRINT_DIR")
        }
        [pscustomobject]@{
            Kind = "template"
            Variables = @("KICAD_TEMPLATE_DIR", "KICAD10_TEMPLATE_DIR")
        }
    )
    foreach ($requirement in $requirements) {
        $available = $false
        foreach ($variable in $requirement.Variables) {
            $candidate = [Environment]::GetEnvironmentVariable($variable)
            if ($candidate -and (
                Test-Path -LiteralPath $candidate -PathType Container
            )) {
                $available = $true
                break
            }
        }
        if (-not $available) {
            $candidate = Join-Path $dataRoot $requirement.Kind
            $available = Test-Path -LiteralPath $candidate -PathType Container
        }
        if (-not $available) { return $false }
    }
    return $true
}

function Get-PCBDraftInstallPlan {
    param(
        [string]$RequestedRef,
        [bool]$AllowKiCadInstall,
        [bool]$AllowUvInstall
    )

    $resolvedRef = Resolve-InstallRef $RequestedRef
    $uv = Find-Uv
    if (-not (Test-UvCompatibility $uv)) { $uv = $null }
    $pcbdraft = Find-PCBDraft $uv
    $installTarget = $pcbdraft
    if (-not $installTarget) {
        $toolBin = if ($uv) {
            (& $uv tool dir --bin 2>$null | Select-Object -First 1)
        } else {
            Join-Path $HOME ".local\bin"
        }
        $installTarget = Join-Path (([string]$toolBin).Trim()) "pcbdraft.exe"
    }
    $doctor = Get-PCBDraftDoctorReport $pcbdraft
    $needPCBDraft = $true
    $needSetup = $true
    if ($pcbdraft -and $doctor) {
        $installedVersion = (& $pcbdraft --version 2>$null | Select-Object -First 1)
        $installedVersion = if ($installedVersion) {
            ([string]$installedVersion).Trim()
        } else {
            ""
        }
        $installedCommit = [string]$doctor.runtime.commit
        if (
            $installedVersion -eq "pcbdraft $ExpectedVersion" -and
            $installedCommit -eq $resolvedRef
        ) {
            $needPCBDraft = $false
            $needSetup = -not [bool]$doctor.ok
        }
    }

    $kicad = Find-KiCadCli
    $kicadVersion = Get-KiCadVersion $kicad
    $kicadCompatible = Test-KiCadCompatibility $kicadVersion
    $missingLibraryData = $kicadCompatible -and (
        (Test-DoctorHasMissingLibraryData $doctor) -or
        -not (Test-KiCadStockLibraryData $kicad)
    )
    $needKiCad = -not $kicadCompatible -or $missingLibraryData
    $repairKiCad = $kicadCompatible -and $missingLibraryData
    $packageManager = $null
    $isAdministrator = Test-IsAdministrator
    if ($needKiCad) {
        if (-not $AllowKiCadInstall) {
            if ($repairKiCad) {
                throw "KiCad 原厂符号、封装或模板数据不完整；已指定 -NoInstallKiCad。"
            }
            $found = if ($kicadVersion) { $kicadVersion } else { "未找到" }
            throw "需要稳定版 KiCad >=10.0.0,<10.1.0，当前：$found；已指定 -NoInstallKiCad。"
        }
        $packageManager = Find-KiCadPackageManager
        if (-not $packageManager) {
            throw "KiCad 缺失或不兼容，且未找到 WinGet 或 Chocolatey；请从 https://www.kicad.org/download/windows/ 安装稳定版 10.0.x。"
        }
        if ($packageManager.Name -eq "Chocolatey" -and -not $isAdministrator) {
            throw "Chocolatey 安装 KiCad 需要已提升的 PowerShell。请以管理员身份重新打开 PowerShell，再安全地重跑同一条命令。"
        }
        $needSetup = $true
    }

    $needUv = $needPCBDraft -and -not $uv
    if ($needUv -and -not $AllowUvInstall) {
        throw "安装 PCBDraft 需要兼容的 uv；已指定 -NoInstallUv。"
    }

    return [pscustomobject]@{
        Platform = "Windows"
        IsAdministrator = $isAdministrator
        Architecture = if ($env:PROCESSOR_ARCHITECTURE) {
            $env:PROCESSOR_ARCHITECTURE
        } else {
            [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
        }
        ResolvedRef = $resolvedRef
        Uv = $uv
        KiCad = $kicad
        KiCadVersion = $kicadVersion
        PCBDraft = $pcbdraft
        InstallTarget = $installTarget
        Doctor = $doctor
        PackageManager = $packageManager
        NeedUv = $needUv
        NeedKiCad = $needKiCad
        RepairKiCad = $repairKiCad
        NeedPCBDraft = $needPCBDraft
        NeedSetup = $needSetup
    }
}

function Test-ActionsRequired($Plan) {
    return (
        $Plan.NeedUv -or $Plan.NeedKiCad -or
        $Plan.NeedPCBDraft -or $Plan.NeedSetup
    )
}

function Write-InstallPlan($Plan) {
    $uvStatus = if ($Plan.NeedUv) {
        "为当前用户安装 uv $UvVersion"
    } elseif ($Plan.Uv) {
        "复用 $($Plan.Uv)"
    } else {
        "本次不需要 uv"
    }
    $kicadStatus = if ($Plan.NeedKiCad) {
        $detected = if ($Plan.KiCadVersion) {
            "检测到 $($Plan.KiCadVersion)"
        } else {
            "未找到 kicad-cli.exe"
        }
        $action = if ($Plan.RepairKiCad) { "修复" } else { "安装或升级" }
        "$detected；通过 $($Plan.PackageManager.Name) $action 稳定版 KiCad 10.0.x"
    } else {
        "复用 $($Plan.KiCad) ($($Plan.KiCadVersion))"
    }
    $productStatus = if ($Plan.NeedPCBDraft) {
        "在 $($Plan.InstallTarget) 安装不可变提交 $($Plan.ResolvedRef)"
    } else {
        "复用 $($Plan.PCBDraft)，提交 $($Plan.ResolvedRef)"
    }
    $setupStatus = if ($Plan.NeedSetup) {
        "执行非破坏性 KiCad setup 和最终 doctor 验证"
    } else {
        "已经就绪，仅执行最终 doctor 验证"
    }
    $adminStatus = if ($Plan.NeedKiCad) {
        if ($Plan.PackageManager.Name -eq "Chocolatey") {
            "当前 PowerShell 已提升（Chocolatey 安装所必需）"
        } elseif ($Plan.IsAdministrator) {
            "当前 PowerShell 已提升；WinGet 仍遵守自身安全策略"
        } else {
            "是（由 WinGet 按自身策略请求 UAC）"
        }
    } else {
        "否"
    }

    Write-Host "PCBDraft installation plan:"
    Write-Host "  preflight:     $($Plan.Platform) / $($Plan.Architecture) / target $($Plan.ResolvedRef)"
    Write-Host "  prerequisites: $uvStatus; $kicadStatus"
    Write-Host "  pcbdraft:      $productStatus"
    Write-Host "  setup:         $setupStatus"
    Write-Host "  verify:        run pcbdraft doctor --json and require core readiness"
    Write-Host "  admin:         $adminStatus"
}

function Confirm-InstallPlan([bool]$AssumeYes) {
    if ($AssumeYes) { return }
    $answer = Read-Host "Continue with this plan? [y/N]"
    if ($answer -notmatch '^(?i:y|yes)$') {
        throw "用户取消安装。"
    }
}

function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Program 失败，退出码 $LASTEXITCODE。"
    }
}

function Install-Uv([string]$Temporary) {
    $installer = Join-Path $Temporary "uv-install.ps1"
    Write-Info "下载 Astral 官方 uv $UvVersion 安装器。"
    Invoke-WebRequest -Uri "https://astral.sh/uv/$UvVersion/install.ps1" `
        -OutFile $installer
    Unblock-File -LiteralPath $installer
    $previousNoModifyPath = $env:UV_NO_MODIFY_PATH
    try {
        $env:UV_NO_MODIFY_PATH = "1"
        & $installer
    } finally {
        $env:UV_NO_MODIFY_PATH = $previousNoModifyPath
    }
}

function Install-KiCad($Plan) {
    if ($Plan.PackageManager.Name -eq "WinGet") {
        $verb = if ($Plan.RepairKiCad) {
            "install"
        } elseif ($Plan.KiCad) {
            "upgrade"
        } else {
            "install"
        }
        Write-Info "通过 WinGet 安装 KiCad $KiCadInstallVersion。"
        $arguments = @(
            $verb, "--id", "KiCad.KiCad", "--exact",
            "--version", $KiCadInstallVersion, "--silent",
            "--accept-package-agreements", "--accept-source-agreements"
        )
        if ($Plan.RepairKiCad) { $arguments += "--force" }
        Invoke-Checked $Plan.PackageManager.Path $arguments
    } else {
        $verb = if ($Plan.KiCad) { "upgrade" } else { "install" }
        Write-Info "通过 Chocolatey 安装 KiCad $KiCadInstallVersion。"
        $arguments = @(
            $verb, "kicad", "--version=$KiCadInstallVersion",
            "--yes", "--no-progress"
        )
        if ($Plan.RepairKiCad) { $arguments += "--force" }
        Invoke-Checked $Plan.PackageManager.Path $arguments
    }
}

function Get-ConstraintFiles([string]$Temporary, [string]$ResolvedRef) {
    $constraints = Join-Path $Temporary "runtime-constraints.txt"
    $buildConstraints = Join-Path $Temporary "build-constraints.txt"
    Write-Info "获取不可变提交 $ResolvedRef 的依赖约束。"
    Invoke-WebRequest `
        -Uri "https://raw.githubusercontent.com/qixuancao/pcbdraft/$ResolvedRef/constraints/runtime.txt" `
        -OutFile $constraints
    Invoke-WebRequest `
        -Uri "https://raw.githubusercontent.com/qixuancao/pcbdraft/$ResolvedRef/constraints/build.txt" `
        -OutFile $buildConstraints
    if (-not (Select-String -LiteralPath $constraints -SimpleMatch "kicad-sch-api==")) {
        throw "运行时约束文件无效。"
    }
    if (-not (Select-String -LiteralPath $buildConstraints -SimpleMatch "setuptools==")) {
        throw "构建约束文件无效。"
    }
    return [pscustomobject]@{
        Runtime = $constraints
        Build = $buildConstraints
    }
}

function Install-PCBDraft($Plan, [string]$UvPath, [string]$Temporary) {
    $files = Get-ConstraintFiles $Temporary $Plan.ResolvedRef
    Write-Info "安装 PCBDraft $ExpectedVersion（Python 由 uv 自动管理）。"
    Invoke-Checked $UvPath @(
        "tool", "install", "--python", "3.12", "--reinstall",
        "--constraints", $files.Runtime,
        "--build-constraints", $files.Build,
        "$RepositoryUrl/archive/$($Plan.ResolvedRef).tar.gz"
    )
    $toolBin = (& $UvPath tool dir --bin | Select-Object -First 1)
    if ($LASTEXITCODE -ne 0 -or -not $toolBin) {
        throw "无法读取 uv 工具目录。"
    }
    return Join-Path (([string]$toolBin).Trim()) "pcbdraft.exe"
}

function Invoke-FinalDoctor([string]$PCBDraftPath) {
    $json = (& $PCBDraftPath doctor --json 2>&1 | Out-String).Trim()
    $exitCode = $LASTEXITCODE
    if ($json) { Write-Host $json }
    if ($exitCode -ne 0 -or -not $json) {
        throw "pcbdraft doctor 报告环境尚未就绪。"
    }
    try {
        $report = $json | ConvertFrom-Json
    } catch {
        throw "pcbdraft doctor 返回了无效的 JSON。"
    }
    if (-not [bool]$report.ok) {
        throw "pcbdraft doctor 未确认 KiCad 运行环境就绪。"
    }
    return $report
}

function Invoke-InstallPlan($Plan) {
    $uv = $Plan.Uv
    $kicad = $Plan.KiCad
    $pcbdraft = $Plan.PCBDraft

    $script:PCBDraftPhase = "prerequisites"
    if ($Plan.NeedKiCad) {
        # This is the first package-manager call. It verifies the exact candidate
        # before uv, KiCad, or PCBDraft changes are made.
        Test-KiCadPackageCandidate $Plan.PackageManager
    }
    if (Test-ActionsRequired $Plan) {
        $script:PCBDraftTemporary = Join-Path `
            ([IO.Path]::GetTempPath()) ("pcbdraft-install-" + [guid]::NewGuid())
        $null = New-Item -ItemType Directory -Path $script:PCBDraftTemporary
    }

    if ($Plan.NeedUv) {
        Install-Uv $script:PCBDraftTemporary
        $uv = Find-Uv
        if (-not (Test-UvCompatibility $uv)) {
            throw "uv 安装后仍无法找到兼容的可执行文件。"
        }
    }
    if ($Plan.NeedKiCad) {
        Install-KiCad $Plan
        $kicad = Find-KiCadCli
        $kicadVersion = Get-KiCadVersion $kicad
        if (-not (Test-KiCadCompatibility $kicadVersion)) {
            throw "KiCad 安装后仍未检测到稳定版 10.0.x；当前：$kicadVersion"
        }
    }

    $script:PCBDraftPhase = "pcbdraft"
    if ($Plan.NeedPCBDraft) {
        if (-not $uv) { throw "安装 PCBDraft 需要兼容的 uv。" }
        $pcbdraft = Install-PCBDraft $Plan $uv $script:PCBDraftTemporary
    }
    if (-not $pcbdraft -or -not (
        Test-Path -LiteralPath $pcbdraft -PathType Leaf
    )) {
        throw "安装完成但未找到 PCBDraft 命令：$pcbdraft"
    }
    $installedVersion = (& $pcbdraft --version 2>$null | Select-Object -First 1)
    if (([string]$installedVersion).Trim() -ne "pcbdraft $ExpectedVersion") {
        throw "安装后的 PCBDraft 版本与安装器不一致。"
    }

    $env:KICAD_CLI = $kicad
    $script:PCBDraftPhase = "setup"
    if ($Plan.NeedSetup) {
        Write-Info "准备 KiCad 用户环境（不会覆盖已有库表）。"
        Invoke-Checked $pcbdraft @("setup")
    }

    $script:PCBDraftPhase = "verify"
    Write-Info "运行最终环境诊断。"
    $doctor = Invoke-FinalDoctor $pcbdraft
    return [pscustomobject]@{
        Uv = $uv
        KiCad = $kicad
        KiCadVersion = Get-KiCadVersion $kicad
        PCBDraft = $pcbdraft
        Doctor = $doctor
    }
}

function Write-InstallSummary($Plan, $Result) {
    $toolBin = Split-Path -Parent $Result.PCBDraft
    Write-Info "核心运行环境已就绪。"
    Write-Host "PCBDraft executable: $($Result.PCBDraft)"
    Write-Host "PCBDraft version: $ExpectedVersion"
    Write-Host "Installed commit: $($Plan.ResolvedRef)"
    Write-Host "KiCad version: $($Result.KiCadVersion)"
    Write-Host "Core runtime: ready"
    Write-Host "Launch now: & '$($Result.PCBDraft)'"
    if ([bool]$Result.Doctor.model_available) {
        Write-Host "Model connection: ready"
    } else {
        Write-Host "Model connection: not configured (next optional step: & '$($Result.PCBDraft)' connect)"
    }
    $pathEntries = $env:PATH -split ';' |
        ForEach-Object { $_.TrimEnd('\') }
    if ($pathEntries -notcontains $toolBin.TrimEnd('\')) {
        Write-Host "Add command directory to user PATH: [Environment]::SetEnvironmentVariable('Path', '$toolBin;' + [Environment]::GetEnvironmentVariable('Path', 'User'), 'User')"
    }
}

function Remove-InstallerTemporary {
    if (-not $script:PCBDraftTemporary) { return }
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
    $candidate = [IO.Path]::GetFullPath($script:PCBDraftTemporary)
    $parent = [IO.Path]::GetDirectoryName($candidate).TrimEnd('\')
    if (
        $parent -eq $tempRoot -and
        [IO.Path]::GetFileName($candidate).StartsWith("pcbdraft-install-") -and
        (Test-Path -LiteralPath $candidate -PathType Container)
    ) {
        Remove-Item -LiteralPath $candidate -Recurse -Force
    }
    $script:PCBDraftTemporary = $null
}

function Write-InstallerFailure([string]$Message) {
    [Console]::Error.WriteLine(
        "PCBDraft installer [$script:PCBDraftPhase]: $Message"
    )
    [Console]::Error.WriteLine(
        "修复上述问题后可安全地重新运行同一条安装命令。"
    )
}

function Invoke-PCBDraftInstaller {
    [CmdletBinding()]
    param(
        [string]$Ref = $env:PCBDRAFT_INSTALL_REF,
        [switch]$NoInstallKiCad,
        [switch]$NoInstallUv,
        [switch]$Check,
        [switch]$Yes
    )

    $previousErrorAction = $ErrorActionPreference
    $previousProgress = $ProgressPreference
    $previousSecurityProtocol = [Net.ServicePointManager]::SecurityProtocol
    $script:PCBDraftPhase = "preflight"
    $script:PCBDraftTemporary = $null
    try {
        $ErrorActionPreference = "Stop"
        $ProgressPreference = "SilentlyContinue"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        if (-not $HOME -or -not [IO.Path]::IsPathRooted($HOME)) {
            throw "HOME 必须是绝对路径。"
        }
        $plan = Get-PCBDraftInstallPlan `
            -RequestedRef $Ref `
            -AllowKiCadInstall (-not $NoInstallKiCad) `
            -AllowUvInstall (-not $NoInstallUv)
        Write-InstallPlan $plan
        if ($Check) {
            if (Test-ActionsRequired $plan) {
                Write-Info "检查完成：需要执行上面的安装动作。"
                return $CheckActionsRequired
            }
            Write-Info "检查完成：核心运行环境已经就绪。"
            return 0
        }
        if (Test-ActionsRequired $plan) {
            Confirm-InstallPlan ([bool]$Yes)
        }
        $result = Invoke-InstallPlan $plan
        Write-InstallSummary $plan $result
        return 0
    } catch {
        Write-InstallerFailure $_.Exception.Message
        return 1
    } finally {
        Remove-InstallerTemporary
        $ErrorActionPreference = $previousErrorAction
        $ProgressPreference = $previousProgress
        [Net.ServicePointManager]::SecurityProtocol = $previousSecurityProtocol
    }
}

# InvocationName is "." only for dot-sourcing. File and dynamic scriptblock
# invocation (the documented one-line command) both execute main.
if ($MyInvocation.InvocationName -ne ".") {
    $installerExitCode = Invoke-PCBDraftInstaller @PSBoundParameters
    if ($installerExitCode -ne 0) {
        if ($MyInvocation.MyCommand.Path) { exit $installerExitCode }
        $global:LASTEXITCODE = $installerExitCode
        return $installerExitCode
    }
}
