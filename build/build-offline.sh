#!/bin/bash
# 生成「复制过去就能用」的完整离线目录（在构建机执行一次）。
#
#   ./build/build-offline.sh                    # 运行时 + 静态 openvpn（推荐在 manylinux2014 容器里跑）
#   ./build/build-offline.sh --skip-openvpn     # 不预编二进制：目标机首次安装时就地编译（要 gcc/make/perl）
#   VENDOR_ONLY=1 ./build/build-offline.sh      # 严格离线：不用网络，缺件直接失败
#
# 依赖「本地优先」：三样东西都先看本地，缺了才联网，联网下到的会回填 vendor/，下次不用再下。
#   1. Python 运行时   仓库自带 runtime/        → vendor/python/*.tar.gz → python-build-standalone
#   2. Django 全家桶/openpyxl   vendor/wheels/*.whl → PyPI
#   3. 静态 OpenVPN    vendor/src/*.tar.gz      → 各上游镜像（见 build-static-openvpn.sh）
#
# 仓库布局（源码树是分层的，打出来的包是平铺的）：
#   src/      运行时代码 → 包根（install.sh 依赖 core.py/serve.py 与 install.sh 同级）
#   bin/      预编译静态二进制 → 包 bin/
#   runtime/  离线 Python → 包 python/
#   vendor/   构建期三方件 → 包 vendor/{static,src,wheels}
#
# 为什么必须在老系统上构建：目标最低 CentOS 7（glibc 2.17 / OpenSSL 1.0.2），
# 在 Ubuntu 26 上 pip 会拉 manylinux_2_28+ 的 wheel、解出的 openvpn 会链 libssl.so.3，
# 拷到老系统直接起不来。老 glibc 上构建的产物能在新系统跑，反过来不行。
set -euo pipefail

BUILD="$(cd "$(dirname "$0")" && pwd)"     # 本脚本所在目录 build/
ROOT="$(cd "$BUILD/.." && pwd)"            # 仓库根
SRC="$ROOT/src"                            # 运行时代码
DIST="${DIST:-$ROOT/dist}"
VENDOR="${VENDOR:-$ROOT/vendor}"
NAME="openvpn-web-x86_64"
OUT="$DIST/$NAME"
SKIP_OVPN=0
VENDOR_ONLY="${VENDOR_ONLY:-0}"
[ "${1:-}" = "--skip-openvpn" ] && SKIP_OVPN=1

# 仅在没有本地运行时、且 vendor/python 也没有时的最后兜底
PBS_VER="${PBS_VER:-20250818}"
PBS_URL="${PBS_URL:-https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_VER/cpython-3.11.13%2B$PBS_VER-x86_64-unknown-linux-gnu-install_only.tar.gz}"

