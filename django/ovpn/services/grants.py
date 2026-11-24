# ovpn/services/grants.py
"""ACL 授权与客户端互访

ACL 授权（用户/组 → 资产组）与客户端互访白名单。
"""
from .base import *          # noqa: F401,F403  共享命名空间（常量/纯逻辑/ORM 模型）
from .base import _check_ports, _not_expired  # noqa: F401  端口/有效期校验（system.core 纯逻辑）
from .audit import log_audit
from .assets import _asset_group_map




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

# 只导出本模块自己实现的函数：`from .base import *` 带进来的同名函数
# （如 base 继承的 list_users）不能借 import * 反向覆盖别的子模块。
__all__ = [
    "list_grants",
    "add_grant",
    "delete_grant",
    "active_grants",
    "expand_grants",
    "list_peer_grants",
    "add_peer_grant",
    "delete_peer_grant",
]
