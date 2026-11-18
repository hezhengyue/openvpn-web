#!/bin/bash
# 编译「第三方库全静态、只依赖 glibc」的 OpenVPN 服务端二进制。
#
# 必须在 x86_64 / glibc 2.17 的环境里执行（CentOS 7 或 quay.io/pypa/manylinux2014_x86_64），
# 产物才能从 CentOS 7 一路跑到 Ubuntu 26。用仓库提供的 Dockerfile.build 最省事。
#
# 源码「本地优先」：
#   1. 先找 vendor/src/<包名>.tar.gz（离线可用，随仓库走）
#   2. 没有才联网下载，并且【下载后回填 vendor/src】，下次就不必再联网
#   3. 每个包都按镜像列表顺序试，官方源挂了自动换 GitHub 镜像
#   4. VENDOR_ONLY=1 时完全不联网，缺件直接失败（机房/隔离网构建用）
#
#   ./build/build-static-openvpn.sh           # -> ./bin/openvpn
#   ./build/build-static-openvpn.sh /tmp/openvpn     # 指定输出路径
#   VENDOR_ONLY=1 ./build/build-static-openvpn.sh    # 严格离线
#   FETCH_ONLY=1 ./build/build-static-openvpn.sh     # 只把源码拉进 vendor/，不编译（vendor/fetch.sh 用）
#   VENDOR=/data/vendor ./build/build-static-openvpn.sh   # 用外部 vendor 仓库（可被多项目共享）
#
# 升级用（Web 上「上传源码自动编译」走的就是这两条）：
#   OPENVPN_SRC=/tmp/openvpn-2.6.24.tar.gz ./build-static-openvpn.sh /opt/.../bin/openvpn
#   STATIC_LIBS=1 OPENVPN_SRC=... ./build-static-openvpn.sh    # 直接用 vendor/static 里预编好的 .a，只编 openvpn
set -euo pipefail

# 本脚本有两种活法：① 在仓库的 build/ 下（源码树分层）；② 被拷到安装目录根平铺（worker.py 调它）。
# 用「同级有没有 src/app.py」来区分，避免把 VENDOR 指到 /opt/vendor 这种不存在的地方去。
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$HERE/../src/app.py" ]; then
    ROOT="$(cd "$HERE/.." && pwd)"
else
    ROOT="$HERE"
fi
VENDOR="${VENDOR:-$ROOT/vendor}"
VDIR="$VENDOR/src"
SUMS="$VENDOR/SHA256SUMS"
STATIC_TGZ="${STATIC_TGZ:-$VENDOR/static/prefix.tar.gz}"

OUT="${1:-$ROOT/bin/openvpn}"
OPENVPN_VER="${OPENVPN_VER:-2.6.23}"
OPENSSL_VER="${OPENSSL_VER:-3.0.16}"
LZO_VER="${LZO_VER:-2.10}"
LZ4_VER="${LZ4_VER:-1.9.4}"
LIBCAPNG_VER="${LIBCAPNG_VER:-0.8.5}"
VENDOR_ONLY="${VENDOR_ONLY:-0}"
FETCH_ONLY="${FETCH_ONLY:-0}"
STATIC_LIBS="${STATIC_LIBS:-0}"        # 1 = 只编 openvpn，其余依赖用预编译静态库
OPENVPN_SRC="${OPENVPN_SRC:-}"         # 指定源码包（上传的 tar.gz），跳过 vendor 里的版本
MAX_GLIBC="${MAX_GLIBC:-2.17}"         # 产物允许引用的最高 glibc 符号版本

WORK="$(mktemp -d)"; PFX="$WORK/prefix"; mkdir -p "$PFX"
trap 'rm -rf "$WORK" 2>/dev/null || true' EXIT
JOBS="$(nproc 2>/dev/null || echo 4)"

# 上传的源码包：从文件名解析版本，避免和 vendor 里的版本混淆
if [ -n "$OPENVPN_SRC" ]; then
    [ -s "$OPENVPN_SRC" ] || { echo "错误: 源码包不存在 $OPENVPN_SRC"; exit 1; }
    v="$(basename "$OPENVPN_SRC" | sed -n 's/.*openvpn[-_]\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\).*/\1/p')"
    if [ -n "$v" ]; then OPENVPN_VER="$v"; fi
fi

echo "==> 编译静态 OpenVPN $OPENVPN_VER (OpenSSL $OPENSSL_VER)"
echo "    工作目录 $WORK"
echo "    源码仓库 $VDIR $([ "$VENDOR_ONLY" = "1" ] && echo '（严格离线）')"
[ "$STATIC_LIBS" = "1" ] && echo "    依赖库：复用预编译静态库 $STATIC_TGZ"

