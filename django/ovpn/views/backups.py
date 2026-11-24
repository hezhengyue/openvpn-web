# ovpn/views/backups.py
"""快照 / 台账 / 全量备份。"""
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
    items = core.list_backups(kind="users", limit=100)
    # 只向浏览器暴露文件名，不把服务器绝对路径透出去
    for item in items:
        if item.get("path"):
            item["path"] = os.path.basename(item["path"])
    return ok({"items": items})



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
    # 备份包可能上百 MB：交给 FileResponse 流式发送，别整包读进内存
    return file_response(path, "application/gzip", name)



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


__all__ = [
    "backups_get",
    "backups_create",
    "backups_restore",
    "user_backups_list",
    "user_backup_download",
    "user_backup_restore",
    "full_backup_list",
    "full_backup_create",
    "full_backup_download",
    "full_backup_upload",
    "full_backup_restore",
    "full_backup_delete",
]
