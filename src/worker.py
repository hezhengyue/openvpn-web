#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
特权执行器：由 Web 通过 sudo -n 调用，命令行只接受一个 task_id，不接自由参数。

    sudo -n /opt/openvpn-web/worker.py <task_id>

动作与参数从 <ROOT>/data/tasks/<task_id>.json 读取（Web 写入），并做白名单校验。
所有系统级动作集中在这里，Web 进程以低权限用户运行。

sudoers（install.sh 生成）:
    ovpnweb ALL=(root) NOPASSWD: /opt/openvpn-web/worker.py
"""
import os
import re
import sys
import json
import time
import shutil
import zipfile
import subprocess
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

ROOT = core.ROOT            # 数据/安装根（由 OVPN_WEB_ROOT 决定）
DATA = core.DATA
TASKS = os.path.join(DATA, "tasks")
STATE = os.path.join(DATA, "state")
BIN = os.path.join(ROOT, "bin")
MOCK = os.environ.get("OVPN_WEB_MOCK") == "1"
LOGF = None

ACTIONS = {
    "apply-rules", "start-vpn", "stop-vpn", "restart-vpn", "mask-system-unit",
    "install-timer", "replace-binary", "build-openvpn", "collect-deny", "selfcheck-system",
    "restore-snapshot", "update-web", "restorecon", "kick-user",
}


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    if LOGF:
        with open(LOGF, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def run(cmd, timeout=60, check=False):
    if MOCK:
        log("（模拟）$ " + " ".join(str(x) for x in cmd))
        return 0, ""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.stdout.strip():
        log(r.stdout.strip()[-1500:])
    if r.stderr.strip():
        log("[stderr] " + r.stderr.strip()[-800:])
    if check and r.returncode != 0:
        raise RuntimeError("命令失败(%d): %s" % (r.returncode, " ".join(str(x) for x in cmd)))
    return r.returncode, r.stdout


def safe_path(p, base=DATA):
    """所有路径必须落在受控目录内，杜绝 ../ 穿越"""
    rp = os.path.realpath(p)
    if not (rp == os.path.realpath(base) or rp.startswith(os.path.realpath(base) + os.sep)):
        raise ValueError("路径越界: %s" % p)
    return rp


def fix_owner():
    """worker 以 root 写过的文件，归还给 Web 用户，否则 Web 之后改不动"""
    try:
        st = os.stat(DATA)
        uid, gid = st.st_uid, st.st_gid
        if uid == 0:      # 数据目录本身就归 root（开发/单机模式），无需处理
            return
        for base, dirs, files in os.walk(DATA):
            for n in dirs + files:
                try:
                    os.chown(os.path.join(base, n), uid, gid)
                except Exception:
                    pass
    except Exception:
        pass


def vpn_pid():
    p = os.path.join(DATA, "openvpn.pid")
    try:
        pid = int(open(p).read().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return 0


def action_apply_rules(p):
    script = safe_path(os.path.join(STATE, "rules.sh"))
    if not os.path.exists(script):
        raise RuntimeError("规则脚本不存在，请先在 Web 上保存一次配置")
    # IPv4 转发：路由模式与客户端互访（tun0 -> tun0）都依赖它，重启后也要生效
    if not MOCK:
        try:
            os.makedirs("/etc/sysctl.d", exist_ok=True)
            with open("/etc/sysctl.d/99-openvpn-web.conf", "w") as f:
                f.write("net.ipv4.ip_forward = 1\n")
        except Exception as e:
            log("写入 sysctl.d 失败（不阻断）: %s" % e)
    run(["sysctl", "-w", "net.ipv4.ip_forward=1"], timeout=15)
    log("全量重建 OVPNWEB_NAT / OVPN_ACL 链（网络模式与 ACL 均按当前库状态）")
    run(["/bin/bash", script], timeout=60, check=True)
    log("规则已下发")


def action_start_vpn(p):
    conf = safe_path(os.path.join(DATA, "server.conf"))
    pidf = os.path.join(DATA, "openvpn.pid")
    if not os.path.exists(conf):
        raise RuntimeError("server.conf 不存在")
    if vpn_pid():
        log("OpenVPN 已在运行 (pid %d)" % vpn_pid()); return
    binary = os.path.join(BIN, "openvpn")
    if not os.path.exists(binary) and not MOCK:
        raise RuntimeError("缺少 bin/openvpn")
    run([binary, "--config", conf, "--writepid", pidf, "--daemon"], timeout=30, check=True)
    if not MOCK:
        for _ in range(20):
            if vpn_pid():
                break
            time.sleep(0.3)
    log("OpenVPN 已启动 pid=%s" % (vpn_pid() or "（模拟）"))


def action_stop_vpn(p):
    pid = vpn_pid()
    if not pid:
        log("OpenVPN 未运行"); return
    run(["kill", "-TERM", str(pid)], timeout=10)
    if not MOCK:
        for _ in range(20):
            if not vpn_pid():
                break
            time.sleep(0.3)
        if vpn_pid():
            run(["kill", "-KILL", str(pid)], timeout=10)
    log("OpenVPN 已停止")


def action_restart_vpn(p):
    action_stop_vpn(p)
    time.sleep(0.5)
    action_start_vpn(p)


def action_mask_system_unit(p):
    # 防止发行版 postinst 拉起第二个实例抢端口
    for unit in ("openvpn@.service", "openvpn-server@.service"):
        run(["systemctl", "mask", unit], timeout=20)
    log("已 mask 发行版 openvpn unit")


def action_install_timer(p):
    svc = "/etc/systemd/system/openvpn-web-replay.service"
    tim = "/etc/systemd/system/openvpn-web-replay.timer"
    script = os.path.join(STATE, "rules.sh")
    service = ("[Unit]\nDescription=Replay OpenVPN Web ACL rules\n"
               "[Service]\nType=oneshot\nExecStart=/bin/bash %s\n" % script)
    timer = ("[Unit]\nDescription=Replay OpenVPN Web ACL rules periodically\n"
             "[Timer]\nOnBootSec=2min\nOnUnitActiveSec=5min\nAccuracySec=1min\n"
             "[Install]\nWantedBy=timers.target\n")
    if not MOCK:
        with open(svc, "w") as f:
            f.write(service)
        with open(tim, "w") as f:
            f.write(timer)
    run(["systemctl", "daemon-reload"], timeout=30)
    run(["systemctl", "enable", "--now", "openvpn-web-replay.timer"], timeout=30)
    log("规则重放定时器已启用（firewalld reload / 重启后自动恢复）")


def action_replace_binary(p):
    src = safe_path(p.get("staged", ""), base=os.path.join(DATA, "staging"))
    if not os.path.exists(src):
        raise RuntimeError("上传的二进制不存在")
    chk = core.check_binary(src, allow_major=bool(p.get("allow_major")))
    if not chk["ok"]:
        raise RuntimeError(chk["msg"])
    os.makedirs(BIN, exist_ok=True)
    dst = os.path.join(BIN, "openvpn")
    if os.path.exists(dst):
        shutil.copy2(dst, os.path.join(DATA, "snapshots", "openvpn.previous"))
    # 直接覆盖正在运行的二进制会 ETXTBSY（Text file busy）；
    # 先写同目录临时文件再 rename，内核允许 rename 覆盖运行中的可执行文件，
    # 旧进程继续用旧 inode，重启后自然切到新版本。
    tmp = dst + ".new"
    shutil.copy2(src, tmp)
    os.chmod(tmp, 0o755)
    os.replace(tmp, dst)
    v, br = core.binary_version(dst)
    core.set_meta("openvpn_version", v)
    core.set_meta("last_update", core.now() + " 更新至 " + v)
    log("二进制已替换为 %s（%s 分支）" % (v, br))
    os.remove(src)


def action_build_openvpn(p):
    """上传的 openvpn 源码包 -> 本机编译 -> 校验 -> 替换 bin/openvpn

    依赖库用 vendor/static/prefix.tar.gz 里预编译好的 .a，只编 openvpn 本体，
    所以有 gcc + make + pkg-config 的机器十几秒就能出一个新版本，全程不需要联网。
    """
    src = safe_path(p.get("staged", ""), base=os.path.join(DATA, "staging"))
    if not os.path.exists(src):
        raise RuntimeError("上传的源码包不存在")
    script = os.path.join(ROOT, "build-static-openvpn.sh")
    if not os.path.exists(script):
        raise RuntimeError("缺少 build-static-openvpn.sh，无法编译")
    env = dict(os.environ)
    env.update({
        "STATIC_LIBS": "1",
        "VENDOR_ONLY": "1",
        "VENDOR": os.path.join(ROOT, "vendor"),
        "OPENVPN_SRC": src,
        # 本机编译不强求 glibc 2.17：产物保证在本机及更高版本上跑
        "MAX_GLIBC": "99",
    })
    out = os.path.join(DATA, "staging", "openvpn.built")
    if os.path.exists(out):
        os.remove(out)
    log("开始编译（预编译静态库 + 仅编 openvpn 本体），可能需要 1~3 分钟…")
    t0 = time.time()
    r = subprocess.run(["/bin/bash", script, out], env=env, capture_output=True,
                       text=True, timeout=1800, cwd=ROOT)
    for line in ((r.stdout or "") + "\n" + (r.stderr or "")).strip().splitlines()[-25:]:
        log("  " + line)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError("编译失败（rc=%d），详见任务日志" % r.returncode)
    log("编译完成，用时 %.0f 秒" % (time.time() - t0))
    p2 = dict(p)
    p2["staged"] = out
    action_replace_binary(p2)
    try:
        os.remove(src)
    except Exception:
        pass


def action_collect_deny(p):
    """把内核 LOG 里的越权尝试收成 JSON lines，供 Web 日志页展示"""
    out = []
    try:
        if MOCK:
            txt = ""
        else:
            r = subprocess.run(["journalctl", "-k", "--since", "-2 hours", "--no-pager"],
                               capture_output=True, text=True, timeout=20)
            txt = r.stdout
            if not txt:
                for f in ("/var/log/kern.log", "/var/log/messages"):
                    if os.path.exists(f):
                        txt = open(f, errors="replace").read()[-200000:]
                        break
        for line in txt.splitlines():
            if "OVPN_DENY" not in line and "OVPN_OBSERVE" not in line:
                continue
            g = lambda k: (re.search(k + r"=([^\s]+)", line).group(1)
                           if re.search(k + r"=([^\s]+)", line) else "")
            out.append({"ts": line[:24].strip(), "mode": "拦截" if "OVPN_DENY" in line else "观察",
                        "src": g("SRC"), "dst": g("DST"), "dport": g("DPT"), "proto": g("PROTO"),
                        "raw": line[-200:]})
    except Exception as e:
        log("读取内核日志失败: %s" % e)
    p = os.path.join(STATE, "deny.jsonl")
    seen = set()
    if os.path.exists(p):
        for line in open(p, "r", encoding="utf-8", errors="replace"):
            try:
                seen.add(json.loads(line).get("raw", ""))
            except Exception:
                pass
    fresh = [o for o in out if o.get("raw", "") not in seen]
    with open(p, "a", encoding="utf-8") as f:
        for o in fresh[-500:]:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    log("收集越权记录 %d 条（新增 %d）" % (len(out), len(fresh)))


def action_selfcheck_system(p):
    res = {}
    for name, cmd in (("forward", ["iptables", "-S", "FORWARD"]),
                      ("acl", ["iptables", "-S", "OVPN_ACL"]),
                      ("nat", ["iptables", "-t", "nat", "-S", "POSTROUTING"]),
                      ("nat_chain", ["iptables", "-t", "nat", "-S", "OVPNWEB_NAT"])):
        rc, out = run(cmd, timeout=10)
        res[name] = out if rc == 0 else ""
    with open(os.path.join(STATE, "system-check.json"), "w") as f:
        json.dump(res, f, ensure_ascii=False)
    log("系统状态采样完成")


def action_restore_snapshot(p):
    # 快照与全备份已合并：新格式是 full-backups 下的 tar.gz，
    # 老版本 data/snapshots/ 下的目录快照仍允许恢复。
    raw = p.get("path", "")
    base = os.path.join(DATA, "full-backups") if raw.endswith(".tar.gz") \
        else os.path.join(DATA, "snapshots")
    path = safe_path(raw, base=base)
    core.restore_snapshot(path)
    log("已恢复备份 %s" % path)


WEB_FILES = {"app.py", "core.py", "index.html", "auth-verify.py", "README.md", "version.txt",
             "self.html"}  # self.html 已并入 app.py；留着是给旧版本包兜底，不是主路径


def action_update_web(p):
    src = safe_path(p.get("staged", ""), base=os.path.join(DATA, "staging"))
    if not os.path.exists(src):
        raise RuntimeError("Web 更新包不存在")
    code_dir = os.path.dirname(os.path.abspath(__file__))
    with zipfile.ZipFile(src) as z:
        for name in z.namelist():
            if name.startswith("/") or ".." in name.split("/"):
                raise RuntimeError("更新包含非法路径: %s" % name)
            base = os.path.basename(name)
            # 白名单：不允许更新包替换 worker.py（root 执行体）或 python/ / data/ / bin/
            if base not in WEB_FILES:
                continue
            z.extract(name, code_dir)
            if "/" in name:
                shutil.move(os.path.join(code_dir, name), os.path.join(code_dir, base))
    log("Web 文件已更新，准备重启服务（页面会自动重连）")
    os.remove(src)
    if not MOCK:
        # 留 2 秒让本次 HTTP 响应先发回浏览器，再重启服务
        subprocess.Popen(["/bin/sh", "-c", "sleep 2; systemctl restart openvpn-web"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def action_restorecon(p):
    if shutil.which("restorecon"):
        run(["restorecon", "-R", ROOT], timeout=60)


def action_kick_user(p):
    name = p.get("username", "")
    if not core.valid_username(name):
        raise ValueError("用户名非法")
    if not core.mgmt_kill(name):
        raise RuntimeError("踢下线失败：management 接口 127.0.0.1:7505 无响应，"
                           "先确认 OpenVPN 服务在运行")
    log("已踢下线: %s（客户端通常会自动重连）" % name)


DISPATCH = {
    "apply-rules": action_apply_rules, "start-vpn": action_start_vpn,
    "stop-vpn": action_stop_vpn, "restart-vpn": action_restart_vpn,
    "mask-system-unit": action_mask_system_unit, "install-timer": action_install_timer,
    "replace-binary": action_replace_binary, "build-openvpn": action_build_openvpn,
    "collect-deny": action_collect_deny,
    "selfcheck-system": action_selfcheck_system, "restore-snapshot": action_restore_snapshot,
    "update-web": action_update_web, "restorecon": action_restorecon,
    "kick-user": action_kick_user,
}


def main():
    global LOGF
    if len(sys.argv) != 2 or not re.match(r"^[0-9a-f]{32}$", sys.argv[1]):
        print("usage: worker.py <task_id>", file=sys.stderr)
        sys.exit(2)
    tid = sys.argv[1]
    LOGF = safe_path(os.path.join(TASKS, tid + ".log"))
    job = safe_path(os.path.join(TASKS, tid + ".json"))
    if not os.path.exists(job):
        log("任务文件不存在"); sys.exit(2)
    with open(job, "r", encoding="utf-8") as f:
        spec = json.load(f)
    act = spec.get("action", "")
    params = spec.get("params", {}) or {}
    if act not in ACTIONS:
        log("动作不在白名单: %s" % act); sys.exit(2)
    log("开始执行: %s" % act)
    try:
        DISPATCH[act](params)
        fix_owner()
        log("完成: %s" % act)
        sys.exit(0)
    except Exception as e:
        fix_owner()
        log("失败: %s: %s" % (type(e).__name__, e))
        log(traceback.format_exc()[-800:])
        sys.exit(1)


if __name__ == "__main__":
    main()
