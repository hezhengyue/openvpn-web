#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenVPN Web 一体化管理系统 —— 核心逻辑层（无 Web 依赖，可被 app / worker / 认证脚本共用）

设计要点：
  * 无 NAT 的 tun/subnet 路由模式，内网服务器看到的源 IP 即账号固定 IP（溯源）
  * 账号密码认证（auth-user-pass-verify via-file），一人一账号一固定 IP
  * 全员同一个 company.ovpn，不含任何个人凭据
  * ACL 默认拒绝，按「账号固定 IP -> 目标/端口/协议」全量重建 iptables 自定义链
  * 所有写操作先备份快照，失败可回滚；配置/规则全量重建，不做增量
"""
import os
import re
import io
import csv
import json
import time
import copy
import socket
import shutil
import hashlib
import secrets
import sqlite3
import ipaddress
import subprocess
import base64
from datetime import datetime, timedelta

ROOT = os.environ.get("OVPN_WEB_ROOT", "/opt/openvpn-web")
DATA = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA, "openvpn-web.db")
STATE = os.path.join(DATA, "state")
CCD = os.path.join(DATA, "ccd")
TASKS = os.path.join(DATA, "tasks")
SNAPSHOTS = os.path.join(DATA, "snapshots")
STAGING = os.path.join(DATA, "staging")
BACKUPS = os.path.join(DATA, "backups")
BIN = os.path.join(ROOT, "bin", "openvpn")
WORKER = os.path.join(ROOT, "worker.py")
AUTH_VERIFY = os.path.join(ROOT, "auth-verify.py")
PY = os.environ.get("OVPN_WEB_PY") or (
    os.path.join(ROOT, "python", "bin", "python3")
    if os.path.exists(os.path.join(ROOT, "python", "bin", "python3")) else "/usr/bin/python3"
)
MOCK = os.environ.get("OVPN_WEB_MOCK") == "1"

WEB_VERSION = "1.2.0"
MIN_SECURE_VERSION = "2.6.23"          # 2026 年那批 CVE 的修复线
ALLOWED_BRANCH = "2.6"                  # 默认只允许 2.6.x -> 2.6.y
BRANCH_EOL = {"2.6": "2028-08-31", "2.7": "官方未公布（当前 Full Stable）"}
# 账号名允许中文（OpenVPN 的 common name 就是原始字节，ccd 文件名跟着它走）。
# 但仍禁止 / 空格 引号 等会破坏 ccd 文件名或配置行的字符；"." / ".." 单独拦掉。
USERNAME_RE = re.compile(r"^[A-Za-z0-9._\-\u4e00-\u9fff\u3400-\u4dbf]{1,32}$")
FIRST_HOST = 2                          # .1 是服务端自己，.2 起才能分给客户端
LAST_HOST = 254                         # 广播地址留给网段

DEFAULT_CFG = {
    "public_addr": "",
    "port": 1194,
    "proto": "tcp",                 # 默认 TCP：穿透性最好，弱网/被限 UDP 的机房线路更稳
    "vpn_cidr": "10.8.0.0/24",
    # lan_nets 已废弃：页面不再暴露「内网网段」这个概念，只保留一个「附加推送路由」。
    # 旧库里的 lan_nets 会在 init_db 时自动并入 extra_routes，随后清空。
    "lan_nets": [],
    "extra_routes": [],
    "dns": [],
    "ntp": [],
    "tun_mtu": 1400,                # 公网 / PPPoE / 跨境线路的稳妥值，见 ui 提示
    "compress": "off",              # off / lz4-v2 / lzo（改这个必须重新分发 company.ovpn）
    # data-ciphers 是「协商顺序表」不是「同时开多个」：每条连接最终只用协商出来的那一个。
    # 默认两个足够覆盖 AES-NI 服务器与无 AES 加速的 ARM 手机端，少一个是少一次协商分支。
    "data_ciphers": "AES-256-GCM:CHACHA20-POLY1305",
    "tls_min": "1.2",
    # 动态地址池（ifconfig-pool）：默认关闭。
    # 本系统每个账号都由 ccd 的 ifconfig-push 固定下发 IP，池子是给「没台账的临时连接」
    # 兜底用的，用不上就别开；要开就自己指定起止地址，不再写死 .2~.9。
    "pool_enabled": False,
    "pool_start": "10.8.0.2",
    "pool_end": "10.8.0.9",
    "acl_mode": "observe",              # observe=只记录不拦截 / enforce=默认拒绝
    # 网络模式（「无 NAT 溯源」做成默认关闭、可开启的开关）：
    #   nat    默认。对 VPN 网段做 SNAT/MASQUERADE，开箱即用，不需要机房网关配回程路由；
    #          代价是内网服务器日志里的源 IP 全是 VPN 服务器自己，无法溯源到人
    #   no-nat 无 NAT 路由模式。内网服务器看到的是账号固定 IP，可溯源；
    #          必须先让网管在核心网关配 VPN 网段回程静态路由，否则客户端 ping 不通内网
    "nat_mode": "nat",
    "allow_major_upgrade": False,
    "web_allow_cidr": "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
}


# --------------------------------------------------------------------------- 基础

def ensure_dirs():
    for d in (DATA, STATE, CCD, TASKS, SNAPSHOTS, STAGING, BACKUPS):
        os.makedirs(d, exist_ok=True)


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def db():
    ensure_dirs()
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    # journal_mode 只在 init_db 里设一次：每次连接都执行 PRAGMA journal_mode 会去抢写锁，
    # 和后台任务并发时在 Windows 上会抛 "attempt to write a readonly database"。
    con.execute("PRAGMA busy_timeout=10000")
    return con


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY, vpn_ip TEXT UNIQUE NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  password_hash TEXT NOT NULL, note TEXT DEFAULT '', real_name TEXT DEFAULT '',
  dept TEXT DEFAULT '', created_at TEXT, updated_at TEXT);
-- 资产组（容器）与资产条目（真正的 目标/协议/端口）拆开：
-- 一个组可以装 3 个 IP、也可以装 2 个网段，授权时按「组」一次性给
CREATE TABLE IF NOT EXISTS asset_groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS asset_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL, cidr TEXT NOT NULL,
  proto TEXT NOT NULL DEFAULT 'tcp', ports TEXT NOT NULL DEFAULT '', note TEXT DEFAULT '');
-- 用户组：把 3 个账号划成一组，授权时按组给，加减人只改成员表
CREATE TABLE IF NOT EXISTS user_groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS user_group_members (
  group_id INTEGER NOT NULL, username TEXT NOT NULL, UNIQUE(group_id, username));
-- subject_type: user=单个账号 / group=用户组
CREATE TABLE IF NOT EXISTS acl_grants (
  id INTEGER PRIMARY KEY AUTOINCREMENT, subject_type TEXT NOT NULL DEFAULT 'user',
  subject TEXT NOT NULL, asset_group_id INTEGER NOT NULL,
  expires_at TEXT DEFAULT '', note TEXT DEFAULT '',
  UNIQUE(subject_type, subject, asset_group_id));
CREATE TABLE IF NOT EXISTS peer_grants (
  id INTEGER PRIMARY KEY AUTOINCREMENT, src_username TEXT NOT NULL, dst_username TEXT NOT NULL,
  proto TEXT NOT NULL DEFAULT 'any', ports TEXT NOT NULL DEFAULT '',
  expires_at TEXT DEFAULT '', note TEXT DEFAULT '',
  UNIQUE(src_username, dst_username, proto, ports));
CREATE TABLE IF NOT EXISTS auth_attempts (k TEXT NOT NULL, ts REAL NOT NULL, ok INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS auth_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, username TEXT, src_ip TEXT,
  result TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, action TEXT, detail TEXT,
  ip TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, kind TEXT, status TEXT, progress INTEGER, msg TEXT,
  created_at TEXT, finished_at TEXT, actor TEXT);
CREATE TABLE IF NOT EXISTS backups (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, path TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS preview_tokens (
  token TEXT PRIMARY KEY, payload TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS idx_attempts ON auth_attempts(k, ts);
CREATE INDEX IF NOT EXISTS idx_authlog ON auth_log(id DESC);
CREATE INDEX IF NOT EXISTS idx_audit ON audit(id DESC);
CREATE INDEX IF NOT EXISTS idx_items ON asset_items(group_id);
CREATE INDEX IF NOT EXISTS idx_members ON user_group_members(group_id);
CREATE INDEX IF NOT EXISTS idx_aclgrants ON acl_grants(asset_group_id);
"""


def init_db():
    ensure_dirs()
    con = db()
    try:
        # WAL 让 Web 与后台 worker / 认证脚本可以并发读；但 Windows 下多进程同时写 WAL 库
        # 会偶发 "attempt to write a readonly database"（POSIX 无此问题），
        # 所以只在真正的目标平台（Linux）启用 WAL，Windows 本地调试退回 DELETE 日志。
        con.execute("PRAGMA journal_mode=%s" % ("DELETE" if os.name == "nt" else "WAL"))
    except Exception:
        pass
    con.executescript(SCHEMA)
    con.commit()
    for k, v in DEFAULT_CFG.items():
        con.execute("INSERT OR IGNORE INTO meta(k,v) VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))
    for k, v in (("initialized", 0), ("wizard_step", 0), ("admin_hash", ""),
                 ("admin_salt", ""), ("config_fingerprint", ""), ("openvpn_version", ""),
                 ("last_update", ""), ("csrf", ""), ("schema_v2", 0),
                 ("admin_name", ""), ("allow_init_v2", 0)):
        con.execute("INSERT OR IGNORE INTO meta(k,v) VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))
    con.commit()
    # 迁移只搬数据，不建表（表已由 SCHEMA 建好）；万一某台机器上老库结构特殊，
    # 也不能让 Web 起不来——记一笔继续启动，功能降级但服务还在。
    try:
        _migrate(con)
    except Exception as e:
        print("[migrate] 跳过（不影响启动）: %s" % e)
    try:
        _tighten_web_allow(con)
    except Exception as e:
        print("[migrate] 管理端白名单沿用原值: %s" % e)
    con.close()


LEGACY_WEB_ALLOW = "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"


def _tighten_web_allow(con):
    """老库里的白名单是「所有私网」——等于任何一条 VPN（包括别的公司的）都能打开后台。

    只做一次：把「还是老默认值」的白名单收紧成「本机网卡所在网段 + 127.0.0.1」。
    用户自己改过（值不等于老默认）就一个字都不动，避免升级后把人锁在门外。
    """
    if get_meta("allow_init_v2", con=con):
        return 0
    cur = (get_meta("web_allow_cidr", con=con) or "").strip()
    if cur.replace(" ", "") == LEGACY_WEB_ALLOW:
        subnets = local_subnets()
        if subnets:
            set_meta("web_allow_cidr", ",".join(subnets), con=con)
            print("[migrate] 管理端白名单已收紧为: %s" % ",".join(subnets))
    set_meta("allow_init_v2", 1, con=con)
    con.commit()
    return 1


def _has_table(con, name):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (name,)).fetchone())


def _normalize_items(con):
    """把老库里的目标/端口写法统一（10.9.9.9 -> 10.9.9.9/32，端口排序），只跑一次"""
    if get_meta("norm_items_v2", con=con):
        return 0
    n = 0
    for r in con.execute("SELECT id,cidr,proto,ports FROM asset_items").fetchall():
        try:
            c, p, po = norm_item(r["cidr"], r["proto"], r["ports"])
        except Exception:
            continue
        if (c, p, po) != (r["cidr"], r["proto"], r["ports"] or ""):
            con.execute("UPDATE asset_items SET cidr=?,proto=?,ports=? WHERE id=?", (c, p, po, r["id"]))
            n += 1
    set_meta("norm_items_v2", 1, con=con)
    con.commit()
    return n


def _dedupe_items(con):
    """老版本允许「同目标同协议同端口」重复添加，升级时合并一次。

    只处理「整组条目在别的组里都已经存在」的组：把它的授权并到保留组，然后删掉它。
    组里只要还有一条独有目标就不动它，避免误删用户真想要的组。
    """
    if get_meta("dedupe_v2", con=con):
        return 0
    groups = {}
    for it in con.execute("SELECT * FROM asset_items ORDER BY id").fetchall():
        groups.setdefault(it["group_id"], []).append(dict(it))
    owner, merged = {}, 0
    for gid in sorted(groups):
        items = groups[gid]
        keys = [(i["cidr"], i["proto"], norm_ports(i["ports"])) for i in items]
        if keys and all(k in owner for k in keys):
            keep = owner[keys[0]]
            con.execute("INSERT OR IGNORE INTO acl_grants"
                        "(subject_type,subject,asset_group_id,expires_at,note) "
                        "SELECT subject_type,subject,?,expires_at,note FROM acl_grants "
                        "WHERE asset_group_id=?", (keep, gid))
            con.execute("DELETE FROM acl_grants WHERE asset_group_id=?", (gid,))
            con.execute("DELETE FROM asset_items WHERE group_id=?", (gid,))
            con.execute("DELETE FROM asset_groups WHERE id=?", (gid,))
            merged += 1
            continue
        for i, k in zip(items, keys):
            if k in owner and owner[k] != gid:
                con.execute("DELETE FROM asset_items WHERE id=?", (i["id"],))
            else:
                owner.setdefault(k, gid)
    set_meta("dedupe_v2", 1, con=con)
    con.commit()
    return merged


