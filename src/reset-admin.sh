#!/bin/bash
# 忘记管理后台账户名/密码时，在服务器上用 root 执行本脚本即可重新生成一组凭据。
#   bash /opt/openvpn-web/reset-admin.sh
# 新凭据会打印在屏幕上，同时写入 data/admin_password.txt（第一行密码、第二行账户名）。
# 写出后必须重启 Web 服务：systemctl restart openvpn-web
set -e

DEST="$(cd "$(dirname "$0")" && pwd)"
PY="$DEST/python/bin/python3"
USER="ovpnweb"

[ "$(id -u)" -eq 0 ] || { echo "请用 root 执行：sudo bash $0" >&2; exit 1; }
[ -x "$PY" ] || PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "找不到 Python，请在 $DEST 下手动执行 python3 app.py --reset-admin" >&2; exit 1; }

echo "== 重置 OpenVPN Web 管理凭据 =="
# app.py 会写 data/ 下的数据库与凭据文件，必须以 ovpnweb 身份跑，避免属主变成 root
if id "$USER" >/dev/null 2>&1 && command -v runuser >/dev/null 2>&1; then
    cd "$DEST" && runuser -u "$USER" -- "$PY" "$DEST/app.py" --reset-admin
elif id "$USER" >/dev/null 2>&1; then
    cd "$DEST" && su -s /bin/sh "$USER" -c "cd '$DEST' && '$PY' '$DEST/app.py' --reset-admin"
else
    cd "$DEST" && "$PY" "$DEST/app.py" --reset-admin
fi

echo
echo "== 重启 Web 服务 =="
systemctl restart openvpn-web 2>/dev/null && echo "已重启 openvpn-web" \
    || echo "未能自动重启，请手动执行：systemctl restart openvpn-web"
echo
echo "用上面打印的账户名 + 密码登录后台；旧的立刻作废。"