# 取源码：本地优先 → 镜像下载 → 回填 vendor → 校验 sha256 → 解包到 $WORK
# 用法: getsrc <文件名> <候选URL...>
getsrc() {
    local name="$1"; shift
    mkdir -p "$VDIR"
    if [ -s "$VDIR/$name" ]; then
        echo "    [本地] $name"
    elif [ "$VENDOR_ONLY" = "1" ]; then
        echo "错误: 严格离线模式下缺少 $VDIR/$name"
        echo "      在有网机器上执行一次 ./vendor/fetch.sh，把 vendor/ 一起拷过来。"
        exit 1
    else
        local ok=0
        for u in "$@"; do
            echo "    [下载] $u"
            if curl -fL --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 900 \
                     -o "$VDIR/$name.part" "$u"; then
                mv -f "$VDIR/$name.part" "$VDIR/$name"; ok=1; break
            fi
            rm -f "$VDIR/$name.part"
            echo "    [跳过] 该镜像不可用，换下一个"
        done
        [ "$ok" = "1" ] || { echo "错误: $name 所有镜像都下载失败"; exit 1; }
        echo "    [回填] 已缓存到 $VDIR/$name（下次离线可用）"
    fi

    # 有清单就校验；清单里没这个文件则跳过（允许临时换版本）
    if [ -f "$SUMS" ]; then
        local want got
        want="$(awk -v f="$name" '$2==f || $2=="*"f {print $1; exit}' "$SUMS")"
        if [ -n "$want" ]; then
            got="$(sha256sum "$VDIR/$name" | awk '{print $1}')"
            if [ "$want" != "$got" ]; then
                echo "错误: $name sha256 不匹配（文件被换过或下载不完整）"
                echo "      期望 $want"
                echo "      实际 $got"
                exit 1
            fi
            echo "    [校验] sha256 OK"
        fi
    fi
    if [ "$FETCH_ONLY" = "1" ]; then return 0; fi
    tar -xzf "$VDIR/$name" -C "$WORK"
}

pkgdir() {   # 取刚解包的顶层目录名（发行包与 GitHub 归档的目录名不一定一致）
    # 注意：`tar -t | head -1` 会让 tar 收到 SIGPIPE(141)，在 set -o pipefail 下会把整个脚本带崩，
    # 所以这里显式吞掉管道状态。
    local l
    l="$(tar -tzf "$1" 2>/dev/null | head -1 || true)"
    [ -n "$l" ] || l="$(basename "$1" .tar.gz)"
    printf '%s\n' "${l%%/*}"
}

# 上游只有 configure.ac 时必须先生成 configure（GitHub tag 归档就是这样）
ensure_configure() {
    local d="$1"
    [ -x "$d/configure" ] && return 0
    echo "    [autogen] $(basename "$d") 没有 configure，生成中（需要 autoconf automake libtool）"
    ( cd "$d" && if [ -x ./autogen.sh ]; then ./autogen.sh; else autoreconf -fi; fi ) >/dev/null
    [ -x "$d/configure" ] || {
        echo "错误: 仍然没有 configure。请安装 autoconf automake libtool，或改用官方发布包（自带 configure）"
        exit 1; }
}

# ── 依赖库：预编译静态库 或 现场编译
if [ "$STATIC_LIBS" = "1" ]; then
    [ "$FETCH_ONLY" = "1" ] && exit 0
    [ -s "$STATIC_TGZ" ] || {
        echo "错误: STATIC_LIBS=1 需要预编译静态库 $STATIC_TGZ"
        echo "      用 build-buildkit.sh 或 Dockerfile.build 生成后放进 vendor/static/"
        exit 1; }
    echo "==> 展开预编译静态库"
    tar -xzf "$STATIC_TGZ" -C "$PFX"
    # libtool 的 .la 里记着构建机的绝对路径，复用时会刷一屏 "library was moved" 警告；
    # 我们只靠 .pc + -L 链接，.la 没有任何用处，直接删掉。
    find "$PFX" -name '*.la' -type f -delete 2>/dev/null || true
    # .pc 里记的是构建时的绝对路径，解包后必须改成本次 $PFX，否则 pkg-config 找不到库
    find "$PFX" -name '*.pc' -type f 2>/dev/null | while read -r pc; do
        sed -i "s#^prefix=.*#prefix=$PFX#" "$pc"
    done
    for a in libssl.a libcrypto.a liblzo2.a liblz4.a libcap-ng.a; do
        [ -f "$PFX/lib/$a" ] || [ -f "$PFX/lib64/$a" ] || { echo "错误: 预编译库里缺少 $a"; exit 1; }
    done
    OPENVPN_SRC_FILE="$OPENVPN_SRC"
    if [ -z "$OPENVPN_SRC_FILE" ]; then
        getsrc "openvpn-$OPENVPN_VER.tar.gz" \
            "https://mirrors.aliyun.com/openvpn/openvpn-$OPENVPN_VER.tar.gz" \
            "https://swupdate.openvpn.org/community/releases/openvpn-$OPENVPN_VER.tar.gz" \
            "https://github.com/OpenVPN/openvpn/archive/refs/tags/v$OPENVPN_VER.tar.gz"
        OVPN_DIR="$WORK/$(pkgdir "$VDIR/openvpn-$OPENVPN_VER.tar.gz")"
    else
        echo "    [上传] $(basename "$OPENVPN_SRC_FILE")"
        mkdir -p "$WORK/upload"
        tar -xzf "$OPENVPN_SRC_FILE" -C "$WORK/upload"
        OVPN_DIR="$WORK/upload/$(pkgdir "$OPENVPN_SRC_FILE")"
    fi
    ensure_configure "$OVPN_DIR"
