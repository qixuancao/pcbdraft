#!/usr/bin/env bash
# One-command PCBDraft installer for Linux and macOS.

readonly PCBDRAFT_REPOSITORY_URL="https://github.com/qixuancao/pcbdraft"
readonly PCBDRAFT_EXPECTED_VERSION="0.1.0"
readonly PCBDRAFT_UV_VERSION="0.12.1"
readonly PCBDRAFT_CHECK_ACTIONS_REQUIRED=10

PCBDRAFT_INSTALL_TEMP=""
PCBDRAFT_INSTALL_KICAD=1
PCBDRAFT_INSTALL_UV=1
PCBDRAFT_CHECK_ONLY=0
PCBDRAFT_ASSUME_YES=0
PCBDRAFT_REQUESTED_REF=${PCBDRAFT_INSTALL_REF:-}
PCBDRAFT_PHASE="preflight"
PCBDRAFT_SYSTEM=""
PCBDRAFT_PLATFORM=""
PCBDRAFT_INSTALL_REF_RESOLVED=""
PCBDRAFT_UV_BIN=""
PCBDRAFT_KICAD_BIN=""
PCBDRAFT_KICAD_VERSION=""
PCBDRAFT_BIN=""
PCBDRAFT_DOCTOR_JSON=""
PCBDRAFT_NEED_UV=0
PCBDRAFT_NEED_KICAD=0
PCBDRAFT_NEED_PCBDRAFT=0
PCBDRAFT_NEED_SETUP=0

info() {
    printf 'PCBDraft [%s]: %s\n' "$PCBDRAFT_PHASE" "$*" >&2
}

fail() {
    printf 'PCBDraft installer [%s]: %s\n' "$PCBDRAFT_PHASE" "$*" >&2
    exit 1
}

usage() {
    printf '%s\n' \
        'Usage: install.sh [--check] [--yes] [--ref COMMIT_SHA] [--no-install-kicad] [--no-install-uv]'
    printf '%s\n' 'Installs PCBDraft for the current user and prepares stable KiCad 10.0.x.'
    printf '%s\n' '  --check              inspect and print the plan without changing the machine'
    printf '%s\n' '  --yes                approve the printed plan without an interactive prompt'
    printf '%s\n' '  --ref COMMIT_SHA     install an immutable 40-character commit'
    printf '%s\n' '  --no-install-kicad   fail instead of installing or upgrading KiCad'
    printf '%s\n' '  --no-install-uv      fail instead of installing or upgrading uv'
}

cleanup() {
    if [[ -n "$PCBDRAFT_INSTALL_TEMP" \
        && -d "$PCBDRAFT_INSTALL_TEMP" \
        && "$PCBDRAFT_INSTALL_TEMP" == "${TMPDIR:-/tmp}"/pcbdraft-install.* ]]; then
        rm -rf -- "$PCBDRAFT_INSTALL_TEMP"
    fi
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --check)
                PCBDRAFT_CHECK_ONLY=1
                shift
                ;;
            --yes)
                PCBDRAFT_ASSUME_YES=1
                shift
                ;;
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
}

detect_system() {
    if [[ "${PCBDRAFT_INSTALL_TESTING:-0}" == 1 \
        && -n "${PCBDRAFT_TEST_SYSTEM:-}" ]]; then
        printf '%s\n' "$PCBDRAFT_TEST_SYSTEM"
        return
    fi
    uname -s
}

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
    local candidate
    if [[ "${PCBDRAFT_INSTALL_TESTING:-0}" == 1 \
        && "${PCBDRAFT_TEST_KICAD_MISSING:-0}" == 1 ]]; then
        return 1
    fi
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

