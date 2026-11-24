# ovpn/views/settings_view.py
"""服务端设置 / PKI / 客户端配置。"""
import io
import os
import re
import uuid

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import render

from .. import bridge, selectors
from ..bridge import (core, fullbackup, ok, jbody, client_ip, actor,
                      admin_user, file_response,
                      CTX, TASKS_DIR, tlog, sudo_worker, start_task,
                      after_change, install_code, pubip_get, MOCK)




# =========================================================== 服务端设置
def settings_get(request):
    return ok({"cfg": core.cfg(), "version": core.version_info()})



def settings_save(request):
    d = jbody(request)
    c = core.cfg()
    for k in core.DEFAULT_CFG:
        if k in d and k not in ("allow_major_upgrade", "lan_nets"):
            c[k] = d[k]
    errs = core.validate_cfg(c)
    if errs:
        raise ValueError("；".join(errs))
    _conf = core.pool_user_conflicts(c)
    if _conf:
        raise ValueError("动态地址池 %s 与 %d 个账号的固定 IP 重叠（%s）：同一个地址会被池子再发给别人，"
                         "请把地址池挪到空档，或先把这些账号的地址改到池子外"
                         % (core.pool_text(c), len(_conf),
                            "、".join("%s %s" % (x["username"], x["vpn_ip"]) for x in _conf[:8])))
    old_fp = core.get_meta("config_fingerprint")
    for k, v in c.items():
        core.set_meta(k, v)
    core.write_all_config()
    new_fp = core.get_meta("config_fingerprint")
    need = old_fp != new_fp
    tid = uuid.uuid4().hex
    sudo_worker(tid, "apply-rules")
    sudo_worker(tid, "restart-vpn")
    core.log_audit("admin", "settings.save", "指纹 %s -> %s" % (old_fp, new_fp))
    stale = core.stale_ip_users(c)
    msg = "配置已应用，OpenVPN 已重启（在线用户会断线自动重连）"
    if stale:
        msg += ("；需手工把 %d 个账号的固定 IP 改到新网段（%s），同步按尾号对齐，未改的会拨不上来"
                % (len(stale), c["vpn_cidr"]))
    return ok({"need_redistribute": need, "fingerprint": new_fp,
               "warnings": core.route_warnings(c),
               "vpn_cidr": c["vpn_cidr"], "stale": stale, "msg": msg})



def settings_preview(request):
    c = core.cfg()
    return ok({"text": core.render_server_conf(c),
               "routes": ["%s %s" % (n.network_address, n.netmask) for n in core.push_targets(c)],
               "warnings": core.route_warnings(c)})



def settings_public_ip(request):
    ip, src, cached = pubip_get(force=request.GET.get("refresh") == "1")
    return ok({"ip": ip, "source": src, "cached": cached,
               "msg": ("已识别公网 IP %s" % ip) if ip else
                      "没探测到公网 IP（内网机器或无外网出口），请手动填写公网 IP 或域名"})



def settings_capacity(request):
    c = core.cfg()
    users = core.list_users()
    usable = core.usable_host_ips(c)
    used = len([u for u in users if u["vpn_ip"] in usable])
    return ok({"usable": len(usable), "used": used, "free": max(0, len(usable) - used),
               "enabled": len([u for u in users if u["enabled"]]),
               "total": len(users), "pool": core.pool_text(c),
               "pool_enabled": bool(c.get("pool_enabled")),
               "msg": "网段 %s 可分配固定 IP %d 个，已用 %d 个，还剩 %d 个"
                      % (c["vpn_cidr"], len(usable), used, max(0, len(usable) - used))})



def api_pki(request):
    return ok({"items": core.pki_info()})



def api_pki_file(request, name):
    fn = {"ca": "ca.crt", "server": "server.crt"}.get(name)
    if not fn:
        raise ValueError("只能下载 ca / server 证书")
    p = os.path.join(core.DATA, fn)
    if not os.path.exists(p):
        raise ValueError("证书尚未生成")
    return file_response(p, "application/x-pem-file", fn)



def client_ovpn(request):
    p = os.path.join(core.DATA, "company.ovpn")
    if not os.path.exists(p):
        raise ValueError("尚未生成，请先完成安装向导")
    core.log_audit("admin", "ovpn.download", "下载 company.ovpn")
    return file_response(p, "application/x-openvpn-profile", "company.ovpn")



def settings_rebuild(request):
    tid = uuid.uuid4().hex
    core.write_all_config()
    sudo_worker(tid, "apply-rules")
    sudo_worker(tid, "collect-deny")
    core.log_audit("admin", "rules.rebuild", "全量重建")
    return ok({"msg": "规则已全量重建（含清理 MASQUERADE）"})



def settings_reinit_ca(request):
    core.snapshot("重新初始化 CA 前")

    def a():
        core.gen_pki()
        return "已重签 CA / 服务端证书 / tls-crypt"

    def b():
        r = core.write_all_config()
        return "新指纹 %s，需全员替换 company.ovpn" % r["fingerprint"]

    def c():
        sudo_worker(CTX["tid"], "restart-vpn")
        return "OpenVPN 已重启"

    tid = start_task("reinit-ca", [("重新签发", a), ("重建配置", b), ("重启服务", c)], actor(request) or "admin")
    core.log_audit("admin", "ca.reinit", "高危操作")
    return ok({"task_id": tid, "need_redistribute": True})



def settings_restore_default(request):
    core.snapshot("恢复默认前")
    for k, v in core.DEFAULT_CFG.items():
        if k in ("web_allow_cidr",):
            continue
        core.set_meta(k, v)
    core.write_all_config()
    core.log_audit("admin", "settings.default", "恢复默认")
    stale = core.stale_ip_users()
    msg = "已恢复默认参数，请重新填写对外地址后再应用"
    if stale:
        msg += "；当前网段 %s 下还有 %d 个账号的固定 IP 需要手工改过来" % (
            core.cfg()["vpn_cidr"], len(stale))
    return ok({"msg": msg, "stale": stale})


__all__ = [
    "settings_get",
    "settings_save",
    "settings_preview",
    "settings_public_ip",
    "settings_capacity",
    "api_pki",
    "api_pki_file",
    "client_ovpn",
    "settings_rebuild",
    "settings_reinit_ca",
    "settings_restore_default",
]
