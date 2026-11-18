#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenVPN Web 一体化管理系统 —— Web 入口

单进程、单线程、零构建：Flask 自带 server + SQLite + 单文件前端。
    app.run(host, port, threaded=False, use_reloader=False, debug=False)

长任务（安装/更新/重启）走独立后台线程并持全局写锁，HTTP 请求只返回 task_id，
前端轮询进度与日志；所有特权系统动作交给 worker.py（sudo 白名单）。
"""
import os
import io
import re
import sys
import json
import uuid
import time
import shutil
import threading
import functools
import subprocess
import traceback
from datetime import timedelta

from flask import (Flask, request, session, jsonify, send_file, Response,
                   send_from_directory)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))      # 代码所在目录
PORT = int(os.environ.get("OVPN_WEB_PORT", "8800"))
MOCK = os.environ.get("OVPN_WEB_MOCK") == "1"

app = Flask(__name__, static_folder=None)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=80 * 1024 * 1024,
)

WRITE_LOCK = threading.Lock()
LOGIN_FAILS = {}          # src_ip -> [(ts, ...)]，Web 登录失败锁定
TASKS_DIR = os.path.join(core.DATA, "tasks")
CTX = {"tid": ""}         # 当前长任务 id（全局写锁保证同一时刻只有一个执行者）


def _default_secret():
    p = os.path.join(core.DATA, "web.secret")
    core.ensure_dirs()
    if os.path.exists(p):
        return open(p, "rb").read()
    s = os.urandom(32)
    with open(p, "wb") as f:
        f.write(s)
    os.chmod(p, 0o600)
    return s


app.secret_key = os.environ.get("OVPN_WEB_SECRET") or _default_secret()


def bootstrap_admin():
    """首次启动：读取 install.sh 生成的初始管理密码"""
    if core.get_meta("admin_hash"):
        return
    p = os.path.join(core.DATA, "admin_password.txt")
    if os.path.exists(p):
        core.set_meta("admin_hash", core.hash_password(open(p).read().strip()))
        core.log_audit("system", "bootstrap", "初始管理密码已导入")


# --------------------------------------------------------------------------- 鉴权

def client_ip():
    """真实来源 IP。

    只有「本机反代」（remote_addr 是 127.0.0.1）才信 X-Forwarded-For：
    否则任何人随手加一个 XFF 头就能伪装成白名单里的地址，白名单等于形同虚设。
    """
    ra = (request.remote_addr or "").strip()
    if ra in ("127.0.0.1", "::1", "localhost"):
        xff = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if xff:
            return xff
    return ra


@app.before_request
def guard():
    ip = client_ip()
    # 审计上下文：让每条日志都带上「谁 + 从哪个 IP」
    core.AUDIT_CTX.update({"actor": session.get("user") or "", "ip": ip})
    # 自助改密要覆盖两类人：白名单里的，和已经拨进本机 VPN 隧道的
    is_self = (request.path == "/self" or request.path == "/api/session"
               or request.path.startswith("/api/self"))
    if not core.client_allowed(ip) and not (is_self and core.vpn_client_allowed(ip)):
        return jsonify(ok=False, allow=core.allow_list(),
                       msg="来源 IP %s 不在管理端白名单内；要放行请在服务器上执行 "
                           "python3 app.py --allow <网段>" % ip), 403
    # 首页本身就是登录页容器，未登录也要能加载
    if request.path in ("/", "/favicon.ico", "/api/login", "/self") or \
            request.path.startswith("/api/session") or request.path.startswith("/api/self"):
        return None
    if not session.get("user"):
        return jsonify(ok=False, msg="未登录", need_login=True), 401
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        token = request.headers.get("X-CSRF-Token") or (request.get_json(silent=True) or {}).get("csrf")
        if not token or token != session.get("csrf"):
            return jsonify(ok=False, msg="CSRF 校验失败，请刷新页面"), 403
    return None


def ok(data=None, **kw):
    d = {"ok": True}
    if data is not None:
        d.update(data if isinstance(data, dict) else {"data": data})
    d.update(kw)
    return jsonify(d)


def err(e, code=400):
    msg = str(e) if isinstance(e, Exception) else str(e)
    return jsonify(ok=False, msg=msg), code


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, ValueError):
        return err(e)
    traceback.print_exc()
    return jsonify(ok=False, msg="%s: %s" % (type(e).__name__, e)), 500


# --------------------------------------------------------------------------- 会话

@app.get("/api/session")
def api_session():
    if not session.get("csrf"):
        session["csrf"] = uuid.uuid4().hex
    return ok({"logged": bool(session.get("user")), "csrf": session["csrf"],
               "initialized": bool(core.get_meta("initialized")),
               "web_version": core.WEB_VERSION, "mock": MOCK,
               "admin_name": core.admin_name(), "src_ip": client_ip(),
               "allow": core.allow_list()})


@app.post("/api/login")
def api_login():
    ip = client_ip()
    fails = [t for t in LOGIN_FAILS.get(ip, []) if time.time() - t < 600]
    LOGIN_FAILS[ip] = fails
    if len(fails) >= 5:
        return jsonify(ok=False, msg="失败次数过多，10 分钟后再试"), 429
    d = request.get_json(silent=True) or {}
    if not core.admin_verify(d.get("password", "")):
        fails.append(time.time())
        return jsonify(ok=False, msg="密码错误"), 401
    LOGIN_FAILS.pop(ip, None)
    session.clear()
    session.permanent = True
    # 登录只有密码没有用户名，所以会话里的「操作者」就是可配置的操作者名称
    session["user"] = core.admin_name()
    session["csrf"] = uuid.uuid4().hex
    core.log_audit(session["user"], "login", "登录成功")
    return ok({"csrf": session["csrf"], "initialized": bool(core.get_meta("initialized")),
               "admin_name": session["user"]})


@app.post("/api/logout")
def api_logout():
    core.log_audit(session.get("user") or core.admin_name(), "logout", "退出登录")
    session.clear()
    return ok()


@app.post("/api/password")
def api_password():
    d = request.get_json(silent=True) or {}
    if not core.admin_verify(d.get("old", "")):
        raise ValueError("原密码错误")
    core.admin_set_password(d.get("new", ""))
    return ok(msg="管理密码已修改")


@app.post("/api/admin-name")
def api_admin_name():
    """改操作者名称：审计日志里的「操作者」列就显示这个名字"""
    d = request.get_json(silent=True) or {}
    name = core.set_admin_name(d.get("name", ""))
    session["user"] = name          # 立即生效，不用重新登录
    core.log_audit(name, "admin.rename", "操作者名称已修改")
    return ok({"name": name, "msg": "操作者名称已改为 " + name})


@app.get("/api/security/allow")
def api_allow_get():
    return ok({"allow": core.allow_list(), "detected": core.local_subnets(),
               "current_ip": client_ip(), "admin_name": core.admin_name(),
               "vpn_cidr": core.cfg().get("vpn_cidr", "")})


@app.post("/api/security/allow")
def api_allow_set():
    """管理端访问白名单：8800 端口只对名单内的来源开放"""
    d = request.get_json(silent=True) or {}
    items = d.get("allow")
    if not isinstance(items, list):
        items = str(items or "").replace("，", ",").split(",")
    old = core.allow_list()
    new = core.set_allow_list(items, keep_ip=client_ip())
    core.log_audit("", "security.allow", "%s -> %s" % (",".join(old), ",".join(new)))
    return ok({"allow": new, "msg": "白名单已更新（立即生效，不用重启）"})


# --------------------------------------------------------------------------- 用户自助

# 自助改密页直接嵌在 app.py 里，不依赖独立的 self.html。
# 原因：以前是单独文件，Web 热更新（只允许替换 app.py/core.py/index.html）
# 永远带不上它，改了这个页面等于改不动；而且少一个文件就少一处「版本不一致」。
SELF_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>修改 VPN 密码</title>
<style>
:root{--pri:#2d8cf0;--ok:#19be6b;--warn:#f90;--err:#ed4014;--txt:#303133;--muted:#8a94a6;--line:#e4e8ee}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
color:var(--txt);background:linear-gradient(135deg,#1f2d3d,#2d8cf0);display:flex;align-items:center;justify-content:center;padding:20px}
.box{background:#fff;border-radius:10px;box-shadow:0 12px 40px rgba(0,0,0,.25);width:400px;max-width:100%;padding:28px 26px}
h1{font-size:18px;margin:0 0 4px}
.sub{color:var(--muted);font-size:12px;margin:0 0 20px}
label{display:block;font-size:13px;margin-bottom:14px}
label span{display:block;color:#606266;margin-bottom:5px}
input{width:100%;border:1px solid var(--line);border-radius:5px;padding:9px 11px;font-size:14px;font-family:inherit;outline:none;background:#fff}
input:focus{border-color:var(--pri);box-shadow:0 0 0 2px rgba(45,140,240,.12)}
button{width:100%;border:0;border-radius:5px;background:var(--pri);color:#fff;padding:10px;font-size:15px;cursor:pointer;font-family:inherit}
button:hover{opacity:.9}button:disabled{opacity:.55;cursor:not-allowed}
.msg{margin-top:14px;padding:9px 12px;border-radius:5px;font-size:13px;display:none}
.msg.ok{display:block;background:#e8f8f0;color:#0f9b56;border-left:3px solid var(--ok)}
.msg.err{display:block;background:#fdf0ee;color:#c92a0c;border-left:3px solid var(--err)}
.who{background:#eef6ff;border-left:3px solid var(--pri);border-radius:5px;padding:10px 12px;font-size:13px;margin-bottom:16px;line-height:1.8}
.who b{color:#1d63b8}
.who.bad{background:#fdf0ee;border-left-color:var(--err);color:#a02c14}
.who.bad b{color:#c92a0c}
.tip{margin-top:18px;padding-top:14px;border-top:1px solid var(--line);color:var(--muted);font-size:12px;line-height:1.7}
.tip b{color:#606266;font-weight:600}
</style>
</head>
<body>
<form class="box" id="f" autocomplete="off">
  <h1>修改 VPN 密码</h1>
  <p class="sub">连上 VPN 后打开本页，系统会自动认出你，只需填密码。</p>
  <div class="who" id="who">正在识别你的账号…</div>
  <label><span>当前密码</span><input id="old" type="password" autocomplete="current-password" disabled></label>
  <label><span>新密码（至少 8 位）</span><input id="new" type="password" autocomplete="new-password" disabled></label>
  <label><span>再输一次新密码</span><input id="new2" type="password" autocomplete="new-password" disabled></label>
  <button id="btn" type="submit" disabled>确认修改</button>
  <div class="msg" id="msg"></div>
  <div class="tip">
    <b>不用填 IP：</b>你拨上 VPN 之后，来源地址就是台账里分配给你的固定 IP，系统按它认人。反过来，没连 VPN 就打不开这个页面 —— 这是故意的，避免有人拿它试别人的密码。<br>
    <b>改完要做什么：</b>下次拨号用新密码即可，客户端配置（company.ovpn）不用重下。<br>
    <b>连续错 5 次</b>会锁 10 分钟，这是为了防止有人拿它试密码。
  </div>
</form>
<script>
var $ = function (s) { return document.querySelector(s); };
var CSRF = "";
function say(text, ok) { var m = $("#msg"); m.className = "msg " + (ok ? "ok" : "err"); m.textContent = text; }
function lockAll(on) {
  ["old", "new", "new2", "btn"].forEach(function (id) { $("#" + id).disabled = !!on; });
}
(async function () {
  try {
    var r = await fetch("/api/session", { credentials: "same-origin" });
    var j = await r.json();
    CSRF = j.csrf || "";
  } catch (e) { say("无法连接服务端，请确认已连上公司网络", false); return; }
  // 认人只依赖来源地址，前端没有任何可填的 IP
  try {
    var r2 = await fetch("/api/self/whoami", { credentials: "same-origin" });
    var w = await r2.json();
    var box = $("#who");
    if (w.ok && w.identified) {
      box.className = "who";
      box.innerHTML = "已识别：<b>" + (w.real_name || w.username) + "</b>"
        + (w.dept ? "（" + w.dept + "）" : "") + " · 账号 <b>" + w.username + "</b>"
        + " · 固定 IP <b>" + w.vpn_ip + "</b>"
        + (w.enabled ? "" : "<br><span style='color:#c92a0c'>该账号已停用，改密码也连不上，请联系管理员</span>");
      if (w.enabled) { lockAll(false); $("#old").focus(); }
    } else {
      box.className = "who bad";
      box.innerHTML = "<b>没认出你的账号</b><br>本页按来源 IP 认人，请先<b>连上 VPN</b>，再用浏览器打开本页。<br>"
        + "已经连着 VPN 还是这样，说明台账里没有分配给你的固定 IP（或不对），请联系管理员。";
      lockAll(true);
    }
  } catch (e) {
    $("#who").className = "who bad";
    $("#who").textContent = "识别账号失败，请刷新重试。";
    lockAll(true);
  }
})();
$("#f").addEventListener("submit", async function (ev) {
  ev.preventDefault();
  var old = $("#old").value, np = $("#new").value, np2 = $("#new2").value;
  if (!old || !np) { say("请把当前密码和新密码填完", false); return; }
  if (np.length < 8) { say("新密码至少 8 位", false); return; }
  if (np !== np2) { say("两次输入的新密码不一致", false); return; }
  lockAll(true);
  try {
    var r = await fetch("/api/self/password", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
      // 不传 vpn_ip：由服务端按来源地址认人，前端无法指定别人的账号
      body: JSON.stringify({ old: old, new: np })
    });
    var j = await r.json();
    if (j.ok) {
      say("修改成功（账号 " + (j.username || "") + "）。下次拨号请使用新密码。", true);
      $("#old").value = ""; $("#new").value = ""; $("#new2").value = "";
      lockAll(false);
    } else {
      say(j.msg || "修改失败", false);
      if (j.msg && j.msg.indexOf("过期") >= 0) location.reload();
      lockAll(false);
    }
  } catch (e) { say("请求失败：" + e, false); lockAll(false); }
});
</script>
</body>
</html>
"""


