#!/usr/bin/env bash
# One-command PCBDraft installer for Linux and macOS.
set -euo pipefail

readonly PCBDRAFT_REPOSITORY_URL="https://github.com/qixuancao/pcbdraft"
readonly PCBDRAFT_EXPECTED_VERSION="1.1.0.dev0"
readonly PCBDRAFT_UV_VERSION="0.12.1"
PCBDRAFT_INSTALL_TEMP=""
PCBDRAFT_INSTALL_KICAD=1
PCBDRAFT_INSTALL_UV=1
PCBDRAFT_REQUESTED_REF=${PCBDRAFT_INSTALL_REF:-}

info() {
    printf 'PCBDraft: %s\n' "$*" >&2
}

fail() {
    printf 'PCBDraft installer: %s\n' "$*" >&2
    exit 1
}

usage() {
    printf '%s\n' 'Usage: install.sh [--ref COMMIT_SHA] [--no-install-kicad] [--no-install-uv]'
    printf '%s\n' 'Installs PCBDraft for the current user and prepares KiCad 10.0.x.'
}

cleanup() {
    if [[ -n "$PCBDRAFT_INSTALL_TEMP" \
        && -d "$PCBDRAFT_INSTALL_TEMP" \
        && "$PCBDRAFT_INSTALL_TEMP" == "${TMPDIR:-/tmp}"/pcbdraft-install.* ]]; then
        rm -rf -- "$PCBDRAFT_INSTALL_TEMP"
    fi
}
trap cleanup EXIT INT TERM

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ref)
            [[ $# -ge 2 ]] || fail "--ref requires a full commit SHA"
            PCBDRAFT_REQUESTED_REF=$2
            shift 2
            ;;
        --no-install-kicad)
            PCBDRAFT_INSTALL_KICAD=0
            shift
            ;;
        --no-install-uv)
            PCBDRAFT_INSTALL_UV=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *) fail "unknown option: $1" ;;
    esac
done

uv_is_compatible() {
    local candidate=$1 help
    help=$("$candidate" tool install --help 2>/dev/null) || return 1
    [[ "$help" == *"--build-constraints"* ]] \
        && "$candidate" tool dir --bin >/dev/null 2>&1
}

find_compatible_uv() {
    local candidate path_candidate
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [[ -x "$candidate" ]] && uv_is_compatible "$candidate"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    path_candidate=$(command -v uv 2>/dev/null || true)
    if [[ -n "$path_candidate" ]] && uv_is_compatible "$path_candidate"; then
        printf '%s\n' "$path_candidate"
        return 0
    fi
    return 1
}

