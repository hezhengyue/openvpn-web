# ovpn/services/assets.py
"""资产组与条目

资产组与资产条目（CIDR / 协议 / 端口）。
"""
from .base import *          # noqa: F401,F403  共享命名空间（常量/纯逻辑/ORM 模型）
from .base import _check_group_name  # noqa: F401  组名规范校验（system.core 纯逻辑）
from .audit import log_audit




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

# 只导出本模块自己实现的函数：`from .base import *` 带进来的同名函数
# （如 base 继承的 list_users）不能借 import * 反向覆盖别的子模块。
__all__ = [
    "_asset_group_map",
    "list_asset_groups",
    "_find_dup_item",
    "_insert_item",
    "add_asset_group",
    "update_asset_group",
    "delete_asset_group",
    "add_asset_items",
    "add_asset_item",
    "update_asset_item",
    "delete_asset_item",
]
