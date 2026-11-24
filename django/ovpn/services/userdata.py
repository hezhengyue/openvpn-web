# ovpn/services/userdata.py
"""台账备份 / 快照 / Excel 同步

台账备份、快照与 Excel 全量同步（导入走两阶段确认）。
"""
from .base import *          # noqa: F401,F403  共享命名空间（常量/纯逻辑/ORM 模型）
from .base import _base, _PREVIEW_CACHE_PREFIX, _PREVIEW_TTL  # noqa: F401
from .audit import log_audit
from .users import list_users




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
            PeerGrant.objects.filter(src_username=n).delete()
            PeerGrant.objects.filter(dst_username=n).delete()
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

# 只导出本模块自己实现的函数：`from .base import *` 带进来的同名函数
# （如 base 继承的 list_users）不能借 import * 反向覆盖别的子模块。
__all__ = [
    "backup_users",
    "prune_user_backups",
    "snapshot",
    "restore_snapshot",
    "list_backups",
    "preview_import",
    "apply_import",
    "_apply_import",
]
