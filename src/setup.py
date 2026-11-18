#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键配置器（以 root 执行）：把「签 CA → 写配置 → 下发 ACL/NAT → 起 OpenVPN → 建账号」一次做完。

install.sh 会自动调用它；装完之后也可以随时重跑（幂等）：

    ./setup.py                                  # 用现有配置重新下发并重启
    ./setup.py --public-addr 203.0.113.10 --port 1194 --proto udp
    ./setup.py --lan 10.10.0.0/16,192.168.1.0/24 --nat-mode nat
    ./setup.py --user alice:Passw0rd --user bob
    ./setup.py --reinit-ca                      # 重签 CA（所有客户端都要换 ovpn）

为什么不用 Web 向导：向导要靠 sudo worker 提权，适合已装好的系统；
首次安装时我们本来就是 root，直接做完更省事，也避免「装完还得点 5 步」。
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

ROOT = core.ROOT
BIN = core.BIN


def sh(cmd, check=False, timeout=120, quiet=True):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        print("  ! 执行失败 %s: %s" % (" ".join(cmd), e))
        return 1, ""
    if r.stdout.strip() and not quiet:
        print("  " + r.stdout.strip()[-800:])
    if r.returncode != 0 and r.stderr.strip():
        print("  ! " + r.stderr.strip()[-500:])
    if check and r.returncode != 0:
        raise RuntimeError("命令失败(%d): %s" % (r.returncode, " ".join(cmd)))
    return r.returncode, r.stdout


def detect_ip():
    """取默认路由出口 IP，避免把 127.0.0.1 或 docker0 写进 ovpn"""
    for cmd in (["ip", "-4", "route", "get", "1.1.1.1"],
                ["ip", "-4", "-o", "addr", "show", "scope", "global"]):
        rc, out = sh(cmd)
        if rc != 0 or not out.strip():
            continue
        m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    return ""


def save_cfg(a):
    c = core.cfg()
    if a.public_addr:
        c["public_addr"] = a.public_addr
    elif not c.get("public_addr"):
        c["public_addr"] = detect_ip()
    if a.port:
        c["port"] = int(a.port)
    if a.proto:
        c["proto"] = a.proto
    if a.vpn_cidr:
        c["vpn_cidr"] = a.vpn_cidr
    if a.lan is not None:
        # 已经没有「内网网段」这个概念了，--lan 就是给客户端下发的附加推送路由
        c["extra_routes"] = [x.strip() for x in a.lan.replace("，", ",").split(",") if x.strip()]
        c["lan_nets"] = []
    if a.nat_mode:
        c["nat_mode"] = a.nat_mode
    if a.acl_mode:
        c["acl_mode"] = a.acl_mode
    errs = core.validate_cfg(c)
    if errs:
        raise SystemExit("配置不合法：%s" % "；".join(errs))
    for k, v in c.items():
        core.set_meta(k, v)
    return c


def firewall(c):
    port = int(c["port"])
    proto = c["proto"]
    done = []
    if shutil.which("firewall-cmd"):
        rc, out = sh(["firewall-cmd", "--state"])
        if rc == 0 and "running" in out:
            sh(["firewall-cmd", "--permanent", "--add-port=%d/%s" % (port, proto)])
            # Web 端口先对私网放行；应用层还有一道「管理端访问白名单」（默认收紧到
            # 本机网卡所在网段 + 127.0.0.1），VPN 拨号用户只能打开自助改密页
            sh(["firewall-cmd", "--permanent", "--add-rich-rule",
                "rule family=ipv4 source address=10.0.0.0/8 port port=%d protocol=tcp accept"
                % int(os.environ.get("OVPN_WEB_PORT", 8800))])
            sh(["firewall-cmd", "--permanent", "--add-rich-rule",
                "rule family=ipv4 source address=172.16.0.0/12 port port=%d protocol=tcp accept"
                % int(os.environ.get("OVPN_WEB_PORT", 8800))])
            sh(["firewall-cmd", "--permanent", "--add-rich-rule",
                "rule family=ipv4 source address=192.168.0.0/16 port port=%d protocol=tcp accept"
                % int(os.environ.get("OVPN_WEB_PORT", 8800))])
            sh(["firewall-cmd", "--reload"])
            done.append("firewalld: %d/%s 放行，Web %s/tcp 限私网"
                        % (port, proto, os.environ.get("OVPN_WEB_PORT", 8800)))
    if shutil.which("ufw"):
        sh(["ufw", "allow", "%d/%s" % (port, proto)])
        done.append("ufw: %d/%s 放行" % (port, proto))
    if not done:
        done.append("未检测到 firewalld/ufw；若开了 iptables 白名单，请自行放行 %d/%s" % (port, proto))
    return done


