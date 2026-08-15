#!/usr/bin/env bash
# Install PCBDraft for the current Linux user.  No sudo is required or used.
set -euo pipefail

readonly PCBDRAFT_REPOSITORY_URL="https://github.com/qixuancao/pcbdraft.git"
readonly PCBDRAFT_EXPECTED_VERSION="1.1.0.dev0"
readonly KICAD_SUPPORTED_VERSION="10.0.5"
PCBDRAFT_INSTALL_TEMP=""

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

cleanup() {
    if [[ -n "$PCBDRAFT_INSTALL_TEMP" \
        && -d "$PCBDRAFT_INSTALL_TEMP" \
        && "$PCBDRAFT_INSTALL_TEMP" == "${TMPDIR:-/tmp}"/pcbdraft-install.* ]]; then
        rm -rf -- "$PCBDRAFT_INSTALL_TEMP"
    fi
}
trap cleanup EXIT INT TERM

prepare_kicad_tables() {
    local version template_dir config_dir table source target header
    command -v kicad-cli >/dev/null 2>&1 \
        || fail "需要已安装的 KiCad 10（kicad-cli）。请先安装 KiCad，再重新运行本脚本。"
    version=$(kicad-cli --version 2>/dev/null || true)
    [[ "$version" == "$KICAD_SUPPORTED_VERSION" ]] \
        || fail "需要经过验收的 KiCad $KICAD_SUPPORTED_VERSION，当前检测到：${version:-未知版本}"

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
    local install_ref source_dir constraints_file build_constraints_file resolved_ref
    [[ "${EUID:-$(id -u)}" -ne 0 ]] \
        || fail "请以普通用户运行；本安装器只安装到当前用户的 Home 目录。"
    [[ "$(uname -s)" == "Linux" ]] || fail "目前只支持 Linux。"
    [[ -n "${HOME:-}" && "$HOME" == /* ]] || fail "HOME 必须是绝对路径。"
    command -v git >/dev/null 2>&1 \
        || fail "需要 Git 才能从公开 GitHub 仓库安装 PCBDraft。"
    install_ref=${PCBDRAFT_INSTALL_REF:-}
    [[ "$install_ref" =~ ^[0-9a-f]{40}$ ]] \
        || fail "PCBDRAFT_INSTALL_REF 必须是 GitHub 上完整的 40 位提交 SHA；拒绝安装可变分支或标签。"

    if ! find_uv >/dev/null; then
        fail "未找到 uv。请先通过发行版包管理器或 https://docs.astral.sh/uv/ 安装并核验 uv；本脚本不会执行远程安装脚本。"
    fi

    PCBDRAFT_INSTALL_TEMP=$(mktemp -d "${TMPDIR:-/tmp}/pcbdraft-install.XXXXXX")
    source_dir="$PCBDRAFT_INSTALL_TEMP/source"
    constraints_file="$PCBDRAFT_INSTALL_TEMP/runtime-constraints.txt"
    build_constraints_file="$PCBDRAFT_INSTALL_TEMP/build-constraints.txt"
    git init --quiet "$source_dir"
    git -C "$source_dir" remote add origin "$PCBDRAFT_REPOSITORY_URL"
    info "获取并核验不可变提交 $install_ref。"
    git -C "$source_dir" fetch --quiet --depth=1 origin "$install_ref" \
        || fail "无法从公开仓库获取指定提交；请确认 SHA 和网络连接。"
    resolved_ref=$(git -C "$source_dir" rev-parse FETCH_HEAD)
    [[ "$resolved_ref" == "$install_ref" ]] \
        || fail "远端返回的提交与请求 SHA 不一致。"
    git -C "$source_dir" show "$install_ref:constraints/runtime.txt" \
        > "$constraints_file" \
        || fail "指定提交缺少运行时约束文件。"
    git -C "$source_dir" show "$install_ref:constraints/build.txt" \
        > "$build_constraints_file" \
        || fail "指定提交缺少构建约束文件。"
    [[ -s "$constraints_file" ]] \
        && grep -Fq 'kicad-sch-api==' "$constraints_file" \
        || fail "运行时约束文件无效。"
    [[ -s "$build_constraints_file" ]] \
        && grep -Fq 'setuptools==' "$build_constraints_file" \
        || fail "构建约束文件无效。"

    # Fail before changing user files when the required KiCad runtime is absent.
    prepare_kicad_tables

    local uv_bin tool_bin_dir
    uv_bin=$(find_uv)
    tool_bin_dir=${UV_TOOL_BIN_DIR:-$HOME/.local/bin}
    export UV_TOOL_BIN_DIR="$tool_bin_dir"

    info "使用锁定依赖安装 PCBDraft $PCBDRAFT_EXPECTED_VERSION 到当前用户目录。"
    "$uv_bin" tool install \
        --reinstall \
        --constraints "$constraints_file" \
        --build-constraints "$build_constraints_file" \
        "git+$PCBDRAFT_REPOSITORY_URL@$install_ref"

    [[ -x "$tool_bin_dir/pcbdraft" ]] \
        || fail "PCBDraft 已安装，但未在预期位置找到命令：$tool_bin_dir/pcbdraft"
    [[ "$("$tool_bin_dir/pcbdraft" --version)" == "pcbdraft $PCBDRAFT_EXPECTED_VERSION" ]] \
        || fail "安装后的版本与安装器提交不一致。"
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