find_kicad_cli() {
    if [[ -n "${KICAD_CLI:-}" && -x "$KICAD_CLI" ]]; then
        printf '%s\n' "$KICAD_CLI"
        return 0
    fi
    if command -v kicad-cli >/dev/null 2>&1; then
        command -v kicad-cli
        return 0
    fi
    for candidate in \
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli" \
        "$HOME/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli" \
        "/usr/bin/kicad-cli" \
        "/usr/local/bin/kicad-cli"; do
        if [[ -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

install_uv() {
    command -v curl >/dev/null 2>&1 \
        || fail "自动安装 uv 需要 curl；也可先自行安装 uv 后重试。"
    local installer="$PCBDRAFT_INSTALL_TEMP/uv-install.sh"
    info "下载 Astral 官方 uv $PCBDRAFT_UV_VERSION 安装器。"
    curl --fail --location --proto '=https' --tlsv1.2 \
        --output "$installer" \
        "https://astral.sh/uv/$PCBDRAFT_UV_VERSION/install.sh"
    UV_NO_MODIFY_PATH=1 sh "$installer"
}

install_kicad() {
    local system
    system=$(uname -s)
    if [[ "$system" == "Darwin" ]]; then
        command -v brew >/dev/null 2>&1 \
            || fail "未找到 Homebrew。请从 https://www.kicad.org/download/macos/ 安装 KiCad 10.0.x，或先安装 Homebrew。"
        info "通过 Homebrew 安装 KiCad 稳定版。"
        brew install --cask kicad
        return
    fi
    [[ "$system" == "Linux" ]] || fail "支持 Linux 和 macOS；Windows 请运行 scripts/install.ps1。"
    [[ -r /etc/os-release ]] || fail "无法识别 Linux 发行版；请先安装 KiCad 10.0.x。"
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}" in
        ubuntu|linuxmint)
            command -v sudo >/dev/null 2>&1 || fail "自动安装 KiCad 需要 sudo。"
            if ! command -v add-apt-repository >/dev/null 2>&1; then
                sudo apt-get update
                sudo apt-get install --yes software-properties-common
            fi
            sudo add-apt-repository --yes ppa:kicad/kicad-10.0-releases
            sudo apt-get update
            sudo apt-get install --yes --no-install-recommends kicad kicad-libraries
            ;;
        debian)
            command -v sudo >/dev/null 2>&1 || fail "自动安装 KiCad 需要 sudo。"
            sudo apt-get update
            sudo apt-get install --yes --no-install-recommends kicad kicad-libraries
            ;;
        fedora)
            command -v sudo >/dev/null 2>&1 || fail "自动安装 KiCad 需要 sudo。"
            sudo dnf install --assumeyes dnf-plugins-core
            sudo dnf copr enable --assumeyes @kicad/kicad-stable
            sudo dnf install --assumeyes kicad
            ;;
        arch)
            command -v sudo >/dev/null 2>&1 || fail "自动安装 KiCad 需要 sudo。"
            sudo pacman -Syu --needed --noconfirm kicad kicad-library
            ;;
        *)
            fail "尚不能自动安装 ${PRETTY_NAME:-当前发行版} 的 KiCad；请从 https://www.kicad.org/download/linux/ 安装稳定版 10.0.x。"
            ;;
    esac
}

resolve_install_ref() {
    if [[ -n "$PCBDRAFT_REQUESTED_REF" ]]; then
        [[ "$PCBDRAFT_REQUESTED_REF" =~ ^[0-9a-f]{40}$ ]] \
            || fail "--ref/PCBDRAFT_INSTALL_REF 必须是完整的 40 位提交 SHA。"
        printf '%s\n' "$PCBDRAFT_REQUESTED_REF"
        return
    fi
    local response resolved
    response=$(curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
        "https://api.github.com/repos/qixuancao/pcbdraft/git/ref/heads/main")
    resolved=$(printf '%s\n' "$response" \
        | sed -nE 's/^[[:space:]]*"sha":[[:space:]]*"([0-9a-f]{40})".*/\1/p' \
        | head -n 1)
    [[ "$resolved" =~ ^[0-9a-f]{40}$ ]] \
        || fail "无法把公开 main 分支解析为不可变提交；可通过 --ref 指定 SHA。"
    printf '%s\n' "$resolved"
}

check_kicad_version() {
    local executable=$1 version parsed
    version=$("$executable" --version 2>/dev/null || true)
    if [[ "$version" =~ (^|[^0-9])10\.0\.([0-9]+)([^0-9]|$) ]]; then
        parsed="10.0.${BASH_REMATCH[2]}"
    else
        fail "需要稳定版 KiCad >=10.0.0,<10.1.0，当前检测到：${version:-未知版本}"
    fi
    [[ ! "$version" =~ ([Rr][Cc]|[Aa]lpha|[Bb]eta|[Nn]ightly|[Dd]ev) ]] \
        || fail "拒绝 KiCad 预发布版：$version"
    info "检测到兼容的 KiCad $parsed。"
}