@app.get("/self")
def self_page():
    """用户自助改密页（不需要后台登录；拨着 VPN 时按来源 IP 自动认人）"""
    return Response(SELF_PAGE, mimetype="text/html; charset=utf-8")


@app.get("/api/self/whoami")
def api_self_whoami():
    """自助改密页进来先问一句：你能从来源 IP 认出我吗？

    拨着 VPN 访问时来源就是你的固定 IP，能认出来就不用再手填 IP。
    """
    ip = client_ip()
    u = core.user_by_vpn_ip(ip)
    if not u:
        return ok({"identified": False, "src_ip": ip,
                   "msg": "没有从来源地址认出账号：请先连上 VPN 再打开本页"})
    return ok({"identified": True, "vpn_ip": u["vpn_ip"], "username": u["username"],
               "real_name": u.get("real_name") or "", "dept": u.get("dept") or "",
               "enabled": bool(u["enabled"])})


@app.post("/api/self/password")
def api_self_password():
    ip = client_ip()
    key = "self:" + ip
    fails = [t for t in LOGIN_FAILS.get(key, []) if time.time() - t < 600]
    LOGIN_FAILS[key] = fails
    if len(fails) >= 5:
        return jsonify(ok=False, msg="失败次数过多，10 分钟后再试"), 429
    token = request.headers.get("X-CSRF-Token") or (request.get_json(silent=True) or {}).get("csrf")
    if not token or token != session.get("csrf"):
        return jsonify(ok=False, msg="页面已过期，请刷新后重试"), 403
    d = request.get_json(silent=True) or {}
    # 只认来源地址：不再接受前端传 vpn_ip（否则谁都能填别人的 IP 去试密码）。
    # 认不出来 = 没连 VPN，直接给明确提示，而不是笼统报「密码错」。
    u = core.user_by_vpn_ip(ip)
    if not u:
        fails.append(time.time())
        return jsonify(ok=False,
                       msg="没识别到你的账号，请先连上 VPN 再打开本页；已经连着 VPN 请联系管理员核对固定 IP"), 401
    try:
        r = core.self_change_password(u["vpn_ip"], d.get("old", ""), d.get("new", ""), src_ip=ip)
    except ValueError as e:
        fails.append(time.time())
        return jsonify(ok=False, msg=str(e)), 401
    LOGIN_FAILS.pop(key, None)
    return ok({"username": r["username"], "msg": "密码已修改，下次拨号请使用新密码"})