else
    # ── 第一阶段：把所有源码取到 vendor/src（本地优先，缺了才下载并回填）
    # 1) OpenSSL：只出 .a
    getsrc "openssl-$OPENSSL_VER.tar.gz" \
        "https://www.openssl.org/source/openssl-$OPENSSL_VER.tar.gz" \
        "https://mirrors.aliyun.com/openssl/source/openssl-$OPENSSL_VER.tar.gz" \
        "https://github.com/openssl/openssl/releases/download/openssl-$OPENSSL_VER/openssl-$OPENSSL_VER.tar.gz"

    # 2) LZO / LZ4：只出 .a
    getsrc "lzo-$LZO_VER.tar.gz" \
        "https://www.oberhumer.com/opensource/lzo/download/lzo-$LZO_VER.tar.gz" \
        "https://fossies.org/linux/misc/lzo-$LZO_VER.tar.gz"

    getsrc "lz4-$LZ4_VER.tar.gz" \
        "https://github.com/lz4/lz4/archive/refs/tags/v$LZ4_VER.tar.gz"

    # 4) libcap-ng：OpenVPN 2.6 用它降权限（capabilities），是硬依赖，不能关。
    #    发行版的 -devel 只给 .so，静态链接需要自己编一份 .a，否则产物会多一个 libcap-ng.so 动态依赖。
    getsrc "libcap-ng-$LIBCAPNG_VER.tar.gz" \
        "https://people.redhat.com/sgrubb/libcap-ng/libcap-ng-$LIBCAPNG_VER.tar.gz" \
        "https://github.com/stevegrubb/libcap-ng/archive/refs/tags/v$LIBCAPNG_VER.tar.gz"

    # 3) OpenVPN：裁掉所有插件依赖（我们只用 auth-user-pass-verify 脚本认证，一个插件都不需要）
    #    官方发布主机 swupdate.openvpn.org 时好时坏（2026-11 实测直接连不上），所以把阿里云镜像放前面：
    #    它镜像的就是官方发布包（自带 configure）；GitHub tag 归档作最后兜底，但它没有 configure，需 autoreconf。
    if [ -z "$OPENVPN_SRC" ]; then
        getsrc "openvpn-$OPENVPN_VER.tar.gz" \
            "https://mirrors.aliyun.com/openvpn/openvpn-$OPENVPN_VER.tar.gz" \
            "https://swupdate.openvpn.org/community/releases/openvpn-$OPENVPN_VER.tar.gz" \
            "https://github.com/OpenVPN/openvpn/archive/refs/tags/v$OPENVPN_VER.tar.gz"
        OVPN_DIR="$WORK/$(pkgdir "$VDIR/openvpn-$OPENVPN_VER.tar.gz")"
    else
        echo "    [上传] $(basename "$OPENVPN_SRC")"
        mkdir -p "$WORK/upload"
        tar -xzf "$OPENVPN_SRC" -C "$WORK/upload"
        OVPN_DIR="$WORK/upload/$(pkgdir "$OPENVPN_SRC")"
    fi

    if [ "$FETCH_ONLY" = "1" ]; then
        echo "==> 只取源码：$VDIR 已就绪，跳过编译"
        exit 0
    fi

    # ── 第二阶段：编译依赖
    ( cd "$WORK/openssl-$OPENSSL_VER" &&
      ./Configure linux-x86_64 no-shared no-tests no-legacy --prefix="$PFX" --openssldir="$PFX/ssl" &&
      make -j"$JOBS" && make install_sw ) >/dev/null

    ( cd "$WORK/lzo-$LZO_VER" && ./configure --prefix="$PFX" --enable-static --disable-shared &&
      make -j"$JOBS" && make install ) >/dev/null

    # LZO 2.10 默认不装 lzo2.pc，而 openvpn 的 configure 用 pkg-config 找它，缺了就报 "pkg-config is required"
    mkdir -p "$PFX/lib/pkgconfig"
    if [ ! -f "$PFX/lib/pkgconfig/lzo2.pc" ]; then
        cat > "$PFX/lib/pkgconfig/lzo2.pc" <<PC