def _migrate(con):
    """老库平滑升级：不动业务数据的原地迁移，可重复执行"""
    # 1) users 补 姓名 / 部门
    cols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
    for col in ("real_name", "dept"):
        if col not in cols:
            con.execute("ALTER TABLE users ADD COLUMN %s TEXT DEFAULT ''" % col)
    # 2) audit 补「操作者来源 IP」（老库里没有这一列，升级后新记录才有值）
    acols = {r[1] for r in con.execute("PRAGMA table_info(audit)")}
    if "ip" not in acols:
        con.execute("ALTER TABLE audit ADD COLUMN ip TEXT DEFAULT ''")
    con.commit()

    if not get_meta("schema_v2", con=con):
        # 2) 「内网网段」并入「附加推送路由」：页面以后只留一个入口
        lan = get_meta("lan_nets", con=con) or []
        routes = list(get_meta("extra_routes", con=con) or [])
        if lan:
            for x in lan:
                if x not in routes:
                    routes.append(x)
            set_meta("extra_routes", routes, con=con)
            set_meta("lan_nets", [], con=con)

        # 3) 旧 assets/grants（一个资产=一个网段）升级成「资产组 + 条目」与「主体授权」
        amap = {}
        if _has_table(con, "assets"):
            for a in con.execute("SELECT id,name,cidr,proto,ports,note FROM assets ORDER BY id").fetchall():
                name = a["name"] or ("资产组-%s" % a["id"])
                row = con.execute("SELECT id FROM asset_groups WHERE name=?", (name,)).fetchone()
                if row:
                    gid = row[0]
                else:
                    cur = con.execute("INSERT INTO asset_groups(name,note) VALUES(?,?)",
                                      (name, a["note"] or ""))
                    gid = cur.lastrowid
                con.execute("INSERT INTO asset_items(group_id,cidr,proto,ports,note) VALUES(?,?,?,?,?)",
                            (gid, a["cidr"], a["proto"], a["ports"] or "", a["note"] or ""))
                amap[a["id"]] = gid
        if _has_table(con, "grants"):
            for g in con.execute("SELECT username,asset_id,expires_at,note FROM grants").fetchall():
                gid = amap.get(g["asset_id"])
                if not gid:
                    continue
                con.execute("INSERT OR IGNORE INTO acl_grants"
                            "(subject_type,subject,asset_group_id,expires_at,note) VALUES('user',?,?,?,?)",
                            (g["username"], gid, g["expires_at"] or "", g["note"] or ""))
        set_meta("schema_v2", 1, con=con)
        con.commit()
        # 指纹算法也换了（按最终下发路由集合算），把基线重算一次，
        # 免得升级后第一次保存设置就误报「配置已变，必须全员重发 ovpn」
        try:
            refresh_fingerprint()
        except Exception:
            pass

    # 4) 条目写法规范化 + 完全重复的资产组合并（各自只跑一次，可安全重复执行）
    try:
        _normalize_items(con)
        n = _dedupe_items(con)
        if n:
            print("[migrate] 合并了 %d 个完全重复的资产组" % n)
    except Exception as e:
        print("[migrate] 条目去重跳过: %s" % e)


def get_meta(key, default=None, con=None):
    own = con is None
    con = con or db()
    row = con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    if own:
        con.close()
    if not row:
        return copy.deepcopy(DEFAULT_CFG.get(key, default))
    try:
        return json.loads(row[0])
    except Exception:
        return row[0]


def set_meta(key, value, con=None):
    own = con is None
    con = con or db()
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, json.dumps(value, ensure_ascii=False)))
    if own:
        con.commit()
        con.close()


def cfg():
    """完整配置 dict"""
    con = db()
    out = {}
    for k in DEFAULT_CFG:
        out[k] = get_meta(k, con=con)
    con.close()
    return out


# --------------------------------------------------------------------------- 密码 / 校验

def hash_password(pw, iterations=200000):
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (iterations, salt, dk.hex())


def verify_password(pw, stored):
    try:
        algo, iters, salt, want = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(iters))
        import hmac
        return hmac.compare_digest(dk.hex(), want)
    except Exception:
        return False


def rand_password(n=16):
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#%-_"
    return "".join(secrets.choice(alphabet) for _ in range(n))


def valid_username(name):
    name = name or ""
    # "." / ".." 会变成 ccd 目录本身，文件名层面也建不出来
    if name.strip(".") == "":
        return False
    return bool(USERNAME_RE.match(name))


def parse_version(v):
    """'2.6.23' -> (2,6,23)"""
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", v or "")
    return tuple(int(x or 0) for x in m.groups()) if m else (0, 0, 0)


def vpn_network(c):
    return ipaddress.IPv4Network(c["vpn_cidr"], strict=False)


def pool_span(c):
    """动态地址池的序号范围 (起, 止)；未启用或配置不合法时返回 None。

    序号 = 相对网络地址的偏移量（.1 = 1）。池子只在
    「pool_enabled 打开、且起止都落在当前 VPN 网段内」时生效，
    这样改了 VPN 网段以后老池子不会静默生效到别的网段上。
    """
    if not c.get("pool_enabled"):
        return None
    net = vpn_network(c)
    try:
        a = int(ipaddress.IPv4Address(str(c.get("pool_start")).strip()))
        b = int(ipaddress.IPv4Address(str(c.get("pool_end")).strip()))
    except Exception:
        return None
    s, e = a - int(net.network_address), b - int(net.network_address)
    if s < FIRST_HOST or e > net.num_addresses - 2 or s > e:
        return None
    return (s, e)


def usable_host_ips(c):
    """台账能用的固定 IP 清单：去掉网络地址、广播地址、服务端自己（.1）与动态池。

    这是唯一口径：分配、校验、导入校验、容量提示都从这里取，
    免得「界面说 245 个、实际只能分 236 个」这种对不上的事。
    """
    net = vpn_network(c)
    last = min(LAST_HOST, net.num_addresses - 2)
    pool = pool_span(c)
    out = []
    for i in range(FIRST_HOST, last + 1):
        if pool and pool[0] <= i <= pool[1]:
            continue
        out.append(str(net.network_address + i))
    return out


def static_capacity(c):
    """台账最多能放多少个固定 IP（一人一个，用满就分配不出来）。"""
    return len(usable_host_ips(c))


def pool_text(c):
    """给界面/日志看的一句话：动态池是开着还是关着、范围多少"""
    span = pool_span(c)
    if not span:
        return "未启用（所有地址都给台账固定分配）"
    net = vpn_network(c)
    return "%s ~ %s" % (net.network_address + span[0], net.network_address + span[1])


def split_around(net, avoid):
    """从 net 中扣掉 avoid，返回剩余子网（avoid 与 net 无交集时原样返回）"""
    if not net.overlaps(avoid):
        return [net]
    if net.prefixlen >= avoid.prefixlen:
        return []
    return sorted(net.address_exclude(avoid), key=lambda n: int(n.network_address))


def push_targets(c, include_vpn=True):
    """真正要下发给客户端的路由清单。

    两件事必须在这里做掉，否则客户端会拿到自相矛盾的 push：
    1) 与 VPN 网段重叠的段做「减法」。例：VPN 10.55.200.0/22、内网 10.55.0.0/16，
       直接推 10.55.0.0/16 会被 OpenVPN 判定与本机 tun 网段冲突而丢弃，
       现象就是"隧道通了但内网一个都 ping 不通"。这里拆成 10.55.0.0/17 … 等几段绕开。
    2) 去重与收敛：/16 和它里面的 /24 同时写，只推 /16。

    include_vpn=True 时把 **VPN 网段本身也放进清单**（排在收敛之后统一处理）：
    客户端要访问 10.8.0.1 上的管理页、要跟别的客户端互访，都得有一条到 VPN 网段的路由。
    以前这行是 render_server_conf 里单独写死的，一旦别处（界面预览、路由指引、
    指纹计算）需要同一份清单就得各写一遍，容易漏；现在只有这一个来源。

    include_vpn=False 只给 config_fingerprint 用：VPN 网段是 ifconfig 的一部分，
    客户端连上就知道，加不加这条 push 都不需要重发 ovpn，不该让它改变指纹。
    """
    vpn = vpn_network(c)
    nets = []
    for item in list(c.get("lan_nets") or []) + list(c.get("extra_routes") or []):
        try:
            n = ipaddress.IPv4Network(str(item).strip(), strict=False)
        except Exception:
            continue
        nets.extend(split_around(n, vpn))
    if include_vpn:
        nets.append(vpn)
    nets = sorted(set(nets), key=lambda n: (int(n.network_address), n.prefixlen))
    out = []
    for n in nets:
        if any(n.subnet_of(o) for o in out):
            continue
        out = [o for o in out if not o.subnet_of(n)]
        out.append(n)
    return sorted(out, key=lambda n: int(n.network_address))


def route_warnings(c):
    """不致命但必须让运维看见的问题（配置页会红字显示）"""
    w = []
    vpn = vpn_network(c)
    for item in list(c.get("lan_nets") or []) + list(c.get("extra_routes") or []):
        try:
            n = ipaddress.IPv4Network(str(item).strip(), strict=False)
        except Exception:
            continue
        if n.overlaps(vpn):
            rest = split_around(n, vpn)
            if not rest:
                shown = "无（完全被 VPN 网段覆盖）"
            elif len(rest) <= 3:
                shown = "、".join(str(x) for x in rest)
            else:
                shown = "%s 等 %d 段（已自动拼成不重叠的子网）" % (
                    "、".join(str(x) for x in rest[:3]), len(rest))
            w.append("%s 与 VPN 网段 %s 重叠：直接推整段会被客户端当成"
                     "「本机 tun 网段」而丢弃，表现为隧道通了但内网不通。"
                     "已自动改成推送 %s。" % (n, vpn, shown))
        if n.prefixlen == 0:
            w.append("推送 0.0.0.0/0 等同让客户端全流量走 VPN，但不接管默认网关，"
                     "结果会变成一半通一半不通；要让全流量走隧道请用 redirect-gateway。")
        if n.is_loopback or n.is_multicast or n.is_link_local:
            w.append("%s 是保留网段，推送没有意义。" % n)
    lan = [ipaddress.IPv4Network(str(x), strict=False) for x in (c.get("lan_nets") or [])
           if str(x).strip()]
    for i, a in enumerate(lan):
        for b in lan[i + 1:]:
            if a.overlaps(b):
                w.append("内网网段 %s 与 %s 重叠，已按最宽的 %s 推送。"
                         % (a, b, a if a.prefixlen <= b.prefixlen else b))
    try:
        enabled = len([u for u in list_users() if u["enabled"]])
        cap = static_capacity(c)
        if enabled > cap:
            w.append("启用账号 %d 个，但网段只能放 %d 个固定 IP：多出来的账号分不到地址，"
                     "请扩大 VPN 网段（或关掉动态地址池腾出地址）。" % (enabled, cap))
    except Exception:
        pass
    return w


def validate_vpn_ip(ip, c, exclude_user=None, con=None):
    net = vpn_network(c)
    try:
        addr = ipaddress.IPv4Address(str(ip).strip())
    except Exception:
        raise ValueError("固定 IP 格式不正确：%s" % ip)
    if addr not in net:
        raise ValueError("固定 IP %s 不在 VPN 网段 %s 内" % (ip, c["vpn_cidr"]))
    span = pool_span(c)
    if span and span[0] <= int(addr) - int(net.network_address) <= span[1]:
        raise ValueError("固定 IP %s 落在动态地址池（%s）里，请换一个" % (ip, pool_text(c)))
    if str(addr) not in usable_host_ips(c):
        raise ValueError("固定 IP %s 不可用：网络/广播地址与 .1（服务端自己）不能分给账号" % ip)
    own = con is None
    con = con or db()
    row = con.execute("SELECT username FROM users WHERE vpn_ip=?", (str(addr),)).fetchone()
    if own:
        con.close()
    if row and row[0] != exclude_user:
        raise ValueError("固定 IP %s 已被账号 %s 占用" % (ip, row[0]))


def validate_cfg(c):
    errs = []
    if not c.get("public_addr") or not re.match(r"^[A-Za-z0-9._:-]{1,128}$", c["public_addr"]):
        errs.append("对外地址/域名不合法")
    try:
        port = int(c["port"])
        if not (1 <= port <= 65535):
            raise ValueError
    except Exception:
        errs.append("端口必须是 1~65535")
    if c["proto"] not in ("udp", "tcp"):
        errs.append("协议只能是 udp 或 tcp")
    try:
        net = vpn_network(c)
        if net.prefixlen > 29 or net.num_addresses < 16:
            raise ValueError
    except Exception:
        errs.append("VPN 网段不合法（至少 /29）")
    for field in ("lan_nets", "extra_routes", "dns", "ntp"):
        for item in c.get(field) or []:
            if field in ("dns", "ntp"):
                try:
                    ipaddress.IPv4Address(item)
                except Exception:
                    errs.append("%s 中的 %s 不是合法 IPv4" % (field, item))
            else:
                try:
                    ipaddress.IPv4Network(item, strict=False)
                except Exception:
                    errs.append("%s 中的 %s 不是合法网段" % (field, item))
    if c["acl_mode"] not in ("observe", "enforce"):
        errs.append("ACL 模式非法")
    if c.get("nat_mode") not in ("nat", "no-nat"):
        errs.append("网络模式只能是 nat / no-nat")
    if c.get("compress", "off") not in ("off", "lz4-v2", "lzo"):
        errs.append("压缩方式只能是 off / lz4-v2 / lzo")
    if not (c.get("data_ciphers") or "").strip():
        errs.append("数据加密套件不能为空")
    else:
        bad = [x for x in c["data_ciphers"].split(":") if x and not re.match(r"^[A-Za-z0-9-]+$", x)]
        if bad:
            errs.append("数据加密套件格式错误：%s（用冒号分隔，如 AES-256-GCM:CHACHA20-POLY1305）"
                        % "、".join(bad))
    try:
        mtu = int(c.get("tun_mtu", 1400))
        if not (576 <= mtu <= 1600):
            errs.append("tun-mtu 需在 576~1600 之间（公网推荐 1400，局域网可 1500）")
    except Exception:
        errs.append("tun-mtu 必须是数字")
    # 动态地址池：要么不开，开了就必须是网段内的合法一段，且不能把地址全吃掉
    if c.get("pool_enabled"):
        try:
            net = vpn_network(c)
            a = ipaddress.IPv4Address(str(c.get("pool_start")).strip())
            b = ipaddress.IPv4Address(str(c.get("pool_end")).strip())
            if a not in net or b not in net:
                errs.append("动态地址池的起止地址必须都在 VPN 网段 %s 内" % c["vpn_cidr"])
            elif int(a) - int(net.network_address) < FIRST_HOST:
                errs.append("动态地址池不能包含 .1（服务端自己占用）")
            elif a > b:
                errs.append("动态地址池的起始地址不能大于结束地址")
            elif not usable_host_ips(c):
                errs.append("动态地址池把可用地址全部占满了，台账就没有固定 IP 可分")
        except Exception:
            errs.append("动态地址池的起止地址不合法")
    return errs


