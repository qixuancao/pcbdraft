#!/usr/bin/env bash
# Install PCBDraft for the current Linux user.  No sudo is required or used.
set -euo pipefail

readonly PCBDRAFT_REPOSITORY_URL="https://github.com/qixuancao/pcbdraft.git"
readonly UV_INSTALLER_URL="https://astral.sh/uv/install.sh"

info() {
    printf 'PCBDraft: %s\n' "$*" >&2
}

fail() {
    printf 'PCBDraft installer: %s\n' "$*" >&2
    exit 1
}

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    if [[ -x "$HOME/.local/bin/uv" ]]; then
        printf '%s\n' "$HOME/.local/bin/uv"
        return 0
    fi
    if [[ -x "$HOME/.cargo/bin/uv" ]]; then
        printf '%s\n' "$HOME/.cargo/bin/uv"
        return 0
    fi
    return 1
}

install_uv() {
    local installer
    command -v curl >/dev/null 2>&1 || fail "需要 curl 来安装 uv。"
    installer=$(mktemp "${TMPDIR:-/tmp}/pcbdraft-uv-installer.XXXXXX")
    info "未找到 uv；正在把它安装到当前用户目录。"
    curl --proto '=https' --tlsv1.2 -fsSL "$UV_INSTALLER_URL" -o "$installer" \
        || { rm -f -- "$installer"; fail "无法下载 uv 安装器：$UV_INSTALLER_URL"; }
    sh "$installer" || { rm -f -- "$installer"; fail "uv 安装失败。"; }
    rm -f -- "$installer"
    find_uv || fail "uv 已安装，但未能在 ~/.local/bin、~/.cargo/bin 或 PATH 中找到它。"
}

prepare_kicad_tables() {
    local version template_dir config_dir table source target header
    command -v kicad-cli >/dev/null 2>&1 \
        || fail "需要已安装的 KiCad 10（kicad-cli）。请先安装 KiCad，再重新运行本脚本。"
    version=$(kicad-cli --version 2>/dev/null || true)
    [[ "$version" == 10.* ]] \
        || fail "需要 KiCad 10，当前检测到：${version:-未知版本}"

    template_dir=${KICAD_TEMPLATE_DIR:-/usr/share/kicad/template}
    config_dir="$HOME/.config/kicad/10.0"
    [[ ! -e "$config_dir" || -d "$config_dir" ]] \
        || fail "KiCad 配置路径不是目录：$config_dir"
    install -d -m 0700 -- "$config_dir"

    for table in sym-lib-table fp-lib-table; do
        source="$template_dir/$table"
        target="$config_dir/$table"
        case "$table" in
            sym-lib-table) header='(sym_lib_table' ;;
            fp-lib-table) header='(fp_lib_table' ;;
        esac
        [[ -s "$source" ]] && grep -Fq "$header" "$source" \
            || fail "KiCad 库模板无效或缺失：$source"
        if [[ ! -e "$target" ]]; then
            install -m 0644 -- "$source" "$target"
        elif [[ ! -f "$target" ]] || ! grep -Fq "$header" "$target"; then
            fail "现有 KiCad 库表无效，未覆盖：$target"
        fi
    done
}

main() {
    [[ "${EUID:-$(id -u)}" -ne 0 ]] \
        || fail "请以普通用户运行；本安装器只安装到当前用户的 Home 目录。"
    [[ "$(uname -s)" == "Linux" ]] || fail "目前只支持 Linux。"
    [[ -n "${HOME:-}" && "$HOME" == /* ]] || fail "HOME 必须是绝对路径。"
    command -v git >/dev/null 2>&1 \
        || fail "需要 Git 才能从已授权的 GitHub 仓库安装 PCBDraft。"

    # Fail before changing user files when the required KiCad runtime is absent.
    prepare_kicad_tables

    local uv_bin tool_bin_dir
    if ! uv_bin=$(find_uv); then
        uv_bin=$(install_uv)
    fi
    tool_bin_dir=${UV_TOOL_BIN_DIR:-$HOME/.local/bin}
    export UV_TOOL_BIN_DIR="$tool_bin_dir"

    info "使用 $uv_bin 安装 PCBDraft 到当前用户目录。"
    "$uv_bin" tool install --reinstall "git+$PCBDRAFT_REPOSITORY_URL"

    [[ -x "$tool_bin_dir/pcbdraft" ]] \
        || fail "PCBDraft 已安装，但未在预期位置找到命令：$tool_bin_dir/pcbdraft"
    "$tool_bin_dir/pcbdraft" --help >/dev/null

    info "安装完成：$tool_bin_dir/pcbdraft"
    if [[ ":$PATH:" != *":$tool_bin_dir:"* ]]; then
        printf '将以下内容加入 shell 配置后重新打开终端：\n'
        printf '  export PATH="%s:$PATH"\n' "$tool_bin_dir"
    fi
    printf '启动：pcbdraft\n'
    printf '诊断：pcbdraft doctor --json\n'
    printf '配置：%s/.config/pcbdraft/config.toml\n' "$HOME"
    printf 'PCB 项目仓库：首次启动将创建并记录 %s/PCBDraft（可用 `pcbdraft repository /路径` 更改）\n' "$HOME"
}

main "$@"