def write_sysctl():
    try:
        os.makedirs("/etc/sysctl.d", exist_ok=True)
        with open("/etc/sysctl.d/99-openvpn-web.conf", "w") as f:
            f.write("net.ipv4.ip_forward = 1\n")
    except Exception as e:
        print("  ! 写 sysctl.d 失败（不阻断）: %s" % e)
    sh(["sysctl", "-w", "net.ipv4.ip_forward=1"])


def mask_units():
    for unit in ("openvpn@.service", "openvpn-server@.service"):
        sh(["systemctl", "mask", unit])


def install_timer():
    svc = "/etc/systemd/system/openvpn-web-replay.service"
    tim = "/etc/systemd/system/openvpn-web-replay.timer"
    with open(svc, "w") as f:
        f.write("[Unit]\nDescription=Replay OpenVPN Web ACL rules\n"
                "[Service]\nType=oneshot\nExecStart=/bin/bash %s\n"
                % os.path.join(core.STATE, "rules.sh"))
    with open(tim, "w") as f:
        f.write("[Unit]\nDescription=Replay OpenVPN Web ACL rules periodically\n"
                "[Timer]\nOnBootSec=2min\nOnUnitActiveSec=5min\nAccuracySec=1min\n"
                "[Install]\nWantedBy=timers.target\n")
    sh(["systemctl", "daemon-reload"])
    sh(["systemctl", "enable", "--now", "openvpn-web-replay.timer"])


def start_vpn():
    conf = os.path.join(core.DATA, "server.conf")
    pidf = os.path.join(core.DATA, "openvpn.pid")
    # 先停掉旧进程，避免端口占用导致新配置没生效
    try:
        pid = int(open(pidf).read().strip())
        os.kill(pid, 15)
        import time
        for _ in range(20):
            try:
                os.kill(pid, 0)
            except Exception:
                break
            time.sleep(0.2)
    except Exception:
        pass
    if not os.path.exists(BIN):
        raise SystemExit("缺少 %s，无法启动 OpenVPN" % BIN)
    rc, out = sh([BIN, "--config", conf, "--writepid", pidf, "--daemon"], timeout=60)
    if rc != 0:
        raise SystemExit("OpenVPN 启动失败，看 %s/openvpn.log" % core.DATA)
    import time
    for _ in range(30):
        if core._vpn_alive():
            return True
        time.sleep(0.3)
    return core._vpn_alive()


def fix_owner():
    """root 写过的文件（证书、conf、ovpn、ccd、pid）要还回 Web 用户，
    否则后台改配置时会 EACCES —— 这是装完之后「页面能看不能存」的经典原因。"""
    try:
        st = os.stat(core.DATA)
        if st.st_uid == 0 and os.environ.get("OVPN_WEB_OWNER"):
            import pwd
            pw = pwd.getpwnam(os.environ["OVPN_WEB_OWNER"])
            uid, gid = pw.pw_uid, pw.pw_gid
        else:
            uid, gid = st.st_uid, st.st_gid
        if uid == 0:
            return
        for base, dirs, files in os.walk(core.DATA):
            for n in dirs + files:
                try:
                    os.chown(os.path.join(base, n), uid, gid)
                except Exception:
                    pass
    except Exception:
        pass