# --------------------------------------------------------------------------- 长任务

def tlog(tid, msg):
    core.ensure_dirs()
    with open(os.path.join(TASKS_DIR, tid + ".log"), "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))


def task_update(tid, **kw):
    con = core.db()
    sets = ",".join("%s=?" % k for k in kw)
    con.execute("UPDATE tasks SET %s WHERE id=?" % sets, list(kw.values()) + [tid])
    con.commit(); con.close()


def sudo_worker(tid, action, params=None, timeout=300):
    """特权动作：Web 只写任务文件，worker 从固定目录读，命令行不传自由参数"""
    core.ensure_dirs()
    with open(os.path.join(TASKS_DIR, tid + ".json"), "w", encoding="utf-8") as f:
        json.dump({"action": action, "params": params or {}}, f, ensure_ascii=False)
    if MOCK:
        cmd = [sys.executable, os.path.join(ROOT, "worker.py"), tid]
    else:
        cmd = ["sudo", "-n", core.PY, os.path.join(ROOT, "worker.py"), tid]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, OVPN_WEB_ROOT=core.ROOT,
                                    OVPN_WEB_MOCK="1" if MOCK else "0"))
    except subprocess.TimeoutExpired:
        raise RuntimeError("特权动作超时: %s" % action)
    if r.returncode != 0:
        tail = (r.stdout + r.stderr).strip()[-600:]
        raise RuntimeError("特权动作失败(%s): %s" % (action, tail or "见任务日志"))
    return True


