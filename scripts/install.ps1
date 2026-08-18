# One-command PCBDraft installer for Windows 10/11.
[CmdletBinding()]
param(
    [string]$Ref = $env:PCBDRAFT_INSTALL_REF,
    [switch]$NoInstallKiCad,
    [switch]$NoInstallUv
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$RepositoryUrl = "https://github.com/qixuancao/pcbdraft"
$ExpectedVersion = "0.1.0"
$UvVersion = "0.12.1"
$KiCadInstallVersion = "10.0.5"

function Write-Info([string]$Message) {
    Write-Host "PCBDraft: $Message"
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
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

function Test-UvCompatibility([string]$Path) {
    if (-not $Path) { return $false }
    $output = (& $Path tool install --help 2>&1 | Out-String)
    return $LASTEXITCODE -eq 0 -and $output.Contains("--build-constraints")
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

function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Program failed with exit code $LASTEXITCODE"
    }
}

$temporary = Join-Path ([IO.Path]::GetTempPath()) ("pcbdraft-install-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $temporary | Out-Null
try {
    $uv = Find-Uv
    if (-not (Test-UvCompatibility $uv)) {
        if ($NoInstallUv) { throw "a compatible uv was not found" }
        $uvInstaller = Join-Path $temporary "uv-install.ps1"
        Write-Info "Installing uv $UvVersion with Astral's official installer."
        Invoke-WebRequest -Uri "https://astral.sh/uv/$UvVersion/install.ps1" `
            -OutFile $uvInstaller
        $previousNoModifyPath = $env:UV_NO_MODIFY_PATH
        try {
            $env:UV_NO_MODIFY_PATH = "1"
            & $uvInstaller
        } finally {
            $env:UV_NO_MODIFY_PATH = $previousNoModifyPath
        }
        $uv = Find-Uv
        if (-not (Test-UvCompatibility $uv)) {
            throw "uv was installed but a compatible executable was not found"
        }
    }

    $kicad = Find-KiCadCli
    if (-not $kicad) {
        if ($NoInstallKiCad) { throw "KiCad 10.0.x was not found" }
        if (Get-Command winget.exe -ErrorAction SilentlyContinue) {
            Write-Info "Installing KiCad $KiCadInstallVersion with WinGet."
            Invoke-Checked "winget.exe" @(
                "install", "--id", "KiCad.KiCad", "--exact",
                "--version", $KiCadInstallVersion, "--silent",
                "--accept-package-agreements", "--accept-source-agreements"
            )
        } elseif (Get-Command choco.exe -ErrorAction SilentlyContinue) {
            Write-Info "Installing KiCad $KiCadInstallVersion with Chocolatey."
            Invoke-Checked "choco.exe" @(
                "install", "kicad", "--version=$KiCadInstallVersion",
                "--yes", "--no-progress"
            )
        } else {
            throw "KiCad is missing and neither WinGet nor Chocolatey is available; use https://www.kicad.org/download/windows/"
        }
        $kicad = Find-KiCadCli
        if (-not $kicad) { throw "KiCad was installed but kicad-cli.exe was not found" }
    }

    $kicadVersion = (& $kicad --version | Select-Object -First 1).Trim()
    if ($LASTEXITCODE -ne 0 -or $kicadVersion -notmatch '(?<!\d)10\.0\.\d+(?!\d)') {
        throw "PCBDraft requires stable KiCad >=10.0.0,<10.1.0; found: $kicadVersion"
    }
    if ($kicadVersion -match '(?i)(?:rc|alpha|beta|nightly|dev)') {
        throw "KiCad prerelease builds are not supported: $kicadVersion"
    }
    Write-Info "Found compatible KiCad $kicadVersion."

    if ($Ref) {
        if ($Ref -notmatch '^[0-9a-f]{40}$') { throw "Ref must be a full 40-character commit SHA" }
    } else {
        Write-Info "Resolving public main to an immutable commit."
        $reference = Invoke-RestMethod `
            -Uri "https://api.github.com/repos/qixuancao/pcbdraft/git/ref/heads/main"
        $Ref = [string]$reference.object.sha
        if ($Ref -notmatch '^[0-9a-f]{40}$') { throw "GitHub returned an invalid commit SHA" }
    }

    $constraints = Join-Path $temporary "runtime-constraints.txt"
    $buildConstraints = Join-Path $temporary "build-constraints.txt"
    Invoke-WebRequest `
        -Uri "https://raw.githubusercontent.com/qixuancao/pcbdraft/$Ref/constraints/runtime.txt" `
        -OutFile $constraints
    Invoke-WebRequest `
        -Uri "https://raw.githubusercontent.com/qixuancao/pcbdraft/$Ref/constraints/build.txt" `
        -OutFile $buildConstraints
    if (-not (Select-String -LiteralPath $constraints -SimpleMatch "kicad-sch-api==")) {
        throw "runtime constraints are invalid"
    }
    if (-not (Select-String -LiteralPath $buildConstraints -SimpleMatch "setuptools==")) {
        throw "build constraints are invalid"
    }

    Write-Info "Installing PCBDraft $ExpectedVersion with uv-managed Python."
    Invoke-Checked $uv @(
        "tool", "install", "--python", "3.12", "--reinstall",
        "--constraints", $constraints,
        "--build-constraints", $buildConstraints,
        "$RepositoryUrl/archive/$Ref.tar.gz"
    )
    $toolBin = (& $uv tool dir --bin | Select-Object -First 1).Trim()
    $pcbdraft = Join-Path $toolBin "pcbdraft.exe"
    if (-not (Test-Path -LiteralPath $pcbdraft -PathType Leaf)) {
        throw "PCBDraft installed but its command was not found at $pcbdraft"
    }
    $installedVersion = (& $pcbdraft --version | Select-Object -First 1).Trim()
    if ($installedVersion -ne "pcbdraft $ExpectedVersion") {
        throw "installed PCBDraft version does not match this installer"
    }
    $env:KICAD_CLI = $kicad
    Invoke-Checked $pcbdraft @("setup")

    Write-Info "Installation complete: $pcbdraft"
    Write-Host "Installed commit: $Ref"
    Write-Host "Normal launch: pcbdraft"
    if (($env:PATH -split ';') -notcontains $toolBin) {
        Write-Host "Restart the terminal if pcbdraft is not yet on PATH: $toolBin"
    }
} finally {
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Recurse -Force
    }
}