# --------------------------------------------------------------------------- 日志 / 审计

# 当前请求的审计上下文：actor=操作者名称，ip=来源 IP，由 app.py 在每个请求开头写入。
# log_audit 缺省时从这里取值，这样 core 内部（add_user / update_user …）记的日志
# 也能自动带上「谁、从哪个 IP 操作的」，不用把参数一路透传。
AUDIT_CTX = {"actor": "", "ip": ""}


def admin_name():
    """后台操作者名称（可在「管理密码与安全」里改），审计日志用它代替写死的 admin"""
    return (get_meta("admin_name") or "").strip() or "管理员"


def set_admin_name(name):
    name = (name or "").strip()
    if not name or len(name) > 32:
        raise ValueError("操作者名称 1~32 个字符")
    if re.search(r"[\r\n\t]", name):
        raise ValueError("操作者名称不能包含换行或制表符")
    set_meta("admin_name", name)
    return name


def log_audit(actor, action, detail, ip=None):
    ctx = AUDIT_CTX
    # 没登录时的兜底 actor（"admin"/"web"）换成配置里的操作者名称
    if actor in (None, "", "admin", "web") and ctx.get("actor"):
        actor = ctx["actor"]
    if ip is None:
        ip = ctx.get("ip") or ""
    con = db()
    con.execute("INSERT INTO audit(ts,actor,action,detail,ip) VALUES(?,?,?,?,?)",
                (now(), actor, action, detail, ip))
    con.commit()
    con.close()


def log_auth(username, src_ip, result, reason=""):
    con = db()
    con.execute("INSERT INTO auth_log(ts,username,src_ip,result,reason) VALUES(?,?,?,?,?)",
                (now(), username, src_ip, result, reason))
    con.commit()
    con.close()


def get_logs(kind="audit", limit=200):
    con = db()
    if kind == "audit":
        rows = con.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    elif kind == "auth":
        rows = con.execute("SELECT * FROM auth_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    elif kind == "server":
        p = os.path.join(DATA, "openvpn.log")
        if not os.path.exists(p):
            return []
        with open(p, "r", errors="replace") as f:
            lines = f.readlines()[-limit:]
        return [{"ts": "", "detail": x.rstrip()} for x in lines]
    elif kind == "deny":
        p = os.path.join(STATE, "deny.jsonl")
        if not os.path.exists(p):
            return []
        with open(p, "r", errors="replace") as f:
            lines = f.readlines()[-limit:]
        out = []
        for x in lines:
            try:
                out.append(json.loads(x))
            except Exception:
                pass
        return list(reversed(out))
    else:
        rows = []
    con.close()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- 用户台账

def list_users(con=None):
    own = con is None
    con = con or db()
    rows = con.execute("SELECT username,vpn_ip,enabled,note,real_name,dept,created_at,updated_at "
                       "FROM users ORDER BY vpn_ip").fetchall()
    if own:
        con.close()
    return [dict(r) for r in rows]


def next_free_ip(c, con=None):
    own = con is None
    con = con or db()
    used = {r[0] for r in con.execute("SELECT vpn_ip FROM users").fetchall()}
    if own:
        con.close()
    for ip in usable_host_ips(c):
        if ip not in used:
            return ip
    raise ValueError("固定 IP 段已用完（当前网段可用 %d 个）" % static_capacity(c))


def add_user(username, vpn_ip=None, password=None, note="", real_name="", dept="", actor="web"):
    c = cfg()
    if not valid_username(username):
        raise ValueError("账号名只支持中文、字母、数字、点、下划线、连字符，1~32 位，不能有空格")
    con = db()
    if con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        con.close()
        raise ValueError("账号已存在")
    ip = vpn_ip or next_free_ip(c, con)
    validate_vpn_ip(ip, c, con=con)
    pw = password or rand_password()
    con.execute("INSERT INTO users(username,vpn_ip,enabled,password_hash,note,real_name,dept,"
                "created_at,updated_at) VALUES(?,?,1,?,?,?,?,?,?)",
                (username, ip, hash_password(pw), note, real_name or "", dept or "", now(), now()))
    con.commit()
    con.close()
    log_audit(actor, "user.add", "%s -> %s%s" % (username, ip, ("（%s/%s）" % (real_name, dept)) if real_name or dept else ""))
    return {"username": username, "vpn_ip": ip, "password": pw}


def update_user(username, *, vpn_ip=None, enabled=None, note=None, password=None,
                real_name=None, dept=None, actor="web"):
    c = cfg()
    con = db()
    row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        con.close()
        raise ValueError("账号不存在")
    changes = []
    if vpn_ip is not None and vpn_ip != row["vpn_ip"]:
        validate_vpn_ip(vpn_ip, c, exclude_user=username, con=con)
        changes.append("IP %s -> %s" % (row["vpn_ip"], vpn_ip))
    fields, vals = [], []
    if vpn_ip is not None:
        fields.append("vpn_ip=?"); vals.append(vpn_ip)
    if enabled is not None:
        fields.append("enabled=?"); vals.append(1 if enabled else 0)
        changes.append("状态 -> %s" % ("启用" if enabled else "停用"))
    if note is not None:
        fields.append("note=?"); vals.append(note)
    if real_name is not None:
        fields.append("real_name=?"); vals.append(real_name)
    if dept is not None:
        fields.append("dept=?"); vals.append(dept)
    if password:
        fields.append("password_hash=?"); vals.append(hash_password(password))
        changes.append("重置密码")
    fields.append("updated_at=?"); vals.append(now())
    vals.append(username)
    con.execute("UPDATE users SET %s WHERE username=?" % ",".join(fields), vals)
    con.commit()
    con.close()
    log_audit(actor, "user.update", "%s: %s" % (username, "; ".join(changes) or "无变更"))
    return changes


def delete_user(username, actor="web"):
    con = db()
    con.execute("DELETE FROM acl_grants WHERE subject_type='user' AND subject=?", (username,))
    con.execute("DELETE FROM user_group_members WHERE username=?", (username,))
    r = con.execute("DELETE FROM users WHERE username=?", (username,))
    con.commit()
    con.close()
    log_audit(actor, "user.delete", username)
    if not r.rowcount:
        raise ValueError("账号不存在")


def backup_users(actor="web"):
    """每次台账变更前备份，误删可直接把备份当台账导入回滚"""
    ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(BACKUPS, "users-%s.tsv" % ts)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["用户名", "姓名", "部门", "密码", "VPN固定IP", "状态", "备注"])
        for u in list_users():
            w.writerow([u["username"], u.get("real_name") or "", u.get("dept") or "", "",
                        u["vpn_ip"], "启用" if u["enabled"] else "停用", u["note"] or ""])
    con = db()
    con.execute("INSERT INTO backups(ts,kind,path,note) VALUES(?,?,?,?)",
                (now(), "users", path, "台账变更自动备份"))
    con.commit()
    con.close()
    return path


# --------------------------------------------------------------------------- Excel / CSV 台账（全量同步）

EXPORT_HEADER = ["用户名", "姓名", "部门", "密码", "VPN固定IP", "状态", "备注"]


def export_users(fmt="xlsx"):
    rows = [EXPORT_HEADER]
    for u in list_users():
        rows.append([u["username"], u.get("real_name") or "", u.get("dept") or "", "",
                     u["vpn_ip"], "启用" if u["enabled"] else "停用", u["note"] or ""])
    if fmt == "xlsx":
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill
            wb = Workbook()
            ws = wb.active
            ws.title = "认证账号"
            for r in rows:
                ws.append(r)
            for cell in ws[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="2C3E50")
            for col, width in (("A", 18), ("B", 12), ("C", 16), ("D", 18), ("E", 15), ("F", 9), ("G", 26)):
                ws.column_dimensions[col].width = width
            ws.freeze_panes = "A2"
            buf = io.BytesIO()
            wb.save(buf)
            return "用户台账.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        except Exception:
            fmt = "csv"
    sio = io.StringIO()
    w = csv.writer(sio)
    w.writerows(rows)
    return "用户台账.csv", ("\ufeff" + sio.getvalue()).encode("utf-8"), "text/csv; charset=utf-8"


def parse_users_file(content, filename=""):
    """返回 (rows, errs)。主格式 xlsx；顺带兼容 csv/tsv（老台账、手工导出）"""
    rows = []
    try:
        if filename.lower().endswith(".xlsx") or content[:2] == b"PK":
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            ws = wb.active
            for r in ws.iter_rows(values_only=True):
                rows.append([("" if v is None else str(v).strip()) for v in r])
        else:
            text = content.decode("utf-8-sig", "replace")
            delim = "\t" if filename.lower().endswith(".tsv") or "\t" in text.splitlines()[0] else ","
            rows = [[x.strip() for x in r] for r in csv.reader(io.StringIO(text), delimiter=delim)]
    except Exception as e:
        return [], ["文件解析失败: %s" % e]
    if not rows:
        return [], ["空文件"]
    head = [x.replace(" ", "") for x in rows[0]]
    if "用户名" not in head or "VPN固定IP" not in head:
        return [], ["表头必须包含「用户名」和「VPN固定IP」两列（姓名/部门/密码列可留空）"]
    keys = ("用户名", "姓名", "部门", "密码", "VPN固定IP", "状态", "备注")
    idx = {k: head.index(k) for k in keys if k in head}
    out, errs = [], []
    for i, r in enumerate(rows[1:], start=2):
        def cell(k):
            j = idx.get(k)
            return r[j] if j is not None and j < len(r) else ""
        name = cell("用户名")
        if not name:
            continue
        if not valid_username(name):
            errs.append("第 %d 行用户名非法: %s" % (i, name)); continue
        ip = cell("VPN固定IP")
        try:
            ipaddress.IPv4Address(ip)
        except Exception:
            errs.append("第 %d 行固定 IP 非法: %s" % (i, ip)); continue
        state = cell("状态") or "启用"
        if state not in ("启用", "停用", "1", "0"):
            errs.append("第 %d 行状态只能是 启用/停用" % i); continue
        out.append({"username": name, "password": cell("密码"), "vpn_ip": ip,
                    "enabled": state in ("启用", "1"), "note": cell("备注"),
                    "real_name": cell("姓名"), "dept": cell("部门")})
    seen = {}
    for o in out:
        if o["username"] in seen:
            errs.append("用户名重复: %s" % o["username"])
        if o["vpn_ip"] in seen.values():
            errs.append("固定 IP 重复: %s" % o["vpn_ip"])
        seen[o["username"]] = o["vpn_ip"]
    usable = set(usable_host_ips(cfg()))
    for o in out:
        if o["vpn_ip"] not in usable:
            errs.append("%s 的固定 IP %s 不可用（不在 VPN 网段内，或落在动态池 / .1 / 广播地址上）"
                        % (o["username"], o["vpn_ip"]))
    return out, errs


def preview_import(content, filename="", actor="web"):
    """全量同步预览：表里没有的账号 = 删除。返回变更清单 + 5 分钟令牌"""
    rows, errs = parse_users_file(content, filename)
    if errs:
        raise ValueError("整批拒绝，未落盘：\n" + "\n".join(errs))
    # 全表内查重（绕过逐行查重的「已存在」误报）
    ips = [r["vpn_ip"] for r in rows]
    if len(set(ips)) != len(ips):
        raise ValueError("整批拒绝：表内固定 IP 有重复")
    c = cfg()
    usable = set(usable_host_ips(c))
    for r in rows:
        if r["vpn_ip"] not in usable:
            raise ValueError("整批拒绝：%s 不可用（不在 VPN 网段内，或落在动态池 / 保留地址上）"
                             % r["vpn_ip"])
    old = {u["username"]: u for u in list_users()}
    new = {r["username"]: r for r in rows}
    changes = []
    for name, r in new.items():
        if name not in old:
            changes.append({"op": "新增", "username": name,
                            "detail": "%s，%s" % (r["vpn_ip"],
                                                  "使用表中密码" if r["password"] else "自动生成初始密码")})
        else:
            bits = []
            if old[name]["vpn_ip"] != r["vpn_ip"]:
                bits.append("IP %s -> %s（重连生效）" % (old[name]["vpn_ip"], r["vpn_ip"]))
            if bool(old[name]["enabled"]) != r["enabled"]:
                bits.append("状态 -> %s" % ("启用" if r["enabled"] else "停用"))
            # 密码列留空 = 不动原密码（避免拿一份没填密码的台账把所有人密码冲掉）
            bits.append("密码 -> 按表中值覆盖" if r["password"] else "密码保持不变（列留空）")
            if old[name]["note"] != r["note"]:
                bits.append("备注变更")
            if (old[name].get("real_name") or "") != (r.get("real_name") or ""):
                bits.append("姓名 %s -> %s" % (old[name].get("real_name") or "空", r.get("real_name") or "空"))
            if (old[name].get("dept") or "") != (r.get("dept") or ""):
                bits.append("部门 %s -> %s" % (old[name].get("dept") or "空", r.get("dept") or "空"))
            if bits:
                changes.append({"op": "修改", "username": name, "detail": "；".join(bits)})
    for name in old:
        if name not in new:
            changes.append({"op": "删除", "username": name, "detail": old[name]["vpn_ip"]})
    token = secrets.token_urlsafe(24)
    con = db()
    con.execute("DELETE FROM preview_tokens WHERE created_at < ?", (time.time() - 1800,))
    con.execute("INSERT INTO preview_tokens(token,payload,created_at) VALUES(?,?,?)",
                (token, json.dumps(rows, ensure_ascii=False), time.time()))
    con.commit()
    con.close()
    log_audit(actor, "import.preview", "变更 %d 项" % len(changes))
    return {"changes": changes, "token": token, "expires_in": 300}


