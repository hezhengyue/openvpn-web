#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenVPN auth-user-pass-verify via-file 认证脚本。

server.conf 里：auth-user-pass-verify "<python> <ROOT>/system/auth-verify.py" via-file
OpenVPN 把「用户名/密码」写进临时文件（tmp-dir /dev/shm），第一行用户名、第二行密码。

铁律：
  * 任何异常一律 exit 1（fail-closed），绝不因脚本出错放人进来
  * 密码只走临时文件，不进环境变量、不进命令行
  * 同一「账号 + 来源公网 IP」10 分钟内失败 5 次锁定（换源端口绕不过去）

与 Web 的分工（这一层是 root 进程，必须零 Django 依赖）：
  * 账号台账：以 **只读 URI**（`?mode=ro`）打开同一个 db.sqlite3 读 `users` 表，
    绝不写主库——root 反向写 Web 的库会让 -wal/-shm 变成 root 所有，
    之后 Django 侧就会 `database is locked / readonly`。
  * 失败计数：独立小库 data/state/vpn-auth.sqlite3，只有 root 读写。
  * 认证流水：追加写 data/state/auth.jsonl，Web 的 /api/logs?kind=auth 直接读文本。
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from system import core  # noqa: E402

LOCK_WINDOW = 600
LOCK_MAX = 5
ATTEMPTS_DB = os.path.join(core.STATE, "vpn-auth.sqlite3")
AUTH_LOG = os.path.join(core.STATE, "auth.jsonl")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_auth(username, src, result, reason=""):
    """认证流水写 jsonl：谁都能追加，不需要锁主库。"""
    try:
        os.makedirs(core.STATE, exist_ok=True)
        line = json.dumps({"ts": _now(), "username": username or "", "src_ip": src or "",
                           "result": result or "", "reason": reason or ""},
                          ensure_ascii=False)
        with open(AUTH_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def deny(username, src, reason):
    log_auth(username, src, "FAIL", reason)
    sys.exit(1)


def _attempts_db():
    os.makedirs(core.STATE, exist_ok=True)
    con = sqlite3.connect(ATTEMPTS_DB, timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS auth_attempts (k TEXT NOT NULL, ts REAL NOT NULL, ok INTEGER NOT NULL)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_attempts ON auth_attempts(k, ts)")
    return con


def _ro_db():
    """只读打开主库：认证脚本永远不写 Django 的库。"""
    uri = "file:%s?mode=ro" % core.DB_PATH.replace("\\", "/")
    con = sqlite3.connect(uri, uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


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
        att = _attempts_db()
        try:
            att.execute("DELETE FROM auth_attempts WHERE ts < ?", (time.time() - LOCK_WINDOW,))
            fails = att.execute("SELECT COUNT(*) FROM auth_attempts WHERE k=? AND ok=0",
                                (key,)).fetchone()[0]
            att.commit()
        finally:
            att.close()
        if fails >= LOCK_MAX:
            deny(username, src, "失败次数过多，锁定 10 分钟")

        # 只读查台账：有效期已到的账号当场拒，不等 Web 去停用
        ro = _ro_db()
        try:
            row = ro.execute(
                "SELECT enabled,password_hash,expires_at FROM users WHERE username=?",
                (username,)).fetchone()
        finally:
            ro.close()
        if not row:
            _record(key, False)
            deny(username, src, "账号不存在")
        if not row["enabled"]:
            _record(key, False)
            deny(username, src, "账号已停用")
        exp = (row["expires_at"] or "").strip()
        if exp and exp[:10] < datetime.now().strftime("%Y-%m-%d"):
            _record(key, False)
            deny(username, src, "有效期已到")
        ok = core.verify_password(password, row["password_hash"])
        _record(key, ok, clear_on_ok=True)
        if not ok:
            deny(username, src, "密码错误")
        log_auth(username, src, "OK", "认证通过")
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        # 数据库打不开、文件读不了…… 一律拒绝，绝不 fail-open
        deny(username, src, "认证脚本异常: %s" % type(e).__name__)


def _record(key, ok, clear_on_ok=False):
    try:
        att = _attempts_db()
        try:
            att.execute("INSERT INTO auth_attempts(k,ts,ok) VALUES(?,?,?)",
                        (key, time.time(), 1 if ok else 0))
            if ok and clear_on_ok:
                att.execute("DELETE FROM auth_attempts WHERE k=? AND ok=0", (key,))
            att.commit()
        finally:
            att.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
