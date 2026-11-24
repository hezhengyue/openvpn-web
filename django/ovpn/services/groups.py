# ovpn/services/groups.py
"""用户组

用户组与组成员（改名同步 ACL 授权）。
"""
from .base import *          # noqa: F401,F403  共享命名空间（常量/纯逻辑/ORM 模型）
from .base import _check_group_name  # noqa: F401  组名规范校验（system.core 纯逻辑）
from .audit import log_audit




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
            old_name = g.name
            new_name = name.strip()
            # AclGrant 按组名引用主体，改名必须同步搬迁，否则该组所有授权静默失效。
            # 新名若已有孤儿授权（历史残留），先清掉再搬，避免重复授权。
            AclGrant.objects.filter(subject_type='group', subject=new_name).delete()
            AclGrant.objects.filter(subject_type='group', subject=old_name).update(subject=new_name)
            g.name = new_name
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

# 只导出本模块自己实现的函数：`from .base import *` 带进来的同名函数
# （如 base 继承的 list_users）不能借 import * 反向覆盖别的子模块。
__all__ = [
    "list_user_groups",
    "_clean_members",
    "add_user_group",
    "update_user_group",
    "delete_user_group",
]
