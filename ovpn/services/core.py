# ovpn/services/core.py
"""Web 侧业务门面。

分工：
  * `system/core.py`  —— 纯逻辑层（网络计算、配置渲染、iptables 渲染、PKI、版本校验、
    密码哈希……）与特权脚本共用的 SQLite 实现。**零 Django 依赖**，给
    `system/worker.py`、`system/auth-verify.py`、`system/setup.py` 用。
  * 本模块 —— 把纯逻辑整体继承过来，再**用 Django ORM 重新实现全部数据访问**，
    供 Web 进程使用。源码根内不出现任何 `sqlite3.connect` 或业务 `cursor.execute`。

对外函数名、参数、返回结构与原 Flask 版 `core.py` 完全一致，`ovpn/views.py`
和前端 `index.html` 一行都不用改。
"""
import copy
import json
import os
import sys
import time
import ipaddress
from datetime import datetime

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

# 纯逻辑层：安装根下的 system 包（交付包与源码布局都成立）
_ROOT = str(getattr(settings, 'PROJECT_ROOT', '') or '')
if _ROOT and _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from system import core as _base  # noqa: E402

from ..models import (  # noqa: E402
    SystemSetting, VpnUser, AssetGroup, AssetItem, UserGroup, UserGroupMember,
    AclGrant, PeerGrant, AuthLog, OvpnTask, BackupRecord,
)

from audit.models import AuditEventType, AuditLog  # noqa: E402
from audit.services import audit_log as _audit_write  # noqa: E402

# --------------------------------------------------------------------------- 继承纯逻辑
# 常量（ROOT/DATA/BIN/DEFAULT_CFG/WEB_VERSION…）、纯函数（hash_password、vpn_network、
# render_server_conf、render_rules、gen_pki、online_users、allow_list…）全部直接继承，
# 不做第二份实现。数据访问函数在下面逐个覆盖。
for _k, _v in list(vars(_base).items()):
    if not _k.startswith('__'):
        globals()[_k] = _v

# 下面这些原实现依赖裸 SQL 或已由 Django 迁移接管，统一覆盖掉。
_PREVIEW_CACHE_PREFIX = 'ovpn:preview:'
_PREVIEW_TTL = 300


# --------------------------------------------------------------------------- 基础设施

def db():
    """原实现返回 sqlite3 连接。Web 侧已全部改走 ORM，这里显式拒绝，避免有人绕回来。"""
    raise RuntimeError(
        "Web 侧不再直接持有数据库连接：请使用 ovpn/models.py 里的模型。"
        "特权脚本（system/）仍可用 system.core.db()。")


def init_db():
    """建目录 + 收紧权限；建表交给 Django migrations。"""
    ensure_dirs()
    secure_data_perms()


def _enabled_int(v):
    """原库 enabled 是 0/1 整数，前端按原样渲染。这里保持返回 int，不返回 bool。"""
    return 1 if v else 0


# --------------------------------------------------------------------------- 配置项

def get_meta(key, default=None, con=None):
    row = SystemSetting.objects.filter(k=key).values_list('v', flat=True).first()
    if row is None:
        return copy.deepcopy(DEFAULT_CFG.get(key, default))
    try:
        return json.loads(row)
    except Exception:
        return row


def set_meta(key, value, con=None):
    SystemSetting.objects.update_or_create(
        k=key, defaults={'v': json.dumps(value, ensure_ascii=False)})


def cfg():
    return {k: get_meta(k) for k in DEFAULT_CFG}


# --------------------------------------------------------------------------- 审计 / 日志

# 动作码前缀 → 模块名。审计后台按模块筛选，前端日志页显示原始动作码。
_AUDIT_MODULES = {
    'user': 'VPN 账号', 'users': 'VPN 账号',
    'assetgroup': '资产组', 'assetitem': '资产条目',
    'usergroup': '用户组', 'grant': 'ACL 授权', 'peer': '互联授权',
    'security': '安全策略', 'settings': '系统设置', 'wizard': '初始化向导',
    'update': '版本更新', 'backup': '备份恢复', 'user_backup': '账号备份',
    'password': '密码', 'ca': '证书', 'rules': '防火墙规则',
    'service': '服务', 'network': '网络', 'ovpn': '客户端配置',
    'login': '登录', 'logout': '退出', 'import': '批量导入',
    'setup': '安装', 'unit': '自检', 'admin': '管理账号',
}

# 这些动作归 security，其余归 business。分级是为了在后台按「安全事件」快速筛。
_SECURITY_ACTIONS = frozenset({
    'login', 'logout', 'login.fail', 'password.change',
    'security.allow', 'ca.reinit', 'admin.reset',
})


def _audit_module(action):
    prefix = (action or '').split('.')[0]
    return _AUDIT_MODULES.get(prefix, prefix)[:100]