echo "============================================================"
echo " 构建离线目录 -> $OUT"
echo " 本地依赖仓库: $VENDOR $([ "$VENDOR_ONLY" = "1" ] && echo '（严格离线，不联网）')"
echo "============================================================"
rm -rf "$OUT"; mkdir -p "$OUT"/{bin,data}
# 从 Windows/zip 中转过来的文件会丢可执行位，先补回来，否则会被误判成"没有二进制"而去重编
[ -d "$ROOT/bin" ] && chmod +x "$ROOT"/bin/* 2>/dev/null || true
chmod +x "$ROOT"/build/*.sh "$SRC"/*.sh 2>/dev/null || true
# 运行时代码平铺到包根：install.sh 与 core.py/serve.py 必须同级（脚本里写死了 $DEST/…）
cp -f "$SRC"/*.py "$SRC"/*.html "$SRC"/*.sh "$OUT"/ 2>/dev/null || true
cp -f "$ROOT/README.md" "$OUT"/ 2>/dev/null || true
# 目标机「上传源码自动编译」要用这个脚本，worker.py 是按 ROOT/build-static-openvpn.sh 找的
cp -f "$BUILD/build-static-openvpn.sh" "$OUT"/ 2>/dev/null || true
chmod 755 "$OUT"/*.sh
# 安装码脚本能造令牌 → 只给 root 执行，别在包里就摆成 755
[ -f "$OUT/install-code.sh" ] && chmod 700 "$OUT/install-code.sh"
# 收进仓库里已编好的东西：静态 openvpn + 静态 openssl（签证书用，最小化系统可能没有 openssl）
if [ -d "$ROOT/bin" ]; then
    for f in "$ROOT"/bin/*; do [ -f "$f" ] && cp -f "$f" "$OUT/bin/"; done
fi
chmod 755 "$OUT"/bin/* 2>/dev/null || true
# 预编译静态库：目标机上「上传源码自动编译」时只编 openvpn 本体，靠它省掉 OpenSSL 的几分钟
if [ -d "$VENDOR/static" ]; then
    mkdir -p "$OUT/vendor/static"
    cp -f "$VENDOR"/static/*.tar.gz "$OUT/vendor/static/" 2>/dev/null || true
fi
# 源码包（约 18MB）一起带上：万一包里没二进制，目标机的 install.sh 能就地编译，
# 而不是装到一半停下、让人去还没起来的网页里上传。离线安装靠的就是这一份。
if [ -d "$VENDOR/src" ]; then
    mkdir -p "$OUT/vendor/src"
    cp -f "$VENDOR"/src/*.tar.gz "$OUT/vendor/src/" 2>/dev/null || true
fi
# wheel 一起带上（不到 1MB）：装完后如果 waitress 缺了，install.sh 能就地补装，
# 不用把包搬回构建机重打一遍
if ls "$VENDOR"/wheels/*.whl >/dev/null 2>&1; then
    mkdir -p "$OUT/vendor/wheels"
    cp -f "$VENDOR"/wheels/*.whl "$OUT/vendor/wheels/" 2>/dev/null || true
fi

# 仓库里已经带了编好的静态二进制 → 直接用，不再编译（想重编: FORCE_BUILD_OPENVPN=1）
if [ -x "$ROOT/bin/openvpn" ] && [ "$SKIP_OVPN" -eq 0 ] && [ "${FORCE_BUILD_OPENVPN:-0}" != "1" ]; then
    echo "[提示] 复用仓库自带 bin/openvpn（$("$ROOT/bin/openvpn" --version 2>/dev/null | head -1)）"
    echo "       要重新编译请用 FORCE_BUILD_OPENVPN=1 ./build/build-offline.sh"
    SKIP_OVPN=1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP" 2>/dev/null || true' EXIT

# 1) Python 运行时：本地优先
echo "[1/3] 准备 Python 运行时 …"

# 经 Windows / 网盘 / zip 中转过的 python/ 会有两个毛病：
#   a) bin/python3 这类符号链接被拍成十几字节的文本文件（内容是链接目标名）
#   b) 可执行位丢失
# 这里就地修一次，避免「明明带着运行时却在目标机报 python3.12: command not found」。
prepare_runtime() {
    local d="$1" pair l t
    chmod +x "$d"/bin/* 2>/dev/null || true
    chmod -R a+rX "$d" 2>/dev/null || true
    for pair in python:python3.12 python3:python3.12 2to3:2to3-3.12 idle3:idle3.12 \
                pydoc3:pydoc3.12 python3-config:python3.12-config; do
        l="${pair%%:*}"; t="${pair##*:}"
        [ -L "$d/bin/$l" ] && continue
        [ -f "$d/bin/$t" ] || continue
        # 小于 64 字节的普通文件 = 被拍平的软链存根
        if [ "$(wc -c < "$d/bin/$l" 2>/dev/null || echo 999)" -lt 64 ]; then
            ln -sfn "$t" "$d/bin/$l" && echo "    修复软链 bin/$l -> $t"
        fi
    done
    if [ -f "$d/lib/libpython3.12.so.1.0" ] && [ ! -L "$d/lib/libpython3.12.so" ]; then
        ln -sfn libpython3.12.so.1.0 "$d/lib/libpython3.12.so" 2>/dev/null || true
    fi
}

LOCAL_PY="$ROOT/runtime/bin/python3"
VENDOR_PY="$(ls -1 "$VENDOR"/python/*install_only*.tar.gz "$VENDOR"/python/cpython-*.tar.gz 2>/dev/null | head -1 || true)"
if [ -f "$ROOT/runtime/bin/python3.12" ]; then
    echo "    复用仓库自带 runtime/ ——零下载"
    rm -rf "$OUT/python"
    cp -a "$ROOT/runtime" "$OUT/python"
    prepare_runtime "$OUT/python"
elif [ -n "$VENDOR_PY" ] && [ -s "$VENDOR_PY" ]; then
    echo "    从 vendor 解包 $(basename "$VENDOR_PY")"
    mkdir -p "$OUT/python"
    tar -xzf "$VENDOR_PY" -C "$OUT/python" --strip-components=1
    prepare_runtime "$OUT/python"
else
    if [ "$VENDOR_ONLY" = "1" ]; then
        echo "错误: 严格离线模式下没有可用运行时（既无 $LOCAL_PY，也无 $VENDOR/python/*.tar.gz）"
        exit 1
    fi
    echo "    本地没有运行时，从 python-build-standalone 下载（会缓存到 vendor/python/）"
    mkdir -p "$VENDOR/python"
    curl -fL --retry 3 --retry-delay 2 --connect-timeout 15 -o "$VENDOR/python/$(basename "${PBS_URL%%\?*}")" "$PBS_URL" || {
        echo "下载失败。两个办法："
        echo "  a) 把已有的 python/ 目录放回仓库根（本项目仓库本来就带一个，直接复用即可）"
        echo "  b) 手下一个 cpython-*-x86_64-unknown-linux-gnu-install_only.tar.gz 放进 vendor/python/"
        echo "     来源: https://github.com/astral-sh/python-build-standalone/releases"
        exit 1; }
    mkdir -p "$OUT/python"
    tar -xzf "$VENDOR"/python/*install_only*.tar.gz -C "$OUT/python" --strip-components=1 2>/dev/null || \
    tar -xzf "$VENDOR"/python/cpython-*.tar.gz -C "$OUT/python" --strip-components=1
    prepare_runtime "$OUT/python"
fi
PY="$OUT/python/bin/python3"
"$PY" -V

# 2) Flask / openpyxl：vendor/wheels 优先（--no-index 完全不碰 PyPI）
echo "[2/3] 安装离线 wheel …"
PKGS=("flask==3.1.3" "openpyxl==3.1.5" "waitress==3.0.2")
if ls "$VENDOR"/wheels/*.whl >/dev/null 2>&1; then
    echo "    使用 $VENDOR/wheels（--no-index，不联网）"
    "$PY" -m pip install --no-cache-dir --disable-pip-version-check --no-index \
          --find-links "$VENDOR/wheels" "${PKGS[@]}"
else
    if [ "$VENDOR_ONLY" = "1" ]; then
        echo "错误: 严格离线模式下缺少 $VENDOR/wheels/*.whl"
        exit 1
    fi
    echo "    没有本地 wheel，从 PyPI 装（建议之后执行 ./vendor/fetch.sh 把 wheel 固化下来）"
    "$PY" -m pip install --no-cache-dir --disable-pip-version-check --only-binary=:all: "${PKGS[@]}"
fi
"$PY" -c 'import flask, openpyxl, waitress; print("    依赖自检 OK: flask", flask.__version__ if hasattr(flask,"__version__") else "3.x", "/ openpyxl", openpyxl.__version__, "/ waitress", __import__("importlib.metadata", fromlist=["version"]).version("waitress"))'

# 3) 静态 OpenVPN 二进制
if [ "$SKIP_OVPN" -eq 0 ]; then
    echo "[3/3] 编译静态 OpenVPN（第三方库全静态，只依赖 glibc）…"
    VENDOR="$VENDOR" VENDOR_ONLY="$VENDOR_ONLY" bash "$BUILD/build-static-openvpn.sh" "$OUT/bin/openvpn"
else
    echo "[3/3] 跳过 openvpn 编译：目标机装的时候会就地编译（需要 gcc/make/perl/pkg-config）"
fi

# 4) 打包
cat > "$OUT/version.txt" <<EOF
OpenVPN Web 一体化管理系统
Web: $(grep -m1 'WEB_VERSION =' "$OUT/core_legacy.py" | tr -d '"' | awk '{print $3}')
构建时间: $(date '+%Y-%m-%d %H:%M:%S %z')
构建机: $(uname -srm) / glibc $(ldd --version 2>/dev/null | head -1 || echo unknown)
EOF
if [ -x "$OUT/bin/openvpn" ]; then
    echo "openvpn: $("$OUT/bin/openvpn" --version 2>/dev/null | head -1)" >> "$OUT/version.txt"
    echo "ldd: $(ldd "$OUT/bin/openvpn" 2>/dev/null | tr '\n' ' ')" >> "$OUT/version.txt"
    echo "最低 glibc 符号: $(objdump -T "$OUT/bin/openvpn" 2>/dev/null | grep -o 'GLIBC_[0-9.]*' | sort -Vu | tail -1)" >> "$OUT/version.txt"
    echo "兼容下限: CentOS 7 / glibc 2.17（向上兼容到当前任意发行版）" >> "$OUT/version.txt"
fi
(cd "$DIST" && tar -czf "$NAME.tar.gz" "$NAME")
echo
echo "============================================================"
echo " 完成: $DIST/$NAME.tar.gz"
echo " 用法: 拷到机房任意 x86_64 Linux → tar xzf → ./install.sh"
if [ -x "$OUT/bin/openvpn" ]; then
    echo "       （不跑 apt/yum、不跑 pip、不编译；装完 openvpn + web 都在跑）"
else
    echo " 注意: 这个包没带 openvpn 二进制，install.sh 会在目标机上就地编译，"
    echo "       目标机需要 gcc / make / perl / pkg-config；编不出来安装会明确失败。"
fi
echo "============================================================"