find_pcbdraft_bin() {
    local candidate path_candidate tool_bin
    if [[ -n "${PCBDRAFT_CLI:-}" && -x "$PCBDRAFT_CLI" ]]; then
        printf '%s\n' "$PCBDRAFT_CLI"
        return 0
    fi
    for candidate in "$HOME/.local/bin/pcbdraft" "$HOME/.cargo/bin/pcbdraft"; do
        if [[ -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    path_candidate=$(command -v pcbdraft 2>/dev/null || true)
    if [[ -n "$path_candidate" && -x "$path_candidate" ]]; then
        printf '%s\n' "$path_candidate"
        return 0
    fi
    if [[ -n "$PCBDRAFT_UV_BIN" ]]; then
        tool_bin=$("$PCBDRAFT_UV_BIN" tool dir --bin 2>/dev/null || true)
        candidate="$tool_bin/pcbdraft"
        if [[ -n "$tool_bin" && -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    fi
    return 1
}

kicad_version_is_compatible() {
    local version=$1
    [[ "$version" =~ (^|[^0-9])10\.0\.([0-9]+)([^0-9]|$) ]] \
        && [[ ! "$version" =~ ([Rr][Cc]|[Aa]lpha|[Bb]eta|[Nn]ightly|[Dd]ev) ]]
}

check_kicad_version() {
    local executable=$1 version parsed
    version=$("$executable" --version 2>/dev/null || true)
    if ! kicad_version_is_compatible "$version"; then
        fail "需要稳定版 KiCad >=10.0.0,<10.1.0，当前检测到：${version:-未知版本}"
    fi
    [[ "$version" =~ (^|[^0-9])10\.0\.([0-9]+)([^0-9]|$) ]]
    parsed="10.0.${BASH_REMATCH[2]}"
    PCBDRAFT_KICAD_VERSION=$parsed
    info "检测到兼容的 KiCad $parsed。"
}

resolve_install_ref() {
    if [[ -n "$PCBDRAFT_REQUESTED_REF" ]]; then
        [[ "$PCBDRAFT_REQUESTED_REF" =~ ^[0-9a-f]{40}$ ]] \
            || fail "--ref/PCBDRAFT_INSTALL_REF 必须是完整的 40 位小写提交 SHA。"
        printf '%s\n' "$PCBDRAFT_REQUESTED_REF"
        return
    fi
    local response resolved
    response=$(curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
        "https://api.github.com/repos/qixuancao/pcbdraft/git/ref/heads/main") \
        || fail "无法读取公开 main 分支；可通过 --ref 指定完整提交 SHA。"
    resolved=$(printf '%s\n' "$response" \
        | sed -nE 's/^[[:space:]]*"sha":[[:space:]]*"([0-9a-f]{40})".*/\1/p' \
        | head -n 1)
    [[ "$resolved" =~ ^[0-9a-f]{40}$ ]] \
        || fail "无法把公开 main 分支解析为不可变提交；可通过 --ref 指定 SHA。"
    printf '%s\n' "$resolved"
}

load_linux_release() {
    local release_file=/etc/os-release
    if [[ "${PCBDRAFT_INSTALL_TESTING:-0}" == 1 \
        && -n "${PCBDRAFT_TEST_OS_RELEASE_FILE:-}" ]]; then
        release_file=$PCBDRAFT_TEST_OS_RELEASE_FILE
    fi
    [[ -r "$release_file" ]] \
        || fail "无法识别 Linux 发行版；请先安装稳定版 KiCad 10.0.x。"
    # shellcheck disable=SC1090
    . "$release_file"
    PCBDRAFT_PLATFORM=${ID:-unknown}
}

validate_kicad_install_support() {
    if [[ "$PCBDRAFT_SYSTEM" == Darwin ]]; then
        command -v brew >/dev/null 2>&1 \
            || fail "KiCad 缺失或不兼容，且未找到 Homebrew。请先安装 Homebrew，或从 https://www.kicad.org/download/macos/ 手动安装 KiCad 10.0.x。"
        return
    fi
    load_linux_release
    command -v sudo >/dev/null 2>&1 \
        || fail "自动安装 KiCad 需要 sudo；请先手动安装稳定版 KiCad 10.0.x。"
    case "$PCBDRAFT_PLATFORM" in
        ubuntu|linuxmint|debian)
            command -v apt-get >/dev/null 2>&1 \
                || fail "当前发行版缺少 apt-get，无法自动安装 KiCad。"
            ;;
        fedora)
            command -v dnf >/dev/null 2>&1 \
                || fail "当前发行版缺少 dnf，无法自动安装 KiCad。"
            ;;
        arch)
            command -v pacman >/dev/null 2>&1 \
                || fail "当前发行版缺少 pacman，无法自动安装 KiCad。"
            ;;
        *)
            fail "尚不能自动安装 ${PRETTY_NAME:-当前发行版} 的 KiCad；请从 https://www.kicad.org/download/linux-distros/ 安装稳定版 10.0.x。"
            ;;
    esac
}

