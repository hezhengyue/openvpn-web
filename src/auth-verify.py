#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenVPN auth-user-pass-verify via-file 认证脚本。

server.conf 里：auth-user-pass-verify "<python> <ROOT>/auth-verify.py" via-file
OpenVPN 把「用户名/密码」写进临时文件（tmp-dir /dev/shm），第一行用户名、第二行密码。

铁律：
  * 任何异常一律 exit 1（fail-closed），绝不因脚本出错放人进来
  * 密码只走临时文件，不进环境变量、不进命令行
  * 同一「账号 + 来源公网 IP」10 分钟内失败 5 次锁定（换源端口绕不过去）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

LOCK_WINDOW = 600
LOCK_MAX = 5


def deny(username, src, reason):
    try:
        core.log_auth(username, src, "FAIL", reason)
    except Exception:
        pass
    sys.exit(1)


def main():
    if len(sys.argv) < 2:
        sys.exit(1)
    username = ""
    src = os.environ.get("untrusted_ip") or os.environ.get("trusted_ip") or "unknown"
    try:
        with open(sys.argv[1], "r", errors="replace") as f:
            lines = f.read().splitlines()
        if len(lines) < 2:
            deny("", src, "凭据文件不完整")
        username, password = lines[0].strip(), lines[1]
        # 临时凭据文件读完立刻抹掉
        try:
            with open(sys.argv[1], "w") as f:
                f.write("\n" * max(2, len(lines)))
        except Exception:
            pass

        if not core.valid_username(username):
            deny(username, src, "用户名非法")

        key = "%s|%s" % (username, src)
        con = core.db()
        con.execute("DELETE FROM auth_attempts WHERE ts < ?", (time.time() - LOCK_WINDOW,))
        fails = con.execute("SELECT COUNT(*) FROM auth_attempts WHERE k=? AND ok=0", (key,)).fetchone()[0]
        con.commit()
        if fails >= LOCK_MAX:
            con.close()
            deny(username, src, "失败次数过多，锁定 10 分钟")

        row = con.execute("SELECT enabled,password_hash FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            con.close()
            deny(username, src, "账号不存在")
        if not row["enabled"]:
            con.close()
            deny(username, src, "账号已停用")
        ok = core.verify_password(password, row["password_hash"])
        con.execute("INSERT INTO auth_attempts(k,ts,ok) VALUES(?,?,?)", (key, time.time(), 1 if ok else 0))
        if ok:
            con.execute("DELETE FROM auth_attempts WHERE k=? AND ok=0", (key,))
        con.commit()
        con.close()
        if not ok:
            deny(username, src, "密码错误")
        core.log_auth(username, src, "OK", "认证通过")
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        # 数据库打不开、文件读不了…… 一律拒绝，绝不 fail-open
        deny(username, src, "认证脚本异常: %s" % type(e).__name__)


if __name__ == "__main__":
    main()