def start_task(kind, steps, actor="admin", on_error=None):
    """steps: [(label, fn)]；fn 返回字符串写入日志。全程持全局写锁，任何时刻只有一个执行者。"""
    tid = uuid.uuid4().hex
    core.ensure_dirs()
    con = core.db()
    con.execute("INSERT INTO tasks(id,kind,status,progress,msg,created_at,actor) VALUES(?,?,?,?,?,?,?)",
                (tid, kind, "running", 0, "排队中", core.now(), actor))
    con.commit(); con.close()
    CTX["tid"] = tid      # 先绑定，线程启动后 steps 立即可用

    def runner():
        try:
            with WRITE_LOCK:
                CTX["tid"] = tid
                total = len(steps)
                for i, (label, fn) in enumerate(steps):
                    task_update(tid, msg=label, progress=int(i * 100 / total))
                    tlog(tid, "▶ " + label)
                    res = fn()
                    if res:
                        tlog(tid, "  " + str(res))
                    task_update(tid, progress=int((i + 1) * 100 / total))
                task_update(tid, status="done", progress=100, msg="完成", finished_at=core.now())
                tlog(tid, "✔ 全部完成")
        except Exception as e:
            tlog(tid, "✘ 失败: %s" % e)
            tlog(tid, traceback.format_exc()[-1500:])
            task_update(tid, status="failed", msg=str(e), finished_at=core.now())
            if on_error:
                try:
                    on_error(tid)
                except Exception as ee:
                    tlog(tid, "回滚也失败: %s" % ee)

    threading.Thread(target=runner, daemon=True).start()
    return tid


@app.get("/api/task/<tid>")
def api_task(tid):
    con = core.db()
    r = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    con.close()
    if not r:
        return ok({"id": tid, "kind": "", "status": "unknown", "progress": 100,
                   "msg": "任务记录已随快照回滚，请以自检结果为准", "created_at": "",
                   "finished_at": "", "actor": ""})
    return ok(dict(r))


@app.get("/api/task/<tid>/log")
def api_task_log(tid):
    offset = int(request.args.get("offset", "0"))
    p = os.path.join(TASKS_DIR, tid + ".log")
    if not os.path.exists(p):
        return ok({"text": "", "offset": 0})
    size = os.path.getsize(p)
    if offset > size:
        offset = 0
    with open(p, "rb") as f:
        f.seek(offset)
        text = f.read().decode("utf-8", "replace")
    return ok({"text": text, "offset": size})


# --------------------------------------------------------------------------- 概览 / 状态

@app.get("/api/state")
def api_state():
    c = core.cfg()
    users = core.list_users()
    vi = core.version_info()
    online = core.online_users()
    con = core.db()
    n_grants = con.execute("SELECT COUNT(*) FROM acl_grants").fetchone()[0]
    n_peers = con.execute("SELECT COUNT(*) FROM peer_grants").fetchone()[0]
    recent = [dict(r) for r in con.execute("SELECT * FROM auth_log ORDER BY id DESC LIMIT 8").fetchall()]
    con.close()
    return ok({
        "initialized": bool(core.get_meta("initialized")),
        "version": vi, "cfg": c, "admin_name": core.admin_name(), "src_ip": client_ip(),
        "stats": {"users": len(users), "enabled": len([u for u in users if u["enabled"]]),
                  "online": len(online), "grants": n_grants, "peers": n_peers,
                  "acl_mode": c["acl_mode"], "nat_mode": c.get("nat_mode", "nat")},
        "recent_auth": recent,
        "vpn_alive": MOCK or core._vpn_alive(),
        "need_redistribute": False,
    })


# --------------------------------------------------------------------------- 安装向导

@app.post("/api/wizard/check")
def wizard_check():
    core.log_audit(session.get("user", "admin"), "wizard.check", "环境自检")
    return ok({"items": core.env_check()})


@app.post("/api/wizard/params")
def wizard_params():
    d = request.get_json(silent=True) or {}
    c = core.cfg()
    for k in core.DEFAULT_CFG:
        if k in d and k not in ("acl_mode", "allow_major_upgrade", "web_allow_cidr"):
            c[k] = d[k]   # 含 nat_mode：向导里就能选「默认 NAT」或「无 NAT 溯源」
    errs = core.validate_cfg(c)
    if errs:
        raise ValueError("；".join(errs))
    for k, v in c.items():
        core.set_meta(k, v)
    core.set_meta("wizard_step", 3)
    core.log_audit("admin", "wizard.params", "对外地址 %s:%s/%s" % (c["public_addr"], c["port"], c["proto"]))
    return ok({"cfg": c})


@app.post("/api/wizard/binary")
def wizard_binary():
    """向导里直接上传静态 openvpn 二进制 —— 全 Web，不用命令行"""
    f = request.files.get("file")
    if not f:
        raise ValueError("未选择文件")
    core.ensure_dirs()
    staged = os.path.join(core.STAGING, "openvpn.upload")
    f.save(staged)
    chk = core.check_binary(staged, allow_major=False,
                            expect_sha256=request.form.get("sha256", ""))
    if not chk["ok"]:
        os.remove(staged)
        raise ValueError(chk["msg"])
    os.makedirs(os.path.dirname(core.BIN), exist_ok=True)
    shutil.copy2(staged, core.BIN)
    os.chmod(core.BIN, 0o755)
    v, br = core.binary_version(core.BIN)
    core.set_meta("openvpn_version", v)
    os.remove(staged)
    core.log_audit("admin", "wizard.binary", "上传 %s" % v)
    return ok({"version": v, "branch": br, "msg": chk["msg"]})


def _install_rollback(tid):
    tlog(tid, "开始回滚…")
    try:
        sudo_worker(tid, "stop-vpn", timeout=30)
    except Exception:
        pass


