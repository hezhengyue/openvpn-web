# ovpn/services/users.py
"""VPN 账号

VPN 账号：增删改查、IP 校验与分配、到期停用、自助改密。
"""
from .base import *          # noqa: F401,F403  共享命名空间（常量/纯逻辑/ORM 模型）
from .audit import log_audit, log_auth
from .base import _enabled_int

_PENDING_EXPIRED = set()
_PENDING_EXPIRED_LOCK = threading.Lock()




def mark_expired_pending(names):
    """把（处理失败的）到期名单放回待办，下次请求再补删 ccd / 踢人。"""
    with _PENDING_EXPIRED_LOCK:
        _PENDING_EXPIRED.update(n for n in names if n)



def take_expired_users():
    """取出并清空待办名单；只有真正处理完才会被取走。"""
    with _PENDING_EXPIRED_LOCK:
        pending = list(_PENDING_EXPIRED)
        _PENDING_EXPIRED.clear()
    return pending



def expire_users(actor="system"):
    """扫描并自动停用到期账号，返回本次被停用的用户名列表。"""
    today = datetime.now().strftime("%Y-%m-%d")
    qs = (VpnUser.objects.filter(enabled=True)
          .exclude(expires_at='').filter(expires_at__isnull=False)
          .filter(expires_at__lt=today))
    names = list(qs.values_list('username', flat=True))
    if names:
        qs.update(enabled=False, updated_at=now())
        mark_expired_pending(names)
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
            for pg in list(PeerGrant.objects.filter(src_username=username)):
                clash = PeerGrant.objects.filter(
                    src_username=renamed, dst_username=pg.dst_username,
                    proto=pg.proto, ports=pg.ports).exists()
                if clash:
                    # 目标行已经表达了同一条互访关系；留着旧名那行就是孤儿授权
                    PeerGrant.objects.filter(pk=pg.pk).delete()
                else:
                    PeerGrant.objects.filter(pk=pg.pk).update(src_username=renamed)
            for pg in list(PeerGrant.objects.filter(dst_username=username)):
                clash = PeerGrant.objects.filter(
                    src_username=pg.src_username, dst_username=renamed,
                    proto=pg.proto, ports=pg.ports).exists()
                if clash:
                    PeerGrant.objects.filter(pk=pg.pk).delete()
                else:
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
        # 互访规则两端都是用户名：不清掉的话，以后新建同名账号会自动「继承」
        # 前一个人的 P2P 权限。
        PeerGrant.objects.filter(src_username=username).delete()
        PeerGrant.objects.filter(dst_username=username).delete()
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

# 只导出本模块自己实现的函数：`from .base import *` 带进来的同名函数
# （如 base 继承的 list_users）不能借 import * 反向覆盖别的子模块。
__all__ = [
    "mark_expired_pending",
    "take_expired_users",
    "expire_users",
    "list_users",
    "next_free_ip",
    "validate_vpn_ip",
    "add_user",
    "_add_user",
    "update_user",
    "delete_user",
    "user_by_vpn_ip",
    "self_change_password",
]