def log_audit(actor, action, detail, ip=None):
    """统一审计写入口（薄壳）。

    **签名不许改**：views/auth 共 50 处调用点、前端动作码、selftest 都挂在
    (actor, action, detail, ip) 这组参数上。内部转调 audit.services.audit_log()，
    操作人一律以字符串快照落 actor_name，不建外键、不依赖调用方是 User 实例。

    线程内的兜底上下文（system.core.audit_ctx_set）仍然有效：后台任务线程用
    它把「谁发起的」带进来，Web 请求则由 AuditContextMiddleware 提供 request。
    """
    _a, _i = audit_ctx_get()
    if actor in (None, "", "admin", "web") and _a:
        actor = _a
    if ip is None:
        ip = _i or ""
    action = action or ""
    _audit_write(
        action=action,
        actor_name=str(actor or ""),
        remote_addr=str(ip or ""),
        message=detail or "",
        module=_audit_module(action),
        event_type=(AuditEventType.SECURITY if action in _SECURITY_ACTIONS
                    else AuditEventType.BUSINESS),
    )


def log_auth(username, src_ip, result, reason=""):
    AuthLog.objects.create(ts=now(), username=username or "", src_ip=src_ip or "",
                           result=result or "", reason=reason or "")


def count_grants():
    return AclGrant.objects.count()


def count_peer_grants():
    return PeerGrant.objects.count()


def recent_auth(limit=8):
    return list(AuthLog.objects.order_by('-id')[:int(limit)].values())


AUDIT_JSONL = "ops-audit.jsonl"


def _audit_rows_from_db(limit):
    """统一审计表 → 前端契约字段（ts/actor/action/detail/ip）。"""
    rows = []
    for r in AuditLog.objects.order_by('-created_at')[:limit]:
        rows.append({
            "id": str(r.pk),
            "ts": timezone.localtime(r.created_at).strftime("%Y-%m-%d %H:%M:%S"),
            "actor": r.actor_name,
            "action": r.action,
            "detail": r.message,
            "ip": r.remote_addr,
        })
    return rows


def _audit_rows_from_jsonl(limit):
    """特权脚本（root）追加写的审计：安装、备份恢复、升级等。

    root 侧不连 Django 数据库（此时可能还没 migrate），只往 data/state/
    ops-audit.jsonl 追加一行 JSON；这里读尾部并合并进同一个列表，
    保证「谁在哪个环节动了系统」在同一个日志页里看得到。
    """
    p = os.path.join(STATE, AUDIT_JSONL)
    if not os.path.exists(p):
        return []
    rows = []
    for line in read_tail_lines(p, limit):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        rows.append({
            "id": "ops-%s" % (d.get("ts") or ""),
            "ts": d.get("ts", ""),
            "actor": d.get("actor", ""),
            "action": d.get("action", ""),
            "detail": d.get("detail", ""),
            "ip": d.get("ip", ""),
        })
    return rows


def get_logs(kind="audit", limit=200):
    limit = int(limit)
    if kind == "audit":
        rows = _audit_rows_from_db(limit) + _audit_rows_from_jsonl(limit)
        # 两个来源按时间合并；时间串格式一致（%Y-%m-%d %H:%M:%S），可直接比较
        rows.sort(key=lambda r: (r.get("ts") or ""), reverse=True)
        return rows[:limit]
    if kind == "auth":
        # 两个来源：Web 自助改密写 ORM(AuthLog)；OpenVPN 拨号认证由 root 的
        # system/auth-verify.py 追加写 data/state/auth.jsonl（不反向写主库）。
        rows = [{"id": r["id"], "ts": r["ts"], "username": r["username"],
                 "src_ip": r["src_ip"], "result": r["result"], "reason": r["reason"]}
                for r in AuthLog.objects.order_by('-id')[:limit].values()]
        p = os.path.join(STATE, "auth.jsonl")
        if os.path.exists(p):
            extra = []
            for x in read_tail_lines(p, limit):
                try:
                    d = json.loads(x)
                except Exception:
                    continue
                extra.append({"id": 0, "ts": d.get("ts", ""), "username": d.get("username", ""),
                              "src_ip": d.get("src_ip", ""), "result": d.get("result", ""),
                              "reason": d.get("reason", "")})
            rows = sorted(rows + extra, key=lambda r: (r.get("ts") or ""), reverse=True)[:limit]
        return rows
    if kind == "server":
        p = os.path.join(DATA, "openvpn.log")
        if not os.path.exists(p):
            return []
        return [{"ts": "", "detail": x} for x in read_tail_lines(p, limit)]
    if kind == "deny":
        p = os.path.join(STATE, "deny.jsonl")
        if not os.path.exists(p):
            return []
        out = []
        for x in read_tail_lines(p, limit):
            try:
                out.append(json.loads(x))
            except Exception:
                pass
        return list(reversed(out))
    return []


# --------------------------------------------------------------------------- 账号

def expire_users(actor="system"):
    """扫描并自动停用到期账号，返回本次被停用的用户名列表。"""
    today = datetime.now().strftime("%Y-%m-%d")
    qs = (VpnUser.objects.filter(enabled=True)
          .exclude(expires_at='').filter(expires_at__isnull=False)
          .filter(expires_at__lt=today))
    names = list(qs.values_list('username', flat=True))
    if names:
        qs.update(enabled=False, updated_at=now())
    for n in names:
        log_audit(actor, "user.expire", "%s 有效期已到，自动停用" % n)
    return names


def list_users(con=None):
    expire_users()
    rows = list(VpnUser.objects.order_by('vpn_ip').values(
        'username', 'vpn_ip', 'enabled', 'note', 'real_name', 'dept',
        'created_at', 'updated_at', 'password_plain', 'expires_at'))
    for r in rows:
        r['enabled'] = _enabled_int(r.get('enabled'))
        r['note'] = r.get('note') or ''
        r['real_name'] = r.get('real_name') or ''
        r['dept'] = r.get('dept') or ''
        r['created_at'] = r.get('created_at') or ''
        r['updated_at'] = r.get('updated_at') or ''
        r['password_plain'] = r.get('password_plain') or ''
        r['expires_at'] = r.get('expires_at') or ''
    return rows