@app.post("/api/wizard/execute")
def wizard_execute():
    # 二次确认由前端弹窗按钮完成；这里只保留登录 + CSRF + 参数校验
    c = core.cfg()
    errs = core.validate_cfg(c)
    if errs:
        raise ValueError("；".join(errs))
    if not os.path.exists(core.BIN) and not MOCK:
        raise ValueError("缺少 bin/openvpn，请在向导第 2 步上传静态二进制")
    core.set_meta("wizard_step", 4)

    def s1():
        return "快照: " + (core.snapshot("安装前") if os.path.exists(os.path.join(core.DATA, "ca.crt")) else "首次安装，无需快照")

    def s2():
        core.gen_pki()
        return "已签发 CA + 服务端证书 + tls-crypt 密钥（账号密码模式，无客户端证书、无 CRL 过期问题）"

    def s3():
        r = core.write_all_config()
        return "配置已生成，ccd %d 个，指纹 %s" % (r["ccd"], r["fingerprint"])

    def s4():
        sudo_worker(CTX["tid"], "mask-system-unit")
        sudo_worker(CTX["tid"], "install-timer")
        return "已 mask 发行版 unit，规则重放定时器已启用"

    def s5():
        sudo_worker(CTX["tid"], "apply-rules")
        return "ACL 以【观察模式】下发：只记录不拦截"

    def s6():
        sudo_worker(CTX["tid"], "start-vpn")
        return "OpenVPN 已启动"

    def s7():
        sudo_worker(CTX["tid"], "selfcheck-system")
        core.set_meta("initialized", 1)
        core.set_meta("wizard_step", 5)
        return "自检完成；请把回程路由交给网管"

    steps = [("备份与前置检查", s1), ("签发 CA / 服务端证书 / tls-crypt", s2),
             ("生成 server.conf、ccd、company.ovpn、ACL 脚本", s3),
             ("接管进程与规则重放", s4), ("下发 ACL（观察模式）", s5),
             ("启动 OpenVPN", s6), ("自检并标记安装完成", s7)]
    tid = start_task("install", steps, on_error=lambda t: (_install_rollback(t), core.set_meta("initialized", 0)))
    return ok({"task_id": tid})


@app.get("/api/wizard/steps")
def wizard_steps():
    return ok({"step": core.get_meta("wizard_step"), "initialized": bool(core.get_meta("initialized"))})


# --------------------------------------------------------------------------- 账号

@app.get("/api/users")
def users_get():
    return ok({"users": core.list_users()})


@app.get("/api/users/next-ip")
def users_next_ip():
    """表单里的「自动分配」按钮：直接取下一个可用固定 IP，不用手算"""
    return ok({"vpn_ip": core.next_free_ip(core.cfg())})


@app.post("/api/users")
def users_add():
    d = request.get_json(silent=True) or {}
    core.backup_users()
    r = core.add_user(d.get("username", ""), d.get("vpn_ip") or None,
                      d.get("password") or None, d.get("note", ""),
                      d.get("real_name", ""), d.get("dept", ""))
    r["_need"] = "rebuild"
    after_change(kick=False)
    return ok(r)


@app.post("/api/users/<name>")
def users_update(name):
    d = request.get_json(silent=True) or {}
    core.backup_users()
    changes = core.update_user(name, vpn_ip=d.get("vpn_ip"), enabled=d.get("enabled"),
                               note=d.get("note"), password=d.get("password"),
                               real_name=d.get("real_name"), dept=d.get("dept"))
    kick = any("IP " in x for x in changes) or ("enabled" in d and not d["enabled"])
    after_change(kick=kick, username=name if kick else None)
    return ok({"changes": changes})


@app.delete("/api/users/<name>")
def users_delete(name):
    core.backup_users()
    core.delete_user(name)
    after_change(kick=True, username=name)
    return ok()


@app.post("/api/users/kick")
def users_kick():
    name = (request.get_json(silent=True) or {}).get("username", "")
    if not core.valid_username(name):
        raise ValueError("用户名非法")
    tid = uuid.uuid4().hex
    sudo_worker(tid, "kick-user", {"username": name})
    core.log_audit("admin", "user.kick", name)
    return ok()


def after_change(kick=False, username=None):
    """账号变更统一动作：全量重建 ccd + 规则（改 IP 必重建 ACL），可选踢下线"""
    core.write_all_config()
    tid = uuid.uuid4().hex
    sudo_worker(tid, "apply-rules")
    if kick and username:
        sudo_worker(tid, "kick-user", {"username": username})


@app.get("/api/users/export")
def users_export():
    fmt = request.args.get("fmt", "xlsx")
    name, data, mime = core.export_users(fmt)
    core.log_audit("admin", "users.export", fmt)
    return send_file(io.BytesIO(data), mimetype=mime, as_attachment=True, download_name=name)


@app.post("/api/users/import")
def users_import():
    """step=preview 或 step=apply（apply 需 5 分钟内的预览令牌；确认由前端弹窗按钮完成）"""
    step = request.form.get("step", "preview")
    if step == "preview":
        f = request.files.get("file")
        if not f:
            raise ValueError("未选择文件")
        content = f.read()
        r = core.preview_import(content, f.filename or "")
        return ok(r)
    token = request.form.get("token", "")
    reset_empty = request.form.get("reset_empty", "") in ("1", "true", "on", "yes")
    r = core.apply_import(token, reset_empty=reset_empty)
    after_change(kick=False)
    return ok(r)


# --------------------------------------------------------------------------- 资产 / 授权 / ACL

@app.get("/api/acl")
def acl_get():
    c = core.cfg()
    return ok({"asset_groups": core.list_asset_groups(),
               "user_groups": core.list_user_groups(),
               "grants": core.list_grants(),
               "peers": core.list_peer_grants(), "users": core.list_users(),
               "expanded": len(core.expand_grants()),
               "mode": c["acl_mode"], "nat_mode": c.get("nat_mode", "nat")})


@app.post("/api/network/nat")
def api_nat_mode():
    """切换「无 NAT 溯源」开关。只重建规则，不重启 OpenVPN、不改 ovpn、在线不断。"""
    d = request.get_json(silent=True) or {}
    mode = d.get("mode")
    if mode not in ("nat", "no-nat"):
        raise ValueError("网络模式只能是 nat / no-nat")
    old = core.cfg().get("nat_mode", "nat")
    core.set_meta("nat_mode", mode)
    core.write_all_config()
    tid = uuid.uuid4().hex
    sudo_worker(tid, "apply-rules")
    core.log_audit("admin", "network.nat", "%s -> %s" % (old, mode))
    msg = ("已开启【无 NAT 溯源】：内网服务器日志里的源 IP = 账号固定 IP；"
           "请确认网关回程静态路由已配好" if mode == "no-nat" else
           "已切回【NAT 模式】：开箱可用、不需要回程路由；内网日志源 IP = VPN 服务器，无法溯源到人")
    return ok({"nat_mode": mode, "fingerprint": core.get_meta("config_fingerprint"),
               "need_redistribute": False, "msg": msg})


@app.post("/api/peers")
def peers_add():
    d = request.get_json(silent=True) or {}
    core.add_peer_grant(d.get("src", ""), d.get("dst", ""), d.get("proto", "any"),
                        d.get("ports", ""), d.get("expires_at", ""), d.get("note", ""))
    after_change()
    return ok({"msg": "客户端互访授权已添加，规则已重建"})


