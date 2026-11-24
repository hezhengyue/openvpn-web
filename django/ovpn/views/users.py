# ovpn/views/users.py
"""VPN 账号管理。"""
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


__all__ = [
    "users_get",
    "users_next_ip",
    "users_add",
    "users_update",
    "users_delete",
    "users_export",
    "users_import",
]
