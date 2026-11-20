#!/bin/bash
# 在【有网机器】上跑一次，把构建要用到的三方件全部落到 vendor/，之后构建机可全程离线。
#
#   ./vendor/fetch.sh              # 补齐 vendor/src 与 vendor/wheels，并更新 SHA256SUMS
#   VENDOR=/data/vendor ./vendor/fetch.sh   # 落到共享仓库
#
# 幂等：已经存在且能下到的文件不重复下载（getsrc 只补缺失项）。
# 源码 URL 与版本只写在 build-static-openvpn.sh 里，这里复用它，避免两处不一致。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR="${VENDOR:-$ROOT/vendor}"
mkdir -p "$VENDOR/src" "$VENDOR/wheels"

echo "==> [1/3] 取源码（OpenSSL / LZO / LZ4 / OpenVPN）→ $VENDOR/src"
FETCH_ONLY=1 VENDOR="$VENDOR" bash "$ROOT/build/build-static-openvpn.sh"

echo "==> [2/3] 取 wheel（flask / openpyxl / waitress 及其依赖）→ $VENDOR/wheels"
PY="$ROOT/runtime/bin/python3"
if [ ! -x "$PY" ]; then
    echo "    跳过: 没有可用的 $PY（构建时若 vendor/wheels 为空会去 PyPI 装）"
else
    PYV="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    ABI="cp${PYV/./}"
    echo "    目标运行时: python $PYV / $ABI"
    "$PY" -m pip download --disable-pip-version-check --only-binary=:all: \
        --platform manylinux2014_x86_64 --python-version "$PYV" --abi "$ABI" \
        -d "$VENDOR/wheels" flask==3.1.3 openpyxl==3.1.5 waitress==3.0.2
fi

echo "==> [3/3] 更新校验清单 $VENDOR/SHA256SUMS"
( cd "$VENDOR/src" && sha256sum ./*.tar.gz ) > "$VENDOR/SHA256SUMS.$$"
mv -f "$VENDOR/SHA256SUMS.$$" "$VENDOR/SHA256SUMS"
sed -i 's#\./##' "$VENDOR/SHA256SUMS" 2>/dev/null || true
cat "$VENDOR/SHA256SUMS"

echo
echo "完成。现在可以把整个仓库（含 vendor/）拷到无网构建机，用："
echo "  VENDOR_ONLY=1 ./build/build-offline.sh"