prefix=$PFX
exec_prefix=\${prefix}
libdir=\${prefix}/lib
includedir=\${prefix}/include

Name: lzo2
Description: LZO data compression library
Version: $LZO_VER
Libs: -L\${libdir} -llzo2
Cflags: -I\${includedir}
PC
    fi

    ( cd "$WORK/lz4-$LZ4_VER" && make -j"$JOBS" lib && make install PREFIX="$PFX" BUILD_SHARED=no ) >/dev/null

    ensure_configure "$WORK/libcap-ng-$LIBCAPNG_VER"
    ( cd "$WORK/libcap-ng-$LIBCAPNG_VER" &&
      ./configure --prefix="$PFX" --disable-shared --enable-static --without-python --without-python3 &&
      make -j"$JOBS" && make install ) >/dev/null

    ensure_configure "$OVPN_DIR"
fi

# 系统 pkg-config（不是 $PFX/bin/pkg-config——那个从来不存在的路径是老脚本的坑）
PKG_CONFIG_BIN="${PKG_CONFIG_BIN:-$(command -v pkg-config || true)}"
if [ -z "$PKG_CONFIG_BIN" ]; then
    echo "错误: 缺少 pkg-config（yum install pkgconfig / apt install pkg-config）"; exit 1
fi

# ── 第三阶段：编译 OpenVPN
( cd "$OVPN_DIR" &&
  PKG_CONFIG="$PKG_CONFIG_BIN --static" \
  PKG_CONFIG_PATH="$PFX/lib/pkgconfig:$PFX/lib64/pkgconfig" \
  CPPFLAGS="-I$PFX/include" LDFLAGS="-L$PFX/lib -L$PFX/lib64 -static-libgcc -Wl,--as-needed" \
  LIBS="-lpthread -ldl" \
  ./configure --prefix="$PFX" --disable-shared --disable-plugin-auth-pam \
      --disable-plugin-down-root --disable-systemd --disable-pkcs11 \
      --disable-dco --enable-lzo --enable-lz4 --enable-iproute2=no &&
  make -j"$JOBS" ) >/dev/null

BIN="$OVPN_DIR/src/openvpn/openvpn"
[ -x "$BIN" ] || { echo "编译产物未找到"; exit 1; }
mkdir -p "$(dirname "$OUT")"
cp -f "$BIN" "$OUT"; chmod 755 "$OUT"

# ── 第四阶段：ABI 闸门
echo "==> ldd 检查:"
ldd "$OUT" || true
if ldd "$OUT" 2>/dev/null | grep -E 'libssl|libcrypto|liblz4|liblzo2|libpam|libsystemd|libcap-ng'; then
    echo "错误: 仍有第三方动态依赖，说明没有静态链进去"; exit 1
fi
if command -v objdump >/dev/null 2>&1; then
    MAXSEEN="$(objdump -T "$OUT" 2>/dev/null | grep -o 'GLIBC_[0-9.]*' | sed 's/GLIBC_//' | sort -Vu | tail -1)"
    echo "==> 需要的最低 glibc 符号版本: ${MAXSEEN:-未知}（上限 $MAX_GLIBC）"
    if [ -n "$MAXSEEN" ]; then
        HIGHEST="$(printf '%s\n%s\n' "$MAX_GLIBC" "$MAXSEEN" | sort -V | tail -1)"
        if [ "$HIGHEST" != "$MAX_GLIBC" ]; then
            echo "错误: 产物引用了 GLIBC_$MAXSEEN，高于 $MAX_GLIBC，在 CentOS 7 上会报 GLIBC_X.XX not found"
            echo "      请在 CentOS 7 / manylinux2014 环境里重新编译"
            exit 1
        fi
    fi
fi
"$OUT" --version | head -1

# ── 第五阶段：按需导出「预编译静态库 + 头文件」包
#    有了它，别人只编 openvpn 本体（STATIC_LIBS=1）就能升级版本，几分钟出结果，
#    不必每次都把 OpenSSL 重新编一遍。
PREFIX_OUT="${PREFIX_OUT:-}"
if [ -n "$PREFIX_OUT" ]; then
    echo "==> 导出预编译静态库: $PREFIX_OUT"
    find "$PFX" -name '*.la' -type f -delete 2>/dev/null || true
    mkdir -p "$(dirname "$PREFIX_OUT")"
    ( cd "$PFX" && tar -czf "$PREFIX_OUT" include lib lib64 bin/openssl 2>/dev/null \
        || tar -czf "$PREFIX_OUT" include lib bin/openssl )
    ls -lh "$PREFIX_OUT"
fi

echo "==> 完成: $OUT"
