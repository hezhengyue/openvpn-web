#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安装前环境预检（纯标准库，用随包携带的 Python 运行）。

设计原则：
  * 「能自带的全自带」——交付包完整性是最重要的一项，缺了必须当场拦住，
    绝不允许悄悄退回目标机系统 python3（那会把「拷过去就能用」变成看环境脸色）。
  * 开局一次报齐：所有硬错误一起列出来，别让人装到一半才发现缺 TUN / 缺 glibc。
  * 只探测、不修改系统。时钟只警告不校准（离线机器没有可信时间基准）。
  * 所有探测都容错：命令不存在、文件读不到，一律降级成 warn/skip，不抛异常。
    这样本模块也能在 Windows 开发机上被 selftest 直接 import 调用。

退出码：0 = 可以继续安装（可能带警告）；1 = 有硬错误，install.sh 必须停手。
"""
import argparse
import glob
import os
import platform
import shutil
import socket
import subprocess
import sys
import time

LEVELS = {"ok": "  OK  ", "warn": " 警告 ", "error": " 错误 ", "skip": " 跳过 ", "info": " 信息 "}


def _run(cmd, timeout=5):
    """跑一条只读探测命令；不存在/超时/失败都返回空串，绝不抛异常。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except Exception:
        return ""


def _ok(cmd, timeout=20):
    """只关心退出码：import 成功时 Python 可能往 stderr 打 warning，不能拿输出判成败。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False


def _add(out, key, level, title, detail):
    out.append({"key": key, "level": level, "title": title, "detail": detail})
    return out


def collect(root, port=8800, vpn_port=1194, reinstall=False):
    """返回 findings 列表。不做任何写操作，可在任意系统上安全调用。"""
    out = []
    linux = sys.platform.startswith("linux")
    add = lambda level, key, title, detail: _add(out, key, level, title, detail)

    # ---------------------------------------------------------------- root
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        add("skip", "root", "root 权限", "非 POSIX 系统，跳过")
    elif geteuid() == 0:
        add("ok", "root", "root 权限", "当前是 root，可以写 /etc 与启动服务")
    else:
        add("error", "root", "root 权限", "请用 root 执行 ./install.sh")

    # ---------------------------------------------------------------- 架构
    mach = (platform.machine() or "").lower()
    if mach in ("x86_64", "amd64"):
        add("ok", "arch", "CPU 架构", "%s（本交付包按 x86_64 构建）" % mach)
    elif not linux:
        add("skip", "arch", "CPU 架构", "非 Linux，跳过（%s）" % mach)
    else:
        add("error", "arch", "CPU 架构",
            "当前是 %s，包内是 x86_64 预编译二进制，无法运行；请在构建机上改用源码编译" % mach)

    # ---------------------------------------------------------------- glibc / libc
    if not linux:
        add("skip", "glibc", "glibc 版本", "非 Linux，跳过")
    else:
        libc_txt = ""
        try:
            confstr = getattr(os, "confstr", None)
            if confstr:
                libc_txt = confstr("CS_GNU_LIBC_VERSION") or ""
        except Exception:
            libc_txt = ""
        ver = ""
        if libc_txt.lower().startswith("glibc"):
            ver = libc_txt.split()[-1]
        else:
            ldd = _run(["ldd", "--version"])
            if "musl" in ldd.lower():
                add("error", "glibc", "glibc 版本",
                    "检测到 musl libc（Alpine 等）。包内 Python/openvpn 是 glibc 二进制，无法运行；"
                    "本版本只支持 glibc 系发行版")
                ver = None
            else:
                import re
                m = re.search(r"(\d+\.\d+)", ldd)
                ver = m.group(1) if m else ""
        if ver is not None:
            if not ver:
                add("warn", "glibc", "glibc 版本", "没能读出版本，继续安装但请留意 GLIBC_* not found 报错")
            elif tuple(int(x) for x in (ver.split(".") + ["0"])[:2]) < (2, 17):
                add("error", "glibc", "glibc 版本",
                    "当前 %s，包内二进制需要 glibc ≥ 2.17（CentOS 7 基线）" % ver)
            else:
                add("ok", "glibc", "glibc 版本", "%s ≥ 2.17" % ver)

    # ---------------------------------------------------------------- TUN
    if not linux:
        add("skip", "tun", "TUN 设备", "非 Linux，跳过")
    elif os.path.exists("/dev/net/tun"):
        add("ok", "tun", "TUN 设备", "/dev/net/tun 存在")
    else:
        add("error", "tun", "TUN 设备",
            "缺 /dev/net/tun，OpenVPN 建不了隧道。需宿主机加载 tun 模块或映射设备"
            "（容器要 --device /dev/net/tun --cap-add NET_ADMIN）")

    # ---------------------------------------------------------------- init 系统
    if not linux:
        add("skip", "init", "init 系统", "非 Linux，跳过")
    elif os.path.isdir("/run/systemd/system"):
        add("ok", "init", "init 系统", "systemd 正在运行（PID 1）")
    else:
        # A 档不做无 systemd 降级，所以这里必须硬拦住，别装完发现服务没人管
        add("error", "init", "init 系统",
            "没检测到 systemd（/run/systemd/system 不存在）。本版本用 systemd unit 托管服务与规则重放，"
            "无 init 的系统暂不支持")

    # ---------------------------------------------------------------- 随包运行时（最重要）
    py = os.path.join(root, "python", "bin", "python3")
    py_ok = False
    if not os.path.isfile(py):
        add("error", "runtime", "随包 Python 运行时",
            "缺 %s —— 交付包不完整。本系统强制使用随包 Python，不会退回系统 python3" % py)
    elif not os.access(py, os.X_OK):
        add("error", "runtime", "随包 Python 运行时",
            "%s 不可执行（传输中丢了权限位）。请重新解包，或 chmod +x python/bin/*" % py)
    else:
        v = _run([py, "-V"], timeout=15).strip()
        if not v:
            add("error", "runtime", "随包 Python 运行时",
                "%s 跑不起来（软链存根没还原 / 动态库缺失 / 不是当前架构）。请重新解包" % py)
        else:
            py_ok = True
            add("ok", "runtime", "随包 Python 运行时", "%s · %s（只用包内，不依赖目标机）" % (py, v))

    # 依赖：install.sh 在选解释器后会从 vendor/wheels 离线补装，
    # 所以这里「缺依赖但有 wheel」只提示补装，不是硬错误；两者都缺才是硬错误。
    wheels = glob.glob(os.path.join(root, "vendor", "wheels", "*.whl"))
    deps_ok = False
    if py_ok:
        deps_ok = _ok([py, "-c", "import django, axes, whitenoise, waitress, environ, openpyxl"])
        if deps_ok:
            add("ok", "deps", "Web 依赖", "Django 全家桶 / openpyxl / waitress 已就位")
        elif wheels:
            add("warn", "deps", "Web 依赖",
                "包内运行时缺依赖，但带齐 %d 个 wheel，安装时会离线补装" % len(wheels))
        else:
            add("error", "deps", "Web 依赖",
                "依赖未就位，且 vendor/wheels/ 里没有 wheel 可补装。请重新下载完整交付包")
    else:
        add("skip", "deps", "Web 依赖", "随包 Python 不可用，无法检查")
    if wheels:
        add("ok", "wheels", "离线 wheel", "%d 个（断网也能补依赖）" % len(wheels))
    elif py_ok and deps_ok:
        add("ok", "wheels", "离线 wheel", "运行时已装好依赖，无需补装")
    else:
        add("error", "wheels", "离线 wheel", "vendor/wheels/ 缺失，离线环境补不了依赖")

    # ---------------------------------------------------------------- 二进制
    ovpn = os.path.join(root, "bin", "openvpn")
    if os.path.isfile(ovpn) and os.access(ovpn, os.X_OK):
        head = _run([ovpn, "--version"], timeout=15).strip().splitlines()
        if head:
            add("ok", "binary", "OpenVPN 二进制", head[0])
        elif linux:
            # 比读系统 glibc 更硬的检查：直接跑包内 ELF，跑不起来就是 ABI/架构不匹配。
            # 别等到装完启动服务才发现 GLIBC_2.xx not found。
            add("error", "binary", "OpenVPN 二进制",
                "%s 无法执行（--version 无输出），多半是 glibc 版本过低或架构不符；"
                "请确认交付包与本机匹配" % ovpn)
        else:
            add("ok", "binary", "OpenVPN 二进制", "已就位（非 Linux，未做可执行性验证）")
    elif os.path.isfile(ovpn):
        add("warn", "binary", "OpenVPN 二进制", "存在但不可执行，安装时会 chmod 755 修正")
    else:
        add("warn", "binary", "OpenVPN 二进制",
            "包内未带二进制，安装会尝试就地离线编译（需 gcc/make/perl/pkgconfig 与 vendor/src）。"
            "正式交付包应当自带，离线机器编译可能失败")

    # ---------------------------------------------------------------- 端口
    def _bindable(p, proto):
        fam, typ = socket.AF_INET, socket.SOCK_STREAM if proto == "tcp" else socket.SOCK_DGRAM
        s = socket.socket(fam, typ)
        try:
            s.bind(("0.0.0.0", p))
            return True, ""
        except OSError as e:
            return False, getattr(e, "strerror", str(e)) or str(e)
        finally:
            s.close()

    busy = []
    for p, proto in ((int(port), "tcp"), (int(vpn_port), "tcp"), (int(vpn_port), "udp")):
        ok, err = _bindable(p, proto)
        if not ok:
            busy.append("%d/%s（%s）" % (p, proto, err))
    if busy:
        # 重装时端口多半是本服务自己占着，restart 会处理，只提示不拦
        add("warn" if reinstall else "error", "ports", "端口占用",
            ("已被占用：" + "、".join(busy) + "。") +
            ("重装场景会重启服务，若确认不是本服务占用请先停掉" if reinstall
             else "请先停掉占用进程再安装，否则 Web / VPN 起不来"))
    else:
        add("ok", "ports", "端口占用", "%s/tcp、%s/tcp+udp 均可绑定" % (port, vpn_port))

    # ---------------------------------------------------------------- 磁盘
    probe = root if os.path.isdir(root) else (os.path.dirname(root) or "/")
    try:
        free = shutil.disk_usage(probe).free
        mb = free // (1024 * 1024)
        if mb < 500:
            add("error", "disk", "磁盘空间", "%s 可用仅 %d MB，解包 + 运行至少需要 500 MB" % (probe, mb))
        elif mb < 1024:
            add("warn", "disk", "磁盘空间", "%s 可用 %d MB，够装但不宽裕（日志/备份会增长）" % (probe, mb))
        else:
            add("ok", "disk", "磁盘空间", "%s 可用 %d MB" % (probe, mb))
    except Exception as e:
        add("warn", "disk", "磁盘空间", "没能读取：%s" % e)

    # ---------------------------------------------------------------- 时钟（只警告）
    if not linux:
        add("skip", "clock", "系统时钟", "非 Linux，跳过")
    else:
        skew = None
        try:
            with open("/sys/class/rtc/rtc0/since_epoch") as f:
                rtc = int(f.read().strip())
            skew = abs(rtc - int(time.time()))
        except Exception:
            pass
        if skew is not None and skew > 3600:
            add("warn", "clock", "系统时钟",
                "RTC 与系统时间相差约 %.1f 小时，重启后时间会跳变（典型症状：把北京时间数值当 UTC 写进 RTC，"
                "或系统快 8 小时）。会导致签发的证书显示「尚未生效」、账号有效期提前到期；安装不会自动校准"
                % (skew / 3600.0))
        td = _run(["timedatectl"])
        ntp = "NTP synchronized: yes" in td
        if not ntp:
            cy = _run(["chronyc", "tracking"])
            ntp = "Leap status" in cy and "Not synchronised" not in cy
        if ntp:
            add("ok", "clock", "系统时钟", "检测到 NTP 已同步 · 时区 %s" % (time.tzname[0] or "UTC"))
        else:
            add("warn", "clock", "系统时钟",
                "没检测到 NTP 同步（timedatectl/chronyc 都不可用或未同步）。证书有效期与审计时间依赖系统时钟，"
                "建议装完启用 chrony/ntpd 对时；安装不会自动改系统时间")

    # ---------------------------------------------------------------- 防火墙 / SELinux（只诊断）
    if not linux:
        add("skip", "firewall", "防火墙 / SELinux", "非 Linux，跳过")
    else:
        found = [n for n in ("firewall-cmd", "ufw", "iptables", "nft") if shutil.which(n)]
        fw_active = ""
        if "firewall-cmd" in found:
            fw_active = "firewalld " + ("运行中" if "running" in _run(["firewall-cmd", "--state"]) else "未运行")
        se = ""
        try:
            with open("/sys/fs/selinux/enforce") as f:
                se = "SELinux " + ("Enforcing" if f.read().strip() == "1" else "Permissive")
        except Exception:
            pass
        bits = [x for x in (", ".join(found), fw_active, se) if x]
        fw_note = "。安装脚本会按检测结果放行 VPN 端口；若装完访问不到 Web 端口，请检查这层白名单"
        if "运行中" in fw_active:
            fw_note += ("。firewalld 默认只放行 ICMP、其余转发流量一律 REJECT，"
                        "安装会额外补「私网 → tun0」的转发规则，否则内网访问 VPN 客户端"
                        "会 ping 通但 RDP/SSH 全不通")
        elif "ufw" in found:
            fw_note += ("。ufw 的 DEFAULT_FORWARD_POLICY 为 DROP 时会拦掉转发，"
                        "需要时请改成 ACCEPT 并放行 VPN 网段")
        add("info", "firewall", "防火墙 / SELinux",
            ("；".join(bits) or "未检测到常见防火墙") + fw_note)

    return out


def render(findings):
    """人可读输出。硬错误一次报齐，最后给汇总。"""
    lines = ["", "=" * 60, " 安装前环境预检（只探测、不改系统）", "=" * 60]
    for f in findings:
        lines.append(" [%s] %-16s %s" % (LEVELS[f["level"]], f["title"], f["detail"]))
    errs = [f for f in findings if f["level"] == "error"]
    warns = [f for f in findings if f["level"] == "warn"]
    lines.append("-" * 60)
    if errs:
        lines.append(" 预检未通过：%d 项硬错误，已中止安装（避免装到一半留下半套环境）" % len(errs))
    elif warns:
        lines.append(" 预检通过：%d 项警告，安装继续，请留意上面的提示" % len(warns))
    else:
        lines.append(" 预检通过：环境齐备，交付包完整")
    lines.append("=" * 60)
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="OpenVPN Web 安装前环境预检")
    ap.add_argument("--root", default=os.environ.get("OVPN_WEB_ROOT", "/opt/openvpn-web"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("OVPN_WEB_PORT", "8800")))
    ap.add_argument("--vpn-port", type=int, default=1194)
    ap.add_argument("--reinstall", action="store_true", help="重装场景：端口占用只警告不拦")
    a = ap.parse_args(argv)
    findings = collect(a.root, a.port, a.vpn_port, a.reinstall)
    print(render(findings))
    return 1 if any(f["level"] == "error" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