@app.delete("/api/peers/<int:pid>")
def peers_del(pid):
    core.delete_peer_grant(pid)
    after_change()
    return ok()


@app.post("/api/asset-groups")
def asset_group_add():
    """一次建组 + 多个条目：name / note / items(多行文本或列表)"""
    d = request.get_json(silent=True) or {}
    items = d.get("items")
    if isinstance(items, str) or not items:
        items = core.parse_item_lines(d.get("lines", ""))
    else:
        items = [core.norm_item(i.get("cidr", ""), i.get("proto", "any"), i.get("ports", ""))
                 for i in items]
    r = core.add_asset_group(d.get("name", ""), d.get("note", ""), items)
    core.log_audit(session.get("user", "admin"), "assetgroup.add", "%s %d 条" % (d.get("name", ""), r["count"]))
    return ok(r)


@app.post("/api/asset-groups/<int:gid>")
def asset_group_update(gid):
    d = request.get_json(silent=True) or {}
    core.update_asset_group(gid, d.get("name"), d.get("note"))
    return ok()


@app.delete("/api/asset-groups/<int:gid>")
def asset_group_del(gid):
    core.delete_asset_group(gid)
    after_change()
    return ok()


@app.post("/api/asset-groups/<int:gid>/items")
def asset_item_add(gid):
    d = request.get_json(silent=True) or {}
    r = core.add_asset_item(gid, d.get("cidr", ""), d.get("proto", "any"),
                            d.get("ports", ""), d.get("note", ""))
    after_change()
    return ok(r)


@app.delete("/api/asset-items/<int:iid>")
def asset_item_del(iid):
    core.delete_asset_item(iid)
    after_change()
    return ok()


@app.post("/api/user-groups")
def user_group_add():
    d = request.get_json(silent=True) or {}
    r = core.add_user_group(d.get("name", ""), d.get("note", ""), d.get("members") or [])
    return ok(r)


@app.post("/api/user-groups/<int:gid>")
def user_group_update(gid):
    d = request.get_json(silent=True) or {}
    core.update_user_group(gid, d.get("name"), d.get("note"), d.get("members"))
    after_change()
    return ok()


@app.delete("/api/user-groups/<int:gid>")
def user_group_del(gid):
    core.delete_user_group(gid)
    after_change()
    return ok()


@app.post("/api/grants")
def grants_add():
    d = request.get_json(silent=True) or {}
    r = core.add_grant(d.get("subject_type", "user"), d.get("subject", ""),
                       d.get("asset_group_id", 0), d.get("expires_at", ""), d.get("note", ""))
    after_change()
    return ok(r, msg="授权已生效，防火墙规则已重建")


@app.delete("/api/grants/<int:gid>")
def grants_del(gid):
    core.delete_grant(gid)
    after_change()
    return ok()


@app.post("/api/acl/mode")
def acl_mode():
    d = request.get_json(silent=True) or {}
    mode = d.get("mode")
    if mode not in ("observe", "enforce"):
        raise ValueError("模式只能是 observe / enforce")
    old = core.cfg()["acl_mode"]
    core.set_meta("acl_mode", mode)
    core.write_all_config()
    tid = uuid.uuid4().hex
    sudo_worker(tid, "apply-rules")
    core.log_audit("admin", "acl.mode", "%s -> %s" % (old, mode))
    return ok({"mode": mode})


# --------------------------------------------------------------------------- 服务端设置

@app.get("/api/settings")
def settings_get():
    return ok({"cfg": core.cfg(), "version": core.version_info()})


@app.post("/api/settings")
def settings_save():
    d = request.get_json(silent=True) or {}
    c = core.cfg()
    for k in core.DEFAULT_CFG:
        # lan_nets 已并入 extra_routes，前端不再提交它
        if k in d and k not in ("allow_major_upgrade", "lan_nets"):
            c[k] = d[k]
    errs = core.validate_cfg(c)
    if errs:
        raise ValueError("；".join(errs))
    old_fp = core.get_meta("config_fingerprint")
    for k, v in c.items():
        core.set_meta(k, v)
    core.write_all_config()
    new_fp = core.get_meta("config_fingerprint")
    need = old_fp != new_fp
    tid = uuid.uuid4().hex
    sudo_worker(tid, "apply-rules")
    sudo_worker(tid, "restart-vpn")
    core.log_audit("admin", "settings.save", "指纹 %s -> %s" % (old_fp, new_fp))
    return ok({"need_redistribute": need, "fingerprint": new_fp,
               "warnings": core.route_warnings(c),
               "msg": "配置已应用，OpenVPN 已重启（在线用户会断线自动重连）"})


@app.get("/api/settings/preview")
def settings_preview():
    c = core.cfg()
    return ok({"text": core.render_server_conf(c),
               "routes": ["%s %s" % (n.network_address, n.netmask) for n in core.push_targets(c)],
               "warnings": core.route_warnings(c)})


@app.get("/api/settings/capacity")
def settings_capacity():
    """地址容量：网段里到底还剩多少个固定 IP 可分（一人一个，池子占掉的不算）"""
    c = core.cfg()
    users = core.list_users()
    usable = core.usable_host_ips(c)
    used = len([u for u in users if u["vpn_ip"] in usable])
    return ok({"usable": len(usable), "used": used, "free": max(0, len(usable) - used),
               "enabled": len([u for u in users if u["enabled"]]),
               "total": len(users), "pool": core.pool_text(c),
               "pool_enabled": bool(c.get("pool_enabled")),
               "msg": "网段 %s 可分配固定 IP %d 个，已用 %d 个，还剩 %d 个"
                      % (c["vpn_cidr"], len(usable), used, max(0, len(usable) - used))})


@app.get("/api/pki")
def api_pki():
    return ok({"items": core.pki_info()})


@app.get("/api/pki/<name>")
def api_pki_file(name):
    """只允许下载公开证书，私钥永远不出网"""
    fn = {"ca": "ca.crt", "server": "server.crt"}.get(name)
    if not fn:
        raise ValueError("只能下载 ca / server 证书")
    p = os.path.join(core.DATA, fn)
    if not os.path.exists(p):
        raise ValueError("证书尚未生成")
    return send_file(p, mimetype="application/x-pem-file", as_attachment=True, download_name=fn)