main() {
    local system install_ref uv_bin kicad_bin constraints_file build_constraints_file
    system=$(uname -s)
    [[ "$system" == "Linux" || "$system" == "Darwin" ]] \
        || fail "支持 Linux 和 macOS；Windows 请运行 scripts/install.ps1。"
    [[ "${EUID:-$(id -u)}" -ne 0 ]] \
        || fail "请以普通用户运行；只有安装 KiCad 时会单独请求管理员权限。"
    [[ -n "${HOME:-}" && "$HOME" == /* ]] || fail "HOME 必须是绝对路径。"
    command -v curl >/dev/null 2>&1 || fail "需要 curl 下载经过 HTTPS 固定的安装文件。"
    PCBDRAFT_INSTALL_TEMP=$(mktemp -d "${TMPDIR:-/tmp}/pcbdraft-install.XXXXXX")

    if ! find_compatible_uv >/dev/null; then
        [[ "$PCBDRAFT_INSTALL_UV" == 1 ]] \
            || fail "未找到支持当前安装参数的 uv。"
        install_uv
    fi
    uv_bin=$(find_compatible_uv) \
        || fail "uv 安装后仍无法找到兼容的可执行文件。"

    if ! find_kicad_cli >/dev/null; then
        [[ "$PCBDRAFT_INSTALL_KICAD" == 1 ]] || fail "未找到 KiCad 10.0.x。"
        install_kicad
    fi
    kicad_bin=$(find_kicad_cli) || fail "KiCad 安装后仍无法找到 kicad-cli。"
    check_kicad_version "$kicad_bin"

    install_ref=$(resolve_install_ref)
    constraints_file="$PCBDRAFT_INSTALL_TEMP/runtime-constraints.txt"
    build_constraints_file="$PCBDRAFT_INSTALL_TEMP/build-constraints.txt"
    info "获取不可变提交 $install_ref 的依赖约束。"
    curl --fail --location --proto '=https' --tlsv1.2 \
        --output "$constraints_file" \
        "https://raw.githubusercontent.com/qixuancao/pcbdraft/$install_ref/constraints/runtime.txt"
    curl --fail --location --proto '=https' --tlsv1.2 \
        --output "$build_constraints_file" \
        "https://raw.githubusercontent.com/qixuancao/pcbdraft/$install_ref/constraints/build.txt"
    grep -Fq 'kicad-sch-api==' "$constraints_file" || fail "运行时约束文件无效。"
    grep -Fq 'setuptools==' "$build_constraints_file" || fail "构建约束文件无效。"

    info "安装 PCBDraft $PCBDRAFT_EXPECTED_VERSION（Python 由 uv 自动管理）。"
    "$uv_bin" tool install \
        --python 3.12 \
        --reinstall \
        --constraints "$constraints_file" \
        --build-constraints "$build_constraints_file" \
        "$PCBDRAFT_REPOSITORY_URL/archive/$install_ref.tar.gz"

    local tool_bin_dir pcbdraft_bin
    tool_bin_dir=$("$uv_bin" tool dir --bin)
    pcbdraft_bin="$tool_bin_dir/pcbdraft"
    [[ -x "$pcbdraft_bin" ]] || fail "安装完成但未找到命令：$pcbdraft_bin"
    [[ "$("$pcbdraft_bin" --version)" == "pcbdraft $PCBDRAFT_EXPECTED_VERSION" ]] \
        || fail "安装后的版本与安装器不一致。"
    "$pcbdraft_bin" setup >/dev/null

    info "安装完成：$pcbdraft_bin"
    printf '已安装提交：%s\n' "$install_ref"
    if [[ ":$PATH:" != *":$tool_bin_dir:"* ]]; then
        printf '把命令目录加入 PATH：export PATH="%s:$PATH"\n' "$tool_bin_dir"
    fi
    printf '首次演示：pcbdraft demo "%s"\n' '做一块 3.3V 的温度传感器小板，带状态灯和 I2C 接口'
    printf '正常启动：pcbdraft\n'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main
fi
