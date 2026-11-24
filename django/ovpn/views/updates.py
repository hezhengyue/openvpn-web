# ovpn/views/updates.py
"""OpenVPN 二进制 / 源码 / Web 更新。"""
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


SRC_RE = re.compile(r"^openvpn[-_](\d+\.\d+\.\d+)[^/]*\.(tar\.gz|tgz)$", re.I)




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


__all__ = [
    "update_binary",
    "update_build_env",
    "update_source",
    "update_web",
]