def next_free_ip(c, con=None):
    used = set(VpnUser.objects.values_list('vpn_ip', flat=True))
    for ip in usable_host_ips(c):
        if ip not in used:
            return ip
    raise ValueError("固定 IP 段已用完（当前网段可用 %d 个）" % static_capacity(c))


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
    owner = (VpnUser.objects.filter(vpn_ip=str(addr))
             .values_list('username', flat=True).first())
    if owner and owner != exclude_user:
        raise ValueError("固定 IP %s 已被账号 %s 占用" % (ip, owner))


def add_user(username, vpn_ip=None, password=None, note="", real_name="", dept="",
             expires_at="", actor="web"):
    with SYS_LOCK:
        return _add_user(username, vpn_ip, password, note, real_name, dept, expires_at, actor)


def _add_user(username, vpn_ip=None, password=None, note="", real_name="", dept="",
              expires_at="", actor="web"):
    c = cfg()
    if not valid_username(username):
        raise ValueError("账号名只支持中文、字母、数字、点、下划线、连字符，1~32 位，不能有空格")
    expires_at = validate_expires_at(expires_at)
    with transaction.atomic():
        if VpnUser.objects.filter(username=username).exists():
            raise ValueError("账号已存在")
        # 取号 + 插入必须原子：两个请求同时进来会挑到同一个空闲地址
        ip = vpn_ip or next_free_ip(c)
        validate_vpn_ip(ip, c)
        pw = password or rand_password()
        VpnUser.objects.create(
            username=username, vpn_ip=ip, enabled=True, password_hash=hash_password(pw),
            note=note or "", real_name=real_name or "", dept=dept or "",
            created_at=now(), updated_at=now(), password_plain=pw, expires_at=expires_at)
    log_audit(actor, "user.add", "%s -> %s%s%s" % (
        username, ip, ("（%s/%s）" % (real_name, dept)) if real_name or dept else "",
        ("，有效期至 %s" % expires_at) if expires_at else ""))
    return {"username": username, "vpn_ip": ip, "password": pw, "expires_at": expires_at}


def update_user(username, *, new_username=None, vpn_ip=None, enabled=None, note=None,
                password=None, real_name=None, dept=None, expires_at=None, actor="web"):
    c = cfg()
    expires_at = validate_expires_at(expires_at) if expires_at is not None else None
    try:
        user = VpnUser.objects.get(username=username)
    except VpnUser.DoesNotExist:
        raise ValueError("账号不存在")
    old_ip = user.vpn_ip
    changes = []
    old_name = None
    with transaction.atomic():
        if new_username is not None and new_username.strip() and new_username.strip() != username:
            renamed = new_username.strip()
            if not valid_username(renamed):
                raise ValueError("账号只支持中文/字母/数字/点/下划线/连字符，1~32 位，不能有空格和 /")
            if VpnUser.objects.filter(username=renamed).exists():
                raise ValueError("账号 %s 已存在，换个名字" % renamed)
            # 用户名同时是 users 主键、组成员、ACL 主体、互访授权两端，一次事务里全搬
            VpnUser.objects.filter(username=username).update(username=renamed, updated_at=now())
            UserGroupMember.objects.filter(username=username).update(username=renamed)
            AclGrant.objects.filter(subject_type='user', subject=username).update(subject=renamed)
            for pg in PeerGrant.objects.filter(src_username=username):
                clash = PeerGrant.objects.filter(
                    src_username=renamed, dst_username=pg.dst_username,
                    proto=pg.proto, ports=pg.ports).exists()
                if not clash:
                    PeerGrant.objects.filter(pk=pg.pk).update(src_username=renamed)
            for pg in PeerGrant.objects.filter(dst_username=username):
                clash = PeerGrant.objects.filter(
                    src_username=pg.src_username, dst_username=renamed,
                    proto=pg.proto, ports=pg.ports).exists()
                if not clash:
                    PeerGrant.objects.filter(pk=pg.pk).update(dst_username=renamed)
            changes.append("账号 %s -> %s" % (username, renamed))
            old_name, username = username, renamed

        fields = {}
        if vpn_ip is not None and vpn_ip != old_ip:
            validate_vpn_ip(vpn_ip, c, exclude_user=username)
            fields['vpn_ip'] = vpn_ip
            changes.append("IP %s -> %s" % (old_ip, vpn_ip))
        if enabled is not None:
            fields['enabled'] = bool(enabled)
            changes.append("状态 -> %s" % ("启用" if enabled else "停用"))
        if note is not None:
            fields['note'] = note
        if real_name is not None:
            fields['real_name'] = real_name
        if dept is not None:
            fields['dept'] = dept
        if expires_at is not None:
            fields['expires_at'] = expires_at
            changes.append("有效期 -> %s" % (expires_at or "长期有效"))
        if password:
            fields['password_hash'] = hash_password(password)
            fields['password_plain'] = password
            changes.append("重置密码")
        fields['updated_at'] = now()
        VpnUser.objects.filter(username=username).update(**fields)

    if old_name:
        # 旧名的 ccd 必须删：ccd-exclusive 只认文件名，留着等于旧账号还能拨进来
        try:
            p = os.path.join(CCD, old_name)
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
    log_audit(actor, "user.update", "%s: %s" % (username, "; ".join(changes) or "无变更"))
    return changes


