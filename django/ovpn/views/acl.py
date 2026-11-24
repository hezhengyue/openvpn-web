# ovpn/views/acl.py
"""资产 / 用户组 / 授权 / 互访 / 网络模式。"""
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




# =========================================================== ACL / 资产 / 授权
def acl_get(request):
    return ok(selectors.acl_overview())



def api_nat_mode(request):
    d = jbody(request)
    mode = d.get("mode")
    if mode not in ("nat", "no-nat"):
        raise ValueError("网络模式只能是 nat / no-nat")
    old = core.cfg().get("nat_mode", "nat")
    core.set_meta("nat_mode", mode)
    core.write_all_config()
    tid = uuid.uuid4().hex
    sudo_worker(tid, "apply-rules")
    core.log_audit("admin", "network.nat", "%s -> %s" % (old, mode))
    msg = ("已开启【无 NAT 溯源】：内网服务器日志里的源 IP = 账号固定 IP；"
           "请确认网关回程静态路由已配好" if mode == "no-nat" else
           "已切回【NAT 模式】：开箱可用、不需要回程路由；内网日志源 IP = VPN 服务器，无法溯源到人")
    return ok({"nat_mode": mode, "fingerprint": core.get_meta("config_fingerprint"),
               "need_redistribute": False, "msg": msg})



def peers_add(request):
    d = jbody(request)
    core.add_peer_grant(d.get("src", ""), d.get("dst", ""), d.get("proto", "any"),
                        d.get("ports", ""), d.get("expires_at", ""), d.get("note", ""))
    after_change()
    return ok({"msg": "客户端互访授权已添加，规则已重建"})



def peers_del(request, pid):
    core.delete_peer_grant(pid)
    after_change()
    return ok()



def asset_group_add(request):
    d = jbody(request)
    items = d.get("items")
    if isinstance(items, str) or not items:
        items = core.parse_item_lines(d.get("lines", ""))
    else:
        items = [core.norm_item(i.get("cidr", ""), i.get("proto", "any"), i.get("ports", ""))
                 for i in items]
    # add_asset_group 内部已写 assetgroup.add 审计，视图层不要再补一条
    r = core.add_asset_group(d.get("name", ""), d.get("note", ""), items)
    return ok(r)



def asset_group_update(request, gid):
    d = jbody(request)
    core.update_asset_group(gid, d.get("name"), d.get("note"))
    return ok()



def asset_group_del(request, gid):
    core.delete_asset_group(gid)
    after_change()
    return ok()



def asset_item_add(request, gid):
    d = jbody(request)
    if "lines" in d:
        items = core.parse_item_lines(d.get("lines", ""))
        if not items:
            raise ValueError("没有可添加的有效条目")
        r = core.add_asset_items(gid, items)
    else:
        r = core.add_asset_item(gid, d.get("cidr", ""), d.get("proto", "any"),
                                d.get("ports", ""), d.get("note", ""))
    after_change()
    return ok(r)



def asset_item_update(request, iid):
    d = jbody(request)
    if "line" in d:
        items = core.parse_item_lines(d.get("line", ""))
        if len(items) != 1:
            raise ValueError("请只填写一条目标（一行）")
        cidr, proto, ports = items[0]
        r = core.update_asset_item(iid, cidr, proto, ports, d.get("note"))
    else:
        r = core.update_asset_item(iid, d.get("cidr"), d.get("proto"),
                                   d.get("ports"), d.get("note"))
    after_change()
    return ok(r)



def asset_item_del(request, iid):
    core.delete_asset_item(iid)
    after_change()
    return ok()



def user_group_add(request):
    d = jbody(request)
    members = core.parse_member_lines(d.get("lines", "")) if "lines" in d else (d.get("members") or [])
    r = core.add_user_group(d.get("name", ""), d.get("note", ""), members)
    return ok(r)



def user_group_update(request, gid):
    d = jbody(request)
    members = core.parse_member_lines(d.get("lines", "")) if "lines" in d else d.get("members")
    core.update_user_group(gid, d.get("name"), d.get("note"), members)
    after_change()
    return ok()



def user_group_del(request, gid):
    core.delete_user_group(gid)
    after_change()
    return ok()



def grants_add(request):
    d = jbody(request)
    r = core.add_grant(d.get("subject_type", "user"), d.get("subject", ""),
                       d.get("asset_group_id", 0), d.get("expires_at", ""), d.get("note", ""))
    after_change()
    return ok(r, msg="授权已生效，防火墙规则已重建")



def grants_del(request, gid):
    core.delete_grant(gid)
    after_change()
    return ok()



def acl_mode(request):
    d = jbody(request)
    mode = d.get("mode")
    if mode not in ("observe", "enforce"):
        raise ValueError("模式只能是 observe / enforce")
    old = core.cfg()["acl_mode"]
    core.set_meta("acl_mode", mode)
    core.write_all_config()
    tid = uuid.uuid4().hex
    sudo_worker(tid, "apply-rules")
    core.log_audit("admin", "acl.mode", "%s -> %s" % (old, mode))
    return ok({"mode": mode})


__all__ = [
    "acl_get",
    "api_nat_mode",
    "peers_add",
    "peers_del",
    "asset_group_add",
    "asset_group_update",
    "asset_group_del",
    "asset_item_add",
    "asset_item_update",
    "asset_item_del",
    "user_group_add",
    "user_group_update",
    "user_group_del",
    "grants_add",
    "grants_del",
    "acl_mode",
]