def apply_import(token, actor="web", reset_empty=False):
    """正式提交全量同步。二次确认由前端弹窗按钮完成；
    这里靠 5 分钟预览令牌防重放，并在写入前自动备份台账。

    密码策略（reset_empty 控制）：
      表里有密码        -> 覆盖（这是用户明确要的「有就覆盖」）
      表里密码为空      -> 默认保留原密码；reset_empty=True 时改为重新生成随机密码
                            （用于「老账号密码已经没人知道了」的批量救急）
      新账号且密码为空  -> 一定生成随机密码，否则建成的是个连不上的死号
    """
    con = db()
    row = con.execute("SELECT payload,created_at FROM preview_tokens WHERE token=?", (token,)).fetchone()
    con.close()
    if not row or time.time() - row["created_at"] > 300:
        raise ValueError("预览已过期（5 分钟），请重新导入")
    rows = json.loads(row["payload"])
    path = backup_users(actor)   # 先备份，误删可把此文件当台账重新导入
    c = cfg()
    con = db()
    initial = []      # 本次产生/重置的明文密码（只在这里出现一次）
    kept = []         # 保留了原密码、没动的账号
    try:
        names = [r["username"] for r in rows]
        keep = set(names)
        gone = [x[0] for x in con.execute("SELECT username FROM users").fetchall() if x[0] not in keep]
        for n in gone:
            con.execute("DELETE FROM acl_grants WHERE subject_type='user' AND subject=?", (n,))
            con.execute("DELETE FROM user_group_members WHERE username=?", (n,))
        if keep:
            q = ",".join("?" * len(keep))
            con.execute("DELETE FROM users WHERE username NOT IN (%s)" % q, list(keep))
        else:
            con.execute("DELETE FROM users")
        for r in rows:
            old = con.execute("SELECT * FROM users WHERE username=?", (r["username"],)).fetchone()
            if old:
                if r["password"]:
                    h = hash_password(r["password"])
                elif reset_empty:
                    pw = rand_password()
                    initial.append({"username": r["username"], "password": pw})
                    h = hash_password(pw)
                else:
                    h = old["password_hash"]
                    kept.append(r["username"])
                con.execute("UPDATE users SET vpn_ip=?,enabled=?,password_hash=?,note=?,"
                            "real_name=?,dept=?,updated_at=? WHERE username=?",
                            (r["vpn_ip"], 1 if r["enabled"] else 0, h, r["note"],
                             r.get("real_name") or "", r.get("dept") or "", now(), r["username"]))
            else:
                pw = r["password"] or rand_password()
                initial.append({"username": r["username"], "password": pw})
                con.execute("INSERT INTO users(username,vpn_ip,enabled,password_hash,note,"
                            "real_name,dept,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                            (r["username"], r["vpn_ip"], 1 if r["enabled"] else 0,
                             hash_password(pw), r["note"], r.get("real_name") or "",
                             r.get("dept") or "", now(), now()))
        con.commit()
    except Exception:
        con.rollback(); con.close()
        raise
    con.close()
    db2 = db()
    db2.execute("DELETE FROM preview_tokens WHERE token=?", (token,))
    db2.commit(); db2.close()
    if initial:
        p = os.path.join(BACKUPS, "初始密码-%s.txt" % datetime.now().strftime("%Y%m%d-%H%M%S"))
        with open(p, "w", encoding="utf-8") as f:
            for x in initial:
                f.write("%s\t%s\n" % (x["username"], x["password"]))
        os.chmod(p, 0o600)
    else:
        p = ""
    log_audit(actor, "import.apply",
              "%d 个账号，重置密码 %d 个，保留原密码 %d 个，备份 %s"
              % (len(rows), len(initial), len(kept), path))
    return {"count": len(rows), "backup": path, "initial_password_file": p,
            "initial": initial, "kept": kept}


# --------------------------------------------------------------------------- 资产组 / 用户组 / 授权

PROTO_ALLOWED = ("tcp", "udp", "icmp", "any")


def norm_ports(ports):
    """端口串规范化：去空格、按数值排序、去重 —— 让 22,80 与 80,22 判定为同一条"""
    out, seen = [], set()
    for p in str(ports or "").replace("，", ",").split(","):
        p = p.strip()
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)

    def key(x):
        a = x.split("-")[0]
        return (int(a) if a.isdigit() else 99999, x)
    return ",".join(sorted(out, key=key))


def _check_ports(ports):
    for p in [x for x in str(ports or "").split(",") if x.strip()]:
        p = p.strip()
        if "-" in p:
            a, b = p.split("-", 1)
            if not (a.isdigit() and b.isdigit() and 1 <= int(a) <= int(b) <= 65535):
                raise ValueError("端口范围非法: %s" % p)
        elif not (p.isdigit() and 1 <= int(p) <= 65535):
            raise ValueError("端口非法: %s" % p)


def norm_cidr(text):
    """目标是单个 IP 时补 /32；网段按掩码规范化（10.0.0.5/24 -> 10.0.0.0/24）"""
    text = str(text or "").strip()
    if not text:
        raise ValueError("目标地址为空")
    if "/" not in text:
        return str(ipaddress.IPv4Network(text + "/32", strict=False))
    return str(ipaddress.IPv4Network(text, strict=False))


def norm_item(cidr, proto="any", ports=""):
    proto = (proto or "any").strip().lower()
    if proto not in PROTO_ALLOWED:
        raise ValueError("协议只能是 tcp/udp/icmp/any")
    cidr = norm_cidr(cidr)
    ports = norm_ports(ports)
    if proto in ("icmp", "any"):
        ports = ""
    else:
        _check_ports(ports)
    return cidr, proto, ports


def parse_item_lines(text):
    """批量粘贴解析，一行一条：目标 [协议] [端口]

        10.10.1.1                      -> 10.10.1.1/32  any
        10.10.1.2 tcp 22,3389          -> 精确到端口
        10.10.2.0/24 tcp 8000-9000
        10.10.3.5:443                   -> 兼容 host:port 写法
        # 井号开头是注释
    """
    out = []
    for line in str(text or "").replace("，", ",").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [x for x in re.split(r"[\s]+", line) if x]
        target = parts[0]
        proto, ports = "", ""
        if len(parts) == 1 and target.count(":") == 1:
            target, ports = target.split(":", 1)
        elif len(parts) >= 2:
            proto = parts[1]
            ports = parts[2] if len(parts) >= 3 else ""
        out.append(norm_item(target, proto or "any", ports))
    return out


def _asset_group_map(con):
    """{id: {id,name,note,items:[...]}}"""
    out = {}
    for r in con.execute("SELECT * FROM asset_groups ORDER BY name"):
        out[r["id"]] = {"id": r["id"], "name": r["name"], "note": r["note"] or "", "items": []}
    for it in con.execute("SELECT * FROM asset_items ORDER BY group_id, cidr, ports"):
        if it["group_id"] in out:
            out[it["group_id"]]["items"].append(dict(it))
    return out


def list_asset_groups(con=None):
    own = con is None
    con = con or db()
    groups = list(_asset_group_map(con).values())
    if own:
        con.close()
    return groups


def _find_dup_item(con, cidr, proto, ports, exclude_id=None):
    """按「规范化后」的目标/协议/端口比对：老库里 10.9.9.9 与 10.9.9.9/32 是同一个东西"""
    for r in con.execute("SELECT i.id,i.cidr,i.proto,i.ports,g.name AS gname "
                         "FROM asset_items i JOIN asset_groups g ON g.id=i.group_id"):
        if exclude_id and r["id"] == exclude_id:
            continue
        try:
            same_cidr = norm_cidr(r["cidr"]) == cidr
        except Exception:
            same_cidr = (r["cidr"] or "") == cidr
        if same_cidr and r["proto"] == proto and norm_ports(r["ports"]) == ports:
            return r
    return None


def _insert_item(con, gid, cidr, proto, ports, note=""):
    cidr, proto, ports = norm_item(cidr, proto, ports)
    dup = _find_dup_item(con, cidr, proto, ports)
    if dup:
        raise ValueError("「%s %s %s」在资产组「%s」里已经有一条一模一样的了"
                         "（同一个目标 + 同一个协议 + 同一组端口），同一份权限不需要加两次。"
                         % (cidr, proto, ports or "全部端口", dup["gname"]))
    con.execute("INSERT INTO asset_items(group_id,cidr,proto,ports,note) VALUES(?,?,?,?,?)",
                (gid, cidr, proto, ports, note or ""))
    return {"cidr": cidr, "proto": proto, "ports": ports}


def _check_group_name(name, what="资产组"):
    if not name or not re.match(r"^[\w\u4e00-\u9fa5.·\- ]{1,40}$", name):
        raise ValueError("%s名称非法（1~40 字，可用中文/字母/数字/空格/·/./-/_）" % what)


def add_asset_group(name, note="", items=None, actor="web"):
    """一次建组并塞进多个条目；items 可以是 [(cidr,proto,ports)] 或 parse_item_lines 的结果"""
    name = (name or "").strip()
    _check_group_name(name)
    norm = [norm_item(i[0], i[1] if len(i) > 1 else "any", i[2] if len(i) > 2 else "")
            for i in (items or [])]
    con = db()
    try:
        if con.execute("SELECT 1 FROM asset_groups WHERE name=?", (name,)).fetchone():
            raise ValueError("资产组「%s」已存在" % name)
        cur = con.execute("INSERT INTO asset_groups(name,note) VALUES(?,?)", (name, note or ""))
        gid = cur.lastrowid
        for cidr, proto, ports in norm:
            _insert_item(con, gid, cidr, proto, ports)
        con.commit()
    except Exception:
        con.rollback(); con.close()
        raise
    con.close()
    log_audit(actor, "assetgroup.add", "%s（%d 条）" % (name, len(norm)))
    return {"id": gid, "count": len(norm)}


def update_asset_group(gid, name=None, note=None, actor="web"):
    con = db()
    row = con.execute("SELECT * FROM asset_groups WHERE id=?", (gid,)).fetchone()
    if not row:
        con.close(); raise ValueError("资产组不存在")
    if name and name.strip() != row["name"]:
        _check_group_name(name.strip())
        if con.execute("SELECT 1 FROM asset_groups WHERE name=? AND id<>?",
                       (name.strip(), gid)).fetchone():
            con.close(); raise ValueError("资产组名称已存在")
        con.execute("UPDATE asset_groups SET name=? WHERE id=?", (name.strip(), gid))
    if note is not None:
        con.execute("UPDATE asset_groups SET note=? WHERE id=?", (note, gid))
    con.commit(); con.close()
    log_audit(actor, "assetgroup.update", "id=%s" % gid)


def delete_asset_group(gid, actor="web"):
    con = db()
    con.execute("DELETE FROM acl_grants WHERE asset_group_id=?", (gid,))
    con.execute("DELETE FROM asset_items WHERE group_id=?", (gid,))
    r = con.execute("DELETE FROM asset_groups WHERE id=?", (gid,))
    con.commit(); con.close()
    if not r.rowcount:
        raise ValueError("资产组不存在")
    log_audit(actor, "assetgroup.delete", "id=%s" % gid)


def add_asset_item(gid, cidr, proto="any", ports="", note="", actor="web"):
    con = db()
    if not con.execute("SELECT 1 FROM asset_groups WHERE id=?", (gid,)).fetchone():
        con.close(); raise ValueError("资产组不存在")
    try:
        r = _insert_item(con, gid, cidr, proto, ports, note)
        con.commit()
    except Exception:
        con.rollback(); con.close()
        raise
    con.close()
    log_audit(actor, "assetitem.add", "组 %s ← %s %s %s" % (gid, r["cidr"], r["proto"], r["ports"]))
    return r


def delete_asset_item(iid, actor="web"):
    con = db()
    r = con.execute("DELETE FROM asset_items WHERE id=?", (iid,))
    con.commit(); con.close()
    if not r.rowcount:
        raise ValueError("条目不存在")
    log_audit(actor, "assetitem.delete", "id=%s" % iid)


# --------------------------------------------------------------------------- 用户组

def list_user_groups(con=None):
    own = con is None
    con = con or db()
    groups = [dict(r) for r in con.execute("SELECT * FROM user_groups ORDER BY name")]
    mem = {}
    for r in con.execute("SELECT m.group_id,m.username,u.vpn_ip,u.real_name,u.dept,u.enabled "
                         "FROM user_group_members m LEFT JOIN users u ON u.username=m.username"):
        mem.setdefault(r["group_id"], []).append({
            "username": r["username"], "vpn_ip": r["vpn_ip"] or "",
            "real_name": r["real_name"] or "", "dept": r["dept"] or "",
            "enabled": bool(r["enabled"])})
    for g in groups:
        g["members"] = mem.get(g["id"], [])
        g["count"] = len(g["members"])
    if own:
        con.close()
    return groups


def _clean_members(con, members):
    if members is None:
        return None
    out, bad = [], []
    for m in members:
        m = str(m or "").strip()
        if not m or m in out:
            continue
        if con.execute("SELECT 1 FROM users WHERE username=?", (m,)).fetchone():
            out.append(m)
        else:
            bad.append(m)
    if bad:
        raise ValueError("这些账号不存在：%s" % "、".join(bad))
    return out


