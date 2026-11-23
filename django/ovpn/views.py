#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ovpn/views.py —— 原 app.py 的全部 REST 路由（Django 版）

业务调用全部落到项目根的 core.py / fullbackup.py（零改动）。
URL、请求字段、响应结构保持与前端 index.html 完全一致。
鉴权相关的 6 个接口在 ovpn/auth.py。
"""
import os
import io
import re
import sys
import json
import uuid
import time
import threading
import traceback

from django.conf import settings
from django.http import HttpResponse, FileResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth import get_user_model

from . import bridge
from .bridge import (core, fullbackup, ok, jerr, err, jbody, client_ip, actor,
                     admin_user, file_response, text_response, WRITE_LOCK,
                     CTX, TASKS_DIR, tlog, task_update, sudo_worker, start_task,
                     after_change, install_code, pubip_get, MOCK, ROOT)


# =========================================================== 概览 / 状态
def api_state(request):
    c = core.cfg()
    users = core.list_users()
    vi = core.version_info()
    online = core.online_users()
    n_grants = core.count_grants()
    n_peers = core.count_peer_grants()
    recent = core.recent_auth(8)
    try:
        _usable = set(core.usable_host_ips(c))
        stale = [{"username": u["username"], "vpn_ip": u["vpn_ip"], "enabled": u["enabled"]}
                 for u in users if u["vpn_ip"] not in _usable]
    except Exception:
        stale = []
    return ok({
        "initialized": bool(core.get_meta("initialized")),
        "version": vi, "cfg": c, "admin_user": admin_user(), "src_ip": client_ip(request),
        "web_version": core.WEB_VERSION,
        "stale_ips": stale,
        "stats": {"users": len(users), "enabled": len([u for u in users if u["enabled"]]),
                  "online": len(online), "grants": n_grants, "peers": n_peers,
                  "acl_mode": c["acl_mode"], "nat_mode": c.get("nat_mode", "nat")},
        "recent_auth": recent,
        "vpn_alive": MOCK or core._vpn_alive(),
        "need_redistribute": False,
    })


# =========================================================== 安全白名单
def api_allow_get(request):
    return ok({"allow": core.allow_list(), "detected": core.local_subnets(),
               "current_ip": client_ip(request), "admin_user": admin_user(),
               "vpn_cidr": core.cfg().get("vpn_cidr", "")})


def api_allow_set(request):
    d = jbody(request)
    items = d.get("allow")
    if not isinstance(items, list):
        items = str(items or "").replace("，", ",").split(",")
    old = core.allow_list()
    new = core.set_allow_list(items, keep_ip=client_ip(request))
    core.log_audit("", "security.allow", "%s -> %s" % (",".join(old), ",".join(new)))
    return ok({"allow": new, "msg": "白名单已更新（立即生效，不用重启）"})


# =========================================================== 长任务
def api_task(request, tid):
    r = core.task_get(tid)
    if not r:
        return ok({"id": tid, "kind": "", "status": "unknown", "progress": 100,
                   "msg": "任务记录已随快照回滚，请以自检结果为准", "created_at": "",
                   "finished_at": "", "actor": ""})
    return ok(r)


def api_task_log(request, tid):
    offset = int(request.GET.get("offset", "0"))
    p = os.path.join(TASKS_DIR, tid + ".log")
    if not os.path.exists(p):
        return ok({"text": "", "offset": 0})
    size = os.path.getsize(p)
    if offset > size:
        offset = 0
    with open(p, "rb") as f:
        f.seek(offset)
        text = f.read().decode("utf-8", "replace")
    return ok({"text": text, "offset": size})


# =========================================================== 安装向导
def wizard_check(request):
    core.log_audit(actor(request) or "admin", "wizard.check", "环境自检")
    return ok({"items": core.env_check()})


def wizard_params(request):
    d = jbody(request)
    c = core.cfg()
    for k in core.DEFAULT_CFG:
        if k in d and k not in ("acl_mode", "allow_major_upgrade", "web_allow_cidr"):
            c[k] = d[k]
    errs = core.validate_cfg(c)
    if errs:
        raise ValueError("；".join(errs))
    _conf = core.pool_user_conflicts(c)
    if _conf:
        raise ValueError("动态地址池 %s 与 %d 个账号的固定 IP 重叠（%s），请把池子挪到空档"
                         % (core.pool_text(c), len(_conf),
                            "、".join("%s %s" % (x["username"], x["vpn_ip"]) for x in _conf[:8])))
    for k, v in c.items():
        core.set_meta(k, v)
    core.set_meta("wizard_step", 3)
    core.log_audit("admin", "wizard.params", "对外地址 %s:%s/%s" % (c["public_addr"], c["port"], c["proto"]))
    return ok({"cfg": c})


def wizard_binary(request):
    f = request.FILES.get("file")
    if not f:
        raise ValueError("未选择文件")
    core.ensure_dirs()
    staged = os.path.join(core.STAGING, "openvpn.upload")
    with open(staged, "wb") as dst:
        for chunk in f.chunks():
            dst.write(chunk)
    chk = core.check_binary(staged, allow_major=False,
                            expect_sha256=request.POST.get("sha256", ""))
    if not chk["ok"]:
        os.remove(staged)
        raise ValueError(chk["msg"])
    try:
        if MOCK or getattr(os, "geteuid", lambda: -1)() == 0:
            dst = core.BIN
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".new"
            import shutil
            shutil.copy2(staged, tmp)
            os.chmod(tmp, 0o755)
            os.replace(tmp, dst)
            os.remove(staged)
        else:
            sudo_worker(uuid.uuid4().hex, "replace-binary",
                        {"staged": staged, "install_code": install_code(request)}, timeout=120)
    except Exception:
        if os.path.exists(staged):
            os.remove(staged)
        raise
    v, br = core.binary_version(core.BIN)
    core.set_meta("openvpn_version", v)
    core.log_audit("admin", "wizard.binary", "上传 %s" % v)
    return ok({"version": v, "branch": br, "msg": chk["msg"]})


def _install_rollback(tid):
    tlog(tid, "开始回滚…")
    try:
        sudo_worker(tid, "stop-vpn", timeout=30)
    except Exception:
        pass


def wizard_execute(request):
    c = core.cfg()
    errs = core.validate_cfg(c)
    if errs:
        raise ValueError("；".join(errs))
    if not os.path.exists(core.BIN) and not MOCK:
        raise ValueError("缺少 bin/openvpn，请在向导第 2 步上传静态二进制")
    core.set_meta("wizard_step", 4)

    def s1():
        return "快照: " + (core.snapshot("安装前") if os.path.exists(os.path.join(core.DATA, "ca.crt")) else "首次安装，无需快照")

    def s2():
        core.gen_pki()
        return "已签发 CA + 服务端证书 + tls-crypt 密钥（账号密码模式，无客户端证书、无 CRL 过期问题）"

    def s3():
        r = core.write_all_config()
        return "配置已生成，ccd %d 个，指纹 %s" % (r["ccd"], r["fingerprint"])

    def s4():
        sudo_worker(CTX["tid"], "mask-system-unit")
        sudo_worker(CTX["tid"], "install-timer")
        return "已 mask 发行版 unit，规则重放定时器已启用"

    def s5():
        sudo_worker(CTX["tid"], "apply-rules")
        return "ACL 以【观察模式】下发：只记录不拦截"

    def s6():
        sudo_worker(CTX["tid"], "start-vpn")
        return "OpenVPN 已启动"

    def s7():
        sudo_worker(CTX["tid"], "selfcheck-system")
        core.set_meta("initialized", 1)
        core.set_meta("wizard_step", 5)
        return "自检完成；请把回程路由交给网管"

    steps = [("备份与前置检查", s1), ("签发 CA / 服务端证书 / tls-crypt", s2),
             ("生成 server.conf、ccd、company.ovpn、ACL 脚本", s3),
             ("接管进程与规则重放", s4), ("下发 ACL（观察模式）", s5),
             ("启动 OpenVPN", s6), ("自检并标记安装完成", s7)]
    tid = start_task("install", steps, actor(request) or "admin",
                     on_error=lambda t: (_install_rollback(t), core.set_meta("initialized", 0)))
    return ok({"task_id": tid})


def wizard_steps(request):
    return ok({"step": core.get_meta("wizard_step"), "initialized": bool(core.get_meta("initialized"))})


# =========================================================== 账号
def users_get(request):
    return ok({"users": core.list_users(), "stale": core.stale_ip_users(),
               "vpn_cidr": core.cfg()["vpn_cidr"]})


def users_next_ip(request):
    return ok({"vpn_ip": core.next_free_ip(core.cfg())})


def users_add(request):
    d = jbody(request)
    core.backup_users()
    r = core.add_user(d.get("username", ""), d.get("vpn_ip") or None,
                      d.get("password") or None, d.get("note", ""),
                      d.get("real_name", ""), d.get("dept", ""),
                      expires_at=d.get("expires_at", ""))
    r["_need"] = "rebuild"
    after_change(kick=False)
    return ok(r)


def users_update(request, name):
    d = jbody(request)
    new_name = (d.get("new_username") or "").strip()
    renamed = bool(new_name and new_name != name)
    core.backup_users()
    changes = core.update_user(name, new_username=new_name or None,
                               vpn_ip=d.get("vpn_ip"), enabled=d.get("enabled"),
                               note=d.get("note"), password=d.get("password"),
                               real_name=d.get("real_name"), dept=d.get("dept"),
                               expires_at=d.get("expires_at") if "expires_at" in d else None)
    kick = (renamed or any("IP " in x for x in changes)
            or ("enabled" in d and not d["enabled"]))
    after_change(kick=kick, username=name if kick else None)
    return ok({"changes": changes, "username": new_name or name})


def users_delete(request, name):
    core.backup_users()
    core.delete_user(name)
    after_change(kick=True, username=name)
    return ok()


def users_export(request):
    fmt = request.GET.get("fmt", "xlsx")
    name, data, mime = core.export_users(fmt)
    core.log_audit("admin", "users.export", fmt)
    return file_response(io.BytesIO(data), mime, name)


def users_import(request):
    step = request.POST.get("step", "preview")
    if step == "preview":
        f = request.FILES.get("file")
        if not f:
            raise ValueError("未选择文件")
        content = f.read()
        r = core.preview_import(content, f.name or "")
        return ok(r)
    token = request.POST.get("token", "")
    r = core.apply_import(token)
    after_change(kick=False)
    return ok(r)


# =========================================================== ACL / 资产 / 授权
def acl_get(request):
    c = core.cfg()
    return ok({"asset_groups": core.list_asset_groups(),
               "user_groups": core.list_user_groups(),
               "grants": core.list_grants(),
               "peers": core.list_peer_grants(), "users": core.list_users(),
               "expanded": len(core.expand_grants()),
               "mode": c["acl_mode"], "nat_mode": c.get("nat_mode", "nat")})


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
    r = core.add_asset_group(d.get("name", ""), d.get("note", ""), items)
    core.log_audit(actor(request) or "admin", "assetgroup.add", "%s %d 条" % (d.get("name", ""), r["count"]))
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


# =========================================================== 版本与更新
def _server_desc():
    return "waitress（Django WSGI，单进程多线程）"


def version_get(request):
    d = core.version_info()
    d["server"], d["threads"] = _server_desc(), 8
    return ok(d)


def update_binary(request):
    f = request.FILES.get("file")
    if not f:
        raise ValueError("未选择文件")
    allow_major = request.POST.get("allow_major") == "1"
    code = install_code(request)
    core.ensure_dirs()
    staged = os.path.join(core.STAGING, "openvpn.new")
    with open(staged, "wb") as dst:
        for chunk in f.chunks():
            dst.write(chunk)
    chk = core.check_binary(staged, allow_major=allow_major,
                            expect_sha256=request.POST.get("sha256", ""))
    if not chk["ok"]:
        os.remove(staged)
        raise ValueError(chk["msg"])
    old_fp = core.get_meta("config_fingerprint")
    snap = core.snapshot("更新二进制前")

    def rb(tid):
        tlog(tid, "更新失败，回滚到快照…")
        try:
            sudo_worker(tid, "rollback-binary", timeout=60)
        except Exception as e:
            tlog(tid, "二进制回滚失败（当前二进制保持不变）: %s" % e)
        try:
            core.restore_snapshot(snap)
            sudo_worker(tid, "restart-vpn")
        except Exception as e:
            tlog(tid, "回滚失败: %s" % e)

    def s1():
        sudo_worker(CTX["tid"], "replace-binary",
                    {"staged": staged, "allow_major": allow_major, "install_code": code})
        v, br = core.binary_version(core.BIN)
        return "已替换: %s（%s，EOL %s）" % (v, br, core.BRANCH_EOL.get(br, "未知"))

    def s2():
        sudo_worker(CTX["tid"], "restart-vpn")
        return "OpenVPN 已重启，在线连接已断开并自动重连"

    def s3():
        sudo_worker(CTX["tid"], "selfcheck-system")
        core.refresh_fingerprint()
        new_fp = core.get_meta("config_fingerprint")
        return "配置指纹 %s -> %s：%s" % (
            old_fp, new_fp, "未变，无需重新分发 ovpn" if old_fp == new_fp else "已变，必须全员替换 ovpn")

    tid = start_task("update-binary", [("校验并替换二进制", s1), ("重启服务", s2), ("自检与指纹比对", s3)],
                     actor(request) or "admin", on_error=rb)
    core.log_audit("admin", "update.binary", "allow_major=%s" % allow_major)
    return ok({"task_id": tid, "check": chk, "note": "只换二进制不改变配置指纹，company.ovpn 无需重新分发"})


def update_build_env(request):
    refresh = request.GET.get("refresh") == "1"
    return ok({"env": core.check_build_env(force=refresh)})


SRC_RE = re.compile(r"^openvpn[-_](\d+\.\d+\.\d+)[^/]*\.(tar\.gz|tgz)$", re.I)


def update_source(request):
    f = request.FILES.get("file")
    if not f:
        raise ValueError("未选择文件")
    name = os.path.basename(f.name or "")
    m = SRC_RE.match(name)
    if not m:
        raise ValueError("文件名需形如 openvpn-2.6.24.tar.gz（官方/GitHub 的源码发布包）")
    ver = m.group(1)
    be = core.check_build_env(force=True)
    if not be["ok"]:
        raise ValueError("本机缺少编译环境：%s。请改装「静态二进制」，或先补装这些工具后再点【重新检测】。"
                         % "、".join(be["missing"]))
    core.ensure_dirs()
    staged = os.path.join(core.STAGING, name)
    with open(staged, "wb") as dst:
        for chunk in f.chunks():
            dst.write(chunk)
    try:
        size = os.path.getsize(staged)
        if size < 100 * 1024 or size > 60 * 1024 * 1024:
            raise ValueError("文件大小 %d 字节，不像 openvpn 源码包" % size)
        with open(staged, "rb") as fh:
            if fh.read(2) != b"\x1f\x8b":
                raise ValueError("不是 gzip 压缩包")
    except Exception:
        os.remove(staged)
        raise
    allow_major = request.POST.get("allow_major") == "1"
    code = install_code(request)
    old_fp = core.get_meta("config_fingerprint")
    snap = core.snapshot("编译升级前")

    def rb(tid):
        tlog(tid, "编译升级失败，回滚到快照…")
        try:
            sudo_worker(tid, "rollback-binary", timeout=60)
        except Exception as e:
            tlog(tid, "二进制回滚失败（当前二进制保持不变）: %s" % e)
        try:
            core.restore_snapshot(snap)
            sudo_worker(tid, "restart-vpn", timeout=60)
        except Exception as e:
            tlog(tid, "回滚失败: %s" % e)

    def s1():
        sudo_worker(CTX["tid"], "build-openvpn",
                    {"staged": staged, "allow_major": allow_major, "install_code": code},
                    timeout=1500)
        v, br = core.binary_version(core.BIN)
        return "已编译并替换为 %s（%s 分支）" % (v, br)

    def s2():
        sudo_worker(CTX["tid"], "restart-vpn", timeout=60)
        return "OpenVPN 已重启，在线连接会断开并自动重连"

    def s3():
        sudo_worker(CTX["tid"], "selfcheck-system", timeout=60)
        core.refresh_fingerprint()
        return "配置指纹 %s -> %s" % (old_fp, core.get_meta("config_fingerprint"))

    tid = start_task("build-openvpn",
                     [("编译源码并替换二进制", s1), ("重启服务", s2), ("自检与指纹比对", s3)],
                     actor(request) or "admin", on_error=rb)
    core.log_audit("admin", "update.source", "%s allow_major=%s" % (name, allow_major))
    return ok({"task_id": tid, "version": ver,
               "note": "只换二进制不改变配置指纹，company.ovpn 无需重新分发"})


def update_web(request):
    f = request.FILES.get("file")
    if not f:
        raise ValueError("未选择文件")
    core.ensure_dirs()
    staged = os.path.join(core.STAGING, "web.zip")
    with open(staged, "wb") as dst:
        for chunk in f.chunks():
            dst.write(chunk)
    core.snapshot("更新 Web 前")
    tid = uuid.uuid4().hex
    sudo_worker(tid, "update-web", {"staged": staged, "install_code": install_code(request)})
    core.log_audit("admin", "update.web", "上传更新包")
    return ok({"task_id": tid, "msg": "Web 正在更新并重启，页面会自动重连；请勿关闭浏览器"})


# =========================================================== 备份 / 迁移
def backups_get(request):
    return ok({"backups": core.list_backups()})


def backups_create(request):
    d = jbody(request)
    p = core.snapshot(d.get("tag") or "手动")
    core.log_audit("admin", "backup.create", p)
    return ok({"path": p})


def backups_restore(request):
    d = jbody(request)
    path = d.get("path", "")
    core.restore_snapshot(path)
    tid = uuid.uuid4().hex
    sudo_worker(tid, "apply-rules")
    sudo_worker(tid, "restart-vpn")
    core.log_audit("admin", "backup.restore", path)
    return ok({"msg": "已恢复备份并重启服务"})


def user_backups_list(request):
    return ok({"items": core.list_backups(kind="users", limit=100)})


def user_backup_download(request):
    data, name = core.read_user_backup(request.GET.get("path", ""))
    ext = os.path.splitext(name)[1] or ".xlsx"
    pretty = "认证账号-" + name[len("users-"):-len(ext)] + ext if name.startswith("users-") else name
    return file_response(io.BytesIO(data),
                         core.XLSX_MIME if ext.lower() == ".xlsx" else "text/csv; charset=utf-8",
                         pretty)


def user_backup_restore(request):
    d = jbody(request)
    if d.get("step") == "apply":
        r = core.apply_import(d.get("token", ""), action="user_backup.restore")
        after_change(kick=False)
        return ok(r)
    data, name = core.read_user_backup(d.get("path", ""))
    r = core.preview_import(data, name)
    r["file"] = name
    return ok(r)


def full_backup_list(request):
    return ok({"items": fullbackup.list_items()})


def full_backup_create(request):
    d = jbody(request)
    r = fullbackup.create(d.get("tag") or "手动", kind="manual")
    core.log_audit("admin", "backup.full_create", r["name"])
    return ok(r)


def full_backup_download(request, name):
    path = os.path.join(fullbackup.FULL, name)
    if os.path.basename(name) != name or not name.endswith(".tar.gz") or not os.path.isfile(path):
        raise ValueError("备份包不存在")
    core.log_audit("admin", "backup.full_download", name)
    with open(path, "rb") as f:
        data = f.read()
    return file_response(io.BytesIO(data), "application/gzip", name)


def full_backup_upload(request):
    f = request.FILES.get("file")
    if not f:
        raise ValueError("未选择备份包")
    return ok(fullbackup.stage(f.read(), f.name or ""))


def full_backup_restore(request):
    d = jbody(request)
    token, name = d.get("token", ""), d.get("name", "")
    if token:
        path = fullbackup.staged_path(token)
    elif name:
        path = fullbackup.local_path(name)
    else:
        raise ValueError("请指定要恢复的备份包")
    r = fullbackup.restore(path)
    tid = uuid.uuid4().hex
    sudo_worker(tid, "apply-rules")
    sudo_worker(tid, "restart-vpn")
    stale = core.stale_ip_users()
    msg = "备份已恢复，服务正在重启"
    if r.get("bin_skipped"):
        msg += "（包里的 openvpn 二进制已跳过，当前二进制没动；要换版本请到「版本与更新」上传并填安装码）"
    if stale:
        msg += ("，但当前网段 %s 下有 %d 个账号的固定 IP 不在网段内"
                "（%s…），请到「认证账号」手工改地址"
                % (core.cfg()["vpn_cidr"], len(stale),
                   "、".join(u["username"] for u in stale[:5])))
    return ok({"msg": msg, "result": r, "stale": stale, "vpn_cidr": core.cfg()["vpn_cidr"]})


def full_backup_delete(request):
    name = (jbody(request) or {}).get("name", "")
    fullbackup.delete(name)
    core.log_audit("admin", "backup.full_delete", name)
    return ok()


# =========================================================== 在线 / 日志 / 自检 / 路由
def api_online(request):
    return ok({"users": core.online_users(), "users_all": core.list_users()})


def api_logs(request):
    kind = request.GET.get("kind", "audit")
    if kind == "deny":
        tid = uuid.uuid4().hex
        try:
            sudo_worker(tid, "collect-deny")
        except Exception:
            pass
    return ok({"rows": core.get_logs(kind, int(request.GET.get("limit", "200")))})


def api_selfcheck(request):
    tid = uuid.uuid4().hex
    try:
        sudo_worker(tid, "selfcheck-system")
    except Exception:
        pass
    return ok({"items": core.selfcheck()})


def api_route_guide(request):
    return ok(core.route_guide())


def api_restart(request):
    tid = uuid.uuid4().hex
    sudo_worker(tid, "restart-vpn")
    core.log_audit("admin", "service.restart", "手动重启")
    return ok({"msg": "已重启，在线用户会断线自动重连"})


# =========================================================== 前端页面
def index(request):
    """单页前端：django/templates/index.html

    走 Django 模板引擎只是为了把版本号从 `core.WEB_VERSION` 注入进去——页面里
    其余 116KB 是纯 HTML/JS，一个 `{{ }}` / `{% %}` 都没有，所以渲染结果与原文件
    逐字节等价。这样版本号只有一处定义，不再需要改前端时同步手改两遍。
    """
    return render(request, "index.html", {"page_version": core.WEB_VERSION})


def favicon(request):
    return HttpResponse(status=204)


def self_page(request):
    """用户自助改密页（不需要后台登录；拨着 VPN 时按来源 IP 自动认人）"""
    p = os.path.join(str(settings.BASE_DIR), "templates", "self_page.html")
    with open(p, "rb") as f:
        return HttpResponse(f.read(), content_type="text/html; charset=utf-8")
