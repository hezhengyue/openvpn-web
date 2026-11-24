# ovpn/views/state.py
"""概览 / 状态 / 会话可见信息。"""
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




# =========================================================== 概览 / 状态
def api_state(request):
    # 聚合取数在 selectors（可单测），这里只补与请求相关、不能缓存的字段
    data = selectors.dashboard_state()
    data.update({
        "admin_user": admin_user(),
        "src_ip": client_ip(request),
        "vpn_alive": MOCK or core._vpn_alive(),
        "need_redistribute": False,
    })
    return ok(data)



# =========================================================== 安全白名单
def api_allow_get(request):
    data = selectors.allow_overview(client_ip(request))
    data["admin_user"] = admin_user()
    return ok(data)



def api_allow_set(request):
    d = jbody(request)
    items = d.get("allow")
    if not isinstance(items, list):
        items = str(items or "").replace("，", ",").split(",")
    # set_allow_list 内部已写 security.allow 审计（含 old -> new），这里不再重复
    new = core.set_allow_list(items, keep_ip=client_ip(request))
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
    if not re.fullmatch(r"[0-9a-f]{32}", tid or ""):
        raise ValueError("任务 ID 非法")
    offset = max(0, int(request.GET.get("offset", "0") or 0))
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
    limit = max(1, min(int(request.GET.get("limit", "200") or 200), 2000))
    return ok({"rows": core.get_logs(kind, limit)})



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



# =========================================================== 版本与更新
def _server_desc():
    return "waitress（Django WSGI，单进程多线程）"



def version_get(request):
    d = core.version_info()
    d["server"], d["threads"] = _server_desc(), 8
    return ok(d)


__all__ = [
    "api_state",
    "api_allow_get",
    "api_allow_set",
    "api_task",
    "api_task_log",
    "api_online",
    "api_logs",
    "api_selfcheck",
    "api_route_guide",
    "api_restart",
    "_server_desc",
    "version_get",
]
