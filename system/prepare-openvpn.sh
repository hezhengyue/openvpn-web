#!/bin/bash
# 确保安装目录里有可用的 openvpn 二进制（install.sh 第 7 步调用，也可以单独跑）。
#
#   ./prepare-openvpn.sh                 # 默认 /opt/openvpn-web
#   ./prepare-openvpn.sh /srv/ovpn
#   ./prepare-openvpn.sh --tools-only    # 只把编译工具补齐，不碰二进制
#
# 来源按顺序试，前面成功就不往后走：
#   1. 包内 bin/openvpn（正式分发包都有，走这一条是秒过的）
#   2. 本机就地编译（vendor/src 里带源码，全程离线，约 5–15 分钟）
#      · 缺 gcc/make/perl/pkg-config 时先试着用本机包管理器补上（离线机器会失败，不致命）
#   3. 都不行 → 明确失败，交给 install.sh 决定「起不起 Web」
#
# 为什么单独拎出来：install.sh 装完必须「openvpn + web 都在跑」，
# 缺二进制不能把活甩给网页向导——那时候 Web 还没起，人只能干瞪眼。
#
# 不想让它碰包管理器：OVPN_NO_AUTO_TOOL_INSTALL=1
set -uo pipefail

TOOLS_ONLY=0
ARGS=()
for a in "$@"; do
    case "$a" in
        --tools-only) TOOLS_ONLY=1 ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
        *) ARGS+=("$a") ;;
    esac
done
DEST="${ARGS[0]:-${OVPN_WEB_ROOT:-/opt/openvpn-web}}"
BIN="$DEST/bin/openvpn"

say() { printf '%s\n' "$*"; }

ok_bin() {   # 能执行、且自报是 openvpn
    [ -x "$1" ] || return 1
    "$1" --version 2>/dev/null | head -1 | grep -qi 'openvpn'
}

# ---------------------------------------------------------------- 编译工具
# $1 = 1 表示包内已有预编译静态库（只编 openvpn 本体，用不到 perl）
missing_tools() {
    local miss="" t
    for t in gcc make tar pkg-config; do
        command -v "$t" >/dev/null 2>&1 || miss="$miss $t"
    done
    if [ "${1:-0}" != "1" ]; then
        # OpenSSL 的 Configure 是 perl 脚本；没有预编译静态库就得连它一起编
        command -v perl >/dev/null 2>&1 || miss="$miss perl"
    fi
    printf '%s' "$miss"
}

pkg_name() {   # $1=包管理器 $2=命令名；各发行版包名不总是一样
    case "$2" in
        pkg-config) if [ "$1" = "apt-get" ]; then echo pkg-config; else echo pkgconfig; fi ;;
        *) echo "$2" ;;
    esac
}

install_tools() {   # $1 = 缺失清单（空格分隔）
    local need="$1" pm pkgs="" t left=""
    [ -n "${need// /}" ] || return 0
    if [ "${OVPN_NO_AUTO_TOOL_INSTALL:-0}" = "1" ]; then
        say "      已禁止自动装包（OVPN_NO_AUTO_TOOL_INSTALL=1）"
        return 1
    fi
    if command -v dnf >/dev/null 2>&1; then pm=dnf
    elif command -v yum >/dev/null 2>&1; then pm=yum
    elif command -v apt-get >/dev/null 2>&1; then pm=apt-get
    else
        say "      本机没有 yum/dnf/apt，没法自动补装"
        return 1
    fi
    for t in $need; do pkgs="$pkgs $(pkg_name "$pm" "$t")"; done
    say "      缺编译工具:$need —— 用 $pm 自动补装（离线机器会失败，不影响后面的判断）"
    if [ "$pm" = "apt-get" ]; then
        DEBIAN_FRONTEND=noninteractive timeout 300 apt-get install -y -q $pkgs >/dev/null 2>&1 || true
    else
        timeout 300 "$pm" install -y $pkgs >/dev/null 2>&1 || true
    fi
    for t in $need; do
        command -v "$t" >/dev/null 2>&1 || left="$left $t"
    done
    if [ -n "$left" ]; then
        say "      自动补装没成功（还缺:$left），可能没网或没配软件源"
        return 1
    fi
    say "      编译工具已补齐"
    return 0
}

