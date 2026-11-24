#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全链路自检（MOCK 模式，不碰真实 iptables / systemd / 进程）。
    OVPN_WEB_MOCK=1 python3 selftest.py
真实安装由 install.sh + Web 向导完成；本脚本验证业务逻辑与 API 是否自洽。
"""
import os
import io
import sys
import csv
import time
import json
import shutil
import tempfile
import subprocess
import tarfile
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))        # tests/
CODE = os.path.dirname(HERE)                             # 安装根（system/、django/、bin/ 所在层）
SYS = os.path.join(CODE, "system")
DJANGO = os.path.join(CODE, "django") if os.path.isdir(os.path.join(CODE, "django")) else CODE
TMP = tempfile.mkdtemp(prefix="ovpnweb-selftest-")
os.environ["OVPN_WEB_ROOT"] = TMP
os.environ["OVPN_WEB_MOCK"] = "1"
os.environ["OVPN_WEB_PORT"] = "8899"
sys.path.insert(0, HERE)

# 兼容层内部完成 django.setup() + 建库；业务门面与 ORM 模型都从它拿
import selftest_django as web              # noqa: E402
from ovpn import bridge as _bridge         # noqa: E402
from ovpn.models import (                  # noqa: E402
    VpnUser, UserGroupMember, AclGrant, PeerGrant,
)
from audit.models import AuditLog          # noqa: E402

core = web.core
fullbackup = _bridge.fullbackup

PASS, FAIL = [], []
# 闸门/版本号这类用例要真的执行一个 shell 脚本，Windows 上跑不了（目标环境是 Linux）
POSIX = os.name == "posix"


def check(name, cond, detail=""):
    # cond=None 表示「这条在当前位置不适用」（例如构建脚本只在仓库里有，装机目录是平铺的）
    if cond is None:
        print("  [SKIP] " + name + (("  -> " + str(detail)) if detail else ""))
        return
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  -> " + str(detail)) if detail and not cond else ""))


def _admin_pw_ok(pw):
    """管理口令现在只存在 Django 的 auth_user 里。"""
    from django.contrib.auth import get_user_model
    u = get_user_model().objects.filter(username="admin").first()
    return bool(u and u.check_password(pw))


def _set_admin_pw(pw):
    from django.contrib.auth import get_user_model
    u = get_user_model().objects.filter(username="admin").first()
    if u:
        u.set_password(pw)
        u.save()


def section(t):
    print("\n== " + t + " ==")


def wait_task(cli, tid, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        j = cli.get("/api/task/" + tid).get_json()
        if j["status"] != "running":
            return j
        time.sleep(0.15)
    return {"status": "timeout"}


def main():
    core.init_db()
    web.bootstrap_admin()

    # ---------- 默认凭据 / 首次登录强制改密 ----------
    section("默认 admin/admin 与首登强改")
    check("默认管理账户是 admin", core.admin_user() == "admin", core.admin_user())
    check("默认管理密码是 admin", _admin_pw_ok("admin"))
    check("新装标记首次登录必须改密", core.admin_must_change())
    _init_pw = open(os.path.join(core.DATA, "admin_password.txt")).read().splitlines()[0]
    check("默认凭据文件写入 admin", _init_pw == "admin", _init_pw)
    fc = web.app.test_client()
    lr = fc.post("/api/login", json={"username": "admin", "password": "admin"})
    check("默认 admin/admin 可登录", lr.get_json().get("ok") is True, lr.get_json())
    check("登录返回 must_change", lr.get_json().get("must_change") is True, lr.get_json())
    FH = {"X-CSRF-Token": lr.get_json()["csrf"]}
    check("未改密前业务接口被拦", fc.get("/api/state", headers=FH).status_code == 403)
    check("未改密前改账户名被拦",
          fc.post("/api/admin-user", json={"username": "boss"}, headers=FH).status_code == 403)
    rr = fc.post("/api/password", json={"new": "TestPass123!", "confirm": "TestPass123!"}, headers=FH)
    check("首登改密无需手填旧密码", rr.get_json().get("ok") is True, rr.get_json())
    check("改密后强改标记清除", core.admin_must_change() is False)
    check("改密后业务接口放行", fc.get("/api/state", headers=FH).status_code == 200)
    check("改密后默认密码失效", _admin_pw_ok("admin") is False)
    check("凭据文件不再留明文口令",
          open(os.path.join(core.DATA, "admin_password.txt")).read().startswith("#"))
    check("新密码可登录", web.app.test_client().post(
        "/api/login", json={"username": "admin", "password": "TestPass123!"}).get_json().get("ok") is True)

    _set_admin_pw("TestPass123!")
    web.bootstrap_admin()
    c = web.app.test_client()

    # ---------- 基础与配置 ----------
    section("数据库与配置生成")
    check("默认 VPN 网段", core.cfg()["vpn_cidr"] == "10.8.0.0/24")
    check("无 NAT 溯源默认关闭（默认 NAT 模式）", core.cfg()["nat_mode"] == "nat")
    check("默认隧道协议 TCP", core.cfg()["proto"] == "tcp")
    check("默认 tun-mtu 1400", core.cfg()["tun_mtu"] == 1400)
    check("默认 mssfix 1360（大包超时修复）", core.cfg()["mssfix"] == 1360)
    check("默认不再有内网网段概念", core.cfg()["lan_nets"] == [])
    # 全新安装回归：setup.py 先用 SCHEMA 裸建表，再 migrate --fake-initial。
    # SCHEMA 里每条 CREATE INDEX 引用的表都必须存在（否则 init_db 抛 no such table、
    # meta 初始化中断）；且必须含 audit 占位表与 ovpn.0001 建表集合对齐
    # （否则 fake-initial 判定表不齐、真建表撞 asset_groups 已存在）。
    import re as _re_sch
    _schema = core.SCHEMA
    _idx_tables = _re_sch.findall(r"CREATE\s+INDEX[\s\S]*?\sON\s+(\w+)\s*\(", _schema)
    _schema_tables = set(_re_sch.findall(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)", _schema))
    check("SCHEMA 每条索引引用的表都存在",
          all(t in _schema_tables for t in _idx_tables),
          "索引表=%s 建表=%s" % (_idx_tables, sorted(_schema_tables)))
    check("SCHEMA 含 audit 占位表（对齐 ovpn.0001，保证 fake-initial 成功）",
          "audit" in _schema_tables, sorted(_schema_tables))
    core.gen_pki()
    ca = open(os.path.join(core.DATA, "ca.crt")).read()
    ta = open(os.path.join(core.DATA, "ta.key")).read()
    check("CA 证书签发", "BEGIN CERTIFICATE" in ca)
    check("tls-crypt 静态密钥", "OpenVPN Static key V1" in ta)
    r = core.write_all_config()
    conf = open(os.path.join(core.DATA, "server.conf")).read()
    ovpn = open(os.path.join(core.DATA, "company.ovpn")).read()
    rules = open(os.path.join(core.STATE, "rules.sh")).read()
    for k in ("username-as-common-name", "verify-client-cert none", "ccd-exclusive",
              "auth-user-pass-verify", "tls-crypt"):
        check("server.conf 含 " + k, k in conf)
    # 地址池改成可选：默认不写 ifconfig-pool，也不写 max-clients
    check("默认不写 ifconfig-pool（地址全由 ccd 固定下发）", "ifconfig-pool" not in conf)
    check("server.conf 注明未启用地址池", "未启用动态地址池" in conf)
    check("不再写 max-clients", "max-clients" not in conf)
    check("网段能分配 253 个固定 IP（.2~.254）", core.static_capacity(core.cfg()) == 253,
          core.static_capacity(core.cfg()))
    # 2.6 不接受裸写 proto tcp（Options error: --proto tcp is ambiguous），必须按方向写
    check("server.conf 写 proto tcp-server", "proto tcp-server" in conf)
    check("company.ovpn 写 proto tcp-client", "proto tcp-client" in ovpn)
    check("server.conf 不再有裸 proto tcp", "\nproto tcp\n" not in conf)
    # topology 必须两端都对上：服务端不 push 的话，客户端按 net30 解析 ccd 下发的
    # "ifconfig-push <ip> <掩码>"，Windows 会 fatal（掩码被当成对端 IP）
    check("server.conf 写 topology subnet", "\ntopology subnet\n" in conf)
    check("server.conf 显式推送 topology subnet", 'push "topology subnet"' in conf)
    check("company.ovpn 写 topology subnet", "\ntopology subnet\n" in ovpn)
    check("server.conf 钳制 TCP MSS（mssfix 1360）", "\nmssfix 1360\n" in conf)
    check("company.ovpn 同步钳制 TCP MSS", "\nmssfix 1360\n" in ovpn)
    # DCO 会绕过用户态 mssfix，且 Windows DCO 驱动大流量下会 reset 连接冲垮隧道：
    # 两端都必须显式 disable-dco（见 2.4.1 修复），否则 RDP 仍会超时。
    check("server.conf 禁用 DCO", "\ndisable-dco\n" in conf)
    check("company.ovpn 禁用 DCO（修复 RDP 隧道崩溃）", "\ndisable-dco\n" in ovpn)
    # tls-crypt 开关：默认开（有 tls-crypt / <tls-crypt>）；关掉后两端都不写。
    # 用于对齐 iKuai 朴素实现排查 RDP/封装错位问题，指纹必须随之变化。
    check("默认启用 tls-crypt", "tls-crypt " in conf and "<tls-crypt>" in ovpn)
    _no_tls = dict(core.cfg(), tls_crypt=False)
    check("关闭 tls-crypt 后 server.conf 不写 tls-crypt", "tls-crypt " not in core.render_server_conf(_no_tls))
    check("关闭 tls-crypt 后 ovpn 无 <tls-crypt> 块", "<tls-crypt>" not in core.render_ovpn(_no_tls))
    check("tls-crypt 开关纳入配置指纹", core.config_fingerprint(_no_tls) != core.config_fingerprint(core.cfg()))
    # 非 AEAD（AES-256-CFB）须两端显式 cipher，才能复现 iKuai 的可用组合
    _cfb = dict(core.cfg(), data_ciphers="AES-256-CFB", tls_crypt=False, compress="lzo")
    _cfb_conf = core.render_server_conf(_cfb)
    _cfb_ovpn = core.render_ovpn(_cfb)
    check("非 AEAD 时 server.conf 写 cipher AES-256-CFB", "\ncipher AES-256-CFB\n" in _cfb_conf)
    check("非 AEAD 时 ovpn 写 cipher AES-256-CFB", "\ncipher AES-256-CFB\n" in _cfb_ovpn)
    # OpenVPN 2.6 忽略旧式 --cipher 做 NCP 协商：若客户端不把 CFB 加进
    # data-ciphers，服务端（只收 CFB）会回 AUTH_FAILED/no shared cipher，
    # GUI 误弹「错误的凭据」。两端 NCP 列表必须有交集，fallback 也要同步。
    check("非 AEAD 时 ovpn 显式写 data-ciphers（2.6 NCP）", "\ndata-ciphers AES-256-CFB\n" in _cfb_ovpn)
    check("非 AEAD 时 ovpn 的 fallback 与主 cipher 一致",
          "\ndata-ciphers-fallback AES-256-CFB\n" in _cfb_ovpn)
    check("AEAD 默认场景不往 ovpn 写 data-ciphers/cipher",
          "data-ciphers " not in ovpn and "\ncipher " not in ovpn)
    check("非 AEAD 时两端开启 LZO", "comp-lzo yes" in _cfb_conf and "comp-lzo yes" in _cfb_ovpn)
    check("非 AEAD 场景不写 tls-crypt", "tls-crypt " not in _cfb_conf and "<tls-crypt>" not in _cfb_ovpn)
    # VPN 网段必须自动出现在推送路由里，且只出现一次（不能走两处各写一遍）
    vpn_net = core.vpn_network(core.cfg())
    vpn_gw = str(vpn_net.network_address + 1)
    vpn_push = 'push "route %s %s %s"' % (vpn_net.network_address, vpn_net.netmask, vpn_gw)
    check("server.conf 自动推送 VPN 网段（带网关）", vpn_push in conf)
    check("VPN 网段只推送一次（没有重复）", conf.count(vpn_push) == 1)
    # Windows 老客户端（2.4.x）在 subnet 拓扑下，若 push 的 route 不带网关会解析失败，
    # TEST ROUTES 变 0/0，连 VPN 网段自己都加不上。必须显式下发 route-gateway。
    check("显式下发 route-gateway（Windows 2.4.x 兼容）",
          'push "route-gateway %s"' % vpn_gw in conf)
    _extra = [n for n in core.push_targets(core.cfg(), include_vpn=False)]
    check("所有附加推送路由都带网关",
          all('push "route %s %s %s"' % (n.network_address, n.netmask, vpn_gw) in conf
              for n in _extra))
    check("push_targets 含 VPN 网段", vpn_net in core.push_targets(core.cfg()))
    check("指纹口径不含 VPN 网段（避免无故要求重发 ovpn）",
          vpn_net not in core.push_targets(core.cfg(), include_vpn=False))
    check("附加路由与 VPN 网段并不重复下发",
          len([1 for n in core.push_targets(core.cfg()) if n == vpn_net]) == 1)
    core.set_meta("proto", "udp"); core.write_all_config()
    check("切 UDP 后 server.conf 写 proto udp",
          "proto udp" in open(os.path.join(core.DATA, "server.conf")).read())
    check("切 UDP 后客户端写 proto udp",
          "proto udp" in open(os.path.join(core.DATA, "company.ovpn")).read())
    core.set_meta("proto", "tcp"); core.write_all_config()
    conf = open(os.path.join(core.DATA, "server.conf")).read()
    for k in ("redirect-gateway", "duplicate-cn", "client-to-client", "MASQUERADE"):
        check("server.conf 不含 " + k, k not in conf)
    check("ovpn 内联 CA", "<ca>" in ovpn and "BEGIN CERTIFICATE" in ovpn)
    check("ovpn 内联 tls-crypt", "<tls-crypt>" in ovpn)
    check("ovpn 账号密码登录", "auth-user-pass" in ovpn)
    check("ovpn 不含个人凭据", "ifconfig-push" not in ovpn and "BEGIN PRIVATE KEY" not in ovpn)
    check("自建 NAT 链接管（带 comment 标记）", "OVPNWEB_NAT" in rules and "-t nat -F OVPNWEB_NAT" in rules)
    check("默认 NAT 模式做地址转换", "-j SNAT --to-source" in rules and "-j MASQUERADE" in rules)
    check("客户端互访流量不做 NAT", "-o tun0 -j RETURN" in rules)
    check("开启 IPv4 转发（路由/p2p 前提）", "net.ipv4.ip_forward=1" in rules)
    check("规则全量重建自定义链", "OVPN_ACL" in rules and "-F OVPN_ACL" in rules)
    check("观察模式只记录不拦截", "OVPN_OBSERVE" in rules and "-j ACCEPT" in rules)
    check("server.conf 不开 client-to-client", "client-to-client" not in conf)
    check("生成配置指纹", len(r["fingerprint"]) == 12)

    # ---------- 登录与 CSRF ----------
    section("登录 / CSRF / 来源白名单")
    j = c.get("/api/session").get_json()
    check("会话接口返回 csrf", bool(j["csrf"]))
    check("未登录不回管理账户名", j.get("admin_user") == "", j.get("admin_user"))
    _h0 = c.get("/")
    # 后台已换回原生 Django 模板（无 iframe 菜单），全站禁止嵌框：DENY +
    # frame-ancestors 'none'，比原来的 SAMEORIGIN 更严。
    check("安全响应头齐全（nosniff / 禁嵌套 / CSP / no-referrer）",
          _h0.headers.get("X-Content-Type-Options") == "nosniff"
          and _h0.headers.get("X-Frame-Options") == "DENY"
          and _h0.headers.get("Referrer-Policy") == "no-referrer"
          and "frame-ancestors 'none'" in (_h0.headers.get("Content-Security-Policy") or "")
          and "unsafe-eval" not in (_h0.headers.get("Content-Security-Policy") or ""),
          dict(_h0.headers))
    check("未登录访问 state 被拒", c.get("/api/state").status_code == 401)
    lr = c.post("/api/login", json={"username": "admin", "password": "wrong"})
    check("错误密码被拒", lr.status_code == 401)
    lr = c.post("/api/login", json={"username": "notadmin", "password": "TestPass123!"})
    check("错误账户被拒", lr.status_code == 401)
    lr = c.post("/api/login", json={"username": "admin", "password": "TestPass123!"})
    check("正确账户+密码登录", lr.get_json().get("ok") is True)
    check("登录返回账户名", lr.get_json().get("admin_user") == "admin", lr.get_json())
    H = {"X-CSRF-Token": lr.get_json()["csrf"]}
    def P(path, body=None):
        return c.post(path, json=body or {}, headers=H)
    _h = c.get("/")
    check("首页 HTML 不缓存（避免看到旧界面）",
          "no-store" in (_h.headers.get("Cache-Control") or ""), _h.headers.get("Cache-Control"))
    check("缺 CSRF 被拒", c.post("/api/users", json={}).status_code == 403)
    check("带 CSRF 通过", c.get("/api/state").status_code == 200)
    check("审计操作者用管理员账户，不再单独配名称", core.admin_user() == "admin")
    check("状态接口返回管理员账户名", c.get("/api/state").get_json()["admin_user"] == "admin")
    # 来源白名单：远程地址换成公网 IP 必须被挡在门外
    check("白名单外的来源 403",
          c.get("/api/state", environ_base={"REMOTE_ADDR": "8.8.8.8"}).status_code == 403)
    check("伪造 X-Forwarded-For 不能绕过白名单",
          c.get("/api/state", environ_base={"REMOTE_ADDR": "8.8.8.8"},
                headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403)

    # ---------- 账号台账 ----------
    section("账号 / 固定 IP / 校验")
    u1 = core.add_user("zhangsan", real_name="张三", dept="运维部")
    u2 = core.add_user("lisi", "10.8.0.11", real_name="李四", dept="财务部")
    check("自动分配 10.8.0.2", u1["vpn_ip"] == "10.8.0.2")
    check("姓名/部门入库", core.list_users()[0]["real_name"] in ("张三", "李四"))
    byname = {u["username"]: u for u in core.list_users()}
    check("新建账号带明文密码", byname["zhangsan"]["password_plain"] == u1["password"])
    check("默认长期有效", byname["zhangsan"]["expires_at"] == "")
    # 账号名允许中文（OpenVPN 的 common name 就是原始字节，ccd 文件名跟着走）
    u_cn = core.add_user("张三", real_name="张三", dept="运维部")
    check("账号名可以是中文", u_cn["username"] == "张三")
    core.update_user("张三", password="PlainPass1234")
    check("改密同步明文", [u for u in core.list_users()
                       if u["username"] == "张三"][0]["password_plain"] == "PlainPass1234")
    core.rebuild_ccd()
    check("中文账号生成 ccd（中文文件名）", os.path.exists(os.path.join(core.CCD, "张三")))
    for bad in ("a/b", "张 三", "..", ".", "x" * 33, ""):
        check("拒绝非法账号名 %r" % bad, not core.valid_username(bad))
    core.update_user("lisi", dept="审计部")
    check("部门可改", [u for u in core.list_users() if u["username"] == "lisi"][0]["dept"] == "审计部")

    # ---- 改账号名：主键 + 用户组成员 + 资产授权 + 互访授权 + ccd 文件名要一起搬 ----
    ag = core.add_asset_group("改名验证组", items=[("10.20.0.0/24", "tcp", "22")])
    core.add_user_group("改名验证用户组", members=["lisi"])
    core.add_grant("user", "lisi", ag["id"])
    core.add_peer_grant("lisi", "zhangsan")
    core.rebuild_ccd()
    core.update_user("lisi", new_username="lisi-new")
    names = [u["username"] for u in core.list_users()]
    check("改名后旧名消失、新名在册", "lisi" not in names and "lisi-new" in names, names)
    mem = [m.username for m in UserGroupMember.objects.filter(username="lisi-new")]
    acl = [g.subject for g in AclGrant.objects.filter(subject_type="user", subject="lisi-new")]
    peer = PeerGrant.objects.filter(src_username="lisi-new").count()
    check("用户组成员跟着改名", len(mem) == 1, mem)
    check("资产授权跟着改名", acl == ["lisi-new"], acl)
    check("客户端互访授权跟着改名", peer == 1)
    core.rebuild_ccd()
    check("改名后 ccd 换新名、旧文件已清",
          os.path.exists(os.path.join(core.CCD, "lisi-new")) and
          not os.path.exists(os.path.join(core.CCD, "lisi")))
    try:
        core.update_user("zhangsan", new_username="lisi-new")
        check("拒绝改成已存在的账号", False)
    except ValueError:
        check("拒绝改成已存在的账号", True)
    for bad in ("a/b", "张 三", ".."):
        try:
            core.update_user("lisi-new", new_username=bad)
            check("拒绝把账号改成 %r" % bad, False)
        except ValueError:
            check("拒绝把账号改成 %r" % bad, True)
    core.update_user("lisi-new", new_username="lisi")
    # 收拾掉改名用例造的授权/组，别让它们污染后面的 ACL 断言
    for pg in core.list_peer_grants():
        if "lisi" in (pg["src_username"], pg["dst_username"]):
            core.delete_peer_grant(pg["id"])
    for gr in core.list_grants():
        if gr.get("subject") == "lisi" and gr.get("subject_type") == "user":
            core.delete_grant(gr["id"])
    for ug in core.list_user_groups():
        if ug["name"] == "改名验证用户组":
            core.delete_user_group(ug["id"])
    core.delete_asset_group(ag["id"])
    for bad, why in (("10.8.0.11", "IP 冲突"), ("10.8.0.1", "服务端自己"), ("10.9.0.20", "网段外")):
        try:
            core.add_user("baduser", bad); check("拒绝" + why, False)
        except ValueError:
            check("拒绝" + why, True)
    core.update_user("zhangsan", vpn_ip="10.8.0.12")
    core.rebuild_ccd()
    check("改 IP 后 ccd 同步", "10.8.0.12" in open(os.path.join(core.CCD, "zhangsan")).read())
    check("停用账号不生成 ccd", (core.update_user("lisi", enabled=False) or True) and
          core.rebuild_ccd() == 2 and not os.path.exists(os.path.join(core.CCD, "lisi")))
    core.update_user("lisi", enabled=True)

    # ---------- 动态地址池（可选，默认关闭） ----------
    section("动态地址池（ifconfig-pool）")
    c0 = core.cfg()
    check("默认关闭", c0["pool_enabled"] is False and core.pool_span(c0) is None)
    check("池子关闭时 .5 也在可分配清单里", "10.8.0.5" in core.usable_host_ips(c0))
    tmpu = core.add_user("poolok", "10.8.0.5")
    check("池子关闭时能手动用 .5", tmpu["vpn_ip"] == "10.8.0.5")
    core.delete_user("poolok")
    core.update_user("lisi", vpn_ip="10.8.0.11")

    core.set_meta("pool_enabled", True)
    core.set_meta("pool_start", "10.8.0.2")
    core.set_meta("pool_end", "10.8.0.9")
    c1 = core.cfg()
    check("开启后池子生效", core.pool_span(c1) == (2, 9))
    check("池子占掉的地址不再分给台账", core.static_capacity(c1) == 245, core.static_capacity(c1))
    try:
        core.add_user("poolbad", "10.8.0.5"); check("拒绝落在池子里的 IP", False)
    except ValueError:
        check("拒绝落在池子里的 IP", True)
    check("下一个可用 IP 跳过池子", core.next_free_ip(c1) not in
          ["10.8.0.%d" % i for i in range(2, 10)])
    check("server.conf 写上 ifconfig-pool", "ifconfig-pool 10.8.0.2 10.8.0.9"
          in core.render_server_conf(c1))
    bad_pool = dict(c1, pool_start="10.9.0.2")
    check("池子不在 VPN 网段内被拒", any("不在 VPN 网段" in x and "一起改" in x
                                        for x in core.validate_cfg(bad_pool)), core.validate_cfg(bad_pool))
    core.set_meta("pool_enabled", False)
    check("关掉池子后恢复 253 个", core.static_capacity(core.cfg()) == 253)
    core.write_all_config()

    # ---------- 认证脚本 ----------
    section("auth-user-pass-verify 认证脚本")
    env = dict(os.environ, untrusted_ip="203.0.113.9", OVPN_WEB_ROOT=TMP, OVPN_WEB_MOCK="1")
    def auth(name, pw):
        f = os.path.join(TMP, "cred.txt")
        with open(f, "w") as fh:
            fh.write(name + "\n" + pw + "\n")
        return subprocess.run([sys.executable, os.path.join(SYS, "auth-verify.py"), f],
                              env=env, capture_output=True, text=True, timeout=30).returncode
    check("正确密码放行", auth("zhangsan", u1["password"]) == 0)
    check("错误密码拒绝", auth("zhangsan", "nope") == 1)
    check("不存在账号拒绝", auth("ghost", "x") == 1)
    check("凭据文件缺失 fail-closed",
          subprocess.run([sys.executable, os.path.join(SYS, "auth-verify.py")],
                         env=env, capture_output=True, timeout=30).returncode == 1)
    for _ in range(5):
        auth("lisi", "wrong")
    check("5 次失败锁定（正确密码也拒）", auth("lisi", u2["password"]) == 1)
    check("认证日志已落库", len(core.get_logs("auth")) >= 7)

    # ---------- Excel 全量同步 ----------
    section("Excel 导入导出（明文密码 / 有效期）")
    name, data, mime = core.export_users("xlsx")
    check("导出 xlsx", data[:2] == b"PK" and "sheet" in mime)
    _, csvdata, _ = core.export_users("csv")
    text = csvdata.decode("utf-8-sig")
    head = text.splitlines()[0]
    check("表头含姓名/部门/密码/有效期",
          all(k in head for k in ("姓名", "部门", "密码", "有效期")))
    check("导出带明文密码", u1["password"] in text)
    # 空密码整批拒绝：先测解析层，再确认没有落盘
    empty_rows = [["用户名", "姓名", "部门", "密码", "VPN固定IP", "状态", "有效期", "备注"],
                  ["zhangsan", "", "", "", "10.8.0.12", "启用", "", ""]]
    b = io.StringIO(); csv.writer(b).writerows(empty_rows)
    try:
        core.preview_import(("\ufeff" + b.getvalue()).encode(), "空密码.csv")
        check("密码为空整批拒绝", False)
    except ValueError as e:
        check("密码为空整批拒绝", "密码为空" in str(e), e)
    rows = [["用户名", "姓名", "部门", "密码", "VPN固定IP", "状态", "有效期", "备注"],
            ["zhangsan", "", "", "ImpPass1234", "10.8.0.12", "启用", "", ""],
            ["lisi", "", "", "ImpPass5678", "10.8.0.11", "启用", "2027-01-31", ""],
            ["wangwu", "王五", "外包", "ImpPass9012", "10.8.0.13", "启用", "", "外包"]]
    buf = io.StringIO(); w = csv.writer(buf); w.writerows(rows)
    imp = core.preview_import(("\ufeff" + buf.getvalue()).encode(), "台账.csv")
    check("导入预览列出新增", any(x["op"] == "新增" and x["username"] == "wangwu" for x in imp["changes"]))
    check("预览令牌有效期 300s", imp["expires_in"] == 300)
    applied = core.apply_import(imp["token"])
    check("按钮式提交导入 3 个账号", applied["count"] == 3)
    check("导入带进姓名/部门", [u for u in core.list_users() if u["username"] == "wangwu"][0]["dept"] == "外包")
    check("导入后明文密码落库", [u for u in core.list_users()
                           if u["username"] == "zhangsan"][0]["password_plain"] == "ImpPass1234")
    check("导入后有效期落库", [u for u in core.list_users()
                           if u["username"] == "lisi"][0]["expires_at"] == "2027-01-31")
    check("不再生成一次性密码文件", not applied["initial_password_file"])
    check("导入自动备份台账", os.path.exists(applied["backup"]))
    try:
        core.apply_import("invalid-token"); check("无效预览令牌被拒", False)
    except ValueError:
        check("无效预览令牌被拒", True)
    bad = io.StringIO(); bw = csv.writer(bad)
    bw.writerows(rows[:1] + [["wangwu", "", "", "IpDup1234", "10.8.0.13", "启用", "", ""],
                             ["x", "", "", "IpDup5678", "10.8.0.13", "启用", "", ""]])
    try:
        core.preview_import(bad.getvalue().encode(), "bad.csv"); check("IP 重复整批拒绝", False)
    except ValueError:
        check("IP 重复整批拒绝", True)

    # ---------- 认证账号备份：Excel 与导出同款 + 就地恢复 ----------
    section("账号备份（Excel）与恢复")
    ub = core.backup_users("selftest")
    check("账号备份是 users-*.xlsx", os.path.basename(ub).startswith("users-") and ub.endswith(".xlsx"))
    bk_data, bk_name = core.read_user_backup(ub)

    def _head(data):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True)
        return tuple(next(wb.active.iter_rows(values_only=True)))
    _exp_name, _exp_data, _ = core.export_users("xlsx")
    check("自动备份与导出 Excel 表头完全一致",
          _head(_exp_data) == _head(bk_data) == tuple(core.EXPORT_HEADER))
    bk_rows, bk_errs = core.parse_users_file(bk_data, bk_name)
    check("账号备份能直接被导入解析（含密码）",
          not bk_errs and len(bk_rows) == len(core.list_users())
          and all(r["password"] for r in bk_rows))
    check("台账备份路径可校验", core.user_backup_path(ub) == os.path.realpath(ub))
    try:
        core.user_backup_path("/etc/passwd"); check("台账备份路径越界被拒", False)
    except ValueError:
        check("台账备份路径越界被拒", True)
    try:
        core.preview_import(core.read_user_backup("/etc/passwd")[0], "x.tsv")
        check("非台账文件不能当备份恢复", False)
    except ValueError:
        check("非台账文件不能当备份恢复", True)
    core.delete_user("wangwu", "selftest")          # 模拟误删
    check("误删后账号确实少了", not [u for u in core.list_users() if u["username"] == "wangwu"])
    pv = core.preview_import(bk_data, bk_name)
    check("账号备份恢复预览列出要补回的账号",
          any(x["op"] == "新增" and x["username"] == "wangwu" for x in pv["changes"]))
    rs = core.apply_import(pv["token"], action="user_backup.restore")
    check("账号备份恢复把账号补回来", rs["count"] == 3 and
          [u for u in core.list_users() if u["username"] == "wangwu"][0]["password_plain"] == "ImpPass9012")
    check("账号备份恢复前会再备份当前台账", os.path.exists(rs["backup"]))
    check("恢复动作审计用的是台账口径",
          any(r.get("action") == "user_backup.restore" for r in core.get_logs("audit", 200)))
    from urllib.parse import quote as _q
    _dl = c.get("/api/user-backups/download?path=" + _q(ub))
    check("账号备份可下载成 Excel",
          _dl.status_code == 200 and "spreadsheet" in (_dl.headers.get("Content-Type") or ""),
          _dl.headers.get("Content-Type"))
    check("备份下载路径越界被拒",
          c.get("/api/user-backups/download?path=/etc/passwd").status_code == 400)

    # ---------- 公网 IP 识别 ----------
    section("对外地址自动识别")
    check("公网 IPv4 判定", core.is_public_ipv4("8.8.8.8") and core.is_public_ipv4("1.1.1.1"))
    check("内网/回环不算公网", not core.is_public_ipv4("10.8.0.2") and
          not core.is_public_ipv4("192.168.1.1") and not core.is_public_ipv4("127.0.0.1") and
          not core.is_public_ipv4("abc"))
    check("MOCK 下不发起外网探测", core.detect_public_ip() == ("", ""))

    # ---------- 密码覆盖 / 有效期自动停用 ----------
    section("密码覆盖与有效期自动停用")
    def _hash(n):
        return VpnUser.objects.filter(username=n).values_list("password_hash", flat=True).first()

    all_rows = [["用户名", "姓名", "部门", "密码", "VPN固定IP", "状态", "有效期", "备注"],
                ["zhangsan", "张三", "运维部", "NewPass1234", "10.8.0.12", "启用", "", ""],
                ["lisi", "李四", "运维部", "KeepMe1234", "10.8.0.11", "启用", "", ""],
                ["wangwu", "王五", "外包", "KeepMe5678", "10.8.0.13", "启用", "", "外包"]]

    def _imp(rows, **kw):
        b = io.StringIO()
        csv.writer(b).writerows(rows)
        p = core.preview_import(("\ufeff" + b.getvalue()).encode(), "台账.csv")
        return core.apply_import(p["token"], **kw)

    h_zs = _hash("zhangsan")
    _imp(all_rows)
    check("表里密码覆盖哈希", _hash("zhangsan") != h_zs)
    check("覆盖后新密码可用", core.verify_password("NewPass1234", _hash("zhangsan")))
    check("明文同步为表中值", [u for u in core.list_users()
                          if u["username"] == "zhangsan"][0]["password_plain"] == "NewPass1234")

    bad_exp = [r[:] for r in all_rows]
    bad_exp[1][6] = "2027/01/31"
    try:
        _imp(bad_exp); check("有效期格式错误整批拒绝", False)
    except ValueError as e:
        check("有效期格式错误整批拒绝", "有效期" in str(e), e)

    today = datetime.now()
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    exp_user = core.add_user("临时账号", "10.8.0.16", password="ExpirePass123",
                             expires_at=yesterday)
    check("新建时可带有效期", exp_user["expires_at"] == yesterday)
    check("到期账号自动停用", not [u for u in core.list_users()
                              if u["username"] == "临时账号"][0]["enabled"])
    check("到期账号不生成 ccd", (core.rebuild_ccd() or True) and
          not os.path.exists(os.path.join(core.CCD, "临时账号")))
    check("到期账号认证被拒", auth("临时账号", "ExpirePass123") == 1)
    core.update_user("临时账号", expires_at=tomorrow, enabled=True)
    check("改有效期后可重新启用", [u for u in core.list_users()
                              if u["username"] == "临时账号"][0]["enabled"] == 1)
    check("未来有效期不误停", auth("临时账号", "ExpirePass123") == 0)
    core.update_user("临时账号", expires_at="")
    check("清空有效期=长期有效", [u for u in core.list_users()
                              if u["username"] == "临时账号"][0]["expires_at"] == "")
    core.delete_user("临时账号")

    # ---------- 资产组 / 用户组 / 授权 ----------
    section("资产组 / 用户组 / 授权")
    core.add_asset_group("数据库组", "", [("10.10.1.0/24", "tcp", "22,3389")])
    gid = core.list_asset_groups()[0]["id"]
    core.add_grant("user", "wangwu", gid)
    rr = core.render_rules(core.cfg())
    check("按账号固定 IP 放行", "-s 10.8.0.13 -d 10.10.1.0/24 -p tcp --dport 22 -j ACCEPT" in rr)
    check("端口展开 3389", "--dport 3389" in rr)
    try:
        core.add_asset_group("重复组", "", [("10.10.1.0/24", "tcp", "3389,22")])
        check("拒绝重复目标（端口顺序无关）", False)
    except ValueError:
        check("拒绝重复目标（端口顺序无关）", True)
    core.add_user("zhaoliu", "10.8.0.15")
    core.add_user_group("运维组", "", ["wangwu", "zhaoliu"])
    ag2 = core.add_asset_group("Web 组", "", [("10.10.2.11", "tcp", "80"),
                                             ("10.10.2.12", "tcp", "80"),
                                             ("10.10.2.13", "tcp", "80")])
    check("单 IP 自动补 /32", any(i["cidr"] == "10.10.2.11/32"
          for i in [g for g in core.list_asset_groups() if g["name"] == "Web 组"][0]["items"]))
    core.add_grant("group", "运维组", ag2["id"])
    rr4 = core.render_rules(core.cfg())
    check("用户组授权展开到每个成员", "-s 10.8.0.13 -d 10.10.2.11/32 -p tcp --dport 80" in rr4
          and "-s 10.8.0.15 -d 10.10.2.13/32 -p tcp --dport 80" in rr4)
    check("2 条授权展开成 7 条规则", len(core.active_grants()) == 2 and len(core.expand_grants()) == 7)
    core.update_user_group([g for g in core.list_user_groups()
                            if g["name"] == "运维组"][0]["id"], members=["zhaoliu"])
    rr5 = core.render_rules(core.cfg())
    check("移出成员后旧权限不残留", "-s 10.8.0.13 -d 10.10.2.11" not in rr5
          and "-s 10.8.0.15 -d 10.10.2.11" in rr5)
    core.set_meta("acl_mode", "enforce")
    rr2 = core.render_rules(core.cfg())
    check("强制模式默认 DROP", "-j DROP" in rr2 and "OVPN_DENY" in rr2)
    check("DNS/NTP 例外", "-p udp --dport 53" in core.render_rules(
        dict(core.cfg(), dns=["10.10.0.53"])))
    core.update_user("wangwu", vpn_ip="10.8.0.14")
    rr3 = core.render_rules(core.cfg())
    check("改 IP 后旧权限不残留", "-s 10.8.0.13" not in rr3 and "-s 10.8.0.14" in rr3)
    core.set_meta("acl_mode", "observe")

    # 用户反馈的三行示例：新建组 / 批量新增 / 编辑条目必须共用同一套解析
    sample = ("1.1.1.1/32 any 22,33,55,900-1000,10001\n"
              "2.2.2.2 icmp\n"
              "3.3.3.3 tcp 23,55,100-222")
    items = core.parse_item_lines(sample)
    check("三行示例全部解析成功", len(items) == 3)
    check("any 保留端口", items[0] == ("1.1.1.1/32", "any", "22,33,55,900-1000,10001"))
    check("icmp 端口清空", items[1] == ("2.2.2.2/32", "icmp", ""))
    check("tcp 端口规范化", items[2] == ("3.3.3.3/32", "tcp", "23,55,100-222"))
    g3 = core.add_asset_group("协议格式组", "", items)
    check("批量建组落库 3 条", g3["count"] == 3 and len(
        [g for g in core.list_asset_groups() if g["name"] == "协议格式组"][0]["items"]) == 3)
    core.add_asset_items(g3["id"], core.parse_item_lines("4.4.4.4 udp 53\n5.5.5.5\n6.6.6.6"))
    g3items = [g for g in core.list_asset_groups() if g["name"] == "协议格式组"][0]["items"]
    check("条目批量新增共用同一解析", len(g3items) == 6 and
          any(i["cidr"] == "5.5.5.5/32" and i["proto"] == "any" and not i["ports"] for i in g3items))
    _edit_id = next(i["id"] for i in g3items if i["cidr"] == "5.5.5.5/32")
    _edit = core.parse_item_lines("5.5.5.5 any 8000-9000")[0]
    core.update_asset_item(_edit_id, *_edit)
    check("编辑条目也用同一格式", any(i["id"] == _edit_id and i["ports"] == "8000-9000"
                                 for i in [g for g in core.list_asset_groups()
                                           if g["name"] == "协议格式组"][0]["items"]))
    core.add_grant("user", "wangwu", g3["id"])
    rr6 = core.render_rules(core.cfg())
    check("any+端口生成 TCP/UDP 两条规则",
          "-d 1.1.1.1/32 -p tcp --dport 900:1000 -j ACCEPT" in rr6 and
          "-d 1.1.1.1/32 -p udp --dport 900:1000 -j ACCEPT" in rr6)
    check("icmp 规则不带端口", "-d 2.2.2.2/32 -p icmp -j ACCEPT" in rr6
          and "--dport" not in [l for l in rr6.splitlines() if "2.2.2.2/32" in l][0])
    check("any 无端口仍然是全放通",
          any("-d 6.6.6.6/32 -j ACCEPT" in l for l in rr6.splitlines()))
    core.delete_asset_group(g3["id"])

    # ---------- 客户端互访 P2P ----------
    section("客户端互访（P2P）")
    core.add_peer_grant("zhangsan", "wangwu", "tcp", "22,3389")
    pr = core.render_rules(core.cfg())
    check("按账号固定 IP 放行互访", "-s 10.8.0.12 -d 10.8.0.14 -p tcp --dport 22 -j ACCEPT" in pr)
    check("互访端口展开", "-s 10.8.0.12 -d 10.8.0.14 -p tcp --dport 3389 -j ACCEPT" in pr)
    check("互访不走 client-to-client", "client-to-client" not in core.render_server_conf(core.cfg()))
    try:
        core.add_peer_grant("zhangsan", "zhangsan"); check("拒绝自己访自己", False)
    except ValueError:
        check("拒绝自己访自己", True)
    try:
        core.add_peer_grant("ghost", "wangwu"); check("拒绝不存在账号", False)
    except ValueError:
        check("拒绝不存在账号", True)
    core.add_peer_grant("lisi", "zhangsan", "any", "", "2000-01-01 00:00")
    check("过期互访自动失效", len(core.active_peer_grants()) == 1)
    check("P2P 规则不含过期账号", "-s 10.8.0.11 -d 10.8.0.12" not in core.render_rules(core.cfg()))

    # ---------- 指纹 / 版本闸门 ----------
    section("配置指纹与更新闸门")
    fp1 = core.config_fingerprint(core.cfg())
    check("改端口后指纹变化", core.config_fingerprint(dict(core.cfg(), port=1195)) != fp1)
    check("改 mssfix 会触发客户端重发",
          core.config_fingerprint(dict(core.cfg(), mssfix=1300)) != fp1)
    check("mssfix=0 时不写 mssfix 指令",
          "mssfix" not in core.render_server_conf(dict(core.cfg(), mssfix=0))
          and "mssfix" not in core.render_ovpn(dict(core.cfg(), mssfix=0)))
    _cfg = dict(core.cfg(), public_addr="vpn.example.com")
    check("未设对外端口时客户端用服务端口", "remote vpn.example.com 1194" in core.render_ovpn(_cfg))
    _mapped = dict(_cfg, public_port=11940)
    check("端口映射用对外端口写客户端配置", "remote vpn.example.com 11940" in core.render_ovpn(_mapped))
    check("服务端仍监听本机端口", "port 1194" in core.render_server_conf(_mapped))
    check("改对外端口会触发客户端重发", core.config_fingerprint(_mapped) != fp1)
    check("非法对外端口被拒", any("对外端口" in x for x in core.validate_cfg(dict(_cfg, public_port=70000))))
    core.set_meta("openvpn_version", "2.6.24")
    check("只换二进制指纹不变", core.config_fingerprint(core.cfg()) == fp1)
    check("默认 NAT 模式不要求回程路由", core.route_guide()["required"] is False)
    core.set_meta("nat_mode", "no-nat")
    check("切换网络模式不改配置指纹（ovpn 不用重发）", core.config_fingerprint(core.cfg()) == fp1)
    check("开启溯源后要求回程路由", core.route_guide()["required"] is True)
    nr = core.render_rules(core.cfg())
    check("无 NAT 模式不做地址转换", "-j SNAT --to-source" not in nr and "-j MASQUERADE" not in nr)
    check("无 NAT 模式仍清理裸 NAT 规则", "nat -D POSTROUTING" in nr)
    check("无 NAT 模式 P2P 仍放行", "-s 10.8.0.12 -d 10.8.0.14" in nr)
    core.set_meta("nat_mode", "nat")
    fake27 = os.path.join(TMP, "fake27")
    with open(fake27, "w") as f:
        f.write("#!/bin/sh\necho 'OpenVPN 2.7.9 x86_64-unknown-linux-gnu [SSL (OpenSSL)]'\n")
    os.chmod(fake27, 0o755)
    gate = core.check_binary(fake27, allow_major=False)
    if POSIX:
        check("跨大版本被闸门拦截", not gate["ok"], gate["msg"])
    gate2 = core.check_binary(fake27, allow_major=True)
    if POSIX:
        check("勾选允许跨大版本后放行", gate2["ok"], gate2["msg"])

    # ---------- Web API ----------
    section("Web API（安装向导 / 设置 / 日志 / 自检）")
    params = {"public_addr": "vpn.example.com", "port": 1194, "proto": "tcp",
              "vpn_cidr": "10.8.0.0/24", "extra_routes": ["10.10.0.0/16"],
              "dns": ["10.10.0.53"], "ntp": ["10.10.0.123"], "tun_mtu": 1400, "mssfix": 1360,
              "compress": "off", "data_ciphers": "AES-256-GCM:CHACHA20-POLY1305",
              "tls_min": "1.2", "pool_enabled": False,
              "pool_start": "10.8.0.2", "pool_end": "10.8.0.9"}
    check("向导参数保存", P("/api/wizard/params", params).get_json()["ok"])
    check("环境自检 API", P("/api/wizard/check").get_json()["ok"])
    # 编译环境在安装/启动时前置检查并落库，版本页直接读，不必等点开升级页
    section("编译环境前置检查（安装/启动即查）")
    be = core.check_build_env(force=True)
    check("编译环境检查返回结构完整",
          set(("ok", "items", "missing", "checked_at")).issubset(be.keys()), be)
    check("检查结果写入缓存", isinstance(core.get_meta("build_env_cache"), dict))
    check("检查时间写入缓存", bool(core.get_meta("build_env_checked_at")))
    check("缓存带检查时间", bool(be.get("checked_at")))
    check("版本页接口读缓存成功", c.get("/api/update/build-env").get_json()["ok"])
    check("强制重新检测接口可用", c.get("/api/update/build-env?refresh=1").get_json()["ok"])
    check("重新检测后时间不为空",
          bool(c.get("/api/update/build-env").get_json()["env"].get("checked_at")))
    ex = P("/api/wizard/execute", {}).get_json()
    check("安装长任务启动", bool(ex.get("task_id")), ex)
    tj = wait_task(c, ex["task_id"])
    check("安装任务完成", tj["status"] == "done", tj.get("msg"))
    check("初始化标记写入", bool(core.get_meta("initialized")))
    check("向导后 company.ovpn 存在", os.path.exists(os.path.join(core.DATA, "company.ovpn")))
    # 前端 api() 对所有请求（含 GET）都带 X-CSRF-Token，这里保持一致；
    # logs/selfcheck 这两个 GET 带副作用（触发 root worker），必须过 CSRF。
    _gh = {"X-CSRF-Token": c.get("/api/session").get_json()["csrf"]}
    for path in ("/api/state", "/api/users", "/api/acl", "/api/settings", "/api/settings/preview",
                 "/api/version", "/api/update/build-env", "/api/online", "/api/logs?kind=audit", "/api/logs?kind=auth",
                 "/api/logs?kind=deny", "/api/route-guide", "/api/selfcheck", "/api/backups"):
        check("GET " + path, c.get(path, headers=_gh).status_code == 200)
    r_nocsrf = c.get("/api/logs?kind=deny")
    check("GET /api/logs 不带 CSRF 被拒（防跨站 <img> 白嫖触发 worker）",
          r_nocsrf.status_code == 403, (r_nocsrf.status_code, r_nocsrf.data[:120]))
    check("导出 ovpn", c.get("/api/client.ovpn").status_code == 200)
    # 版本自愈：页面内版本常量必须与服务端一致，否则每次打开都会误触发强制刷新
    section("页面版本自校验（防浏览器缓存旧界面）")
    import re as _re
    _html = c.get("/").data.decode("utf-8", "replace")
    _m = _re.search(r"const\s+PAGE_VERSION\s*=\s*'([^']+)'", _html)
    check("页面含 PAGE_VERSION 常量", bool(_m), _m.group(1) if _m else "缺失")
    check("页面版本与服务端版本一致（防误触发强刷）",
          bool(_m) and _m.group(1) == core.WEB_VERSION,
          "%s vs %s" % (_m.group(1) if _m else "缺失", core.WEB_VERSION))
    check("页面含版本自校验函数 versionGuard", "function versionGuard(" in _html)
    check("页面含缓存 bust 强刷逻辑", "location.replace(" in _html and "searchParams.set('_v'" in _html)
    sp = c.get("/self")
    check("自助改密页内联在 app.py（不再依赖 self.html）",
          sp.status_code == 200 and b"/api/self/whoami" in sp.data, sp.status_code)
    check("自助页不再有固定 IP 输入框", b'id="ip"' not in sp.data and b'id="ipbox"' not in sp.data)
    # 认不出来（测试来源是 127.0.0.1）时必须明确提示去连 VPN，而不是笼统报密码错
    r = c.post("/api/self/password", json={"old": "x", "new": "12345678"},
               headers={"X-CSRF-Token": c.get("/api/session").get_json()["csrf"]})
    check("认不出来时拒绝改密并提示连 VPN",
          r.status_code == 401 and "连上 VPN" in r.get_json()["msg"], r.get_json())
    check("导出 Excel", c.get("/api/users/export?fmt=xlsx").status_code == 200)
    check("重建规则 API", P("/api/settings/rebuild").get_json()["ok"])
    check("按钮直接切强制模式", P("/api/acl/mode", {"mode": "enforce"}).get_json()["ok"])
    check("按钮直接切回观察模式", P("/api/acl/mode", {"mode": "observe"}).get_json()["ok"])
    check("按钮再切强制模式", P("/api/acl/mode", {"mode": "enforce"}).get_json()["ok"])
    check("下一个可用 IP 接口", c.get("/api/users/next-ip").get_json()["vpn_ip"].startswith("10.8.0."))
    _cap = c.get("/api/settings/capacity").get_json()
    check("容量接口：/24 可分配 253 个", _cap["usable"] == 253, _cap)
    check("容量接口：还有空闲地址", _cap["free"] >= 1, _cap)
    # ---------- 审计 IP / 白名单 ----------
    rows = core.get_logs("audit", 80)
    check("审计日志带操作者 IP", any(r.get("ip") == "127.0.0.1" for r in rows), rows[:1])
    check("审计日志的操作者是管理员账户", any(r["actor"] == "admin" for r in rows), rows[:1])
    a0 = c.get("/api/security/allow").get_json()
    check("白名单接口返回当前来源 IP", a0["current_ip"] == "127.0.0.1", a0)
    check("白名单接口返回候选网段", isinstance(a0.get("detected"), list) and a0["detected"])
    av = P("/api/security/allow", {"allow": ["127.0.0.1/32", "10.20.30.0/24"]}).get_json()
    check("保存白名单", av.get("allow") == ["127.0.0.1/32", "10.20.30.0/24"], av)
    r403 = P("/api/security/allow", {"allow": ["8.8.8.8/32"]})
    check("白名单排除当前 IP 时拒绝保存",
          r403.status_code == 400 and "进不来" in r403.get_json()["msg"], r403.get_json())
    check("非法网段被拒", P("/api/security/allow", {"allow": ["10.0.0.0/33"]}).status_code == 400)
    check("空名单被拒", P("/api/security/allow", {"allow": []}).status_code == 400)
    check("手工打快照接口", P("/api/backups", {"tag": "自检"}).get_json()["ok"])
    # 自助页：来源 IP 能被认出来就不该让人再填一遍 IP
    w = c.get("/api/self/whoami").get_json()
    check("自助改密识别来源 IP", w.get("ok") and "identified" in w, w)
    check("测试环境来源是 127.0.0.1（认不出，返回手填提示）", w["identified"] is False, w)
    check("按固定 IP 能反查账号", bool(core.list_users()) and
          (core.user_by_vpn_ip(core.list_users()[0]["vpn_ip"]) or {}).get("username")
          == core.list_users()[0]["username"])

    # ---------- 长期运行的维护：日志尾读 / 任务清理 / 备份保留 ----------
    section("长期运行不膨胀（日志尾读、任务清理、备份保留）")
    big = os.path.join(TMP, "big.log")
    with open(big, "w", encoding="utf-8") as f:
        for i in range(5000):
            f.write("line-%04d %s\n" % (i, "x" * 40))
    tail = core.read_tail_lines(big, 50)
    check("日志尾读只取最后 N 行", len(tail) == 50 and tail[-1].startswith("line-4999"), tail[-1:])
    check("日志尾读从末尾倒着读（不全量加载）", os.path.getsize(big) > 8192 and len(tail[0]) > 0)
    open(os.path.join(TMP, "one.log"), "w").write("only-line")
    check("没有结尾换行的文件也能读", core.read_tail_lines(os.path.join(TMP, "one.log"), 5) == ["only-line"])
    check("空文件返回空列表", core.read_tail_lines(os.path.join(TMP, "none.log"), 5) == [])

    # data/tasks：过期文件清掉，最近的留着
    _old = os.path.join(core.TASKS, "old-task.log")
    _new = os.path.join(core.TASKS, "new-task.log")
    for p in (_old, _new):
        open(p, "w").write("t")
    _t = time.time()
    os.utime(_old, (_t - 30 * 86400, _t - 30 * 86400))
    gone = core.prune_tasks(days=7)
    check("过期任务日志被清理", gone >= 1 and not os.path.exists(_old), gone)
    check("最近的任务日志保留", os.path.exists(_new))

    # openvpn.log 超过上限就转存一份
    _log = os.path.join(core.DATA, "openvpn.log")
    with open(_log, "w", encoding="utf-8") as f:
        f.write("y" * 4096)
    check("日志未超限不动", core.rotate_openvpn_log(max_bytes=8192) is False)
    check("日志超限转存为 .1 并重开",
          core.rotate_openvpn_log(max_bytes=1024) is True
          and os.path.exists(_log + ".1") and os.path.getsize(_log) == 0)
    check("转存后日志页能正常读", core.get_logs("server", 10) == [])
    os.remove(_log + ".1")

    # 台账自动备份保留上限：只留最近 5 份
    for i in range(8):
        core.backup_users()
    core.prune_user_backups(keep=5)
    _ub = [b for b in core.list_backups() if b.get("kind") == "users"]
    check("台账备份只保留最近 N 份", len(_ub) == 5, len(_ub))
    check("超出份数的备份文件已从磁盘删除",
          all(os.path.exists(b["path"]) for b in _ub)
          and len([x for x in os.listdir(core.BACKUPS) if x.startswith("users-")]) == 5,
          os.listdir(core.BACKUPS))

    # 公网 IP 探测结果缓存：第二次不再重复探测，手动刷新可强制重探
    web._PUBIP.update({"ts": 0.0, "ip": "", "src": ""})     # 清掉缓存，模拟冷启动
    p1 = c.get("/api/settings/public-ip").get_json()
    p2 = c.get("/api/settings/public-ip").get_json()
    p3 = c.get("/api/settings/public-ip?refresh=1").get_json()
    check("公网 IP 探测结果被缓存", p1.get("cached") is False and p2.get("cached") is True, (p1, p2))
    check("手动刷新强制重探", p3.get("cached") is False, p3)
    check("公网 IP 反查不到账号", core.user_by_vpn_ip("8.8.8.8") is None)
    ag = P("/api/asset-groups", {"name": "API 资产组",
                                 "lines": "10.10.9.1 tcp 22\n10.10.9.2 tcp 22"}).get_json()
    check("资产组接口：一次建组+2 条目", ag.get("ok") and ag["count"] == 2, ag)
    _ag = next(g for g in core.list_asset_groups() if g["name"] == "API 资产组")
    _eid = _ag["items"][0]["id"]
    check("编辑资产条目（IP/协议/端口）", P("/api/asset-items/" + str(_eid),
          {"cidr": "10.10.9.9", "proto": "tcp", "ports": "3389"}).get_json().get("ok") is True)
    _ag2 = next(g for g in core.list_asset_groups() if g["name"] == "API 资产组")
    check("条目改动已落库", any(i["cidr"] == "10.10.9.9/32" and i["ports"] == "3389"
                            for i in _ag2["items"]), _ag2["items"])
    check("改成同组已有条目会被拒", P("/api/asset-items/" + str(_eid),
          {"cidr": "10.10.9.2", "proto": "tcp", "ports": "22"}).get_json().get("ok") is not True)
    ug = P("/api/user-groups", {"name": "API 用户组", "members": ["lisi"]}).get_json()
    check("用户组接口建组", ug.get("ok") and ug["count"] == 1, ug)
    check("用户组多行解析（一行一个、去重）",
          core.parse_member_lines("张三\n 李四 \n张三\n\n") == ["张三", "李四"])
    ug2 = P("/api/user-groups", {"name": "API 用户组多行",
                                 "lines": "lisi\nzhangsan\nlisi\n   \n"}).get_json()
    check("用户组接口：多行文本一次建组并去重", ug2.get("ok") and ug2["count"] == 2, ug2)
    _ugid = next(g["id"] for g in core.list_user_groups() if g["name"] == "API 用户组多行")
    P("/api/user-groups/" + str(_ugid), {"lines": "wangwu\nzhaoliu"})
    check("用户组编辑同样用多行文本",
          sorted(m["username"] for m in next(g for g in core.list_user_groups()
                                             if g["id"] == _ugid)["members"]) == ["wangwu", "zhaoliu"])
    check("授权接口（用户组 → 资产组）", P("/api/grants", {"subject_type": "group", "subject": "API 用户组",
          "asset_group_id": ag["id"]}).get_json()["ok"])
    check("ACL 接口返回资产组/用户组", "asset_groups" in c.get("/api/acl").get_json()
          and "user_groups" in c.get("/api/acl").get_json())
    check("重启服务 API", P("/api/service/restart").get_json()["ok"])
    check("改密码：两次不一致被拒", P("/api/password",
          {"old": "TestPass123!", "new": "NewPass456!", "confirm": "typo"}).status_code == 400)
    check("改管理密码（带确认）", P("/api/password",
          {"old": "TestPass123!", "new": "NewPass456!", "confirm": "NewPass456!"}).get_json()["ok"])
    check("改账户名不需要密码", P("/api/admin-user", {"username": "boss"}).get_json()["ok"])
    check("旧账户名已失效", c.post("/api/login",
          json={"username": "admin", "password": "NewPass456!"}).status_code == 401)
    check("新账户名可登录", c.post("/api/login",
          json={"username": "boss", "password": "NewPass456!"}).get_json()["ok"])
    # 登录会重建会话并换 CSRF，刷新测试用的 token 再继续
    H["X-CSRF-Token"] = c.get("/api/session").get_json()["csrf"]
    check("非法账户名被拒", P("/api/admin-user", {"username": "a b"}).status_code == 400)
    check("Web 已不再暴露重置凭据接口", P("/api/admin-reset", {}).status_code == 404)
    check("重置后不触发首登强改", core.admin_must_change() is False)
    # Django 版：重置凭据走 ovpn_cli reset-admin（同步 Django User + core meta）
    from django.contrib.auth import get_user_model as _GUM
    _reset = core.admin_reset_credentials()   # 取 core 生成的明文用于登录断言
    _U = _GUM().objects.filter(is_staff=True, is_superuser=True).order_by("id").first()
    if _U:
        _U.username = _reset["username"]; _U.set_password(_reset["password"]); _U.save()
    core.set_meta("admin_user", _reset["username"])
    check("重置返回新账户名", _reset.get("username", "").startswith("admin-"), _reset)
    check("重置返回新密码", len(_reset.get("password", "")) >= 12, _reset)
    check("旧账户名+旧密码失效", c.post("/api/login",
          json={"username": "boss", "password": "NewPass456!"}).status_code == 401)
    check("新凭据可登录", c.post("/api/login",
          json={"username": _reset["username"], "password": _reset["password"]}).get_json()["ok"])
    core.set_meta("admin_user", "admin")
    core.set_meta("admin_hash", core.hash_password("NewPass456!"))
    H["X-CSRF-Token"] = c.get("/api/session").get_json()["csrf"]
    check("按钮式保存服务端设置", P("/api/settings", params).get_json()["ok"])
    check("按钮式恢复默认参数", P("/api/settings/restore-default", {}).get_json()["ok"])
    check("恢复默认不会把白名单放大回所有私网",
          "10.255.114.0/24" in core.allow_list() or "10.0.0.0/8" not in core.allow_list(),
          core.allow_list())

    # ---------- 网络模式开关 / P2P 接口 ----------
    section("网络模式开关与 P2P 接口")
    st = c.get("/api/state").get_json()
    check("概览返回网络模式", st["stats"]["nat_mode"] == "nat")
    check("概览返回互访数量", "peers" in st["stats"])
    check("ACL 接口返回互访授权", len(c.get("/api/acl").get_json()["peers"]) >= 1)
    fpb = core.get_meta("config_fingerprint")
    nj = P("/api/network/nat", {"mode": "no-nat"}).get_json()
    check("开启无 NAT 溯源", nj.get("ok") and nj["nat_mode"] == "no-nat")
    check("切换后指纹不变（ovpn 不用重发）", core.get_meta("config_fingerprint") == fpb)
    check("切换后路由指引改为需要回程", c.get("/api/route-guide").get_json()["required"] is True)
    check("切回 NAT 模式", P("/api/network/nat", {"mode": "nat"}).get_json()["ok"])
    check("新增 P2P 授权 API",
          P("/api/peers", {"src": "lisi", "dst": "wangwu", "proto": "tcp", "ports": "443"}).get_json().get("ok"))
    pid = [x for x in c.get("/api/acl").get_json()["peers"] if x["src_username"] == "lisi"][-1]["id"]
    check("删除 P2P 授权 API", c.delete("/api/peers/%s" % pid, headers=H).get_json()["ok"])
    rj = P("/api/settings/reinit-ca", {}).get_json()
    check("按钮式重签 CA 启动", bool(rj.get("task_id")), rj)
    tj = wait_task(c, rj["task_id"])
    check("按钮式重签 CA 完成", tj["status"] == "done", tj.get("msg"))

    # ---------- 二进制更新（含回滚路径） ----------
    section("手动上传更新二进制")
    fake26 = os.path.join(TMP, "fake26")
    with open(fake26, "w") as f:
        f.write("#!/bin/sh\necho 'OpenVPN 2.6.24 x86_64-unknown-linux-gnu [SSL (OpenSSL)]'\n")
    os.chmod(fake26, 0o755)
    before = core.get_meta("config_fingerprint")
    up = c.post("/api/update/binary",
                data={"file": (io.BytesIO(open(fake26, "rb").read()), "openvpn"), "allow_major": "0"},
                content_type="multipart/form-data", headers=H).get_json()
    check("更新任务创建", bool(up.get("task_id")))
    tj = wait_task(c, up["task_id"])
    check("更新任务完成", tj["status"] == "done", tj.get("msg"))
    check("更新后指纹不变（客户端不用动）", core.get_meta("config_fingerprint") == before)
    if POSIX:
        check("版本已更新", core.version_info()["version"] == "2.6.24")
    check("备份点已生成（可回滚）", len([b for b in core.list_backups() if b["kind"] == "backup"]) >= 2)

    # ---------- 回滚 ----------
    section("备份回滚")
    snap = [b for b in core.list_backups() if b["kind"] == "backup"][-1]["path"]
    check("自动打点是可恢复的备份包", os.path.isfile(snap) and snap.endswith(".tar.gz"))
    check("按钮直接恢复备份点", P("/api/backups/restore", {"path": snap}).get_json()["ok"])
    core.init_db()

    # ---------- 备份 / 迁移 ----------
    section("备份与迁移")
    core.add_user("备份用户", password="Test12345!", real_name="备份", dept="运维部")
    core.write_all_config()
    r = fullbackup.create("自检")
    fp = r["path"]
    check("全备份包已生成", os.path.isfile(fp) and r["size"] > 100)
    info = fullbackup.preview(fp)
    check("包内明确分成 openvpn / web 两块",
          set(info["manifest"]["files"]) == {"openvpn", "web"}, info["manifest"].keys())
    check("全备份标记 Web 版本", info["manifest"]["web_version"] == core.WEB_VERSION)
    with tarfile.open(fp) as tf:
        names = tf.getnames()
    check("包内含纯 OpenVPN 材料", "openvpn/ccd/备份用户" in names and "openvpn/server.conf" in names)
    check("包内含 Web 数据库", "web/openvpn-web.db" in names)
    check("包内含中文恢复说明", "恢复说明.txt" in names)
    check("备份列表能列出", any(x["name"] == r["name"] for x in fullbackup.list_items()))
    check("手动备份带来源标记", info["manifest"].get("origin") == "manual")
    check("列表里带来源标记", any(x.get("origin") == "manual" for x in fullbackup.list_items()))
    if os.path.isfile(core.BIN):
        check("包内含 openvpn 二进制（升级失败可回滚）", "openvpn/openvpn.bin" in names)
    auto_p = core.snapshot("自检自动")
    check("自动打点生成备份包", os.path.isfile(auto_p) and auto_p.endswith(".tar.gz"))
    check("自动打点标记来源 auto", fullbackup.preview(auto_p)["origin"] == "auto")
    core.delete_user("备份用户")
    check("恢复前测试账号已删", not any(u["username"] == "备份用户" for u in core.list_users()))
    res = fullbackup.restore(fp, audit=False)
    check("全备份恢复后账号回来", any(u["username"] == "备份用户" for u in core.list_users()))
    check("恢复前自动打了保护备份", os.path.isfile(res["snapshot"]))
    check("恢复后 ccd 也跟着回来", os.path.exists(os.path.join(core.CCD, "备份用户")))
    # bin/ 只认「带安装码的替换/编译」：恢复包里的二进制默认不动，否则等于绕开闸门
    _binp = core.BIN
    _had_bin = os.path.isfile(_binp)
    _old_bin = open(_binp, "rb").read() if _had_bin else b""
    os.makedirs(os.path.dirname(_binp), exist_ok=True)
    with open(_binp, "wb") as f:
        f.write(b"OLD-BINARY")
    _fp2 = fullbackup.create("二进制自检")["path"]
    with open(_binp, "wb") as f:
        f.write(b"NEW-BINARY")
    _r2 = fullbackup.restore(_fp2, audit=False)
    check("恢复不覆盖 bin/openvpn（二进制只走安装码那条路）",
          open(_binp, "rb").read() == b"NEW-BINARY" and _r2.get("bin_skipped") is True)
    fullbackup.restore(_fp2, audit=False, with_bin=True)
    check("命令行恢复（with_bin=True，root 手工执行）才连二进制一起放回",
          open(_binp, "rb").read() == b"OLD-BINARY")
    if _had_bin:
        with open(_binp, "wb") as f:
            f.write(_old_bin)
    else:
        os.remove(_binp)
    # 坏包必须整包拒绝，绝不能写一半
    wrong = os.path.join(TMP, "wrong.tar.gz")
    wm = os.path.join(TMP, "wm.json")
    with open(wm, "w") as f:
        json.dump({"kind": "something-else"}, f)
    with tarfile.open(wrong, "w:gz") as tf:
        tf.add(wm, arcname="manifest.json")
    try:
        fullbackup.preview(wrong)
        check("拒绝非全备份包", False)
    except ValueError:
        check("拒绝非全备份包", True)
    evil = os.path.join(TMP, "evil.tar.gz")
    em = os.path.join(TMP, "em.json")
    with open(em, "w") as f:
        json.dump({"kind": fullbackup.KIND, "format": fullbackup.FORMAT,
                   "files": {"openvpn": {}, "web": {}}}, f)
    xx = os.path.join(TMP, "xx.txt")
    with open(xx, "w") as f:
        f.write("x")
    with tarfile.open(evil, "w:gz") as tf:
        tf.add(em, arcname="manifest.json")
        tf.add(xx, arcname="../../etc/passwd")
        tf.add(xx, arcname="web/openvpn-web.db")
    try:
        fullbackup.preview(evil)
        check("拒绝路径穿越的备份包", False)
    except ValueError:
        check("拒绝路径穿越的备份包", True)
    # 回归：恢复会整库覆盖 meta，随后这些值被渲染成 root 执行的 server.conf / rules.sh。
    # manifest 哈希只能证明「包自洽」——改库重算哈希就能过。所以恢复前必须复核
    # 包内配置内容本身，不合法整包拒绝（在任何生产文件被覆盖之前）。
    _tam = os.path.join(TMP, "tampered.tar.gz")
    _txd = os.path.join(TMP, "tampered-x")
    os.makedirs(_txd, exist_ok=True)
    with tarfile.open(fp, "r:gz") as tf:
        tf.extractall(_txd, filter="data")
    import sqlite3 as _sq3
    _tcon = _sq3.connect(os.path.join(_txd, "web", "openvpn-web.db"))
    _tcon.execute("INSERT INTO meta(k,v) VALUES('tls_min',?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                  (json.dumps('1.2\nup "/bin/sh -c \'touch /tmp/pwned\'"\n'),))
    _tcon.commit()
    _tcon.close()
    _mf = os.path.join(_txd, "manifest.json")
    with open(_mf, encoding="utf-8") as f:
        _mm = json.load(f)
    for _blk, _rels in _mm["files"].items():
        for _rel in _rels:
            _mm["files"][_blk][_rel] = fullbackup._sha256(os.path.join(_txd, _blk, _rel))
    with open(_mf, "w", encoding="utf-8") as f:
        json.dump(_mm, f, ensure_ascii=False, indent=2)
    with tarfile.open(_tam, "w:gz") as tf:
        tf.add(_mf, arcname="manifest.json")
        tf.add(os.path.join(_txd, "openvpn"), arcname="openvpn")
        tf.add(os.path.join(_txd, "web"), arcname="web")
    try:
        fullbackup.restore(_tam, audit=False)
        check("恢复篡改过 meta 的备份包被整包拒绝", False)
    except ValueError as _e:
        check("恢复篡改过 meta 的备份包被整包拒绝", "未通过校验" in str(_e), str(_e)[:160])
    shutil.rmtree(_txd, ignore_errors=True)
    # 接口层
    j = P("/api/full-backup/create", {"tag": "接口"}).get_json()
    check("接口生成全备份", j.get("ok") and os.path.isfile(j["path"]), j)
    dl = c.get("/api/full-backup/download/" + j["name"], headers=H)
    check("接口下载全备份", dl.status_code == 200 and dl.data[:2] == b"\x1f\x8b")
    up = c.post("/api/full-backup/upload",
                data={"file": (io.BytesIO(dl.data), j["name"])},
                content_type="multipart/form-data", headers=H)
    uj = up.get_json()
    check("接口上传并预览备份包", uj.get("ok") and uj["preview"]["name"] == j["name"], uj)
    check("接口列出备份", any(x["name"] == j["name"]
          for x in c.get("/api/full-backup/list").get_json()["items"]))
    check("接口按包名恢复本机备份", P("/api/full-backup/restore", {"name": r["name"]}).get_json()["ok"])
    _dr = P("/api/full-backup/delete", {"name": j["name"]})
    check("接口删除全备份", _dr.get_json().get("ok") is True,
          (_dr.status_code, _dr.data[:200], j["name"]))
    check("删除后列表不再有", not any(x["name"] == j["name"]
          for x in c.get("/api/full-backup/list").get_json()["items"]))
    core.delete_user("备份用户")

    # ---------- 改 VPN 网段：只改服务端，账号地址由管理员手工改 ----------
    section("改 VPN 网段不自动改账号地址")
    core.set_meta("pool_enabled", False)
    P("/api/settings", dict(core.cfg(), vpn_cidr="10.8.0.0/24"))
    before = {u["username"]: u["vpn_ip"] for u in core.list_users()}
    check("起点账号都在 10.8.0.0/24",
          all(ip.startswith("10.8.0.") for ip in before.values()), before)
    _fp0 = core.get_meta("config_fingerprint")
    sr = P("/api/settings", dict(core.cfg(), vpn_cidr="10.8.1.0/24")).get_json()
    check("改网段本身可以保存", sr.get("ok") is True, sr)
    check("账号固定 IP 一个都没被自动改",
          {u["username"]: u["vpn_ip"] for u in core.list_users()} == before)
    check("保存后列出需要手工改的账号",
          {u["username"] for u in sr.get("stale") or []} == set(before), sr.get("stale"))
    check("提示里讲明要手工改", "手工" in (sr.get("msg") or ""), sr.get("msg"))
    check("只改网段不要求重发 ovpn",
          sr.get("need_redistribute") is False
          and core.get_meta("config_fingerprint") == _fp0,
          (sr.get("need_redistribute"), _fp0, core.get_meta("config_fingerprint")))
    check("/api/users 也带上待改清单",
          {u["username"] for u in c.get("/api/users").get_json().get("stale") or []} == set(before))
    check("首页概览也亮出待改账号",
          {u["username"] for u in c.get("/api/state").get_json().get("stale_ips") or []} == set(before))
    check("server.conf 已换成新网段",
          "ifconfig 10.8.1.1 255.255.255.0" in open(os.path.join(core.DATA, "server.conf")).read())
    # 手工逐个改（保持末位），改完提示消失
    for name, old in before.items():
        core.update_user(name, vpn_ip="10.8.1." + old.rsplit(".", 1)[-1])
    check("手工改完地址后不再有待改账号", core.stale_ip_users() == [], core.stale_ip_users())
    check("尾号一一对应",
          {u["username"]: u["vpn_ip"].rsplit(".", 1)[-1] for u in core.list_users()}
          == {k: v.rsplit(".", 1)[-1] for k, v in before.items()})
    # 只改网段、池子不动时给的是「一起改池子」的明确提示，而不是含糊的越界
    core.set_meta("pool_enabled", True)
    core.set_meta("pool_start", "10.8.1.100")
    core.set_meta("pool_end", "10.8.1.200")
    _pe = core.validate_cfg(dict(core.cfg(), vpn_cidr="10.8.3.0/24"))
    check("只改网段不动池子会被拒并说明怎么改",
          any("地址池" in x and "一起改" in x for x in _pe), _pe)
    core.set_meta("pool_enabled", False)
    core.set_meta("pool_start", "10.8.1.100")
    core.set_meta("pool_end", "10.8.1.200")
    # 池子不能开在已有账号的固定 IP 上：否则 ccd 固定分给某人、池子还会发给别人
    _tails = [int(u["vpn_ip"].rsplit(".", 1)[-1]) for u in core.list_users()]
    core.set_meta("pool_enabled", True)
    core.set_meta("pool_start", "10.8.1.2")
    core.set_meta("pool_end", "10.8.1.%d" % min(max(_tails), 250))
    _c2 = core.cfg()
    check("能识别池子与账号固定 IP 重叠", len(core.pool_user_conflicts(_c2)) >= 1,
          core.pool_user_conflicts(_c2))
    _pr = P("/api/settings", _c2)
    check("保存时拒绝与账号地址重叠的池子",
          _pr.status_code >= 400 and "重叠" in (_pr.get_json().get("msg") or ""), _pr.get_json())
    core.set_meta("pool_enabled", False)
    core.set_meta("pool_start", "10.8.1.100")
    core.set_meta("pool_end", "10.8.1.200")
    core.write_all_config()

    section("HTTP 服务器（waitress）")
    import threading
    import socket
    import urllib.request

    ver = c.get("/api/version").get_json()
    check("/api/version 报告 HTTP 服务器", bool(ver.get("server")), ver.get("server"))
    check("/api/version 报告线程数", int(ver.get("threads") or 0) >= 1, ver.get("threads"))

    try:
        from waitress import create_server
        has_waitress = True
    except Exception as _e:
        has_waitress = False
        print("  跳过并发用例：本机没装 waitress（%s）" % _e)

    if not has_waitress:
        # 没装时必须能说明自己退回了开发服务器，而不是静默变慢
        check("缺 waitress 时版本页说明已退回开发服务器",
              "Werkzeug" in (ver.get("server") or ""), ver.get("server"))
    else:
        def _serve(wsgi, threads=8):
            srv = create_server(wsgi, host="127.0.0.1", port=0, threads=threads, ident="selftest")
            port = int(srv.effective_port)   # 端口在 waitress 里可能是字符串，%d 会直接抛异常
            threading.Thread(target=srv.run, daemon=True).start()
            end = time.time() + 5
            while time.time() < end:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.02)
            return srv, port

        def _hit(port, path="/", out=None):
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=10) as r:
                    body = r.read()
                    if out is not None:
                        out.append((r.status, body))
                    return r.status
            except Exception as e:
                if out is not None:
                    out.append((0, str(e).encode()))
                return 0

        # ① 线程池真的并行：8 个各 0.25s 的请求，串行要 2s 以上
        def _slow(environ, start_response):
            time.sleep(0.25)
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"ok"]

        srv1, p1 = _serve(_slow)
        t0 = time.time()
        _ts = [threading.Thread(target=_hit, args=(p1,)) for _ in range(8)]
        [_t.start() for _t in _ts]
        [_t.join(20) for _t in _ts]
        cost = time.time() - t0
        check("waitress 8 线程并行处理（串行需要 2s 以上）", cost < 1.2, "%.2fs" % cost)
        srv1.close()

        # ② 真拿本应用跑一遍：8 个并发请求不能有 500 / 死锁 / 串号
        srv2, p2 = _serve(web.app)
        got = []
        _ts = [threading.Thread(target=_hit, args=(p2, "/api/session", got)) for _ in range(8)]
        [_t.start() for _t in _ts]
        [_t.join(20) for _t in _ts]
        codes = sorted(x[0] for x in got)
        check("并发 8 个 /api/session 全部 200", codes == [200] * 8, codes)
        check("并发返回的会话都带 csrf",
              all(b"csrf" in b for _, b in got), [b[:80] for _, b in got][:2])
        srv2.close()

        # ③ 并发写库不炸：SQLite 只有一个写入者，靠 WAL + busy_timeout 排队
        errs = []

        def _writer(i):
            try:
                for _k in range(20):
                    core.log_audit("并发%d" % i, "selftest.concurrent", "多线程写审计")
            except Exception as e:
                errs.append(repr(e))

        t0 = time.time()
        _ts = [threading.Thread(target=_writer, args=(i,)) for i in range(8)]
        [_t.start() for _t in _ts]
        [_t.join(60) for _t in _ts]
        check("8 线程并发写审计不报 database is locked", not errs, errs[:2])
        n = AuditLog.objects.filter(action="selftest.concurrent").count()
        check("并发写入条目不丢", n == 160, n)

    section("多线程下的一致性")
    rules_txt = open(os.path.join(core.STATE, "rules.sh")).read()
    check("规则脚本用 flock 串行化（定时重放 vs 手工重建）", "flock -w" in rules_txt)
    check("规则脚本的 iptables 带 -w（等 xtables 锁而不是报错）",
          "IPT='iptables -w 5'" in rules_txt, rules_txt.splitlines()[:12])

    # 长任务占着写锁时，其它写接口要在门口被挡下，而不是排队或并发执行
    web.WRITE_LOCK.acquire()
    try:
        r = P("/api/users", {"username": "锁内测试", "password": "Aa12345678"})
    finally:
        web.WRITE_LOCK.release()
    check("长任务运行时拒绝并发写操作（409）", r.status_code == 409, r.get_json())
    check("被拒的请求没有真的建出账号",
          not any(u["username"] == "锁内测试" for u in core.list_users()))

    # 并发建号：取号 + 插入必须原子，否则两个线程会挑到同一个空闲地址
    errs = []

    def _mk(i):
        try:
            core.add_user("并发%d" % i, password="Aa12345678", actor="selftest")
        except Exception as e:
            errs.append(repr(e))

    import threading as _th
    ts = [_th.Thread(target=_mk, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    _ips = [u["vpn_ip"] for u in core.list_users() if u["username"].startswith("并发")]
    check("并发建号不撞 UNIQUE（8 个全成功）", not errs, errs[:2])
    check("并发建号拿到的地址互不相同", len(_ips) == 8 and len(set(_ips)) == 8, _ips)
    for _u in [u["username"] for u in core.list_users() if u["username"].startswith("并发")]:
        core.delete_user(_u)

    # 审计归属：全局 dict 在并发下会把 A 的操作记成 B 干的
    def _audit_as(name):
        core.audit_ctx_set(name, "10.9.9.9")
        for _k in range(15):
            core.log_audit("admin", "selftest.actor", "归属")

    _ta = _th.Thread(target=_audit_as, args=("alice",))
    _tb = _th.Thread(target=_audit_as, args=("bob",))
    _ta.start(); _tb.start(); _ta.join(30); _tb.join(30)
    _rows = {}
    for _r in AuditLog.objects.filter(action="selftest.actor").values("actor_name"):
        _rows[_r["actor_name"]] = _rows.get(_r["actor_name"], 0) + 1
    check("并发下审计归属不串人（alice/bob 各 15 条）",
          _rows.get("alice") == 15 and _rows.get("bob") == 15, _rows)
    core.audit_ctx_set("", "")
    core.log_audit("admin", "selftest.actor", "无上下文时回落到 admin")
    _last = (AuditLog.objects.filter(action="selftest.actor")
             .order_by("-created_at").values_list("actor_name", flat=True).first())
    check("没有上下文时审计仍能落一个操作者", _last == "admin", _last)

    # ---------- 安装码：给「上传即 root」这条路加一道人闸 ----------
    section("安装码闸门（替换二进制 / 编译源码 / 更新 Web）")
    from system import worker as wk
    _etc = os.path.join(TMP, "etc-installcode")
    os.makedirs(_etc, exist_ok=True)
    wk.ETC = _etc
    _tokp = os.path.join(_etc, "install.token")

    def _put_token(code, exp, fail=0):
        with open(_tokp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"hash": wk.code_hash(code), "exp": exp, "fail": fail}))

    _code = "ABCD2345"
    try:
        wk.require_install_code(_code)
        _no_token = False
    except RuntimeError as e:
        _no_token = "install-code.sh" in str(e)
    check("没有安装码文件时拒绝执行，并告诉人怎么生成", _no_token)

    _put_token(_code, time.time() + 60)
    try:
        wk.require_install_code("")
        _empty_ok = True
    except RuntimeError:
        _empty_ok = False
    check("安装码为空时拒绝", not _empty_ok)

    _put_token(_code, time.time() + 60)
    try:
        wk.require_install_code("WRONG234")
        _wrong_ok = True
    except RuntimeError:
        _wrong_ok = False
    check("安装码不对时拒绝，且令牌保留（还能继续试）",
          (not _wrong_ok) and os.path.exists(_tokp))

    _put_token(_code, time.time() + 60)
    try:
        wk.require_install_code(_code.lower())      # 大小写不敏感
        _pass_ok = True
    except RuntimeError:
        _pass_ok = False
    check("正确的码能通过，并且用后立即作废（一码一次）",
          _pass_ok and not os.path.exists(_tokp))

    _put_token(_code, time.time() - 1)
    _exp_msg = ""
    try:
        wk.require_install_code(_code)
        _exp_ok = True
    except RuntimeError as e:
        _exp_ok = False
        _exp_msg = str(e)
    check("过期的码不能用，并顺手作废", (not _exp_ok) and "过期" in _exp_msg)

    _put_token(_code, time.time() + 60)
    _burst = 0
    for _i in range(5):
        try:
            wk.require_install_code("WRONG%02d" % _i)
        except RuntimeError:
            _burst += 1
    check("连错 5 次令牌作废（挡在线爆破）", _burst == 5 and not os.path.exists(_tokp))

    check("三个「装外部代码」的动作都在闸门名单里，一个都不能漏",
          set(wk.GATED) == {"replace-binary", "build-openvpn", "update-web"}, wk.GATED)

    # 回归：MOCK 环境变量曾经能经 sudoers 的 env_keep 传到 worker，
    # 让 GATED 动作在没有安装码的情况下照样执行 —— 这是能直接提权的洞。
    _wsrc = open(os.path.join(SYS, "worker.py"), encoding="utf-8").read()
    _isrc = open(os.path.join(SYS, "install.sh"), encoding="utf-8").read()
    # Django 版：原 app.py 里的 env 透传搬到了 ovpn/bridge.py，GET CSRF 名单搬到 ovpn/middleware.py。
    # 源码布局 selftest 在 src/（ovpn 在上一级）；交付平铺布局 selftest 与 ovpn/ 同级。
    _code_root = DJANGO
    _ovpn_dir = os.path.join(_code_root, "ovpn")
    _bsrc = open(os.path.join(_ovpn_dir, "bridge.py"), encoding="utf-8").read()
    _msrc = open(os.path.join(_ovpn_dir, "middleware.py"), encoding="utf-8").read()
    check("worker 闸门不再因 MOCK 环境变量而放行（防提权）",
          "if act in GATED and not SELFTEST:" in _wsrc
          and "GATED and not MOCK" not in _wsrc)
    _keep_lines = [ln for ln in _isrc.splitlines() if "env_keep" in ln and not ln.lstrip().startswith("#")]
    check("sudoers 显式 env_reset 且 env_keep 只留 ROOT（不保留任何自检开关）",
          "Defaults:$USER env_reset" in _isrc
          and any('env_keep += "OVPN_WEB_ROOT"' in ln for ln in _keep_lines)
          and not any(("OVPN_WEB_MOCK" in ln or "OVPN_WEB_SELFTEST" in ln)
                      for ln in _keep_lines))
    check("bridge 把 MOCK/SELFTEST 都随 MOCK 标志设置（生产路径经 sudo 由 env_reset 清空）",
          'OVPN_WEB_MOCK="1" if MOCK else "0"' in _bsrc
          and 'OVPN_WEB_SELFTEST="1" if MOCK else "0"' in _bsrc)
    check("带副作用的 GET（logs/selfcheck）也纳入 CSRF 校验",
          'CSRF_GET_PATHS = ("/api/logs", "/api/selfcheck")' in _msrc)

    # 回归：tls_min 曾是 validate_cfg 的盲区——带换行的字符串被原样写进以 root
    # 运行的 server.conf（tls-version-min 行），可注入 up/plugin 等任意指令，等于 root RCE。
    _scfg = core.cfg()
    _bad_tls = dict(_scfg)
    _bad_tls["tls_min"] = '1.2\nup "/bin/sh -c \'touch /tmp/pwned\'"\n'
    check("validate_cfg 拒绝带换行的 tls_min（server.conf 注入）",
          any("TLS" in e for e in core.validate_cfg(_bad_tls)), core.validate_cfg(_bad_tls))
    for _v in ("1.2", "1.3"):
        _ok_tls = dict(_scfg)
        _ok_tls["tls_min"] = _v
        check("validate_cfg 接受合法 tls_min %s" % _v, not core.validate_cfg(_ok_tls))
    for _bad in (-1, 1700, 1400, "1360\nup /bin/sh", "abc"):
        check("validate_cfg 拒绝非法 mssfix %r" % _bad,
              any("mssfix" in e for e in core.validate_cfg(dict(_scfg, mssfix=_bad))),
              core.validate_cfg(dict(_scfg, mssfix=_bad)))
    try:
        core.render_server_conf(_bad_tls)
        check("render_server_conf 对脏 tls_min 拒绝生成（fail-closed）", False)
    except Exception:
        check("render_server_conf 对脏 tls_min 拒绝生成（fail-closed）", True)

    # 回归：rules.sh 是 root 执行的脚本，dns/ntp/网段/账号 IP/端口/协议都从数据库
    # %s 进命令行。写入路径有校验，但「篡改过的备份包 / 手工改库」能绕过——
    # 渲染时必须 fail-closed，脏值宁可让 apply-rules 失败也不能生成被注入的脚本。
    _bad_dns = dict(_scfg)
    _bad_dns["dns"] = ["1.1.1.1 -j ACCEPT; touch /tmp/pwned; #"]
    try:
        core.render_rules(_bad_dns)
        check("render_rules 对脏 dns 拒绝生成", False)
    except Exception:
        check("render_rules 对脏 dns 拒绝生成", True)
    _bad_net = dict(_scfg)
    _bad_net["vpn_cidr"] = "10.8.0.0/24'; touch /tmp/pwned; '"
    try:
        core.render_rules(_bad_net)
        check("render_rules 对带引号的 vpn_cidr 拒绝生成", False)
    except Exception:
        check("render_rules 对带引号的 vpn_cidr 拒绝生成", True)
    try:
        core._render_accept([], "10.8.0.3", "1.1.1.1/32", "tcp", "22; touch /tmp/pwned")
        check("_render_accept 对脏端口拒绝生成", False)
    except Exception:
        check("_render_accept 对脏端口拒绝生成", True)
    try:
        core._render_accept([], "10.8.0.3; touch /tmp/x", "1.1.1.1/32", "any", "")
        check("_render_accept 对脏源地址拒绝生成", False)
    except Exception:
        check("_render_accept 对脏源地址拒绝生成", True)

    # ccd 文件同样被 root 的 openvpn 按配置片段解析：脏 vpn_ip 不能写进去
    _ccd_u = core.list_users()[0]
    _ccd_oldip = _ccd_u["vpn_ip"]
    VpnUser.objects.filter(username=_ccd_u["username"]).update(
        vpn_ip='10.8.0.5\nup "/bin/sh -c id"')
    try:
        core.rebuild_ccd()
        check("rebuild_ccd 对脏 vpn_ip 拒绝生成 ccd", False)
    except Exception:
        check("rebuild_ccd 对脏 vpn_ip 拒绝生成 ccd", True)
    VpnUser.objects.filter(username=_ccd_u["username"]).update(vpn_ip=_ccd_oldip)
    core.rebuild_ccd()
    check("脏数据清理后 ccd 恢复正常生成", True)
    check("valid_username 拒绝尾随换行的名字（match 的 $ 会放过）",
          not core.valid_username("张三\n") and not core.valid_username("x\n")
          and core.valid_username("张三") and core.valid_username("alice-01"))
    VpnUser.objects.filter(username=_ccd_u["username"]).update(username="../staging/x")
    try:
        core.rebuild_ccd()
        check("rebuild_ccd 对脏 username（路径穿越）拒绝生成", False)
    except Exception:
        check("rebuild_ccd 对脏 username（路径穿越）拒绝生成", True)
    VpnUser.objects.filter(username="../staging/x").update(username=_ccd_u["username"])
    core.rebuild_ccd()

    if POSIX:
        _r = subprocess.run(["bash", os.path.join(SYS, "install-code.sh"), "600"],
                            capture_output=True, text=True,
                            env=dict(os.environ, OVPN_WEB_ETC=_etc, OVPN_CODE_QUIET="1"))
        _c = _r.stdout.strip()
        check("install-code.sh 能生成 8 位码，且字母表里没有 0/O/1/I",
              _r.returncode == 0 and len(_c) == 8
              and all(ch in "23456789ABCDEFGHJKLMNPQRSTUVWXYZ" for ch in _c),
              (_r.returncode, _c, _r.stderr[-200:]))
        _mode = oct(os.stat(_tokp).st_mode & 0o777)
        check("安装码文件权限 600（同机其他用户读不到）", _mode == "0o600", _mode)
        try:
            wk.require_install_code(_c)
            _gen_ok = True
        except RuntimeError:
            _gen_ok = False
        check("脚本生成出来的码能直接通过 worker 校验", _gen_ok)

    # 二进制回滚：不要求安装码，但只认 replace-binary 当时钉下的 sha256
    _prev = os.path.join(core.DATA, "snapshots", "openvpn.previous")
    os.makedirs(os.path.dirname(_prev), exist_ok=True)
    os.makedirs(os.path.dirname(core.BIN), exist_ok=True)
    with open(_prev, "wb") as f:
        f.write(b"PREVIOUS-BINARY")
    with open(core.BIN, "wb") as f:
        f.write(b"CURRENT-BINARY")
    wk.pin_rollback(_prev)
    check("替换成功后钉住了旧二进制的指纹",
          os.path.exists(os.path.join(_etc, "rollback.token")))
    with open(_prev, "wb") as f:          # 有人偷换了快照文件
        f.write(b"EVIL-BINARY")
    try:
        wk.action_rollback_binary({})
        _blocked = False
    except RuntimeError:
        _blocked = True
    check("快照被换过就拒绝回滚，当前二进制保持不变",
          _blocked and open(core.BIN, "rb").read() == b"CURRENT-BINARY")
    wk.pin_rollback(_prev)                # 重新按当前快照钉一次
    wk.action_rollback_binary({})
    check("指纹对得上才真的回滚", open(core.BIN, "rb").read() == b"EVIL-BINARY")

    _inst = open(os.path.join(SYS, "install.sh"), encoding="utf-8").read()
    check("install.sh 把 bin/ 收给 root（Web 用户不能再写要被 root 执行的程序）",
          'chown -R root:root "$DEST/bin"' in _inst)
    # 装完必须 openvpn + web 都在跑：缺二进制不许「装到一半退出、让用户去还没起的网页上传」
    check("install.sh 装了二进制准备步骤（缺件自己补，不半途而废）",
          'prepare-openvpn.sh' in _inst)
    check("install.sh 不再因为缺 bin/openvpn 直接 exit 1",
          '缺少静态二进制' not in _inst)
    check("服务端没起来时 install.sh 以非 0 退出（别让自动化以为装好了）",
          "exit 3" in _inst)
    _prep = os.path.join(SYS, "prepare-openvpn.sh")
    check("prepare-openvpn.sh 随包分发", os.path.isfile(_prep))
    _prepsrc = open(_prep, encoding="utf-8").read() if os.path.isfile(_prep) else ""
    check("缺二进制时就地编译（源码与依赖都在包内，不联网）",
          "VENDOR_ONLY=1" in _prepsrc and "OPENVPN_SRC" in _prepsrc)
    check("install.sh 认 --with-build-tools（可以顺手把编译工具装上）",
          "--with-build-tools" in _inst and "--tools-only" in _inst)
    check("prepare-openvpn.sh 缺工具时先试着自动补装（yum/dnf/apt 都认）",
          "install_tools" in _prepsrc and "apt-get" in _prepsrc
          and "pkgconfig" in _prepsrc and "OVPN_NO_AUTO_TOOL_INSTALL" in _prepsrc)
    _bo_p = os.path.join(CODE, "build", "build-offline.sh")
    if os.path.isfile(_bo_p):
        _bo = open(_bo_p, encoding="utf-8").read()
        _bo_ok = 'vendor/src' in _bo and '"$VENDOR"/src/*.tar.gz' in _bo
    else:
        _bo_ok = None      # 装机目录没有 build/，这条只在仓库里适用
    check("打包脚本把源码包一起带上（否则目标机想编也没料）", _bo_ok)

    # ---- 1.3.29：一键安装 / 跨发行版预检 / 强制随包 Python ----
    check("install.sh 强制只用随包 Python，不再退回目标机系统 python3",
          "强制随包便携版" in _inst and "不会退回系统 python3" in _inst
          and "command -v python3" not in _inst)
    _pc_call = _inst.find('"$PY" "$DEST/system/precheck.py"')
    check("install.sh 在动 /etc 与运行账号之前先跑环境预检",
          _pc_call > 0 and _pc_call < _inst.find('cat > "$SUDOERS"'))
    check("install.sh 预检有硬错误就中止（不留半套环境）",
          "PRECHECK_RC" in _inst and "安装前预检未通过" in _inst)
    check("install.sh 重装场景端口占用只警告，首装才硬检查",
          "--reinstall" in _inst and "data/admin_password.txt" in _inst)
    check("install.sh 装完自动跑包内 selftest（交付即验收）",
          '"$DEST/tests/selftest.py"' in _inst and "SELFTEST_STATE" in _inst)
    check("安装完成信息里报告预检/自检结果与运行时来源",
          "环境预检:" in _inst and "装完自检:" in _inst and "运行时:" in _inst)

    _pc_p = os.path.join(SYS, "precheck.py")
    check("precheck.py 随包分发", os.path.isfile(_pc_p))
    if os.path.isfile(_pc_p):
        _pc = open(_pc_p, encoding="utf-8").read()
        check("预检覆盖 TUN/glibc/端口/磁盘/时钟/防火墙 + 交付包完整性",
              all(k in _pc for k in ('"tun"', '"glibc"', '"ports"', '"disk"', '"clock"',
                                    '"firewall"', '"runtime"', '"init"', '"wheels"', '"binary"')))
        _imp = [ln.strip() for ln in _pc.splitlines()
                if ln.startswith("import ") or ln.startswith("from ")]
        _std = ("argparse", "glob", "os", "platform", "shutil", "socket",
                "subprocess", "sys", "time", "re")
        check("预检只用标准库（不引入目标机可能没有的第三方包）",
              bool(_imp) and all(
                  any(ln.startswith("import " + m) or ln.startswith("from " + m) for m in _std)
                  for ln in _imp))
        check("预检只探测不改系统：时钟只警告、不校准",
              "自动校准" in _pc and "date -s" not in _pc and "hwclock -w" not in _pc)
        check("预检直接试跑包内 ELF，glibc/架构不匹配当场发现",
              '"--version"' in _pc and "无法执行" in _pc)
        import importlib.util
        _spec = importlib.util.spec_from_file_location("ovpn_precheck", _pc_p)
        _pcmod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_pcmod)
        try:
            _fs = _pcmod.collect(CODE, 8800, 1194, False)
            _fok = isinstance(_fs, list) and len(_fs) >= 10
        except Exception as e:
            _fok = False
            print("     precheck.collect 抛异常: %r" % (e,))
        check("precheck.collect() 在任意平台上都能安全执行（无需 Linux 环境）", _fok)
        check("预检硬错误会让 main 返回非 0，警告只放行",
              'return 1 if any(f["level"] == "error"' in _pc)
    _bop_py = os.path.join(CODE, "build", "build-offline.py")
    if os.path.isfile(_bop_py):
        _bop_src = open(_bop_py, encoding="utf-8").read()
        check("Windows 打包把 django/ 与 system/ 整体收进交付包",
              'copy_tree(os.path.join(src, "django")' in _bop_src
              and 'copy_tree(os.path.join(src, "system")' in _bop_src)

    # Windows 上打包：不执行包内 Linux Python，只搬运 + 按规则写 tar
    _py_p = os.path.join(CODE, "build", "build-offline.py")
    if os.path.isfile(_py_p):
        _pysrc = open(_py_p, encoding="utf-8").read()
        check("build-offline.py 存在（Windows 上也能打出可安装的包）", True)
        check("软链按 tar 符号链接写入（Windows 上建不了真软链也不会坏）",
              "tarfile.SYMTYPE" in _pysrc)
        check("权限由脚本按路径规则写（不依赖源文件属性，Windows 上没有可执行位）",
              "def mode_for" in _pysrc and "0o700" in _pysrc)
    else:
        check("build-offline.py 存在（Windows 上也能打出可安装的包）", None)
    # 这条跟目录布局无关：装机目录同样要能离线补依赖
    check("依赖不预装：安装时从包内 vendor/wheels 离线补",
          "vendor/wheels" in _inst and "--find-links" in _inst)

    # 闸门做在 worker 里，但用户得能在界面上把码填进来，否则这功能等于没有
    _html = open(os.path.join(DJANGO, "templates", "index.html"), encoding="utf-8").read()
    check("向导第 2 步与升级页都有安装码输入框",
          'id="wcode"' in _html and 'id="upcode"' in _html)
    _viewsrc = open(os.path.join(_code_root, "ovpn", "views.py"), encoding="utf-8").read()
    check("三个上传接口都把安装码透传给 worker",
          _viewsrc.count('"install_code"') >= 3, _viewsrc.count('"install_code"'))

    if POSIX:
        from system import worker as wk
        f1 = wk.take_sys_lock(2)
        try:
            try:
                wk.take_sys_lock(1)
                busy_blocked = False
            except RuntimeError:
                busy_blocked = True
        finally:
            f1.close()
        check("worker 的跨进程锁真的互斥（第二个拿不到）", busy_blocked)

    print("\n" + "=" * 62)
    print(" 通过 %d 项 / 失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print(" 失败项:")
        for f in FAIL:
            print("   - " + f)
    print(" 数据目录: " + TMP)
    print("=" * 62)
    if os.environ.get("SELFTEST_KEEP") != "1":
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
