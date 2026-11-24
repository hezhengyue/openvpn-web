# ovpn/services/audit.py
"""审计与认证日志

审计与认证日志：统一写 audit_auditlog，读近实时日志（含 JSONL 兜底）。
"""
from .base import *          # noqa: F401,F403  共享命名空间（常量/纯逻辑/ORM 模型）
from .base import _audit_write  # noqa: F401  统一审计写入口（下划线名不被 import * 带出）

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

AUDIT_JSONL = "ops-audit.jsonl"




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



def prune_auth_logs(keep=5000):
    """认证日志按条数封顶：审计表有 audit_purge，这张表以前只增不减。"""
    keep = max(100, int(keep))
    old_ids = list(AuthLog.objects.order_by('-id').values_list('id', flat=True)[keep:])
    for i in range(0, len(old_ids), 500):
        AuthLog.objects.filter(id__in=old_ids[i:i + 500]).delete()
    return len(old_ids)



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

# 只导出本模块自己实现的函数：`from .base import *` 带进来的同名函数
# （如 base 继承的 list_users）不能借 import * 反向覆盖别的子模块。
__all__ = [
    "_audit_module",
    "log_audit",
    "log_auth",
    "count_grants",
    "count_peer_grants",
    "recent_auth",
    "prune_auth_logs",
    "_audit_rows_from_db",
    "_audit_rows_from_jsonl",
    "get_logs",
]
