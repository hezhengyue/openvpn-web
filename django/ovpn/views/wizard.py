# ovpn/views/wizard.py
"""安装向导。"""
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


__all__ = [
    "wizard_check",
    "wizard_params",
    "wizard_binary",
    "_install_rollback",
    "wizard_execute",
    "wizard_steps",
]