def add_user_group(name, note="", members=None, actor="web"):
    name = (name or "").strip()
    _check_group_name(name, "用户组")
    con = db()
    try:
        if con.execute("SELECT 1 FROM user_groups WHERE name=?", (name,)).fetchone():
            raise ValueError("用户组「%s」已存在" % name)
        mem = _clean_members(con, members) or []
        gid = con.execute("INSERT INTO user_groups(name,note) VALUES(?,?)",
                          (name, note or "")).lastrowid
        for m in mem:
            con.execute("INSERT OR IGNORE INTO user_group_members(group_id,username) VALUES(?,?)",
                        (gid, m))
        con.commit()
    except Exception:
        con.rollback(); con.close()
        raise
    con.close()
    log_audit(actor, "usergroup.add", "%s（%d 人）" % (name, len(mem)))
    return {"id": gid, "count": len(mem)}


def update_user_group(gid, name=None, note=None, members=None, actor="web"):
    con = db()
    row = con.execute("SELECT * FROM user_groups WHERE id=?", (gid,)).fetchone()
    if not row:
        con.close(); raise ValueError("用户组不存在")
    try:
        if name and name.strip() != row["name"]:
            _check_group_name(name.strip(), "用户组")
            if con.execute("SELECT 1 FROM user_groups WHERE name=? AND id<>?",
                           (name.strip(), gid)).fetchone():
                raise ValueError("用户组名称已存在")
            con.execute("UPDATE user_groups SET name=? WHERE id=?", (name.strip(), gid))
        if note is not None:
            con.execute("UPDATE user_groups SET note=? WHERE id=?", (note, gid))
        if members is not None:
            mem = _clean_members(con, members) or []
            con.execute("DELETE FROM user_group_members WHERE group_id=?", (gid,))
            for m in mem:
                con.execute("INSERT OR IGNORE INTO user_group_members(group_id,username) VALUES(?,?)",
                            (gid, m))
        con.commit()
    except Exception:
        con.rollback(); con.close()
        raise
    con.close()
    log_audit(actor, "usergroup.update", "id=%s" % gid)


def delete_user_group(gid, actor="web"):
    con = db()
    name = (con.execute("SELECT name FROM user_groups WHERE id=?", (gid,)).fetchone() or [""])[0]
    con.execute("DELETE FROM acl_grants WHERE subject_type='group' AND subject=?", (name,))
    con.execute("DELETE FROM user_group_members WHERE group_id=?", (gid,))
    r = con.execute("DELETE FROM user_groups WHERE id=?", (gid,))
    con.commit(); con.close()
    if not r.rowcount:
        raise ValueError("用户组不存在")
    log_audit(actor, "usergroup.delete", name or ("id=%s" % gid))


# --------------------------------------------------------------------------- 授权（主体 × 资产组）

def list_grants(con=None):
    own = con is None
    con = con or db()
    amap = _asset_group_map(con)
    ug = {r["name"]: r["id"] for r in con.execute("SELECT id,name FROM user_groups")}
    members = {}
    for r in con.execute("SELECT group_id,username FROM user_group_members"):
        members.setdefault(r["group_id"], []).append(r["username"])
    users = {r["username"]: r["vpn_ip"] for r in con.execute("SELECT username,vpn_ip FROM users")}
    out = []
    for g in con.execute("SELECT * FROM acl_grants ORDER BY id").fetchall():
        d = dict(g)
        ag = amap.get(d["asset_group_id"]) or {"name": "(资产组已删除)", "items": []}
        d["asset_group"] = ag["name"]
        d["items"] = len(ag["items"])
        d["targets"] = ["%s %s%s" % (i["cidr"], i["proto"],
                                     (" " + i["ports"]) if i["ports"] else "") for i in ag["items"]]
        if d["subject_type"] == "group":
            d["members"] = members.get(ug.get(d["subject"], -1), [])
            d["scope"] = len(d["members"])
            d["vpn_ip"] = ""
        else:
            d["scope"] = 1
            d["members"] = [d["subject"]]
            d["vpn_ip"] = users.get(d["subject"], "未登记")
        out.append(d)
    if own:
        con.close()
    return out


def add_grant(subject_type, subject, asset_group_id, expires_at="", note="", actor="web"):
    subject_type = subject_type or "user"
    if subject_type not in ("user", "group"):
        raise ValueError("授权主体只能是 user / group")
    subject = (subject or "").strip()
    try:
        asset_group_id = int(asset_group_id)
    except Exception:
        raise ValueError("请选择资产组")
    if expires_at:
        try:
            datetime.strptime(expires_at, "%Y-%m-%d %H:%M")
        except Exception:
            raise ValueError("到期时间格式：YYYY-MM-DD HH:MM")
    con = db()
    if subject_type == "user":
        if not con.execute("SELECT 1 FROM users WHERE username=?", (subject,)).fetchone():
            con.close(); raise ValueError("账号不存在")
    else:
        if not con.execute("SELECT 1 FROM user_groups WHERE name=?", (subject,)).fetchone():
            con.close(); raise ValueError("用户组不存在")
    if not con.execute("SELECT 1 FROM asset_groups WHERE id=?", (asset_group_id,)).fetchone():
        con.close(); raise ValueError("资产组不存在")
    exists = con.execute("SELECT 1 FROM acl_grants WHERE subject_type=? AND subject=? AND asset_group_id=?",
                         (subject_type, subject, asset_group_id)).fetchone()
    con.execute("INSERT OR REPLACE INTO acl_grants"
                "(subject_type,subject,asset_group_id,expires_at,note) VALUES(?,?,?,?,?)",
                (subject_type, subject, asset_group_id, expires_at, note))
    con.commit(); con.close()
    log_audit(actor, "grant.add", "%s%s -> 资产组 %s" % ("组 " if subject_type == "group" else "", subject, asset_group_id))
    return {"exists": bool(exists)}


def delete_grant(gid, actor="web"):
    con = db()
    r = con.execute("DELETE FROM acl_grants WHERE id=?", (gid,))
    con.commit(); con.close()
    if not r.rowcount:
        raise ValueError("授权不存在")
    log_audit(actor, "grant.delete", "id=%s" % gid)


def active_grants():
    """过滤掉已过期的临时授权"""
    return [g for g in list_grants() if _not_expired(g)]


def expand_grants():
    """把「账号/用户组 × 资产组」展开成最终要写进 iptables 的最小单元"""
    con = db()
    users = {r["username"]: r["vpn_ip"] for r in
             con.execute("SELECT username,vpn_ip FROM users WHERE enabled=1")}
    amap = _asset_group_map(con)
    ug = {r["name"]: r["id"] for r in con.execute("SELECT id,name FROM user_groups")}
    members = {}
    for r in con.execute("SELECT group_id,username FROM user_group_members"):
        members.setdefault(r["group_id"], []).append(r["username"])
    rows = [dict(r) for r in con.execute("SELECT * FROM acl_grants ORDER BY id")]
    con.close()
    out, seen = [], set()
    for g in rows:
        if not _not_expired(g):
            continue
        names = [g["subject"]] if g["subject_type"] == "user" else \
            members.get(ug.get(g["subject"], -1), [])
        items = (amap.get(g["asset_group_id"]) or {}).get("items") or []
        for n in names:
            src = users.get(n)
            if not src:
                continue
            for it in items:
                key = (src, it["cidr"], it["proto"], norm_ports(it["ports"]))
                if key in seen:
                    continue
                seen.add(key)
                out.append({"username": n, "src": src, "cidr": it["cidr"],
                            "proto": it["proto"], "ports": it["ports"]})
    return out


# --------------------------------------------------------------------------- 客户端互访（P2P）

def _not_expired(row):
    exp = row["expires_at"]
    if not exp:
        return True
    try:
        return datetime.strptime(exp, "%Y-%m-%d %H:%M") >= datetime.now()
    except Exception:
        return True


def list_peer_grants():
    con = db()
    rows = con.execute("SELECT * FROM peer_grants ORDER BY id").fetchall()
    con.close()
    return [dict(r) for r in rows]


def active_peer_grants():
    return [r for r in list_peer_grants() if _not_expired(r)]


def add_peer_grant(src, dst, proto="any", ports="", expires_at="", note="", actor="web"):
    if src == dst:
        raise ValueError("源账号与目标账号不能相同")
    if proto not in ("any", "tcp", "udp", "icmp"):
        raise ValueError("协议只能是 any/tcp/udp/icmp")
    con = db()
    names = {r[0] for r in con.execute("SELECT username FROM users WHERE enabled=1")}
    if src not in names or dst not in names:
        con.close()
        raise ValueError("源/目标账号不存在或已停用")
    if proto in ("tcp", "udp") and ports:
        for p in ports.split(","):
            p = p.strip()
            if "-" in p:
                a, b = p.split("-", 1)
                if not (a.isdigit() and b.isdigit() and 1 <= int(a) <= int(b) <= 65535):
                    raise ValueError("端口范围非法: %s" % p)
            elif not (p.isdigit() and 1 <= int(p) <= 65535):
                raise ValueError("端口非法: %s" % p)
    if expires_at:
        try:
            datetime.strptime(expires_at, "%Y-%m-%d %H:%M")
        except Exception:
            con.close(); raise ValueError("到期时间格式：YYYY-MM-DD HH:MM")
    con.execute("INSERT OR REPLACE INTO peer_grants"
                "(src_username,dst_username,proto,ports,expires_at,note) VALUES(?,?,?,?,?,?)",
                (src, dst, proto, ports, expires_at, note))
    con.commit(); con.close()
    log_audit(actor, "peer.add", "%s -> %s (%s %s)" % (src, dst, proto, ports or "全部"))


def delete_peer_grant(pid, actor="web"):
    con = db()
    con.execute("DELETE FROM peer_grants WHERE id=?", (pid,))
    con.commit(); con.close()
    log_audit(actor, "peer.delete", "id=%s" % pid)


def _render_accept(L, src, dst, proto, ports):
    if proto == "any":
        L.append("$IPT -A OVPN_ACL -s %s -d %s -j ACCEPT" % (src, dst))
    elif proto == "icmp":
        L.append("$IPT -A OVPN_ACL -s %s -d %s -p icmp -j ACCEPT" % (src, dst))
    else:
        plist = [p.strip() for p in ports.split(",")] if ports else []
        if not plist:
            L.append("$IPT -A OVPN_ACL -s %s -d %s -p %s -j ACCEPT" % (src, dst, proto))
        for p in plist:
            L.append("$IPT -A OVPN_ACL -s %s -d %s -p %s --dport %s -j ACCEPT"
                     % (src, dst, proto, p.replace("-", ":")))


# --------------------------------------------------------------------------- PKI / 密钥

def openssl_bin():
    """优先用包内静态 openssl（CentOS 7 的 1.0.2 也能签，但最小化系统可能压根没装）"""
    p = os.path.join(ROOT, "bin", "openssl")
    if os.path.exists(p) and os.access(p, os.X_OK):
        return p
    return shutil.which("openssl") or "openssl"


def pki_info():
    """证书台账：主体 / 有效期 / 剩余天数 / 指纹。只读，绝不返回私钥。"""
    out = []
    for name, fn, title in (("ca", "ca.crt", "CA 根证书"),
                            ("server", "server.crt", "服务端证书")):
        p = os.path.join(DATA, fn)
        if not os.path.exists(p):
            continue
        r = subprocess.run([openssl_bin(), "x509", "-in", p, "-noout",
                            "-subject", "-dates", "-serial", "-fingerprint", "-sha256"],
                           capture_output=True, text=True, timeout=30)
        f = {}
        for line in (r.stdout or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                # openssl 3.0 输出 "sha256 Fingerprint="，老版本是 "SHA256 Fingerprint="
                if "fingerprint" in k.lower():
                    k = "fingerprint"
                f[k] = v.strip()
        days = None
        try:
            exp = datetime.strptime(f.get("notAfter", ""), "%b %d %H:%M:%S %Y %Z")
            days = (exp - datetime.utcnow()).days
        except Exception:
            pass
        out.append({
            "name": name, "title": title, "file": fn,
            "subject": f.get("subject", ""), "serial": f.get("serial", ""),
            "not_before": f.get("notBefore", ""), "not_after": f.get("notAfter", ""),
            "fingerprint": f.get("fingerprint", ""),
            "days_left": days,
        })
    return out


def gen_ta_key(path=None):
    """优先调用自带 openvpn --genkey，缺失时生成同样格式的 V1 静态密钥（便于离线体验）"""
    path = path or os.path.join(DATA, "ta.key")
    if os.path.exists(BIN) and os.access(BIN, os.X_OK) and not MOCK:
        try:
            subprocess.run([BIN, "--genkey", "tls-crypt", path], check=True, timeout=20)
            return
        except Exception:
            pass
    lines = []
    raw = secrets.token_bytes(256)
    hx = raw.hex()
    for i in range(0, len(hx), 64):
        lines.append(hx[i:i + 64])
    body = "-----BEGIN OpenVPN Static key V1-----\n" + "\n".join(lines) + \
           "\n-----END OpenVPN Static key V1-----\n"
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, 0o600)