def delete_user(username, actor="web"):
    with transaction.atomic():
        existed = VpnUser.objects.filter(username=username).exists()
        AclGrant.objects.filter(subject_type='user', subject=username).delete()
        UserGroupMember.objects.filter(username=username).delete()
        VpnUser.objects.filter(username=username).delete()
    log_audit(actor, "user.delete", username)
    if not existed:
        raise ValueError("账号不存在")


def user_by_vpn_ip(ip):
    try:
        ip = str(ipaddress.IPv4Address((ip or "").strip()))
    except Exception:
        return None
    row = (VpnUser.objects.filter(vpn_ip=ip)
           .values('username', 'real_name', 'dept', 'vpn_ip', 'enabled').first())
    if not row:
        return None
    row['enabled'] = _enabled_int(row.get('enabled'))
    return row


def self_change_password(vpn_ip, old, new, src_ip=""):
    vpn_ip = (vpn_ip or "").strip() or (src_ip or "").strip()
    old = old or ""
    new = new or ""
    try:
        vpn_ip = str(ipaddress.IPv4Address(vpn_ip))
    except Exception:
        raise ValueError("账号或原密码不正确")
    row = (VpnUser.objects.filter(vpn_ip=vpn_ip)
           .values('username', 'enabled', 'password_hash').first())
    if not row or not row['enabled'] or not verify_password(old, row['password_hash']):
        log_auth(vpn_ip, src_ip, "self-fail", "自助改密校验失败")
        raise ValueError("账号或原密码不正确")
    if new == old:
        raise ValueError("新密码不能与原密码相同")
    if len(new) < 8:
        raise ValueError("新密码至少 8 位")
    if len(new) > 128:
        raise ValueError("新密码最长 128 位")
    update_user(row['username'], password=new, actor="self(%s)" % (src_ip or vpn_ip))
    log_auth(row['username'], src_ip, "self-ok", "自助改密成功")
    return {"username": row['username']}


# --------------------------------------------------------------------------- 台账备份

def backup_users(actor="web"):
    """每次台账变更前备份一份「和导出一模一样」的 Excel（含明文密码）。"""
    ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    name, data, _ = users_file(users_rows(strict=False), "xlsx", "users-%s" % ts)
    path = os.path.join(BACKUPS, name)
    stem, ext = os.path.splitext(name)
    i = 1
    while os.path.exists(path):
        i += 1
        name = "%s-%d%s" % (stem, i, ext)
        path = os.path.join(BACKUPS, name)
    with open(path, "wb") as f:
        f.write(data)
    try:
        os.chmod(path, 0o600)   # 台账备份含明文密码
    except Exception:
        pass
    BackupRecord.objects.create(ts=now(), kind="users", path=path,
                                note="账号变更自动备份（Excel，含密码）")
    prune_user_backups(keep=USER_BACKUP_KEEP)
    return path


def prune_user_backups(keep=None):
    keep = USER_BACKUP_KEEP if keep is None else int(keep)
    if keep <= 0:
        return 0
    rows = list(BackupRecord.objects.filter(kind='users').order_by('-id')
                .values('id', 'path'))
    old = rows[keep:]
    base = os.path.realpath(BACKUPS)
    for r in old:
        p = str(r['path'] or "")
        if p and os.path.dirname(os.path.realpath(p)) == base:
            try:
                os.remove(p)
            except OSError:
                pass
        BackupRecord.objects.filter(pk=r['id']).delete()
    return len(old)


# --------------------------------------------------------------------------- 存量快照 / 备份

def snapshot(tag="auto"):
    """打一个回滚点：统一产出备份包（来源 auto），返回包路径。"""
    from . import backup
    r = backup.create(tag, kind="auto")
    BackupRecord.objects.create(ts=now(), kind="backup", path=r["path"], note=tag)
    return r["path"]


def restore_snapshot(path):
    from . import backup
    if not path:
        raise ValueError("未指定备份")
    rp = os.path.realpath(path)
    if os.path.isdir(rp):
        # 老版目录快照：解包/回写 SQLite 属于特权边界动作，交给 system 层做
        return _base._restore_legacy_snapshot(rp)
    base = os.path.realpath(backup.FULL)
    if not rp.startswith(base + os.sep):
        raise ValueError("备份包路径非法")
    return backup.restore(rp, audit=False, protect=False)


def list_backups(kind=None, limit=100):
    qs = BackupRecord.objects.order_by('-id')
    if kind:
        qs = qs.filter(kind=kind)
    return list(qs.values()[:int(limit)])


# --------------------------------------------------------------------------- Excel 全量同步