def main():
    if os.geteuid() != 0:
        raise SystemExit("请用 root 执行（需要写 iptables / 起进程）")
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--public-addr", default="", help="对外地址/域名，默认按默认路由自动探测")
    ap.add_argument("--port", default="", help="服务端口，默认 1194")
    ap.add_argument("--proto", default="", choices=["", "udp", "tcp"])
    ap.add_argument("--vpn-cidr", default="", help="VPN 网段，默认 10.8.0.0/24")
    ap.add_argument("--lan", default=None, help="要推送的内网网段，逗号分隔")
    ap.add_argument("--nat-mode", default="", choices=["", "nat", "no-nat"])
    ap.add_argument("--acl-mode", default="", choices=["", "observe", "enforce"])
    ap.add_argument("--user", action="append", default=[], help="name[:password]，可重复")
    ap.add_argument("--reinit-ca", action="store_true", help="重签 CA（客户端要换 ovpn）")
    ap.add_argument("--skip-rules", action="store_true", help="只写配置，不动 iptables")
    a = ap.parse_args()

    core.ensure_dirs()
    core.init_db()          # 首次安装时库里还没有表，cfg() 会直接炸

    print("[1/8] 校验配置")
    c = save_cfg(a)
    print("      对外 %s:%s/%s  VPN %s  模式 %s  ACL %s"
          % (c["public_addr"], c["port"], c["proto"], c["vpn_cidr"],
             c["nat_mode"], c["acl_mode"]))

    print("[2/8] PKI")
    ca = os.path.join(core.DATA, "ca.crt")
    if a.reinit_ca or not os.path.exists(ca):
        core.gen_pki()
        print("      已签发 CA / 服务端证书 / tls-crypt 密钥")
    else:
        print("      已有 CA，保留（客户端不用换 ovpn）")

    print("[3/8] 生成 server.conf / ccd / company.ovpn / 规则脚本")
    r = core.write_all_config()
    print("      ccd %d 个，配置指纹 %s" % (r["ccd"], r["fingerprint"]))

    print("[4/8] 开启 IPv4 转发")
    write_sysctl()

    if a.skip_rules:
        print("[5/8] 跳过 iptables 下发")
    else:
        print("[5/8] 下发 NAT / ACL 规则")
        sh(["/bin/bash", os.path.join(core.STATE, "rules.sh")], check=True)
        print("      已完成（%s 模式）" % c["nat_mode"])

    print("[6/8] 接管进程与规则重放")
    mask_units()
    install_timer()

    print("[7/8] 启动 OpenVPN")
    ok = start_vpn()
    print("      %s" % ("运行中" if ok else "未确认运行，请查看 data/openvpn.log"))

    print("[8/8] 账号")
    for spec in a.user:
        name, _, pw = spec.partition(":")
        pw = pw or core.rand_password(12)
        try:
            u = core.add_user(name, password=pw, note="安装脚本创建", actor="installer")
            print("      %s -> %s 密码 %s" % (u["username"], u["vpn_ip"], pw))
        except Exception as e:
            print("      %s 跳过: %s" % (name, e))
    # 建账号必须补一次 ccd：server.conf 开了 ccd-exclusive，
    # 少一个 ccd 文件客户端就会在密码认证通过后被 --client-config-dir 判失败（AUTH_FAILED）
    n_ccd = core.rebuild_ccd()
    print("      ccd 已重建 %d 个（ccd-exclusive：没有 ccd 的账号连不上）" % n_ccd)

    core.set_meta("initialized", 1)
    core.set_meta("wizard_step", 5)
    core.log_audit("installer", "setup.auto",
                   "%s:%s/%s %s" % (c["public_addr"], c["port"], c["proto"], c["nat_mode"]))
    for line in firewall(c):
        print("      防火墙 " + line)
    fix_owner()

    print(json.dumps({
        "public_addr": c["public_addr"], "port": c["port"], "proto": c["proto"],
        "vpn_cidr": c["vpn_cidr"], "nat_mode": c["nat_mode"],
        "config": os.path.join(core.DATA, "server.conf"),
        "client_ovpn": os.path.join(core.DATA, "company.ovpn"),
        "vpn_alive": bool(ok),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