pcbdraft_doctor_json() {
    local executable=$1 output
    output=$("$executable" doctor --json 2>/dev/null || true)
    printf '%s\n' "$output"
}

pcbdraft_commit_from_json() {
    local document=$1
    printf '%s\n' "$document" \
        | sed -nE 's/.*"commit"[[:space:]]*:[[:space:]]*"([0-9a-f]{40})".*/\1/p' \
        | head -n 1
}

pcbdraft_json_is_ready() {
    local document=$1
    printf '%s\n' "$document" | grep -Eq '"ok"[[:space:]]*:[[:space:]]*true'
}

pcbdraft_json_model_is_ready() {
    local document=$1
    printf '%s\n' "$document" \
        | grep -Eq '"model_available"[[:space:]]*:[[:space:]]*true'
}

pcbdraft_json_has_missing_library_data() {
    local document=$1 library_data
    [[ "$document" == *'"library_data"'* ]] || return 1
    library_data=$(printf '%s\n' "$document" \
        | sed -nE 's/.*"library_data"[[:space:]]*:[[:space:]]*\{(.*)\},[[:space:]]*"library_tables".*/\1/p')
    [[ -n "$library_data" ]] \
        && printf '%s\n' "$library_data" \
            | grep -Eq '"available"[[:space:]]*:[[:space:]]*false'
}