def preview_import(content, filename="", actor="web"):
    rows, errs = parse_users_file(content, filename)
    if errs:
        raise ValueError("整批拒绝，未落盘：\n" + "\n".join(errs))
    ips = [r["vpn_ip"] for r in rows]
    if len(set(ips)) != len(ips):
        raise ValueError("整批拒绝：表内固定 IP 有重复")
    c = cfg()
    usable = set(usable_host_ips(c))
    for r in rows:
        if r["vpn_ip"] not in usable:
            raise ValueError("整批拒绝：%s 不可用（不在当前 VPN 网段 %s 内，或落在动态池 / 保留地址上）；"
                             "若这份表来自旧网段，请先把「VPN固定IP」整列改成当前网段的地址"
                             % (r["vpn_ip"], c["vpn_cidr"]))
    old = {u["username"]: u for u in list_users()}
    new = {r["username"]: r for r in rows}
    changes = []
    for name, r in new.items():
        if name not in old:
            changes.append({"op": "新增", "username": name,
                            "detail": "%s，使用表中明文密码%s" % (
                                r["vpn_ip"],
                                ("，有效期至 %s" % r["expires_at"]) if r.get("expires_at") else "")})
        else:
            bits = []
            if old[name]["vpn_ip"] != r["vpn_ip"]:
                bits.append("IP %s -> %s（重连生效）" % (old[name]["vpn_ip"], r["vpn_ip"]))
            if bool(old[name]["enabled"]) != r["enabled"]:
                bits.append("状态 -> %s" % ("启用" if r["enabled"] else "停用"))
            bits.append("密码 -> 按表中明文覆盖")
            exp = r.get("expires_at") or ""
            if (old[name].get("expires_at") or "") != exp:
                bits.append("有效期 -> %s" % (exp or "长期有效"))
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
    # 预览令牌放缓存（5 分钟过期），不再单独建表
    cache.set(_PREVIEW_CACHE_PREFIX + token, json.dumps(rows, ensure_ascii=False),
              _PREVIEW_TTL)
    log_audit(actor, "import.preview", "变更 %d 项" % len(changes))
    return {"changes": changes, "token": token, "expires_in": _PREVIEW_TTL}


def apply_import(token, actor="web", reset_empty=False, action="import.apply"):
    with SYS_LOCK:
        return _apply_import(token, actor, reset_empty, action)


def _apply_import(token, actor="web", reset_empty=False, action="import.apply"):
    payload = cache.get(_PREVIEW_CACHE_PREFIX + (token or ""))
    if not payload:
        raise ValueError("预览已过期（5 分钟），请重新导入")
    rows = json.loads(payload)
    path = backup_users(actor)   # 先备份，误删可把此文件当台账重新导入
    initial = []
    kept = []
    with transaction.atomic():
        keep = {r["username"] for r in rows}
        gone = [u for u in VpnUser.objects.values_list('username', flat=True)
                if u not in keep]
        for n in gone:
            AclGrant.objects.filter(subject_type='user', subject=n).delete()
            UserGroupMember.objects.filter(username=n).delete()
        if keep:
            VpnUser.objects.exclude(username__in=list(keep)).delete()
        else:
            VpnUser.objects.all().delete()
        # 两阶段写入：先把所有保留账号的 vpn_ip 挪到不可能冲突的占位值，再写最终值。
        # 否则「新账号接手某个旧账号即将让出的 IP」会在逐行 upsert 时撞 UNIQUE
        # （例：表内「会话」要 10.8.0.2，而该 IP 此刻还被即将改成 .3 的 test 占着）。
        # 用短序号占位，彼此唯一、不超字段长度，且不是合法 IPv4，绝不与最终值冲突。
        pending = list(VpnUser.objects.all().only('pk'))
        for i, u in enumerate(pending):
            VpnUser.objects.filter(pk=u.pk).update(
                vpn_ip='__import_pending_%d__' % i)
        for r in rows:
            exp = r.get("expires_at") or ""
            fields = dict(
                vpn_ip=r["vpn_ip"], enabled=bool(r["enabled"]),
                password_hash=hash_password(r["password"]), password_plain=r["password"],
                expires_at=exp, note=r["note"], real_name=r.get("real_name") or "",
                dept=r.get("dept") or "", updated_at=now())
            if VpnUser.objects.filter(username=r["username"]).exists():
                VpnUser.objects.filter(username=r["username"]).update(**fields)
            else:
                VpnUser.objects.create(username=r["username"], created_at=now(), **fields)
    cache.delete(_PREVIEW_CACHE_PREFIX + (token or ""))
    log_audit(actor, action, "%d 个账号，密码按表中明文覆盖，备份 %s" % (len(rows), path))
    return {"count": len(rows), "backup": path, "initial_password_file": "",
            "initial": initial, "kept": kept}


# --------------------------------------------------------------------------- 资产组 / 条目

def _asset_group_map():
    out = {}
    for g in AssetGroup.objects.order_by('name'):
        out[g.id] = {"id": g.id, "name": g.name, "note": g.note or "", "items": []}
    for it in AssetItem.objects.order_by('group_id', 'cidr', 'ports'):
        if it.group_id in out:
            out[it.group_id]["items"].append({
                "id": it.id, "group_id": it.group_id, "cidr": it.cidr,
                "proto": it.proto, "ports": it.ports or "", "note": it.note or ""})
    return out


def list_asset_groups(con=None):
    return list(_asset_group_map().values())


def _find_dup_item(cidr, proto, ports, exclude_id=None):
    for it in AssetItem.objects.select_related('group').all():
        if exclude_id and it.id == exclude_id:
            continue
        try:
            same_cidr = norm_cidr(it.cidr) == cidr
        except Exception:
            same_cidr = (it.cidr or "") == cidr
        if same_cidr and it.proto == proto and norm_ports(it.ports) == ports:
            return {"id": it.id, "cidr": it.cidr, "proto": it.proto,
                    "ports": it.ports or "", "gname": it.group.name}
    return None