@app.get("/api/client.ovpn")
def client_ovpn():
    p = os.path.join(core.DATA, "company.ovpn")
    if not os.path.exists(p):
        raise ValueError("尚未生成，请先完成安装向导")
    core.log_audit("admin", "ovpn.download", "下载 company.ovpn")
    return send_file(p, mimetype="application/x-openvpn-profile", as_attachment=True,
                     download_name="company.ovpn")


@app.post("/api/settings/rebuild")
def settings_rebuild():
    tid = uuid.uuid4().hex
    core.write_all_config()
    sudo_worker(tid, "apply-rules")
    sudo_worker(tid, "collect-deny")
    core.log_audit("admin", "rules.rebuild", "全量重建")
    return ok({"msg": "规则已全量重建（含清理 MASQUERADE）"})


@app.post("/api/settings/reinit-ca")
def settings_reinit_ca():
    core.snapshot("重新初始化 CA 前")

    def a():
        core.gen_pki()
        return "已重签 CA / 服务端证书 / tls-crypt"
    def b():
        r = core.write_all_config()
        return "新指纹 %s，需全员替换 company.ovpn" % r["fingerprint"]
    def c():
        sudo_worker(CTX["tid"], "restart-vpn")
        return "OpenVPN 已重启"
    tid = start_task("reinit-ca", [("重新签发", a), ("重建配置", b), ("重启服务", c)])
    core.log_audit("admin", "ca.reinit", "高危操作")
    return ok({"task_id": tid, "need_redistribute": True})


@app.post("/api/settings/restore-default")
def settings_restore_default():
    core.snapshot("恢复默认前")
    for k, v in core.DEFAULT_CFG.items():
        # 白名单/操作者这类「安全边界」不跟着服务端参数一起回默认值：
        # 否则点一下「恢复默认」就把访问范围放大回所有私网。
        if k in ("web_allow_cidr",):
            continue
        core.set_meta(k, v)
    core.write_all_config()
    core.log_audit("admin", "settings.default", "恢复默认")
    return ok({"msg": "已恢复默认参数，请重新填写对外地址后再应用"})


# --------------------------------------------------------------------------- 版本与更新

@app.get("/api/version")
def version_get():
    return ok(core.version_info())


@app.post("/api/update/binary")
def update_binary():
    """上传静态 openvpn 二进制：校验 -> 快照 -> 替换 -> 重启 -> 自检 -> 失败回滚

    界面上已不再暴露：正常升级走「上传源码自动编译」。保留此接口是因为
    没有编译器的机器仍需要一条「把别处编好的静态二进制塞进来」的通道。
    """
    f = request.files.get("file")
    if not f:
        raise ValueError("未选择文件")
    allow_major = request.form.get("allow_major") == "1"
    core.ensure_dirs()
    staged = os.path.join(core.STAGING, "openvpn.new")
    f.save(staged)
    chk = core.check_binary(staged, allow_major=allow_major,
                            expect_sha256=request.form.get("sha256", ""))
    if not chk["ok"]:
        os.remove(staged)
        raise ValueError(chk["msg"])
    old_fp = core.get_meta("config_fingerprint")
    snap = core.snapshot("更新二进制前")

    def rb(tid):
        tlog(tid, "更新失败，回滚到快照…")
        try:
            core.restore_snapshot(snap)
            sudo_worker(tid, "restart-vpn")
        except Exception as e:
            tlog(tid, "回滚失败: %s" % e)

    def s1():
        sudo_worker(CTX["tid"], "replace-binary", {"staged": staged, "allow_major": allow_major})
        v, br = core.binary_version(core.BIN)
        return "已替换: %s（%s，EOL %s）" % (v, br, core.BRANCH_EOL.get(br, "未知"))
    def s2():
        sudo_worker(CTX["tid"], "restart-vpn")
        return "OpenVPN 已重启，在线连接已断开并自动重连"
    def s3():
        sudo_worker(CTX["tid"], "selfcheck-system")
        core.refresh_fingerprint()
        new_fp = core.get_meta("config_fingerprint")
        return "配置指纹 %s -> %s：%s" % (
            old_fp, new_fp, "未变，无需重新分发 ovpn" if old_fp == new_fp else "已变，必须全员替换 ovpn")
    tid = start_task("update-binary", [("校验并替换二进制", s1), ("重启服务", s2), ("自检与指纹比对", s3)],
                     on_error=rb)
    core.log_audit("admin", "update.binary", "allow_major=%s" % allow_major)
    return ok({"task_id": tid, "check": chk, "note": "只换二进制不改变配置指纹，company.ovpn 无需重新分发"})


@app.get("/api/update/build-env")
def update_build_env():
    """给前端提前判断「能不能在本机编译源码」，避免传完 100MB 才失败。

    注意不能直接把 build_env() 塞进 ok()：它自己带一个 "ok" 字段（表示环境是否可用），
    会把外层的 ok=True 覆盖掉，前端就当成请求失败——恰恰是没有编译器的机器上必现。
    """
    return ok({"env": core.build_env()})


SRC_RE = re.compile(r"^openvpn[-_](\d+\.\d+\.\d+)[^/]*\.(tar\.gz|tgz)$", re.I)


