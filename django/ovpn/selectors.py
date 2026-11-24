# ovpn/selectors.py
"""只读查询层。

视图只负责「鉴权 → 取数 → 封装响应」中的后两步，凡是需要跨多个业务函数聚合、
或要额外推导口径（可用宿主 IP 判定、统计字段）的**只读**查询都放在这里：

  * dashboard_state()  概览页（/api/state）：用户/在线/授权/失效固定 IP/统计
  * allow_overview()   安全白名单（/api/security/allow GET）：白名单 + 探测网段
  * acl_overview()     ACL 页（/api/acl）：资产组/用户组/授权/互访/展开条数

这样视图层保持薄，查询逻辑可以单独写测试，也不会因为「顺手加个接口」把聚合
逻辑复制成第二份。写操作一律仍走 ovpn.services。
"""
from .bridge import core


def dashboard_state():
    """概览页所需的全部只读数据。

    stale_ips 是「固定 IP 已不在服务器可用网段内」的账号——换过 vpn_cidr 之后
    这些账号拨上来会拿不到地址，概览页要提示管理员改。usable_host_ips() 依赖
    网卡信息，取不到时按「无失效项」处理，不能因为探测失败让整个概览 500。
    """
    cfg = core.cfg()
    users = core.list_users()
    online = core.online_users()
    try:
        usable = set(core.usable_host_ips(cfg))
        stale = [{"username": u["username"], "vpn_ip": u["vpn_ip"], "enabled": u["enabled"]}
                 for u in users if u["vpn_ip"] not in usable]
    except Exception:
        stale = []
    stats = {
        "users": len(users),
        "enabled": len([u for u in users if u["enabled"]]),
        "online": len(online),
        "grants": core.count_grants(),
        "peers": core.count_peer_grants(),
        "acl_mode": cfg["acl_mode"],
        "nat_mode": cfg.get("nat_mode", "nat"),
    }
    return {
        "initialized": bool(core.get_meta("initialized")),
        "version": core.version_info(),
        "cfg": cfg,
        "web_version": core.WEB_VERSION,
        "stale_ips": stale,
        "stats": stats,
        "recent_auth": core.recent_auth(8),
    }


def allow_overview(current_ip=""):
    """安全白名单页：已配置白名单 + 本机探测网段 + 当前来源 IP。"""
    return {
        "allow": core.allow_list(),
        "detected": core.local_subnets(),
        "current_ip": current_ip,
        "vpn_cidr": core.cfg().get("vpn_cidr", ""),
    }


def acl_overview():
    """ACL 页：资产组 / 用户组 / 授权 / 互访 / 授权展开后的规则条数。"""
    cfg = core.cfg()
    return {
        "asset_groups": core.list_asset_groups(),
        "user_groups": core.list_user_groups(),
        "grants": core.list_grants(),
        "peers": core.list_peer_grants(),
        "users": core.list_users(),
        "expanded": len(core.expand_grants()),
        "mode": cfg["acl_mode"],
        "nat_mode": cfg.get("nat_mode", "nat"),
    }