def _insert_item(gid, cidr, proto, ports, note=""):
    cidr, proto, ports = norm_item(cidr, proto, ports)
    dup = _find_dup_item(cidr, proto, ports)
    if dup:
        raise ValueError("「%s %s %s」在资产组「%s」里已经有一条一模一样的了"
                         "（同一个目标 + 同一个协议 + 同一组端口），同一份权限不需要加两次。"
                         % (cidr, proto, ports or "全部端口", dup["gname"]))
    AssetItem.objects.create(group_id=gid, cidr=cidr, proto=proto,
                             ports=ports or "", note=note or "")
    return {"cidr": cidr, "proto": proto, "ports": ports}


def add_asset_group(name, note="", items=None, actor="web"):
    name = (name or "").strip()
    _check_group_name(name)
    norm = [norm_item(i[0], i[1] if len(i) > 1 else "any", i[2] if len(i) > 2 else "")
            for i in (items or [])]
    with transaction.atomic():
        if AssetGroup.objects.filter(name=name).exists():
            raise ValueError("资产组「%s」已存在" % name)
        g = AssetGroup.objects.create(name=name, note=note or "")
        for cidr, proto, ports in norm:
            _insert_item(g.id, cidr, proto, ports)
    log_audit(actor, "assetgroup.add", "%s（%d 条）" % (name, len(norm)))
    return {"id": g.id, "count": len(norm)}


def update_asset_group(gid, name=None, note=None, actor="web"):
    try:
        g = AssetGroup.objects.get(pk=gid)
    except AssetGroup.DoesNotExist:
        raise ValueError("资产组不存在")
    if name and name.strip() != g.name:
        _check_group_name(name.strip())
        if AssetGroup.objects.filter(name=name.strip()).exclude(pk=gid).exists():
            raise ValueError("资产组名称已存在")
        g.name = name.strip()
    if note is not None:
        g.note = note
    g.save()
    log_audit(actor, "assetgroup.update", "id=%s" % gid)


def delete_asset_group(gid, actor="web"):
    with transaction.atomic():
        AclGrant.objects.filter(asset_group_id=gid).delete()
        AssetItem.objects.filter(group_id=gid).delete()
        n, _ = AssetGroup.objects.filter(pk=gid).delete()
    if not n:
        raise ValueError("资产组不存在")
    log_audit(actor, "assetgroup.delete", "id=%s" % gid)


def add_asset_items(gid, items, note="", actor="web"):
    with transaction.atomic():
        if not AssetGroup.objects.filter(pk=gid).exists():
            raise ValueError("资产组不存在")
        added = [_insert_item(gid, c, p, po, note) for c, p, po in items]
    log_audit(actor, "assetitem.add", "组 %s 批量新增 %d 条" % (gid, len(added)))
    return {"count": len(added), "items": added}


def add_asset_item(gid, cidr, proto="any", ports="", note="", actor="web"):
    with transaction.atomic():
        if not AssetGroup.objects.filter(pk=gid).exists():
            raise ValueError("资产组不存在")
        r = _insert_item(gid, cidr, proto, ports, note)
    log_audit(actor, "assetitem.add", "组 %s ← %s %s %s" % (gid, r["cidr"], r["proto"], r["ports"]))
    return r


def update_asset_item(iid, cidr=None, proto=None, ports=None, note=None, actor="web"):
    try:
        it = AssetItem.objects.get(pk=iid)
    except AssetItem.DoesNotExist:
        raise ValueError("条目不存在")
    new_cidr, new_proto, new_ports = norm_item(
        cidr if cidr is not None else it.cidr,
        proto if proto is not None else it.proto,
        ports if ports is not None else it.ports)
    dup = _find_dup_item(new_cidr, new_proto, new_ports, exclude_id=iid)
    if dup:
        raise ValueError("「%s %s %s」在资产组「%s」里已经有一条一模一样的了"
                         % (new_cidr, new_proto, new_ports or "全部端口", dup["gname"]))
    it.cidr = new_cidr
    it.proto = new_proto
    it.ports = new_ports or ""
    it.note = note if note is not None else (it.note or "")
    it.save()
    log_audit(actor, "assetitem.update",
              "id=%s → %s %s %s" % (iid, new_cidr, new_proto, new_ports))
    return {"id": iid, "cidr": new_cidr, "proto": new_proto, "ports": new_ports}


def delete_asset_item(iid, actor="web"):
    n, _ = AssetItem.objects.filter(pk=iid).delete()
    if not n:
        raise ValueError("条目不存在")
    log_audit(actor, "assetitem.delete", "id=%s" % iid)


# --------------------------------------------------------------------------- 用户组

def list_user_groups(con=None):
    groups = [{"id": g.id, "name": g.name, "note": g.note or ""}
              for g in UserGroup.objects.order_by('name')]
    users = {u.username: u for u in VpnUser.objects.all()}
    mem = {}
    for r in UserGroupMember.objects.all():
        u = users.get(r.username)
        mem.setdefault(r.group_id, []).append({
            "username": r.username,
            "vpn_ip": (u.vpn_ip if u else "") or "",
            "real_name": (u.real_name if u else "") or "",
            "dept": (u.dept if u else "") or "",
            "enabled": bool(u.enabled) if u else False})
    for g in groups:
        g["members"] = mem.get(g["id"], [])
        g["count"] = len(g["members"])
    return groups


