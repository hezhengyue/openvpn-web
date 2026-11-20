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

CODE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="ovpnweb-selftest-")
os.environ["OVPN_WEB_ROOT"] = TMP
os.environ["OVPN_WEB_MOCK"] = "1"
os.environ["OVPN_WEB_PORT"] = "8899"
sys.path.insert(0, CODE)

import core          # noqa: E402
import fullbackup    # noqa: E402
import app as web    # noqa: E402

PASS, FAIL = [], []
# 闸门/版本号这类用例要真的执行一个 shell 脚本，Windows 上跑不了（目标环境是 Linux）
POSIX = os.name == "posix"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  -> " + str(detail)) if detail and not cond else ""))


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
    core.set_meta("admin_hash", core.hash_password("TestPass123!"))
    web.bootstrap_admin()
    c = web.app.test_client()

    # ---------- 基础与配置 ----------
    section("数据库与配置生成")
    check("默认 VPN 网段", core.cfg()["vpn_cidr"] == "10.8.0.0/24")
    check("无 NAT 溯源默认关闭（默认 NAT 模式）", core.cfg()["nat_mode"] == "nat")
    check("默认隧道协议 TCP", core.cfg()["proto"] == "tcp")
    check("默认 tun-mtu 1400", core.cfg()["tun_mtu"] == 1400)
    check("默认不再有内网网段概念", core.cfg()["lan_nets"] == [])
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
    # VPN 网段必须自动出现在推送路由里，且只出现一次（不能走两处各写一遍）
    vpn_net = core.vpn_network(core.cfg())
    vpn_push = 'push "route %s %s"' % (vpn_net.network_address, vpn_net.netmask)
    check("server.conf 自动推送 VPN 网段", vpn_push in conf)
    check("VPN 网段只推送一次（没有重复）", conf.count(vpn_push) == 1)
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
    # 账号名允许中文（OpenVPN 的 common name 就是原始字节，ccd 文件名跟着走）
    u_cn = core.add_user("张三", real_name="张三", dept="运维部")
    check("账号名可以是中文", u_cn["username"] == "张三")
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
    con = core.db()
    mem = [r[0] for r in con.execute(
        "SELECT username FROM user_group_members WHERE username='lisi-new'")]
    acl = [r[0] for r in con.execute(
        "SELECT subject FROM acl_grants WHERE subject_type='user' AND subject='lisi-new'")]
    peer = con.execute("SELECT COUNT(*) FROM peer_grants WHERE src_username='lisi-new'").fetchone()[0]
    con.close()
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
    check("池子不在 VPN 网段内被拒", any("均须在" in x or "必须都在" in x
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
        return subprocess.run([sys.executable, os.path.join(CODE, "auth-verify.py"), f],
                              env=env, capture_output=True, text=True, timeout=30).returncode
    check("正确密码放行", auth("zhangsan", u1["password"]) == 0)
    check("错误密码拒绝", auth("zhangsan", "nope") == 1)
    check("不存在账号拒绝", auth("ghost", "x") == 1)
    check("凭据文件缺失 fail-closed",
          subprocess.run([sys.executable, os.path.join(CODE, "auth-verify.py")],
                         env=env, capture_output=True, timeout=30).returncode == 1)
    for _ in range(5):
        auth("lisi", "wrong")
    check("5 次失败锁定（正确密码也拒）", auth("lisi", u2["password"]) == 1)
    check("认证日志已落库", len(core.get_logs("auth")) >= 7)

    # ---------- Excel 全量同步 ----------
    section("Excel 导入导出（唯一表格格式）")
    name, data, mime = core.export_users("xlsx")
    check("导出 xlsx", data[:2] == b"PK" and "sheet" in mime)
    _, csvdata, _ = core.export_users("csv")
    head = csvdata.decode("utf-8-sig").splitlines()[0]
    check("表头含姓名/部门", "姓名" in head and "部门" in head)
    rows = [["用户名", "姓名", "部门", "密码", "VPN固定IP", "状态", "备注"],
            ["zhangsan", "", "", "", "10.8.0.12", "启用", ""],
            ["lisi", "", "", "", "10.8.0.11", "启用", ""],
            ["wangwu", "王五", "外包", "", "10.8.0.13", "启用", "外包"]]
    buf = io.StringIO(); w = csv.writer(buf); w.writerows(rows)
    imp = core.preview_import(("\ufeff" + buf.getvalue()).encode(), "台账.csv")
    check("导入预览列出新增", any(x["op"] == "新增" and x["username"] == "wangwu" for x in imp["changes"]))
    check("预览令牌有效期 300s", imp["expires_in"] == 300)
    # 按钮式提交：无需任何确认词，预览令牌仍在 5 分钟内防重放
    applied = core.apply_import(imp["token"])
    check("按钮式提交导入 3 个账号", applied["count"] == 3)
    check("导入带进姓名/部门", [u for u in core.list_users() if u["username"] == "wangwu"][0]["dept"] == "外包")
    check("自动生成初始密码文件", bool(applied["initial_password_file"]))
    check("导入自动备份台账", os.path.exists(applied["backup"]))
    try:
        core.apply_import("invalid-token"); check("无效预览令牌被拒", False)
    except ValueError:
        check("无效预览令牌被拒", True)
    bad = io.StringIO(); bw = csv.writer(bad)
    bw.writerows(rows[:1] + [["wangwu", "", "", "", "10.8.0.13", "启用", ""],
                             ["x", "", "", "", "10.8.0.13", "启用", ""]])
    try:
        core.preview_import(bad.getvalue().encode(), "bad.csv"); check("IP 重复整批拒绝", False)
    except ValueError:
        check("IP 重复整批拒绝", True)

    # ---------- Excel 导入的密码策略（有则覆盖 / 空则保留 / 可选重置） ----------
    section("Excel 导入密码策略")
    def _hash(n):
        con = core.db()
        r = con.execute("SELECT password_hash FROM users WHERE username=?", (n,)).fetchone()
        con.close()
        return r["password_hash"] if r else None

    all_rows = [["用户名", "姓名", "部门", "密码", "VPN固定IP", "状态", "备注"],
                ["zhangsan", "张三", "运维部", "", "10.8.0.12", "启用", ""],
                ["lisi", "李四", "运维部", "", "10.8.0.11", "启用", ""],
                ["wangwu", "王五", "外包", "KeepMe1234", "10.8.0.13", "启用", "外包"]]

    def _imp(rows, **kw):
        b = io.StringIO()
        csv.writer(b).writerows(rows)
        p = core.preview_import(("\ufeff" + b.getvalue()).encode(), "台账.csv")
        return core.apply_import(p["token"], **kw)

    h_zs = _hash("zhangsan")
    r1 = _imp(all_rows)
    check("密码列留空 -> 原密码不变", _hash("zhangsan") == h_zs)
    check("留空账号进 kept 清单", "zhangsan" in r1["kept"] and "lisi" in r1["kept"])
    check("全部留空时不产生新密码", r1["initial"] == [])

    # 表里给了密码 -> 覆盖
    rows_pw = [r[:] for r in all_rows]
    rows_pw[1][3] = "NewPass1234"
    r2 = _imp(rows_pw)
    check("表里有密码 -> 覆盖原密码", _hash("zhangsan") != h_zs)
    check("覆盖后新密码可用", core.verify_password("NewPass1234", _hash("zhangsan")))
    check("覆盖的账号不在 kept 里", "zhangsan" not in r2["kept"])

    # 老账号密码已经没人知道 -> 勾「空密码也重置」，一次拿到全部新密码
    h_now = _hash("zhangsan")
    r3 = _imp(all_rows, reset_empty=True)
    check("勾选后空密码也重置", _hash("zhangsan") != h_now)
    check("重置结果带明文密码", any(x["username"] == "zhangsan" for x in r3["initial"]))
    check("重置后 kept 为空", r3["kept"] == [])
    check("重置密码文件落盘", bool(r3["initial_password_file"]))

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
              "dns": ["10.10.0.53"], "ntp": ["10.10.0.123"], "tun_mtu": 1400,
              "compress": "off", "data_ciphers": "AES-256-GCM:CHACHA20-POLY1305",
              "tls_min": "1.2", "pool_enabled": False,
              "pool_start": "10.8.0.2", "pool_end": "10.8.0.9"}
    check("向导参数保存", P("/api/wizard/params", params).get_json()["ok"])
    check("环境自检 API", P("/api/wizard/check").get_json()["ok"])
    ex = P("/api/wizard/execute", {}).get_json()
    check("安装长任务启动", bool(ex.get("task_id")), ex)
    tj = wait_task(c, ex["task_id"])
    check("安装任务完成", tj["status"] == "done", tj.get("msg"))
    check("初始化标记写入", bool(core.get_meta("initialized")))
    check("向导后 company.ovpn 存在", os.path.exists(os.path.join(core.DATA, "company.ovpn")))
    for path in ("/api/state", "/api/users", "/api/acl", "/api/settings", "/api/settings/preview",
                 "/api/version", "/api/online", "/api/logs?kind=audit", "/api/logs?kind=auth",
                 "/api/logs?kind=deny", "/api/route-guide", "/api/selfcheck", "/api/backups"):
        check("GET " + path, c.get(path).status_code == 200)
    check("导出 ovpn", c.get("/api/client.ovpn").status_code == 200)
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
    check("重置凭据不需要密码", P("/api/admin-reset", {}).get_json()["ok"])
    _reset = core.admin_reset_credentials()   # 直接取明文用于登录断言
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