def gen_pki():
    """签 CA + 服务端证书。用系统 openssl 命令，不依赖任何 Python wheel。"""
    ensure_dirs()
    ca_key = os.path.join(DATA, "ca.key")
    ca_crt = os.path.join(DATA, "ca.crt")
    srv_key = os.path.join(DATA, "server.key")
    srv_crt = os.path.join(DATA, "server.crt")
    csr = os.path.join(STATE, "server.csr")
    ext = os.path.join(STATE, "server.cnf")
    caext = os.path.join(STATE, "ca.cnf")
    # -config / -extensions 是 OpenSSL 1.0.2（CentOS 7）与 3.x 都支持的写法；
    # req 的 -addext 只在 1.1.1+ 有，3.x 的 req 又没有 -extfile，所以统一用 config。
    with open(ext, "w") as f:
        f.write("[v3_server]\nbasicConstraints=CA:FALSE\n"
                "keyUsage=digitalSignature,keyEncipherment\n"
                "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:openvpn-server\n")
    with open(caext, "w") as f:
        f.write("[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=OpenVPN-Web-CA\n"
                "[v3_ca]\nbasicConstraints=critical,CA:TRUE\n"
                "keyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n")
    OS = openssl_bin()
    cmds = [
        [OS, "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-days", "3650",
         "-nodes", "-keyout", ca_key, "-out", ca_crt,
         "-config", caext, "-extensions", "v3_ca"],
        [OS, "req", "-new", "-newkey", "rsa:3072", "-nodes", "-keyout", srv_key,
         "-out", csr, "-subj", "/CN=openvpn-server"],
        [OS, "x509", "-req", "-in", csr, "-CA", ca_crt, "-CAkey", ca_key,
         "-CAcreateserial", "-out", srv_crt, "-days", "3650", "-sha256",
         "-extfile", ext, "-extensions", "v3_server"],
    ]
    # 包内静态 openssl 编译时把 openssldir 指到了构建机上的路径，那里没有 openssl.cnf，
    # 直接跑会报 "Can't open .../ssl/openssl.cnf"。用 OPENSSL_CONF 显式指定我们写的配置，
    # 既绕开这个绝对路径，也保证在最小化系统（没有 /etc/pki/tls/openssl.cnf）上能用。
    osenv = dict(os.environ, OPENSSL_CONF=caext)
    for cmd in cmds:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=120, env=osenv)
        if r.returncode != 0:
            raise RuntimeError("openssl 失败: %s" % r.stderr.decode("utf-8", "replace")[-500:])
    for p in (ca_key, srv_key):
        os.chmod(p, 0o600)
    gen_ta_key()
    return {"ca": ca_crt, "cert": srv_crt, "key": srv_key, "ta": os.path.join(DATA, "ta.key")}


def read_pki():
    def rd(p):
        return open(p, "r", errors="replace").read() if os.path.exists(p) else ""
    return {"ca": rd(os.path.join(DATA, "ca.crt")),
            "ta": rd(os.path.join(DATA, "ta.key"))}


# --------------------------------------------------------------------------- 配置渲染

def _mask(c):
    return str(vpn_network(c).netmask)


def _proto_name(c, role):
    """把内部存的值（tcp/udp）翻译成配置文件里合法的写法。

    OpenVPN 2.6 在解析配置文件时，`proto tcp` 会因为无法判断方向而直接报
    "Options error: --proto tcp is ambiguous in this context"，
    服务端要 tcp-server、客户端要 tcp-client。udp 两个方向都写 udp。
    数据库和界面上仍统一存 tcp/udp，只在落盘时翻译，避免到处判断。
    """
    return ("tcp-%s" % role) if c["proto"] == "tcp" else "udp"


def render_server_conf(c):
    net = vpn_network(c)
    local = str(net.network_address + 1)
    # 手工展开 --server 宏：官方 wiki 指出宏会占用整段做动态池，与 ccd 静态地址冲突
    L = [
        "# 由 OpenVPN Web 一体化管理系统生成，请勿手工编辑",
        "port %s" % c["port"],
        # 注意：OpenVPN 2.6 不接受配置文件里裸写 "proto tcp"（报 ambiguous），
        # 服务端必须 tcp-server、客户端必须 tcp-client；udp 无歧义可直接写。
        "proto %s" % _proto_name(c, "server"),
        "dev tun",
        "topology subnet",
        "mode server",
        "tls-server",
        "ifconfig %s %s" % (local, _mask(c)),
    ]
    # 地址池是可选项：本系统每个账号的 IP 都由 ccd 的 ifconfig-push 固定下发，
    # 不需要 DHCP 式的动态分配。只有运维显式打开「动态地址池」才写 ifconfig-pool。
    span = pool_span(c)
    if span:
        L += [
            "# 动态地址池：只给没有台账的临时连接兜底（%s ~ %s）" % (
                net.network_address + span[0], net.network_address + span[1]),
            "ifconfig-pool %s %s" % (net.network_address + span[0], net.network_address + span[1]),
        ]
    else:
        L.append("# 未启用动态地址池：客户端地址全部由 ccd 的 ifconfig-push 固定下发")
    # VPN 网段与附加推送路由统一由 push_targets 给出，避免两处各自拼一份、漏掉一条
    for nw in push_targets(c):
        L.append('push "route %s %s"' % (str(nw.network_address), str(nw.netmask)))
    # 不推送 redirect-gateway：不接管客户端默认网关
    L += [
        "client-config-dir %s" % CCD,
        "ccd-exclusive",
        "username-as-common-name",
        "verify-client-cert none",
        'auth-user-pass-verify "%s %s" via-file' % (PY, AUTH_VERIFY),
        "script-security 2",
        "tmp-dir /dev/shm",
        "ca %s" % os.path.join(DATA, "ca.crt"),
        "cert %s" % os.path.join(DATA, "server.crt"),
        "key %s" % os.path.join(DATA, "server.key"),
        "dh none",
        "tls-crypt %s" % os.path.join(DATA, "ta.key"),
        "data-ciphers %s" % c["data_ciphers"],
        "data-ciphers-fallback AES-256-CBC",
        "tls-version-min %s" % c["tls_min"],
        "tun-mtu %s" % c["tun_mtu"],
        "keepalive 10 120",
        "persist-key",
        "persist-tun",
        # 不写 max-clients：并发上限天然由「一人一个固定 IP + ccd-exclusive」决定，
        # 再叠一个数字只会变成莫名其妙的「TLS Error: max-clients reached」。
        "status %s 10" % os.path.join(DATA, "status.log"),
        "log-append %s" % os.path.join(DATA, "openvpn.log"),
        "verb 3",
        "management 127.0.0.1 7505",
        # 不开 duplicate-cn：同账号只允许一处在线
        # 不开 client-to-client：客户端互访也走 FORWARD，由 ACL 挡掉
    ]
    if c["proto"] == "udp":
        L.append("explicit-exit-notify 1")
    if c.get("compress", "off") == "lz4-v2":
        L += ["compress lz4-v2", 'push "compress lz4-v2"']
    elif c.get("compress", "off") == "lzo":
        L += ["comp-lzo yes", 'push "comp-lzo yes"']
    L += list(c.get("server_extra", []) or [])
    return "\n".join(L) + "\n"


def render_ovpn(c):
    pki = read_pki()
    L = [
        "# OpenVPN Web 一体化管理系统 —— 全员共用这一个文件，不含任何个人凭据",
        "client",
        "dev tun",
        "proto %s" % _proto_name(c, "client"),
        "remote %s %s" % (c["public_addr"], c["port"]),
        "resolv-retry infinite",
        "nobind",
        "persist-key",
        "persist-tun",
        "remote-cert-tls server",
        "tls-version-min %s" % c["tls_min"],
        "tun-mtu %s" % c["tun_mtu"],
        "verb 3",
        "auth-user-pass",
    ]
    if c.get("compress", "off") == "lz4-v2":
        L.append("compress lz4-v2")
    elif c.get("compress", "off") == "lzo":
        L.append("comp-lzo yes")
    L += [
        "<ca>",
        pki["ca"].strip(),
        "</ca>",
        "<tls-crypt>",
        pki["ta"].strip(),
        "</tls-crypt>",
    ]
    return "\n".join(L) + "\n"


def config_fingerprint(c):
    """只换服务端二进制时不变；动了 CA / ta.key / 端口 / 加密套件 / 实际推送路由才变"""
    pki = read_pki()
    # 用「真正下发给客户端的路由集合」而不是原始输入：
    # 把内网网段挪进附加推送路由、或换了种写法但结果相同，都不该触发全员重发 ovpn
    # 排除 VPN 网段本身：它由 ifconfig 下发，客户端连上就知道，不构成重发理由
    routes = ["%s/%s" % (n.network_address, n.prefixlen)
              for n in push_targets(c, include_vpn=False)]
    blob = json.dumps({
        "addr": c["public_addr"], "port": c["port"], "proto": c["proto"],
        "vpn_cidr": c["vpn_cidr"], "routes": routes,
        "ciphers": c["data_ciphers"], "tls_min": c["tls_min"], "mtu": c["tun_mtu"],
        "compress": c.get("compress", "off"),
        "ca": hashlib.sha256(pki["ca"].encode()).hexdigest(),
        "ta": hashlib.sha256(pki["ta"].encode()).hexdigest(),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def refresh_fingerprint():
    fp = config_fingerprint(cfg())
    set_meta("config_fingerprint", fp)
    return fp


def rebuild_ccd():
    """全量重建：清空再写，杜绝改 IP 后旧权限被后来者继承"""
    ensure_dirs()
    for f in os.listdir(CCD):
        p = os.path.join(CCD, f)
        if os.path.isfile(p):
            os.remove(p)
    c = cfg()
    mask = _mask(c)
    for u in list_users():
        if not u["enabled"]:
            continue        # 停用账号没有 ccd，ccd-exclusive 直接拒
        with open(os.path.join(CCD, u["username"]), "w") as f:
            f.write("ifconfig-push %s %s\n" % (u["vpn_ip"], mask))
    return len([u for u in list_users() if u["enabled"]])


def render_rules(c):
    """全量重建：OVPNWEB_NAT（按网络模式）+ OVPN_ACL（按账号授权 + 客户端互访）"""
    net = c["vpn_cidr"]
    mode = c["acl_mode"]
    nat_mode = c.get("nat_mode", "nat")
    L = [
        "#!/bin/bash",
        "# 由 OpenVPN Web 一体化管理系统全量重建，勿手工编辑",
        "# 网络模式: %s   ACL 模式: %s" % (nat_mode, mode),
        "set -e",
        "IPT=iptables",
        "NET='%s'" % net,
        "",
        "# 0) IPv4 转发是路由模式与客户端互访的前提（持久化由 worker 写 sysctl.d）",
        "sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || true",
        "",
        "# 1) 接管 POSTROUTING：先删掉针对 VPN 网段的裸 MASQUERADE/SNAT，避免多套规则打架",
        '$IPT -t nat -S POSTROUTING 2>/dev/null | while IFS= read -r line; do',
        '  case "$line" in',
        '    -A\\ POSTROUTING*)',
        '      if echo "$line" | grep -qF -- "-s $NET" && echo "$line" | grep -qE -- "-j (MASQUERADE|SNAT)"; then',
        '        $IPT -t nat -D POSTROUTING ${line#-A POSTROUTING } 2>/dev/null || true',
        '      fi ;;',
        '  esac',
        "done",
        "",
        "# 2) 自建 NAT 链，只管我们自己的规则，带 comment 便于自检与排障",
        "$IPT -t nat -N OVPNWEB_NAT 2>/dev/null || true",
        "$IPT -t nat -F OVPNWEB_NAT",
        '$IPT -t nat -C POSTROUTING -s "$NET" -j OVPNWEB_NAT 2>/dev/null || '
        '$IPT -t nat -I POSTROUTING 1 -s "$NET" -j OVPNWEB_NAT',
        "# 客户端互访是 tun0 -> tun0，绝不能 NAT，否则对端看到的源 IP 会变成 VPN 服务器",
        "$IPT -t nat -A OVPNWEB_NAT -o tun0 -j RETURN",
    ]
    if nat_mode == "nat":
        L += [
            '# 默认 NAT 模式：开箱可用，不需要机房网关配回程路由；但内网日志源 IP = VPN 服务器',
            '# 固定内网 IP 时优先 SNAT（源地址稳定），探测不到再退回 MASQUERADE',
            'GW=$(ip -4 -o addr show scope global 2>/dev/null | awk \'{split($4,a,"/"); print a[1]; exit}\' || true)',
            'if [ -n "$GW" ]; then',
            '  $IPT -t nat -A OVPNWEB_NAT -m comment --comment OVPNWEB_NAT -j SNAT --to-source "$GW"',
            "else",
            "  $IPT -t nat -A OVPNWEB_NAT -m comment --comment OVPNWEB_NAT -j MASQUERADE",
            "fi",
        ]
    else:
        L += [
            "# 无 NAT 溯源模式：链保持空 = 不做任何地址转换，内网服务器看到账号固定 IP",
            "# 前提：网管已在核心网关配好 VPN 网段回程静态路由，否则客户端 ping 不通内网",
        ]
    L += [
        "",
        "# 3) 回包放行（挂在 FORWARD 最前，幂等；p2p 的回包也靠它）",
        '$IPT -C FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || '
        '$IPT -I FORWARD 1 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT',
        "# 4) ACL 自定义链全量重建",
        "$IPT -N OVPN_ACL 2>/dev/null || true",
        "$IPT -F OVPN_ACL",
        '$IPT -C FORWARD -i tun0 -s "$NET" -j OVPN_ACL 2>/dev/null || '
        '$IPT -I FORWARD 1 -i tun0 -s "$NET" -j OVPN_ACL',
    ]
    # DNS / NTP 基础例外（只放具体 IP，不放整段）
    for ip in c.get("dns") or []:
        for proto in ("udp", "tcp"):
            L.append('$IPT -A OVPN_ACL -s "$NET" -d %s -p %s --dport 53 -j ACCEPT' % (ip, proto))
    for ip in c.get("ntp") or []:
        L.append('$IPT -A OVPN_ACL -s "$NET" -d %s -p udp --dport 123 -j ACCEPT' % ip)
    users = {u["username"]: u["vpn_ip"] for u in list_users() if u["enabled"]}
    # 逐账号 → 内网目标：账号/用户组 × 资产组 已在这里展开成最小单元
    for g in expand_grants():
        _render_accept(L, g["src"], g["cidr"], g["proto"], g["ports"])
    # 客户端互访（P2P）：源/目标都是账号固定 IP，仍走 FORWARD，ACL 与日志全覆盖
    for p in active_peer_grants():
        src, dst = users.get(p["src_username"]), users.get(p["dst_username"])
        if src and dst:
            _render_accept(L, src, dst, p["proto"], p["ports"])
    # 越权尝试日志 + 默认动作
    if mode == "enforce":
        L.append('$IPT -A OVPN_ACL -m limit --limit 5/min --limit-burst 10 -j LOG '
                 '--log-prefix "OVPN_DENY " --log-level 4')
        L.append("$IPT -A OVPN_ACL -j DROP")
    else:
        L.append('$IPT -A OVPN_ACL -m limit --limit 5/min --limit-burst 10 -j LOG '
                 '--log-prefix "OVPN_OBSERVE " --log-level 4')
        L.append("$IPT -A OVPN_ACL -j ACCEPT")
    return "\n".join(L) + "\n"


def write_all_config():
    """把库里的状态落到磁盘（配置/ovpn/规则脚本）。由 Web 写入，worker 负责执行。"""
    ensure_dirs()
    c = cfg()
    with open(os.path.join(DATA, "server.conf"), "w") as f:
        f.write(render_server_conf(c))
    with open(os.path.join(DATA, "company.ovpn"), "w") as f:
        f.write(render_ovpn(c))
    with open(os.path.join(STATE, "rules.sh"), "w") as f:
        f.write(render_rules(c))
    os.chmod(os.path.join(STATE, "rules.sh"), 0o750)
    n = rebuild_ccd()
    return {"ccd": n, "fingerprint": refresh_fingerprint(), "config": c}


# --------------------------------------------------------------------------- 快照 / 回滚

def snapshot(tag="auto"):
    ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    path = os.path.join(SNAPSHOTS, ts)
    os.makedirs(path, exist_ok=True)
    for name in ("server.conf", "company.ovpn", "ca.crt", "ca.key", "server.crt",
                 "server.key", "ta.key"):
        p = os.path.join(DATA, name)
        if os.path.exists(p):
            shutil.copy2(p, path)
    # 数据库用 SQLite 在线备份而不是复制文件：WAL 模式下直接拷 .db 会漏掉还在 -wal 里的事务，
    # 删 -wal 又会让并发的后台任务拿到 "attempt to write a readonly database"。
    try:
        src = db()
        dst = sqlite3.connect(os.path.join(path, "openvpn-web.db"))
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
    except Exception:
        p = os.path.join(DATA, "openvpn-web.db")
        if os.path.exists(p):
            shutil.copy2(p, path)
    shutil.copytree(CCD, os.path.join(path, "ccd"), dirs_exist_ok=True)
    if os.path.exists(BIN):
        shutil.copy2(BIN, os.path.join(path, "openvpn.bin"))
    with open(os.path.join(path, "meta.json"), "w") as f:
        json.dump({"tag": tag, "ts": now(), "cfg": cfg(), "fingerprint": get_meta("config_fingerprint")},
                  f, ensure_ascii=False, indent=2)
    con = db()
    con.execute("INSERT INTO backups(ts,kind,path,note) VALUES(?,?,?,?)",
                (now(), "snapshot", path, tag))
    con.commit(); con.close()
    return path


def restore_snapshot(path):
    if not os.path.isdir(path) or os.path.realpath(path).startswith(os.path.realpath(SNAPSHOTS)) is False:
        raise ValueError("快照路径非法")
    # 任务进度与操作审计不属于「业务状态」，回滚时保留，否则前端轮询会断、审计会丢
    keep = {"tasks": [], "audit": []}
    try:
        con = db()
        keep["tasks"] = [tuple(r) for r in con.execute(
            "SELECT id,kind,status,progress,msg,created_at,finished_at,actor FROM tasks")]
        keep["audit"] = [tuple(r) for r in con.execute(
            "SELECT id,ts,actor,action,detail FROM audit")]
        con.close()
    except Exception:
        pass
    for name in ("server.conf", "company.ovpn", "ca.crt", "ca.key", "server.crt",
                 "server.key", "ta.key"):
        p = os.path.join(path, name)
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(DATA, name))
    # 同样走在线备份：把快照库的内容写回当前库，而不是替换文件。
    # 替换文件会让已经打开的连接（后台任务、正在轮询的请求）全部拿到 DBMOVED / readonly。
    src_db = os.path.join(path, "openvpn-web.db")
    if os.path.exists(src_db):
        src = sqlite3.connect(src_db)
        tgt = db()
        try:
            with tgt:
                src.backup(tgt)
        finally:
            src.close(); tgt.close()
    ccdsrc = os.path.join(path, "ccd")
    if os.path.isdir(ccdsrc):
        for f in os.listdir(CCD):
            os.remove(os.path.join(CCD, f))
        for f in os.listdir(ccdsrc):
            shutil.copy2(os.path.join(ccdsrc, f), CCD)
    init_db()
    if keep["tasks"] or keep["audit"]:
        con = db()
        con.executemany("INSERT OR REPLACE INTO tasks"
                        "(id,kind,status,progress,msg,created_at,finished_at,actor)"
                        " VALUES(?,?,?,?,?,?,?,?)", keep["tasks"])
        con.executemany("INSERT OR IGNORE INTO audit(id,ts,actor,action,detail)"
                        " VALUES(?,?,?,?,?)", keep["audit"])
        con.commit(); con.close()
    meta_p = os.path.join(path, "meta.json")
    if os.path.exists(meta_p):
        m = json.load(open(meta_p))
        for k, v in (m.get("cfg") or {}).items():
            if k in DEFAULT_CFG:
                set_meta(k, v)
        set_meta("config_fingerprint", m.get("fingerprint", ""))
    return True


def list_backups():
    con = db()
    rows = con.execute("SELECT * FROM backups ORDER BY id DESC LIMIT 100").fetchall()
    con.close()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- 版本 / 二进制校验

def binary_version(path=BIN):
    if MOCK and not os.path.exists(path):
        return get_meta("openvpn_version") or "2.6.23", "2.6"
    if not os.path.exists(path):
        return "", ""
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"OpenVPN\s+(\d+\.\d+\.\d+)", out)
        if m:
            v = m.group(1)
            return v, ".".join(v.split(".")[:2])
    except Exception:
        pass
    return "", ""