def _clean_members(members):
    if members is None:
        return None
    out, bad = [], []
    for m in members:
        m = str(m or "").strip()
        if not m or m in out:
            continue
        if VpnUser.objects.filter(username=m).exists():
            out.append(m)
        else:
            bad.append(m)
    if bad:
        raise ValueError("这些账号不存在：%s" % "、".join(bad))
    return out


def add_user_group(name, note="", members=None, actor="web"):
    name = (name or "").strip()
    _check_group_name(name, "用户组")
    with transaction.atomic():
        if UserGroup.objects.filter(name=name).exists():
            raise ValueError("用户组「%s」已存在" % name)
        mem = _clean_members(members) or []
        g = UserGroup.objects.create(name=name, note=note or "")
        for m in mem:
            UserGroupMember.objects.get_or_create(group_id=g.id, username=m)
    log_audit(actor, "usergroup.add", "%s（%d 人）" % (name, len(mem)))
    return {"id": g.id, "count": len(mem)}


def update_user_group(gid, name=None, note=None, members=None, actor="web"):
    try:
        g = UserGroup.objects.get(pk=gid)
    except UserGroup.DoesNotExist:
        raise ValueError("用户组不存在")
    with transaction.atomic():
        if name and name.strip() != g.name:
            _check_group_name(name.strip(), "用户组")
            if UserGroup.objects.filter(name=name.strip()).exclude(pk=gid).exists():
                raise ValueError("用户组名称已存在")
            g.name = name.strip()
        if note is not None:
            g.note = note
        g.save()
        if members is not None:
            mem = _clean_members(members) or []
            UserGroupMember.objects.filter(group_id=gid).delete()
            for m in mem:
                UserGroupMember.objects.get_or_create(group_id=gid, username=m)
    log_audit(actor, "usergroup.update", "id=%s" % gid)


def delete_user_group(gid, actor="web"):
    name = (UserGroup.objects.filter(pk=gid)
            .values_list('name', flat=True).first()) or ""
    with transaction.atomic():
        AclGrant.objects.filter(subject_type='group', subject=name).delete()
        UserGroupMember.objects.filter(group_id=gid).delete()
        n, _ = UserGroup.objects.filter(pk=gid).delete()
    if not n:
        raise ValueError("用户组不存在")
    log_audit(actor, "usergroup.delete", name or ("id=%s" % gid))


# --------------------------------------------------------------------------- 授权

def list_grants(con=None):
    amap = _asset_group_map()
    ug = {g.name: g.id for g in UserGroup.objects.all()}
    members = {}
    for r in UserGroupMember.objects.all():
        members.setdefault(r.group_id, []).append(r.username)
    users = {u.username: u.vpn_ip for u in VpnUser.objects.all()}
    out = []
    for g in AclGrant.objects.order_by('id'):
        d = {"id": g.id, "subject_type": g.subject_type, "subject": g.subject,
             "asset_group_id": g.asset_group_id,
             "expires_at": g.expires_at or "", "note": g.note or ""}
        ag = amap.get(d["asset_group_id"]) or {"name": "(资产组已删除)", "items": []}
        d["asset_group"] = ag["name"]
        d["items"] = len(ag["items"])
        d["targets"] = ["%s %s%s" % (i["cidr"], i["proto"],
                                     (" " + i["ports"]) if i["ports"] else "")
                        for i in ag["items"]]
        if d["subject_type"] == "group":
            d["members"] = members.get(ug.get(d["subject"], -1), [])
            d["scope"] = len(d["members"])
            d["vpn_ip"] = ""
        else:
            d["scope"] = 1
            d["members"] = [d["subject"]]
            d["vpn_ip"] = users.get(d["subject"], "未登记")
        out.append(d)
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
    if subject_type == "user":
        if not VpnUser.objects.filter(username=subject).exists():
            raise ValueError("账号不存在")
    else:
        if not UserGroup.objects.filter(name=subject).exists():
            raise ValueError("用户组不存在")
    if not AssetGroup.objects.filter(pk=asset_group_id).exists():
        raise ValueError("资产组不存在")
    exists = AclGrant.objects.filter(
        subject_type=subject_type, subject=subject,
        asset_group_id=asset_group_id).exists()
    AclGrant.objects.update_or_create(
        subject_type=subject_type, subject=subject, asset_group_id=asset_group_id,
        defaults={"expires_at": expires_at or "", "note": note or ""})
    log_audit(actor, "grant.add", "%s%s -> 资产组 %s" % (
        "组 " if subject_type == "group" else "", subject, asset_group_id))
    return {"exists": bool(exists)}


def delete_grant(gid, actor="web"):
    n, _ = AclGrant.objects.filter(pk=gid).delete()
    if not n:
        raise ValueError("授权不存在")
    log_audit(actor, "grant.delete", "id=%s" % gid)


def active_grants():
    return [g for g in list_grants() if _not_expired(g)]


