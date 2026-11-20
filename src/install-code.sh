#!/bin/bash
# ---------------------------------------------------------------- 一次性安装码
# 只有本机 root 能执行（权限 700 root:root）。生成一个 8 位码，写进
# /etc/openvpn-web/install.token（600 root:root），Web 上传二进制 / 源码 /
# 更新包时必须带上它，worker 校验通过后立刻作废。
#
#   sudo /opt/openvpn-web/install-code.sh          # 15 分钟有效
#   sudo /opt/openvpn-web/install-code.sh 3600     # 自定义有效期（秒）
#
# 为什么需要这个：这三个动作会把外部代码装进系统，装上去下次启动就是 root。
# 面板口令一旦泄露，仅凭 Web 侧校验是挡不住的，所以必须有人在机器上按一下。
set -u

ETC="${OVPN_WEB_ETC:-/etc/openvpn-web}"
TTL="${1:-900}"
FAIL_LIMIT=5

if [ "$(id -u)" != "0" ]; then
    echo "错误: 必须以 root 执行（sudo $0）" >&2
    exit 1
fi
case "$TTL" in
    ''|*[!0-9]*) echo "错误: 有效期要写秒数，例如 900" >&2; exit 1 ;;
esac
[ "$TTL" -ge 60 ] || { echo "错误: 有效期至少 60 秒" >&2; exit 1; }

# 去掉 0/O/1/I 这类看错的字符；32 个字符取 8 位 ≈ 40 bit，撞不出来
ALPHABET="23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE=""
while [ "${#CODE}" -lt 8 ]; do
    CODE="$(head -c 64 /dev/urandom | tr -dc "$ALPHABET" | cut -c1-8)"
done
HASH="$(printf 'openvpn-web:%s' "$CODE" | sha256sum | cut -d' ' -f1)"
EXP=$(( $(date +%s) + TTL ))

install -d -m 700 -o root -g root "$ETC" 2>/dev/null || { mkdir -p "$ETC" && chmod 700 "$ETC"; }
printf '{"hash":"%s","exp":%s,"fail":0}\n' "$HASH" "$EXP" > "$ETC/install.token.tmp" || exit 1
chmod 600 "$ETC/install.token.tmp"
chown root:root "$ETC/install.token.tmp" 2>/dev/null
mv -f "$ETC/install.token.tmp" "$ETC/install.token"

# 安装脚本要用它把码印到「安装完成」里，这种情况只要那一串码
if [ "${OVPN_CODE_QUIET:-0}" = "1" ]; then
    printf '%s\n' "$CODE"
    exit 0
fi

cat <<EOF

  一次性安装码：  $CODE

  有效到： $(date -d "@$EXP" '+%Y-%m-%d %H:%M:%S')（$TTL 秒后自动失效）
  用  途： 在网页上传 openvpn 二进制 / 源码包 / Web 更新包时，填进「安装码」框
  规  则： 只能用一次；填错 $FAIL_LIMIT 次自动作废；需要再来一次就重新执行本脚本

EOF