def ldd_third_party(path):
    """返回不允许出现的第三方动态依赖（静态二进制应只依赖 glibc 一家）

    白名单覆盖 glibc 自带的全部共享库：libc/libm/libpthread/libdl/librt/
    libresolv/libnsl/libutil/libanl 都出自 glibc 包，不算第三方。
    注意 libcrypt 在 RHEL8+ 是独立的 libxcrypt，故意不在白名单里。
    """
    allow = ("libc.so", "libpthread", "libdl.so", "libm.so", "librt.so", "libresolv.so",
             "libnsl.so", "libutil.so", "libanl.so", "ld-linux", "linux-vdso")
    try:
        out = subprocess.run(["ldd", path], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    bad = []
    for line in out.splitlines():
        name = line.strip().split()[0] if line.strip() else ""
        if name.startswith("lib") and not any(name.startswith(a) for a in allow):
            bad.append(name)
    return bad


def build_env():
    """本机能否「上传源码 → 自动编译」：只编 openvpn 本体，依赖用预编译静态库。

    最小化系统常常连 gcc 都没有，这时界面上要提前说清楚，别让用户传完 100MB 源码才失败。
    """
    items = []

    def add(name, ok, detail):
        items.append({"name": name, "ok": bool(ok), "detail": detail})

    for c in ("gcc", "make", "pkg-config", "objdump", "tar"):
        p = shutil.which(c)
        add(c, bool(p), p or "缺失")
    sp = os.path.join(ROOT, "vendor", "static", "prefix.tar.gz")
    add("预编译静态库", os.path.exists(sp),
        sp if os.path.exists(sp) else "缺失（只能上传已编译好的二进制）")
    script = os.path.join(ROOT, "build-static-openvpn.sh")
    add("构建脚本", os.path.exists(script), script if os.path.exists(script) else "缺失")
    free = 0.0
    try:
        st = os.statvfs(STAGING if os.path.exists(STAGING) else "/tmp")
        free = st.f_bavail * float(st.f_frsize) / (1024 ** 3)
        add("临时空间", free >= 2.0 or MOCK, "%.1f GB" % free)
    except Exception as e:
        add("临时空间", True, "无法探测: %s" % e)
    missing = [i["name"] for i in items if not i["ok"]]
    return {"ok": not missing, "items": items, "missing": missing}


def check_binary(path, allow_major=False, expect_sha256=""):
    """上传二进制的闸门：ELF 架构 + SHA256 + 版本安全线 + 分支 + 动态依赖"""
    res = {"ok": False, "version": "", "branch": "", "deps": [], "sha256": "", "msg": ""}
    if not os.path.exists(path):
        res["msg"] = "文件不存在"; return res
    if os.path.getsize(path) > 30 * 1024 * 1024:
        res["msg"] = "文件超过 30MB，不是静态 openvpn 二进制"; return res
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    res["sha256"] = digest
    if expect_sha256 and digest.lower() != expect_sha256.strip().lower():
        res["msg"] = "SHA256 不匹配（本地 %s）" % digest[:16]
        return res
    if not MOCK:
        # 上传的文件会被执行 --version 读版本，先确认它确实是 x86_64 ELF，别把任意脚本交给 root
        with open(path, "rb") as f:
            h = f.read(20)
        if len(h) < 20 or h[:4] != b"\x7fELF" or h[4] != 2 or h[18] != 0x3E:
            res["msg"] = "不是 x86_64 ELF 可执行文件，已拒绝"
            return res
    try:
        os.chmod(path, 0o755)
    except Exception:
        pass
    v, br = binary_version(path)
    if not v:
        # mock 模式下上传的可能是占位文件
        if MOCK:
            res.update(ok=True, version="2.6.23", branch="2.6", msg="（模拟模式）已通过")
            return res
        res["msg"] = "无法执行 --version，可能不是 x86_64 Linux 可执行文件或缺执行权限"; return res
    res["version"], res["branch"] = v, br
    if parse_version(v) < parse_version(MIN_SECURE_VERSION):
        res["msg"] = "版本 %s 低于安全线 %s（2026 年多个远程 DoS/内存破坏 CVE）" % (v, MIN_SECURE_VERSION)
        return res
    cur, curbr = binary_version(BIN)
    if curbr and br != curbr and not allow_major:
        res["msg"] = "跨大版本 %s -> %s 被闸门拦截；如确认要升级请勾选「允许跨大版本」并先做并行实测" % (curbr, br)
        return res
    if br != ALLOWED_BRANCH and not allow_major:
        res["msg"] = "当前策略锁定 %s 分支（允许跨大版本未开启）" % ALLOWED_BRANCH
        return res
    bad = ldd_third_party(path)
    res["deps"] = bad
    if bad and not MOCK:
        res["msg"] = "检测到第三方动态依赖 %s；请用静态链接版本，否则换系统起不来" % ", ".join(bad)
        return res
    res["ok"] = True
    res["msg"] = "校验通过：%s（%s 分支，EOL %s）" % (v, br, BRANCH_EOL.get(br, "未知"))
    return res


def version_info():
    v, br = binary_version()
    return {
        "version": v or "未安装", "branch": br, "eol": BRANCH_EOL.get(br, "未知"),
        "min_secure": MIN_SECURE_VERSION, "locked_branch": ALLOWED_BRANCH,
        "secure": bool(v) and parse_version(v) >= parse_version(MIN_SECURE_VERSION),
        "fingerprint": get_meta("config_fingerprint"), "web_version": WEB_VERSION,
        "last_update": get_meta("last_update"),
        "binary_exists": os.path.exists(BIN),
    }


# --------------------------------------------------------------------------- 在线用户 / 自检 / 路由指引

def online_users():
    p = os.path.join(DATA, "status.log")
    if not os.path.exists(p):
        return []
    try:
        text = open(p, "r", errors="replace").read()
    except Exception:
        return []
    out, section = [], ""
    for line in text.splitlines():
        if line.startswith("OpenVPN CLIENT LIST"):
            section = "client"; continue
        if line.startswith("ROUTING TABLE"):
            section = "route"; continue
        if line.startswith("GLOBAL STATS"):
            section = ""; continue
        if line.startswith("Common Name,") or line.startswith("Virtual Address,") or line.startswith("Updated,"):
            continue
        if section == "client" and "," in line:
            f = line.split(",")
            out.append({"username": f[0], "real_addr": f[1] if len(f) > 1 else "",
                        "bytes_recv": f[2] if len(f) > 2 else "", "bytes_sent": f[3] if len(f) > 3 else "",
                        "since": f[4] if len(f) > 4 else "", "vpn_ip": ""})
    users = {u["username"]: u["vpn_ip"] for u in list_users()}
    for o in out:
        o["vpn_ip"] = users.get(o["username"], "未登记")
    return out


def mgmt_kill(username):
    """踢下线：裸 socket 给 management 接口发一句 kill，不引第三方库"""
    if MOCK:
        return True
    try:
        s = socket.create_connection(("127.0.0.1", 7505), timeout=3)
        s.sendall(("kill %s\n" % username).encode())
        s.recv(4096)
        s.close()
        return True
    except Exception:
        return False


def env_check():
    items = []
    def add(name, ok, detail, fatal=False):
        items.append({"name": name, "ok": bool(ok), "detail": detail, "fatal": fatal})
    import platform
    add("CPU 架构", platform.machine() in ("x86_64", "amd64"), platform.machine(), True)
    add("Python", True, "%s（%s）" % (platform.python_version(), PY))
    tun = os.path.exists("/dev/net/tun")
    add("/dev/net/tun", tun or MOCK, "存在" if tun else "不存在（容器/云主机需映射 tun 设备）" if not MOCK else "（模拟）", True)
    exists = os.path.exists(BIN)
    add("openvpn 二进制", exists, BIN if exists else "未放置，可在下一步直接上传", True)
    if exists:
        v, br = binary_version(BIN)
        add("版本安全线 %s" % MIN_SECURE_VERSION,
            parse_version(v) >= parse_version(MIN_SECURE_VERSION), "%s（%s）" % (v, br), True)
        bad = ldd_third_party(BIN)
        add("静态链接", not bad, "仅依赖 glibc" if not bad else "多出: %s" % ", ".join(bad))
    try:
        s = socket.socket(); s.bind(("0.0.0.0", int(cfg()["port"]))); s.close()
        add("服务端口 %s" % cfg()["port"], True, "空闲")
    except Exception as e:
        add("服务端口 %s" % cfg()["port"], False, "被占用: %s" % e, True)
    ob = openssl_bin()
    add("openssl 命令", os.path.exists(ob) or shutil.which(ob) is not None, ob, True)
    be = build_env()
    add("源码编译能力", be["ok"],
        "可上传源码自动编译" if be["ok"] else "缺少 %s（只能上传已编译的静态二进制）"
        % "、".join(be["missing"]))
    add("iptables 命令", shutil.which("iptables") is not None or MOCK,
        shutil.which("iptables") or "（模拟）", True)
    add("systemd", shutil.which("systemctl") is not None or MOCK, shutil.which("systemctl") or "（模拟）")
    add("firewalld", shutil.which("firewall-cmd") is not None,
        "检测到 firewalld，规则需定时重放（已内置）" if shutil.which("firewall-cmd") else "未启用")
    add("已有配置", not os.path.exists(os.path.join(DATA, "server.conf")),
        "首次安装" if not os.path.exists(os.path.join(DATA, "server.conf")) else "检测到旧配置，安装向导会先做快照")
    return items


def selfcheck():
    c = cfg()
    nat = c.get("nat_mode", "nat")
    out = []
    def add(name, ok, detail):
        out.append({"name": name, "ok": bool(ok), "detail": detail})
    add("数据库台账", True, "%d 个账号" % len(list_users()))
    conf = os.path.join(DATA, "server.conf")
    add("server.conf", os.path.exists(conf), conf)
    if os.path.exists(conf):
        t = open(conf).read()
        add("服务端无破坏性指令", "duplicate-cn" not in t and "client-to-client" not in t,
            "未开 duplicate-cn / client-to-client；p2p 走内核 FORWARD，ACL 与日志才能覆盖")
        add("账号名作 CN", "username-as-common-name" in t, "ccd 文件名与状态日志均使用用户名")
    users = [u for u in list_users() if u["enabled"]]
    ccd_files = os.listdir(CCD) if os.path.isdir(CCD) else []
    add("ccd 与台账一致", len(ccd_files) == len(users),
        "启用账号 %d / ccd 文件 %d" % (len(users), len(ccd_files)))
    add("company.ovpn", os.path.exists(os.path.join(DATA, "company.ovpn")),
        os.path.join(DATA, "company.ovpn"))
    add("配置指纹", bool(get_meta("config_fingerprint")), get_meta("config_fingerprint") or "未生成")
    rules = os.path.join(STATE, "rules.sh")
    add("ACL 规则脚本", os.path.exists(rules), "模式: %s" % c["acl_mode"])
    add("ACL 模式", True, "观察（只记录）" if c["acl_mode"] == "observe" else "强制（默认拒绝）")
    add("网络模式", True, "NAT（默认，免回程路由，内网日志不可溯源到人）" if nat == "nat"
        else "无 NAT 溯源（内网服务器看到账号固定 IP，需网关回程路由）")
    add("内网服务器授权", True, "%d 条授权 / 展开后 %d 条规则 / 客户端互访 %d 条"
        % (len(active_grants()), len(expand_grants()), len(active_peer_grants())))
    add("用户组 / 资产组", True, "%d 个用户组 / %d 个资产组"
        % (len(list_user_groups()), len(list_asset_groups())))
    try:
        fwd = open("/proc/sys/net/ipv4/ip_forward").read().strip()
    except Exception:
        fwd = "0"
    add("IPv4 转发", MOCK or fwd == "1",
        "已开启（路由模式与客户端互访的前提）" if (MOCK or fwd == "1") else "未开启，点【重建规则】会自动设置")
    add("openvpn 进程", MOCK or _vpn_alive(), "运行中" if (MOCK or _vpn_alive()) else "未运行")
    if not MOCK:
        # Web 以低权限用户运行，自己跑 iptables 只会 EPERM（于是「已加载 0 条」乱报）。
        # 优先读 worker 刚采样的 state/system-check.json，读不到再自己试。
        snap = {}
        try:
            with open(os.path.join(STATE, "system-check.json"), "r") as f:
                snap = json.load(f)
        except Exception:
            snap = {}

        def ipt(key, args):
            if isinstance(snap.get(key), str) and snap.get(key):
                return snap[key]
            try:
                r = subprocess.run(args, capture_output=True, text=True, timeout=5)
                return r.stdout if r.returncode == 0 else ""
            except Exception:
                return ""

        acl_txt = ipt("acl", ["iptables", "-S", "OVPN_ACL"])
        n_acl = len([l for l in acl_txt.splitlines() if l.startswith("-A OVPN_ACL")])
        add("OVPN_ACL 链", n_acl > 0,
            "已加载 %d 条" % n_acl if n_acl else
            "未加载（点【重建规则】；若刚装好还没下过规则也正常）")
        nats = ipt("nat_chain", ["iptables", "-t", "nat", "-S", "OVPNWEB_NAT"])
        has_nat = "MASQUERADE" in nats or "SNAT" in nats
        if nat == "no-nat":
            post = ipt("nat", ["iptables", "-t", "nat", "-S", "POSTROUTING"])
            stray = [l for l in post.splitlines()
                     if c["vpn_cidr"] in l and ("MASQUERADE" in l or "SNAT" in l)]
            add("无 NAT 残留", not has_nat and not stray,
                "干净，内网服务器能看到账号固定 IP" if not has_nat and not stray
                else "发现 %d 条 NAT 规则，溯源会失效，请点【重建规则】" % (len(stray) + int(has_nat)))
        else:
            add("NAT 生效", has_nat, "OVPNWEB_NAT 已做地址转换（内网日志源 IP = VPN 服务器）"
                if has_nat else "未生效，点【重建规则】")
    return out


def _vpn_alive():
    pidf = os.path.join(DATA, "openvpn.pid")
    if not os.path.exists(pidf):
        return False
    try:
        pid = int(open(pidf).read().strip())
    except Exception:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # openvpn 是 root 起的，Web 是 ovpnweb，杀 0 号信号会 EPERM——这恰恰说明进程还活着
        return True
    except Exception:
        return False


def route_guide():
    c = cfg()
    net = ipaddress.IPv4Network(c["vpn_cidr"], strict=False)
    gw = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80)); gw = s.getsockname()[0]; s.close()
    except Exception:
        gw = "<VPN服务器内网IP>"
    nat = c.get("nat_mode", "nat")
    common = [
        "给 VPN 服务器与内网服务器开 NTP，时间戳对不齐就串不起溯源链",
        "中间有 LB / 反向代理 / NAT 网关时源 IP 会变，那层需透传",
    ]
    if nat == "nat":
        checklist = [
            "当前 NAT 模式：上面命令不用执行，客户端开箱即可访问内网",
            "要溯源到人时，先开「无 NAT 溯源」再让网管按上面命令配",
            "别再手工加 masquerade，NAT 由 OVPNWEB_NAT 链自管",
        ] + common
    else:
        checklist = [
            "不配这一步，客户端能连上但 ping 不通任何内网服务器",
            "网关改不了时，给每台内网服务器加主机路由（命令见下）",
            "配好后内网日志的源 IP 应是账号固定 IP；仍是服务器自己说明有 NAT 残留",
        ] + common
    return {
        "vpn_net": str(net), "gateway_hint": gw, "nat_mode": nat,
        "required": nat == "no-nat",
        "linux": "ip route add %s via %s   # 在机房核心网关上执行" % (net, gw),
        "h3c": "ip route-static %s %s %s" % (str(net.network_address), str(net.netmask), gw),
        "cisco": "ip route %s %s %s" % (str(net.network_address), str(net.netmask), gw),
        "per_host": "ip route add %s via %s   # 网关改不了时，每台内网服务器都加" % (net, gw),
        "checklist": checklist,
    }