def expand_grants():
    """把「账号/用户组 × 资产组」展开成最终要写进 iptables 的最小单元。"""
    users = {u.username: u.vpn_ip
             for u in VpnUser.objects.filter(enabled=True)}
    amap = _asset_group_map()
    ug = {g.name: g.id for g in UserGroup.objects.all()}
    members = {}
    for r in UserGroupMember.objects.all():
        members.setdefault(r.group_id, []).append(r.username)
    rows = [{"id": g.id, "subject_type": g.subject_type, "subject": g.subject,
             "asset_group_id": g.asset_group_id, "expires_at": g.expires_at or "",
             "note": g.note or ""} for g in AclGrant.objects.order_by('id')]
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


# --------------------------------------------------------------------------- 互访授权

def list_peer_grants():
    return list(PeerGrant.objects.order_by('id').values())


def add_peer_grant(src, dst, proto="any", ports="", expires_at="", note="", actor="web"):
    if src == dst:
        raise ValueError("源账号与目标账号不能相同")
    if proto not in ("any", "tcp", "udp", "icmp"):
        raise ValueError("协议只能是 any/tcp/udp/icmp")
    names = set(VpnUser.objects.filter(enabled=True).values_list('username', flat=True))
    if src not in names or dst not in names:
        raise ValueError("源/目标账号不存在或已停用")
    if proto == "icmp":
        ports = ""
    else:
        ports = norm_ports(ports)
        _check_ports(ports)
    if expires_at:
        try:
            datetime.strptime(expires_at, "%Y-%m-%d %H:%M")
        except Exception:
            raise ValueError("到期时间格式：YYYY-MM-DD HH:MM")
    PeerGrant.objects.update_or_create(
        src_username=src, dst_username=dst, proto=proto, ports=ports or "",
        defaults={"expires_at": expires_at or "", "note": note or ""})
    log_audit(actor, "peer.add", "%s -> %s (%s %s)" % (src, dst, proto, ports or "全部"))


def delete_peer_grant(pid, actor="web"):
    PeerGrant.objects.filter(pk=pid).delete()
    log_audit(actor, "peer.delete", "id=%s" % pid)


# --------------------------------------------------------------------------- 长任务

def task_create(tid, kind, actor_name="admin", msg="排队中"):
    OvpnTask.objects.create(id=tid, kind=kind, status="running", progress=0,
                            msg=msg, created_at=now(), actor=actor_name)


def task_update(tid, **kw):
    OvpnTask.objects.filter(pk=tid).update(**kw)


def task_get(tid):
    row = OvpnTask.objects.filter(pk=tid).values().first()
    return row


# --------------------------------------------------------------------------- 管理账户（Django Auth 接管）

def admin_reset_credentials(*args, **kwargs):
    """重新生成一组管理凭据（账户名 + 一次性明文密码）。

    口令本体由调用方写进 Django User（`ovpn_cli reset-admin` 或后台按钮），
    这里只负责生成、落「管理账户名」标记、清掉首登强改并写救场凭据文件。
    """
    name = "admin-" + secrets.token_hex(3)
    pw = rand_password(18)
    set_meta("admin_user", name)
    set_meta("admin_must_change", 0)
    log_audit("admin", "admin.reset", "管理账户与密码已重新生成")
    # 第一行必须是纯密码：bootstrap 只取第一行
    try:
        with open(os.path.join(DATA, "admin_password.txt"), "w") as f:
            f.write(pw + "\n")
            f.write("# 管理账户: %s（重置于 %s）\n" % (name, now()))
        os.chmod(os.path.join(DATA, "admin_password.txt"), 0o600)
    except Exception:
        pass
    return {"username": name, "password": pw}


# --------------------------------------------------------------------------- 打通继承链
# 上面继承来的纯函数（online_users / stale_ip_users / allow_list / set_allow_list /
# users_rows …）内部是按「自己模块的全局名」去调 get_meta / list_users / log_audit 的。
# 只把函数复制到本模块不会改变它们的查找路径，所以这里把 system.core 里那批数据访问
# 函数**就地替换**成 ORM 版本：
#   * 纯逻辑只有一份（system/core.py），不复制；
#   * Web 进程里任何人调 core.xxx() 走的都是 ORM；
#   * system/worker.py、auth-verify.py、setup.py 是独立进程，各自仍是 SQLite 实现，
#     互不影响。
_PATCH_INTO_BASE = [
    "init_db", "get_meta", "set_meta", "cfg",
    "log_audit", "log_auth", "get_logs", "expire_users",
    "list_users", "next_free_ip", "validate_vpn_ip",
    "add_user", "_add_user", "update_user", "delete_user",
    "user_by_vpn_ip", "self_change_password",
    "backup_users", "prune_user_backups", "snapshot", "restore_snapshot", "list_backups",
    "preview_import", "apply_import", "_apply_import",
    "list_asset_groups", "_asset_group_map", "_find_dup_item", "_insert_item",
    "add_asset_group", "update_asset_group", "delete_asset_group",
    "add_asset_items", "add_asset_item", "update_asset_item", "delete_asset_item",
    "list_user_groups", "_clean_members", "add_user_group", "update_user_group",
    "delete_user_group",
    "list_grants", "add_grant", "delete_grant", "active_grants", "expand_grants",
    "list_peer_grants", "add_peer_grant", "delete_peer_grant",
]
for _name in _PATCH_INTO_BASE:
    setattr(_base, _name, globals()[_name])