# --tools-only：只补工具（install.sh --with-build-tools 用它提前备好编译能力）
if [ "$TOOLS_ONLY" = 1 ]; then
    MISS="$(missing_tools 1)"
    if [ -z "${MISS// /}" ]; then
        say "      编译工具齐备：$(gcc --version 2>/dev/null | head -1)"
        exit 0
    fi
    if install_tools "$MISS"; then
        say "      编译工具齐备：$(gcc --version 2>/dev/null | head -1)"
        exit 0
    fi
    exit 1
fi

# ---------------------------------------------------------------- 二进制
if ok_bin "$BIN"; then
    say "      已就位：$("$BIN" --version 2>/dev/null | head -1)"
    exit 0
fi

# 文件在但跑不起来：丢了可执行位、被网盘拍平、或者架构/库不匹配
if [ -f "$BIN" ]; then
    chmod 755 "$BIN" 2>/dev/null || true
    if ok_bin "$BIN"; then
        say "      已修好可执行位：$("$BIN" --version 2>/dev/null | head -1)"
        exit 0
    fi
    say "      现有 bin/openvpn 无法执行（架构或依赖库不匹配），改试就地编译"
fi

# ---------------------------------------------------------------- 就地编译
BUILD="$DEST/build-static-openvpn.sh"
SRC_TGZ="$(ls -1 "$DEST"/vendor/src/openvpn-*.tar.gz 2>/dev/null | head -1 || true)"

if [ ! -x "$BUILD" ]; then
    say "      包内没有 build-static-openvpn.sh，无法就地编译"
elif [ -z "$SRC_TGZ" ]; then
    say "      包内没有 vendor/src/openvpn-*.tar.gz 源码包，无法就地编译"
else
    HAVE_STATIC=0
    [ -s "$DEST/vendor/static/prefix.tar.gz" ] && HAVE_STATIC=1
    MISS="$(missing_tools "$HAVE_STATIC")"
    if [ -n "${MISS// /}" ]; then
        install_tools "$MISS" || true
        MISS="$(missing_tools "$HAVE_STATIC")"
    fi
    if [ -n "${MISS// /}" ]; then
        say "      本机缺编译工具:$MISS，无法就地编译"
    else
        say "      包内没有预置二进制，开始就地编译 $(basename "$SRC_TGZ")"
        if [ "$HAVE_STATIC" = 1 ]; then
            say "      只编 openvpn 本体（依赖用包内预编译静态库），一般十几秒…"
        else
            say "      要连 OpenSSL 一起编，大约 5–15 分钟，期间请勿中断…"
        fi
        mkdir -p "$DEST/bin"
        if [ "$HAVE_STATIC" = 1 ]; then
            # 有预编译静态库就只编 openvpn 本体，能省掉 OpenSSL 那几分钟
            STATIC_LIBS=1 VENDOR_ONLY=1 OPENVPN_SRC="$SRC_TGZ" \
                bash "$BUILD" "$BIN"
        else
            VENDOR_ONLY=1 OPENVPN_SRC="$SRC_TGZ" \
                bash "$BUILD" "$BIN"
        fi
        chmod 755 "$BIN" 2>/dev/null || true
        if ok_bin "$BIN"; then
            say "      编译完成：$("$BIN" --version 2>/dev/null | head -1)"
            exit 0
        fi
        say "      编译结束但没有产出可用的二进制，看上面的报错"
    fi
fi

# ---------------------------------------------------------------- 失败
say ""
say "      错误: 没能准备好 $BIN"
say "      这个目录里的 OpenVPN 起不来，Web 就算起来也是半成品，所以安装不会算成功。"
say "      三种办法，任选一种后重跑 ./install.sh："
say "        1) 用完整包：在构建机跑 ./build/build-offline.sh（不要加 --skip-openvpn）"
say "        2) 本机补编译环境后重跑："
say "           yum install -y gcc make perl pkgconfig     （Debian/Ubuntu 用 apt-get）"
say "        3) 手工把静态二进制放到 $BIN 并 chmod 755"
exit 1