kicad_data_directory_exists() {
    local kind=$1 variable value executable_parent prefix contents
    local -a variables
    case "$kind" in
        symbols) variables=(KICAD_SYMBOL_DIR KICAD10_SYMBOL_DIR) ;;
        footprints) variables=(KICAD_FOOTPRINT_DIR KICAD10_FOOTPRINT_DIR) ;;
        template) variables=(KICAD_TEMPLATE_DIR KICAD10_TEMPLATE_DIR) ;;
        *) return 1 ;;
    esac
    for variable in "${variables[@]}"; do
        value=${!variable:-}
        [[ -n "$value" && -d "$value" ]] && return 0
    done
    if [[ "$PCBDRAFT_SYSTEM" == Darwin ]]; then
        contents=${PCBDRAFT_KICAD_BIN%/MacOS/*}
        [[ "$contents" != "$PCBDRAFT_KICAD_BIN" \
            && -d "$contents/SharedSupport/$kind" ]] && return 0
        [[ -d "/Applications/KiCad/KiCad.app/Contents/SharedSupport/$kind" \
            || -d "$HOME/Applications/KiCad/KiCad.app/Contents/SharedSupport/$kind" ]]
        return
    fi
    executable_parent=$(dirname "$PCBDRAFT_KICAD_BIN")
    if [[ "$(basename "$executable_parent")" == bin ]]; then
        prefix=$(dirname "$executable_parent")
        [[ -d "$prefix/share/kicad/$kind" ]] && return 0
    fi
    [[ -d "/usr/share/kicad/$kind" || -d "/usr/local/share/kicad/$kind" ]]
}

stock_library_data_is_available() {
    local kind
    if [[ "${PCBDRAFT_INSTALL_TESTING:-0}" == 1 \
        && "${PCBDRAFT_TEST_STOCK_LIBRARIES_MISSING:-0}" != 1 ]]; then
        return 0
    fi
    for kind in symbols footprints template; do
        kicad_data_directory_exists "$kind" || return 1
    done
}

inspect_installation() {
    local installed_version installed_commit raw_kicad_version
    PCBDRAFT_INSTALL_REF_RESOLVED=$(resolve_install_ref)

    PCBDRAFT_UV_BIN=$(find_compatible_uv || true)
    PCBDRAFT_BIN=$(find_pcbdraft_bin || true)
    if [[ -n "$PCBDRAFT_BIN" ]]; then
        installed_version=$("$PCBDRAFT_BIN" --version 2>/dev/null || true)
        PCBDRAFT_DOCTOR_JSON=$(pcbdraft_doctor_json "$PCBDRAFT_BIN")
        installed_commit=$(pcbdraft_commit_from_json "$PCBDRAFT_DOCTOR_JSON")
        if [[ "$installed_version" == "pcbdraft $PCBDRAFT_EXPECTED_VERSION" \
            && "$installed_commit" == "$PCBDRAFT_INSTALL_REF_RESOLVED" ]]; then
            if ! pcbdraft_json_is_ready "$PCBDRAFT_DOCTOR_JSON"; then
                PCBDRAFT_NEED_SETUP=1
            fi
        else
            PCBDRAFT_NEED_PCBDRAFT=1
            PCBDRAFT_NEED_SETUP=1
        fi
        if pcbdraft_json_has_missing_library_data "$PCBDRAFT_DOCTOR_JSON"; then
            [[ "$PCBDRAFT_INSTALL_KICAD" == 1 ]] \
                || fail "KiCad 原厂符号、封装或模板数据不完整；已指定 --no-install-kicad。"
            PCBDRAFT_NEED_KICAD=1
            PCBDRAFT_NEED_SETUP=1
        fi
    else
        PCBDRAFT_NEED_PCBDRAFT=1
        PCBDRAFT_NEED_SETUP=1
    fi

    PCBDRAFT_KICAD_BIN=$(find_kicad_cli || true)
    if [[ -n "$PCBDRAFT_KICAD_BIN" ]]; then
        raw_kicad_version=$("$PCBDRAFT_KICAD_BIN" --version 2>/dev/null || true)
        PCBDRAFT_KICAD_VERSION=$raw_kicad_version
        if ! kicad_version_is_compatible "$raw_kicad_version"; then
            [[ "$PCBDRAFT_INSTALL_KICAD" == 1 ]] \
                || fail "检测到不兼容的 KiCad：${raw_kicad_version:-未知版本}；已指定 --no-install-kicad。"
            PCBDRAFT_NEED_KICAD=1
            PCBDRAFT_NEED_SETUP=1
        elif ! stock_library_data_is_available; then
            [[ "$PCBDRAFT_INSTALL_KICAD" == 1 ]] \
                || fail "KiCad 原厂符号、封装或模板数据不完整；已指定 --no-install-kicad。"
            PCBDRAFT_NEED_KICAD=1
            PCBDRAFT_NEED_SETUP=1
        fi
    else
        [[ "$PCBDRAFT_INSTALL_KICAD" == 1 ]] \
            || fail "未找到 KiCad 10.0.x；已指定 --no-install-kicad。"
        PCBDRAFT_NEED_KICAD=1
        PCBDRAFT_NEED_SETUP=1
    fi
    if [[ "$PCBDRAFT_NEED_KICAD" == 1 ]]; then
        validate_kicad_install_support
    fi

    if [[ "$PCBDRAFT_NEED_PCBDRAFT" == 1 && -z "$PCBDRAFT_UV_BIN" ]]; then
        [[ "$PCBDRAFT_INSTALL_UV" == 1 ]] \
            || fail "安装 PCBDraft 需要兼容的 uv；已指定 --no-install-uv。"
        PCBDRAFT_NEED_UV=1
    fi
}

print_plan() {
    local uv_status kicad_status product_status setup_status package_manager
    if [[ "$PCBDRAFT_NEED_UV" == 1 ]]; then
        uv_status="install uv $PCBDRAFT_UV_VERSION for the current user"
    elif [[ -n "$PCBDRAFT_UV_BIN" ]]; then
        uv_status="reuse $PCBDRAFT_UV_BIN"
    else
        uv_status="not needed for this run"
    fi
    if [[ "$PCBDRAFT_NEED_KICAD" == 1 ]]; then
        if [[ "$PCBDRAFT_SYSTEM" == Darwin ]]; then
            package_manager="Homebrew"
        else
            case "$PCBDRAFT_PLATFORM" in
                ubuntu|linuxmint|debian) package_manager="sudo + apt" ;;
                fedora) package_manager="sudo + dnf" ;;
                arch) package_manager="sudo + pacman" ;;
                *) package_manager="the platform package manager" ;;
            esac
        fi
        kicad_status="install or upgrade stable KiCad 10.0.x through $package_manager"
        if [[ -n "$PCBDRAFT_KICAD_VERSION" ]]; then
            kicad_status="$kicad_status (currently $PCBDRAFT_KICAD_VERSION)"
        fi
    else
        kicad_status="reuse $PCBDRAFT_KICAD_BIN (${PCBDRAFT_KICAD_VERSION:-compatible 10.0.x})"
    fi
    if [[ "$PCBDRAFT_NEED_PCBDRAFT" == 1 ]]; then
        product_status="install immutable commit $PCBDRAFT_INSTALL_REF_RESOLVED"
    else
        product_status="reuse $PCBDRAFT_BIN at commit $PCBDRAFT_INSTALL_REF_RESOLVED"
    fi
    if [[ "$PCBDRAFT_NEED_SETUP" == 1 ]]; then
        setup_status="run non-destructive KiCad setup and final doctor verification"
    else
        setup_status="already ready; run final doctor verification only"
    fi
    printf '%s\n' 'PCBDraft installation plan:'
    printf '  preflight:     %s / target %s\n' "$PCBDRAFT_SYSTEM" "$PCBDRAFT_INSTALL_REF_RESOLVED"
    printf '  prerequisites: %s; %s\n' "$uv_status" "$kicad_status"
    printf '  pcbdraft:      %s\n' "$product_status"
    printf '  setup:         %s\n' "$setup_status"
    printf '%s\n' '  verify:        run pcbdraft doctor --json and require core readiness'
}

actions_are_required() {
    [[ "$PCBDRAFT_NEED_UV" == 1 \
        || "$PCBDRAFT_NEED_KICAD" == 1 \
        || "$PCBDRAFT_NEED_PCBDRAFT" == 1 \
        || "$PCBDRAFT_NEED_SETUP" == 1 ]]
}

confirm_plan() {
    local answer
    [[ "$PCBDRAFT_ASSUME_YES" == 1 ]] && return
    printf 'Continue with this plan? [y/N] ' >&2
    if ! IFS= read -r answer; then
        fail "无法读取确认；交互式运行，或显式传入 --yes。"
    fi
    case "$answer" in
        y|Y|yes|YES|Yes) ;;
        *) fail "用户取消安装。" ;;
    esac
}

install_uv() {
    local installer="$PCBDRAFT_INSTALL_TEMP/uv-install.sh"
    info "下载 Astral 官方 uv $PCBDRAFT_UV_VERSION 安装器。"
    curl --fail --location --proto '=https' --tlsv1.2 \
        --output "$installer" \
        "https://astral.sh/uv/$PCBDRAFT_UV_VERSION/install.sh" \
        || fail "uv 安装器下载失败。"
    UV_NO_MODIFY_PATH=1 sh "$installer" || fail "uv 安装失败。"
}

install_kicad() {
    if [[ "$PCBDRAFT_SYSTEM" == Darwin ]]; then
        if [[ -n "$PCBDRAFT_KICAD_BIN" ]]; then
            info "通过 Homebrew 升级 KiCad 稳定版。"
            brew upgrade --cask kicad || fail "Homebrew 升级 KiCad 失败。"
        else
            info "通过 Homebrew 安装 KiCad 稳定版。"
            brew install --cask kicad || fail "Homebrew 安装 KiCad 失败。"
        fi
        return
    fi
    case "$PCBDRAFT_PLATFORM" in
        ubuntu|linuxmint)
            if ! command -v add-apt-repository >/dev/null 2>&1; then
                sudo apt-get update || fail "apt 软件源索引更新失败。"
                sudo apt-get install --yes software-properties-common \
                    || fail "software-properties-common 安装失败。"
            fi
            sudo add-apt-repository --yes ppa:kicad/kicad-10.0-releases \
                || fail "KiCad 10 PPA 配置失败。"
            sudo apt-get update || fail "apt 软件源索引更新失败。"
            sudo apt-get install --yes --no-install-recommends \
                kicad kicad-libraries kicad-symbols kicad-footprints kicad-templates \
                || fail "KiCad 软件包安装失败。"
            ;;
        debian)
            sudo apt-get update || fail "apt 软件源索引更新失败。"
            sudo apt-get install --yes --no-install-recommends \
                kicad kicad-libraries kicad-symbols kicad-footprints kicad-templates \
                || fail "KiCad 软件包安装失败。"
            ;;
        fedora)
            sudo dnf install --assumeyes dnf-plugins-core \
                || fail "dnf 插件安装失败。"
            sudo dnf copr enable --assumeyes @kicad/kicad-stable \
                || fail "KiCad COPR 配置失败。"
            sudo dnf install --assumeyes kicad || fail "KiCad 软件包安装失败。"
            ;;
        arch)
            sudo pacman -Syu --needed --noconfirm kicad kicad-library \
                || fail "KiCad 软件包安装失败。"
            ;;
    esac
}

download_constraints() {
    local constraints_file=$1 build_constraints_file=$2
    info "获取不可变提交 $PCBDRAFT_INSTALL_REF_RESOLVED 的依赖约束。"
    curl --fail --location --proto '=https' --tlsv1.2 \
        --output "$constraints_file" \
        "https://raw.githubusercontent.com/qixuancao/pcbdraft/$PCBDRAFT_INSTALL_REF_RESOLVED/constraints/runtime.txt" \
        || fail "运行时约束下载失败。"
    curl --fail --location --proto '=https' --tlsv1.2 \
        --output "$build_constraints_file" \
        "https://raw.githubusercontent.com/qixuancao/pcbdraft/$PCBDRAFT_INSTALL_REF_RESOLVED/constraints/build.txt" \
        || fail "构建约束下载失败。"
    grep -Fq 'kicad-sch-api==' "$constraints_file" \
        || fail "运行时约束文件无效。"
    grep -Fq 'setuptools==' "$build_constraints_file" \
        || fail "构建约束文件无效。"
}

execute_plan() {
    local constraints_file build_constraints_file tool_bin_dir final_report
    if ! actions_are_required; then
        return
    fi
    PCBDRAFT_INSTALL_TEMP=$(mktemp -d "${TMPDIR:-/tmp}/pcbdraft-install.XXXXXX") \
        || fail "无法创建安装临时目录。"
    trap cleanup EXIT INT TERM

    PCBDRAFT_PHASE="prerequisites"
    if [[ "$PCBDRAFT_NEED_UV" == 1 ]]; then
        install_uv
        PCBDRAFT_UV_BIN=$(find_compatible_uv || true)
        [[ -n "$PCBDRAFT_UV_BIN" ]] \
            || fail "uv 安装后仍无法找到兼容的可执行文件。"
    fi
    if [[ "$PCBDRAFT_NEED_KICAD" == 1 ]]; then
        install_kicad
        PCBDRAFT_KICAD_BIN=$(find_kicad_cli || true)
        [[ -n "$PCBDRAFT_KICAD_BIN" ]] \
            || fail "KiCad 安装后仍无法找到 kicad-cli。"
    fi
    check_kicad_version "$PCBDRAFT_KICAD_BIN"

    PCBDRAFT_PHASE="pcbdraft"
    if [[ "$PCBDRAFT_NEED_PCBDRAFT" == 1 ]]; then
        [[ -n "$PCBDRAFT_UV_BIN" ]] || fail "安装 PCBDraft 需要兼容的 uv。"
        constraints_file="$PCBDRAFT_INSTALL_TEMP/runtime-constraints.txt"
        build_constraints_file="$PCBDRAFT_INSTALL_TEMP/build-constraints.txt"
        download_constraints "$constraints_file" "$build_constraints_file"
        info "安装 PCBDraft $PCBDRAFT_EXPECTED_VERSION（Python 由 uv 自动管理）。"
        "$PCBDRAFT_UV_BIN" tool install \
            --python 3.12 \
            --reinstall \
            --constraints "$constraints_file" \
            --build-constraints "$build_constraints_file" \
            "$PCBDRAFT_REPOSITORY_URL/archive/$PCBDRAFT_INSTALL_REF_RESOLVED.tar.gz" \
            || fail "uv tool install 失败。"
        tool_bin_dir=$("$PCBDRAFT_UV_BIN" tool dir --bin) \
            || fail "无法读取 uv 工具目录。"
        PCBDRAFT_BIN="$tool_bin_dir/pcbdraft"
    fi
    [[ -x "$PCBDRAFT_BIN" ]] \
        || fail "安装完成但未找到命令：${PCBDRAFT_BIN:-未知路径}"
    [[ "$("$PCBDRAFT_BIN" --version 2>/dev/null || true)" == "pcbdraft $PCBDRAFT_EXPECTED_VERSION" ]] \
        || fail "安装后的版本与安装器不一致。"

    export KICAD_CLI=$PCBDRAFT_KICAD_BIN
    PCBDRAFT_PHASE="setup"
    if [[ "$PCBDRAFT_NEED_SETUP" == 1 ]]; then
        info "准备 KiCad 用户环境（不会覆盖已有库表）。"
        "$PCBDRAFT_BIN" setup || fail "pcbdraft setup 失败。"
    fi

    PCBDRAFT_PHASE="verify"
    info "运行最终环境诊断。"
    if ! final_report=$("$PCBDRAFT_BIN" doctor --json); then
        printf '%s\n' "$final_report"
        fail "pcbdraft doctor 报告环境尚未就绪。"
    fi
    printf '%s\n' "$final_report"
    pcbdraft_json_is_ready "$final_report" \
        || fail "pcbdraft doctor 未确认 KiCad 运行环境就绪。"
    PCBDRAFT_DOCTOR_JSON=$final_report
}

print_summary() {
    local tool_bin_dir
    tool_bin_dir=$(dirname "$PCBDRAFT_BIN")
    info "核心运行环境已就绪。"
    printf 'PCBDraft executable: %s\n' "$PCBDRAFT_BIN"
    printf 'PCBDraft version: %s\n' "$PCBDRAFT_EXPECTED_VERSION"
    printf 'Installed commit: %s\n' "$PCBDRAFT_INSTALL_REF_RESOLVED"
    printf 'KiCad version: %s\n' "$PCBDRAFT_KICAD_VERSION"
    printf '%s\n' 'Core runtime: ready'
    printf 'Launch now: %s\n' "$PCBDRAFT_BIN"
    if pcbdraft_json_model_is_ready "$PCBDRAFT_DOCTOR_JSON"; then
        printf '%s\n' 'Model connection: ready'
    else
        printf 'Model connection: not configured (next optional step: %s connect)\n' \
            "$PCBDRAFT_BIN"
    fi
    if [[ ":$PATH:" != *":$tool_bin_dir:"* ]]; then
        printf 'Add the command directory to PATH: export PATH="%s:$PATH"\n' "$tool_bin_dir"
    fi
}

main() {
    set -euo pipefail
    parse_args "$@"
    PCBDRAFT_SYSTEM=$(detect_system)
    [[ "$PCBDRAFT_SYSTEM" == Linux || "$PCBDRAFT_SYSTEM" == Darwin ]] \
        || fail "支持 Linux 和 macOS；Windows 请运行 scripts/install.ps1。"
    [[ "${EUID:-$(id -u)}" -ne 0 ]] \
        || fail "请以普通用户运行；只有安装 KiCad 时会单独请求管理员权限。"
    [[ -n "${HOME:-}" && "$HOME" == /* ]] || fail "HOME 必须是绝对路径。"
    command -v curl >/dev/null 2>&1 \
        || fail "需要 curl 下载经过 HTTPS 固定的安装文件。"

    inspect_installation
    print_plan
    if [[ "$PCBDRAFT_CHECK_ONLY" == 1 ]]; then
        if actions_are_required; then
            info "检查完成：需要执行上面的安装动作。"
            return "$PCBDRAFT_CHECK_ACTIONS_REQUIRED"
        fi
        PCBDRAFT_PHASE="verify"
        execute_plan
        print_summary
        return 0
    fi
    if actions_are_required; then
        confirm_plan
    fi
    execute_plan
    PCBDRAFT_PHASE="verify"
    if ! actions_are_required; then
        local final_report
        info "运行最终环境诊断。"
        if ! final_report=$("$PCBDRAFT_BIN" doctor --json); then
            printf '%s\n' "$final_report"
            fail "pcbdraft doctor 报告环境尚未就绪。"
        fi
        printf '%s\n' "$final_report"
        pcbdraft_json_is_ready "$final_report" \
            || fail "pcbdraft doctor 未确认 KiCad 运行环境就绪。"
        PCBDRAFT_DOCTOR_JSON=$final_report
    fi
    print_summary
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
