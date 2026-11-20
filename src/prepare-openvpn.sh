#!/bin/bash
# 确保安装目录里有可用的 openvpn 二进制（install.sh 第 7 步调用，也可以单独跑）。
#
#   ./prepare-openvpn.sh                 # 默认 /opt/openvpn-web
#   ./prepare-openvpn.sh /srv/ovpn
#
# 来源按顺序试，前面成功就不往后走：
#   1. 包内 bin/openvpn（正式分发包都有，走这一条是秒过）
#   2. 本机就地编译（vendor/src 里带源码，全程离线，约 5–15 分钟）
#   3. 都不行 → 明确失败，交给 install.sh 决定「起不起 Web」
#
# 为什么单独拎出来：install.sh 装完必须「openvpn + web 都在跑」，
# 缺二进制不能把活甩给网页向导——那时候 Web 还没起，人只能干瞪眼。
set -uo pipefail

DEST="${1:-${OVPN_WEB_ROOT:-/opt/openvpn-web}}"
BIN="$DEST/bin/openvpn"

say() { printf '%s\n' "$*"; }

ok_bin() {   # 能执行、且自报是 openvpn
    [ -x "$1" ] || return 1
    "$1" --version 2>/dev/null | head -1 | grep -qi 'openvpn'
}

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
    MISS=""
    # openvpn 的 configure 靠 pkg-config 找 lzo/libcap-ng，缺了会直接报错退出
    for t in gcc make tar pkg-config; do
        command -v "$t" >/dev/null 2>&1 || MISS="$MISS $t"
    done
    # 没有预编译静态库就得连 OpenSSL 一起编，它的 Configure 是 perl 脚本
    if [ "$HAVE_STATIC" != 1 ]; then
        command -v perl >/dev/null 2>&1 || MISS="$MISS perl"
    fi
    if [ -n "$MISS" ]; then
        say "      本机缺编译工具:$MISS，无法就地编译"
    else
        say "      包内没有预置二进制，开始就地编译 $(basename "$SRC_TGZ")"
        say "      源码与依赖都在包内，不联网；大约 5–15 分钟，期间请勿中断…"
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
say "           yum install -y gcc make perl pkg-config autoconf automake libtool"
say "        3) 手工把静态二进制放到 $BIN 并 chmod 755"
exit 1