# --------------------------------------------------------------------------- 管理密码 / 会话

def admin_set_password(pw):
    if len(pw) < 8:
        raise ValueError("管理密码至少 8 位")
    set_meta("admin_hash", hash_password(pw))
    log_audit("admin", "password.change", "管理密码已修改")


def admin_verify(pw):
    h = get_meta("admin_hash")
    return bool(h) and verify_password(pw, h)


def user_by_vpn_ip(ip):
    """按来源 IP 反查账号。

    用户拨上 VPN 后，他的源地址就是台账里分配给他的那个固定 IP，
    所以自助改密页不需要让人再手打一遍 IP —— 直接认来源地址即可。
    公网直连（没拨 VPN）时查不到，前端会退回手填模式。
    """
    try:
        ip = str(ipaddress.IPv4Address((ip or "").strip()))
    except Exception:
        return None
    con = db()
    r = con.execute("SELECT username,real_name,dept,vpn_ip,enabled FROM users WHERE vpn_ip=?",
                    (ip,)).fetchone()
    con.close()
    return dict(r) if r else None


def self_change_password(vpn_ip, old, new, src_ip=""):
    """用户自助改密：用「固定 IP + 旧密码」认人，不需要登录后台。

    只返回"成功/失败"，失败原因不区分「IP 不存在」「账号停用」「旧密码错」，
    避免被拿来枚举台账里有哪些固定 IP。
    vpn_ip 传空时用来源 IP 兜底（拨着 VPN 访问 = 来源就是他自己的固定 IP）。
    """
    vpn_ip = (vpn_ip or "").strip() or (src_ip or "").strip()
    old = old or ""
    new = new or ""
    try:
        vpn_ip = str(ipaddress.IPv4Address(vpn_ip))
    except Exception:
        raise ValueError("账号或原密码不正确")
    con = db()
    row = con.execute("SELECT username,enabled,password_hash FROM users WHERE vpn_ip=?",
                      (vpn_ip,)).fetchone()
    con.close()
    if not row or not row["enabled"] or not verify_password(old, row["password_hash"]):
        log_auth(vpn_ip, src_ip, "self-fail", "自助改密校验失败")
        raise ValueError("账号或原密码不正确")
    if new == old:
        raise ValueError("新密码不能与原密码相同")
    if len(new) < 8:
        raise ValueError("新密码至少 8 位")
    if len(new) > 128:
        raise ValueError("新密码最长 128 位")
    update_user(row["username"], password=new, actor="self(%s)" % (src_ip or vpn_ip))
    log_auth(row["username"], src_ip, "self-ok", "自助改密成功")
    return {"username": row["username"]}


def vpn_client_allowed(ip):
    """来源是不是本机 VPN 隧道里的客户端（只给自助改密用，后台仍需管理密码）"""
    try:
        addr = ipaddress.IPv4Address(ip)
        return addr in vpn_network(cfg())
    except Exception:
        return False


def local_subnets():
    """本机各网卡所在网段 —— 管理端访问白名单的推荐值。

    优先读 `ip -4 -o addr`（Linux 目标环境），拿不到就用 UDP 探测出口地址按 /24 兜底；
    第一项固定是 127.0.0.1，保证本机命令行还能自救。

    隧道网卡（tun/tap/ppp）与 VPN 网段一律排除：VPN 拨号用户只该用自助改密页，
    不该因为「服务器自己有一张 tun0 网卡」就顺带拿到管理后台。
    """
    out = ["127.0.0.1/32"]
    try:
        vpn = vpn_network(cfg())
    except Exception:
        vpn = None
    nets = []

    def add(n):
        try:
            n = ipaddress.IPv4Network(str(n), strict=False)
        except Exception:
            return
        if n.is_loopback or n.is_link_local:
            return
        if vpn and n.overlaps(vpn):
            return
        if str(n) not in out:
            out.append(str(n))

    try:
        r = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True,
                           text=True, timeout=5)
        for line in r.stdout.splitlines():
            ifname = (line.split()[1] if len(line.split()) > 1 else "")
            if ifname.startswith(("tun", "tap", "ppp", "wg", "ipsec")):
                continue
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", line)
            if m and not m.group(1).startswith("127."):
                add("%s/%s" % (m.group(1), m.group(2)))
    except Exception:
        pass
    if len(out) == 1:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("223.5.5.5", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127."):
                add(ip + "/24")
        except Exception:
            pass
    return out


def allow_list():
    """管理端访问白名单（CIDR 列表）。空 = 只允许本机，绝不等于「全部放行」。"""
    raw = get_meta("web_allow_cidr") or ""
    if isinstance(raw, list):
        items = raw
    else:
        items = re.split(r"[,，\s]+", str(raw))
    out = [x.strip() for x in items if x.strip()]
    return out or ["127.0.0.1/32"]


def set_allow_list(items, keep_ip=""):
    """保存白名单。必须保证 keep_ip（当前操作者）仍在名单内，否则会把自己关在门外。"""
    clean, errs = [], []
    for x in items or []:
        x = str(x).strip()
        if not x:
            continue
        try:
            n = ipaddress.IPv4Network(x, strict=False)
        except Exception:
            errs.append(x)
            continue
        if str(n) not in clean:
            clean.append(str(n))
    if errs:
        raise ValueError("这些不是合法网段：%s（例：10.20.30.0/24）" % "、".join(errs))
    if not clean:
        raise ValueError("白名单不能为空；想只允许本机请填 127.0.0.1/32")
    if keep_ip:
        try:
            addr = ipaddress.IPv4Address(keep_ip)
            if not any(addr in ipaddress.IPv4Network(n) for n in clean):
                raise ValueError("保存后你自己（%s）就进不来了：请把当前来源网段加进白名单" % keep_ip)
        except ValueError:
            raise
        except Exception:
            pass
    set_meta("web_allow_cidr", ",".join(clean))
    log_audit("", "security.allow", "白名单 -> %s" % ",".join(clean))
    return clean


def client_allowed(ip):
    """来源 IP 是否允许打开管理端（自助改密页另有 vpn_client_allowed 兜底）"""
    try:
        addr = ipaddress.IPv4Address((ip or "").strip())
    except Exception:
        return False
    if addr.is_loopback:
        return True
    for cidr in allow_list():
        try:
            if addr in ipaddress.IPv4Network(cidr, strict=False):
                return True
        except Exception:
            continue
    return False
