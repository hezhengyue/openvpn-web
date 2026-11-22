#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ovpn/middleware.py —— 对应原 Flask 的 before_request / after_request

中间件职责：
  1) BootstrapMiddleware  : 首次请求前初始化 core 库 + 同步管理账户名（幂等，带锁）
  2) OvpnGuardMiddleware  : 来源 IP 白名单、到期账号处理、登录校验、首登强改密、CSRF
  3) SecurityHeadersMiddleware : 原 security_headers 的安全响应头 + HTML 不缓存

Django 自带的 CsrfViewMiddleware 已从中移除（settings 里注释掉），因为我们沿用
前端既有的「X-CSRF-Token == session['csrf']」契约，由 OvpnGuardMiddleware 校验，
保证 index.html 前端零改动。
"""
import os
import uuid
import threading

from django.contrib.auth import logout as auth_logout

from . import bridge

core = bridge.core

_BOOTSTRAP_DONE = False
_BOOTSTRAP_LOCK = threading.Lock()

# 未登录也要能访问的路径（首页容器、登录、自助页、session、favicon）
PUBLIC_PATHS = ("/", "/api/login", "/self", "/favicon.ico", "/health/")
PUBLIC_PREFIXES = ("/api/session", "/api/self", "/static/", "/media/")
# 首登强改密时仍放行的路径
MUST_CHANGE_ALLOW = ("/api/session", "/api/logout", "/api/password")
# 带副作用、虽是 GET 也要过 CSRF 的路径（与原版一致）
CSRF_GET_PATHS = ("/api/logs", "/api/selfcheck")


def _is_public(path):
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


class BootstrapMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        global _BOOTSTRAP_DONE
        if not _BOOTSTRAP_DONE:
            with _BOOTSTRAP_LOCK:
                if not _BOOTSTRAP_DONE:
                    try:
                        from . import bootstrap
                        bootstrap.ensure_bootstrap()
                        bridge.startup_maintenance()
                    except Exception as e:
                        # 初始化失败不能让整个服务起不来，但要留痕
                        print("[bootstrap] 初始化异常: %s" % e)
                    _BOOTSTRAP_DONE = True
        return self.get_response(request)


class OvpnGuardMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def process_exception(self, request, exception):
        """等价原 Flask 的 @app.errorhandler：ValueError -> JSON 400（不泄漏栈）"""
        from django.http import JsonResponse
        if isinstance(exception, ValueError):
            return JsonResponse({"ok": False, "msg": str(exception)}, status=400)
        return None

    def __call__(self, request):
        path = request.path
        # ovpn 的 API 沿用前端既有的 X-CSRF-Token 契约（下方自行校验），
        # 让 Django 自带 CSRF 跳过这些路径；/admin/ 与 /jobs/ 仍受 Django CSRF 保护。
        if not (path.startswith("/admin/") or path.startswith("/jobs/")):
            request._dont_enforce_csrf_checks = True

        ip = bridge.client_ip(request)

        # 审计上下文：让每条 core 日志都带上「谁 + 从哪个 IP」
        core.audit_ctx_set(bridge.actor(request), ip)

        # 自助改密覆盖两类人：白名单里的，和已拨进本机 VPN 隧道的
        is_self = (path == "/self" or path.startswith("/api/self") or path == "/api/session")
        if not core.client_allowed(ip) and not (is_self and core.vpn_client_allowed(ip)):
            return bridge.jerr(
                "来源 IP %s 不在管理端白名单内；要放行请在服务器上执行 "
                "python3 manage.py allow %s" % (ip, ip), 403,
                allow=core.allow_list())
        core.log_audit = core.log_audit  # noqa  (审计函数从上下文取 actor)

        # 到期的账号自动停用
        try:
            expired = core.expire_users()
            if expired:
                core.write_all_config()
                for name in expired:
                    bridge.sudo_worker(uuid.uuid4().hex, "kick-user", {"username": name})
        except Exception:
            pass

        # 免登录路径
        if _is_public(path):
            return self.get_response(request)

        # 登录校验：Django Auth
        if not getattr(request, "user", None) or not request.user.is_authenticated:
            return bridge.jerr("未登录", 401, need_login=True)

        # 首登强制改密
        if _must_change() and path not in MUST_CHANGE_ALLOW:
            return bridge.jerr("首次登录请先修改管理密码", 403, must_change=True)

        # CSRF：写请求（以及两个带副作用的 GET）都要过
        if request.method in ("POST", "PUT", "DELETE", "PATCH") or path in CSRF_GET_PATHS:
            token = request.META.get("HTTP_X_CSRF_TOKEN") or (bridge.jbody(request) or {}).get("csrf")
            if not token or token != request.session.get("csrf"):
                return bridge.jerr("CSRF 校验失败，请刷新页面", 403)

        # 长任务执行期间拒绝其它写操作
        if request.method not in ("GET", "HEAD", "OPTIONS") and bridge.WRITE_LOCK.locked():
            if not path.startswith("/api/task/") and path not in ("/api/login", "/api/logout", "/api/session"):
                return bridge.jerr("有任务正在执行（安装 / 升级 / 恢复），等它完成后再操作", 409)

        return self.get_response(request)


def _must_change():
    return bool(core.get_meta("admin_must_change"))


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        resp = self.get_response(request)
        resp.setdefault("X-Content-Type-Options", "nosniff")
        resp.setdefault("X-Frame-Options", "DENY")
        resp.setdefault("Referrer-Policy", "no-referrer")
        resp.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        resp.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; "
            "form-action 'self'; frame-ancestors 'none'")
        if request.is_secure or (request.META.get("HTTP_X_FORWARDED_PROTO") or "").lower() == "https":
            resp.setdefault("Strict-Transport-Security", "max-age=31536000")
        ctype = resp.get("Content-Type", "")
        if "text/html" in ctype:
            resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp["Pragma"] = "no-cache"
            resp["Expires"] = "0"
        return resp