@app.post("/api/update/source")
def update_source():
    """上传 openvpn 官方源码包（GitHub/官方 releases 里的 .tar.gz）→ 本机编译 → 替换二进制

    适合没有构建机、或只想在机器上直接跟一个补丁版的场景：
    依赖库用包内预编译静态库，只编 openvpn 本体，全程不联网。
    """
    f = request.files.get("file")
    if not f:
        raise ValueError("未选择文件")
    name = os.path.basename(f.filename or "")
    m = SRC_RE.match(name)
    if not m:
        raise ValueError("文件名需形如 openvpn-2.6.24.tar.gz（官方/GitHub 的源码发布包）")
    ver = m.group(1)
    be = core.build_env()
    if not be["ok"]:
        raise ValueError("本机缺少编译环境：%s。请改装「静态二进制」，或先补装这些工具。"
                         % "、".join(be["missing"]))
    core.ensure_dirs()
    staged = os.path.join(core.STAGING, name)
    f.save(staged)
    try:
        size = os.path.getsize(staged)
        if size < 100 * 1024 or size > 60 * 1024 * 1024:
            raise ValueError("文件大小 %d 字节，不像 openvpn 源码包" % size)
        with open(staged, "rb") as fh:
            if fh.read(2) != b"\x1f\x8b":
                raise ValueError("不是 gzip 压缩包")
    except Exception:
        os.remove(staged)
        raise
    allow_major = request.form.get("allow_major") == "1"
    old_fp = core.get_meta("config_fingerprint")
    snap = core.snapshot("编译升级前")

    def rb(tid):
        tlog(tid, "编译升级失败，回滚到快照…")
        try:
            core.restore_snapshot(snap)
            sudo_worker(tid, "restart-vpn", timeout=60)
        except Exception as e:
            tlog(tid, "回滚失败: %s" % e)

    def s1():
        sudo_worker(CTX["tid"], "build-openvpn",
                    {"staged": staged, "allow_major": allow_major}, timeout=1500)
        v, br = core.binary_version(core.BIN)
        return "已编译并替换为 %s（%s 分支）" % (v, br)

    def s2():
        sudo_worker(CTX["tid"], "restart-vpn", timeout=60)
        return "OpenVPN 已重启，在线连接会断开并自动重连"

    def s3():
        sudo_worker(CTX["tid"], "selfcheck-system", timeout=60)
        core.refresh_fingerprint()
        return "配置指纹 %s -> %s" % (old_fp, core.get_meta("config_fingerprint"))

    tid = start_task("build-openvpn",
                     [("编译源码并替换二进制", s1), ("重启服务", s2), ("自检与指纹比对", s3)],
                     on_error=rb)
    core.log_audit("admin", "update.source", "%s allow_major=%s" % (name, allow_major))
    return ok({"task_id": tid, "version": ver,
               "note": "只换二进制不改变配置指纹，company.ovpn 无需重新分发"})


@app.post("/api/update/web")
def update_web():
    """【不在界面上】Web 自身热更新。

    用户明确表示日常用不到这个功能（更新都是直接覆盖文件），所以前端入口已移除。
    接口保留是当「界面挂了 / 改错代码把页面搞崩」时的远程救场通道：
    只要进程还活着，仍能 POST 一个 zip 把 app.py/core.py/index.html 换回来。
    """
    f = request.files.get("file")
    if not f:
        raise ValueError("未选择文件")
    core.ensure_dirs()
    staged = os.path.join(core.STAGING, "web.zip")
    f.save(staged)
    core.snapshot("更新 Web 前")
    tid = uuid.uuid4().hex
    sudo_worker(tid, "update-web", {"staged": staged})
    core.log_audit("admin", "update.web", "上传更新包")
    return ok({"task_id": tid, "msg": "Web 正在更新并重启，页面会自动重连；请勿关闭浏览器"})


@app.get("/api/backups")
def backups_get():
    return ok({"backups": core.list_backups()})


@app.post("/api/backups")
def backups_create():
    """手工打一份快照（升级前、改大配置前点一下，比等自动快照更安心）"""
    d = request.get_json(silent=True) or {}
    p = core.snapshot(d.get("tag") or "手动")
    core.log_audit("admin", "backup.create", p)
    return ok({"path": p})


@app.post("/api/backups/restore")
def backups_restore():
    d = request.get_json(silent=True) or {}
    path = d.get("path", "")
    core.restore_snapshot(path)
    tid = uuid.uuid4().hex
    sudo_worker(tid, "apply-rules")
    sudo_worker(tid, "restart-vpn")
    core.log_audit("admin", "backup.restore", path)
    return ok({"msg": "已恢复快照并重启服务"})


# --------------------------------------------------------------------------- 在线 / 日志 / 自检 / 路由

@app.get("/api/online")
def api_online():
    return ok({"users": core.online_users(), "users_all": core.list_users()})


@app.get("/api/logs")
def api_logs():
    kind = request.args.get("kind", "audit")
    if kind == "deny":
        tid = uuid.uuid4().hex
        try:
            sudo_worker(tid, "collect-deny")
        except Exception:
            pass
    return ok({"rows": core.get_logs(kind, int(request.args.get("limit", "200")))})


@app.get("/api/selfcheck")
def api_selfcheck():
    tid = uuid.uuid4().hex
    try:
        sudo_worker(tid, "selfcheck-system")
    except Exception:
        pass
    return ok({"items": core.selfcheck()})


@app.get("/api/route-guide")
def api_route_guide():
    return ok(core.route_guide())


@app.post("/api/service/restart")
def api_restart():
    tid = uuid.uuid4().hex
    sudo_worker(tid, "restart-vpn")
    core.log_audit("admin", "service.restart", "手动重启")
    return ok({"msg": "已重启，在线用户会断线自动重连"})


# --------------------------------------------------------------------------- 前端

@app.get("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


def main():
    core.init_db()
    # 命令行救场：白名单配错把自己关在门外时，SSH 上去一条命令就能放行
    argv = sys.argv[1:]
    if argv and argv[0] in ("--allow", "--allow-add"):
        if len(argv) < 2:
            print("用法: python3 app.py --allow 10.20.30.0/24[,10.0.0.0/8]")
            print("      python3 app.py --allow-reset      # 重置成 127.0.0.1 + 本机网卡网段")
            sys.exit(1)
        items = list(core.allow_list()) if argv[0] == "--allow-add" else []
        items += re.split(r"[,，\s]+", argv[1])
        print("新白名单: %s" % ", ".join(core.set_allow_list(items)))
        sys.exit(0)
    if argv and argv[0] == "--allow-reset":
        print("新白名单: %s" % ", ".join(core.set_allow_list(core.local_subnets())))
        sys.exit(0)
    bootstrap_admin()
    print("=" * 60)
    print(" OpenVPN Web 一体化管理系统  v%s" % core.WEB_VERSION)
    print(" 监听  0.0.0.0:%s   (MOCK=%s)" % (PORT, MOCK))
    print(" 数据  %s" % core.DB_PATH)
    print("=" * 60)
    # 显式单线程、关 reloader/debug：避免双进程同时全量重建 iptables 链
    app.run(host="0.0.0.0", port=PORT, threaded=False, use_reloader=False, debug=False)


if __name__ == "__main__":
    main()
