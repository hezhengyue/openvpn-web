#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ovpn/middleware.py —— 对应原 Flask 的 before_request / after_request

中间件职责：
  1) BootstrapMiddleware  : 首次请求前初始化 core 库 + 同步管理账户名（幂等，带锁）
  2) OvpnGuardMiddleware  : 来源 IP 白名单、到期账号处理、登录校验、首登强改密、
                            带副作用 GET 的 CSRF 兜底
  3) SecurityHeadersMiddleware : 原 security_headers 的安全响应头 + HTML 不缓存

CSRF 由 Django 自带的 CsrfViewMiddleware 负责：settings 里
CSRF_HEADER_NAME = 'HTTP_X_CSRF_TOKEN' 与前端既有的头对齐，token 由 /api/session
用 get_token() 下发（同时写 csrftoken cookie）。前端仍然零改动。
只有 GET 但带副作用的两个接口（CSRF_GET_PATHS）Django 不校验，这里按同一份
cookie token 补一次校验。
"""
import time
import uuid
import threading

from django.conf import settings
from django.middleware.csrf import (
    InvalidTokenFormat, _check_token_format, _does_token_match,
)

from . import bridge

core = bridge.core

_BOOTSTRAP_DONE = False
_BOOTSTRAP_LOCK = threading.Lock()
# 账号到期扫描要查库、还可能触发踢人。60 秒一次足够，且必须带锁：
# waitress 多线程下每个轮询请求都扫一遍既浪费，也会重复 kick-user。
_EXPIRE_LAST = 0.0
_EXPIRE_LOCK = threading.Lock()

# 未登录也要能访问的路径（首页容器、登录、自助页、session、favicon）
PUBLIC_PATHS = ("/", "/api/login", "/self", "/favicon.ico", "/health/")
PUBLIC_PREFIXES = ("/api/session", "/api/self", "/static/", "/media/")
# 首登强改密时仍放行的路径
MUST_CHANGE_ALLOW = ("/api/session", "/api/logout", "/api/password")
# 带副作用、虽是 GET 也要过 CSRF 的路径（与原版一致）
CSRF_GET_PATHS = ("/api/logs", "/api/selfcheck")


def _is_public(path):
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def _is_readonly_poll(path):
    """只读轮询：不续期会话、不触发到期扫描。"""
    return (path == "/api/session" or path.startswith("/api/task/")
            or path in ("/health/", "/favicon.ico"))


def _expire_due_users():
    global _EXPIRE_LAST
    now = time.time()
    if now - _EXPIRE_LAST < 60:
        return []
    with _EXPIRE_LOCK:
        now = time.time()
        if now - _EXPIRE_LAST < 60:
            return []
        try:
            return core.expire_users()
        finally:
            _EXPIRE_LAST = time.time()


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
        if isinstance(exception, bridge.TaskBusy):
            return JsonResponse({"ok": False, "msg": str(exception)}, status=409)
        if isinstance(exception, ValueError):
            return JsonResponse({"ok": False, "msg": str(exception)}, status=400)
        return None

    def __call__(self, request):
        path = request.path
        # 监控探活与静态资源必须先于管理端 IP 白名单放行：探活通常来自宿主机、
        # 负载均衡或监控网段，把它们塞进白名单既不合理也会让健康检查 403；
        # /static/ 则由 WhiteNoise 直接托管，本身不接触任何业务数据。
        if path == "/health/" or path.startswith("/static/"):
            return self.get_response(request)
        # CSRF 全部交给 CsrfViewMiddleware：settings 里 CSRF_HEADER_NAME 已对齐
        # 前端的 X-CSRF-Token，token 由 /api/session 用 get_token() 下发。
        # 这里只额外覆盖「带副作用的 GET」（Django 不管 GET），见下方 CSRF_GET_PATHS。

        ip = bridge.client_ip(request)

        # 审计上下文：让每条 core 日志都带上「谁 + 从哪个 IP」
        core.audit_ctx_set(bridge.actor(request), ip)

        # 自助改密覆盖两类人：白名单里的，和已拨进本机 VPN 隧道的
        is_self = (path == "/self" or path.startswith("/api/self") or path == "/api/session")
        if not core.client_allowed(ip) and not (is_self and core.vpn_client_allowed(ip)):
            return bridge.jerr(
                "来源 IP %s 不在管理端白名单内；要放行请在服务器上执行 "
                "python3 manage.py ovpn_cli allow %s" % (ip, ip), 403,
                allow=core.allow_list())

        # 到期的账号自动停用 + 补做副作用。
        # expire_users() 可能被 list_users() 等视图先触发，那时它已经把名单放进
        # core 的待办集合；这里每次请求先消费待办，再合并扫描结果，确保 60 秒
        # 节流窗口内也不会漏掉「删 ccd + 踢下线」。
        expired = []
        try:
            expired = list(dict.fromkeys(core.take_expired_users() + _expire_due_users()))
            if expired:
                core.write_all_config()
                for name in expired:
                    bridge.sudo_worker(uuid.uuid4().hex, "kick-user", {"username": name})
        except Exception:
            # 写配置/踢人失败时放回待办，下次请求重试，绝不因节流而永久漏处理
            try:
                core.mark_expired_pending(expired)
            except Exception:
                pass

        # 免登录路径
        if _is_public(path):
            return self.get_response(request)

        # Django 原生页面（后台 /admin/、任务中心 /jobs/）：IP 白名单之后，
        # 登录与权限完全交给 Django 自己——admin 会跳 /admin/login/，
        # staff_member_required 会 302。若在这里返回 JSON 401，后台登录页
        # 也会被挡死，管理员根本进不去（base 的单测就是抓这个）。
        if path.startswith("/admin/") or path.startswith("/jobs/"):
            # 这里提前 return 会跳过下面的滑动续期；不补这一句，管理员在
            # Django Admin 连续操作超过 1 小时会被 session 到期踢出去。
            _sess = getattr(request, "session", None)
            if _sess is not None and getattr(getattr(request, "user", None),
                                             "is_authenticated", False):
                _sess.modified = True
            return self.get_response(request)

        # 登录校验：Django Auth
        if not getattr(request, "user", None) or not request.user.is_authenticated:
            return bridge.jerr("未登录", 401, need_login=True)

        # 真实操作滑动续期；纯轮询不写 django_session，避免 700ms 日志轮询
        # 不停制造写锁竞争，也避免绕过前端的脚本靠轮询让会话永不过期。
        sess = getattr(request, "session", None)
        if sess is not None and not (request.method in ("GET", "HEAD")
                                     and _is_readonly_poll(path)):
            sess.modified = True

        # 首登强制改密
        if _must_change() and path not in MUST_CHANGE_ALLOW:
            return bridge.jerr("首次登录请先修改管理密码", 403, must_change=True)

        # CSRF：写请求与两个带副作用的 GET 都要过。
        # 生产环境 CsrfViewMiddleware 已校验过一次；这里对 ovpn 自己的 API 再校验
        # 一次，原因是 Django 的 test client 默认关闭 CSRF，只靠外层中间件无法在
        # 自检里证明防护真的生效。/admin/ 与 /jobs/ 已在上面交还 Django，不走这里。
        needs_csrf = path in CSRF_GET_PATHS or request.method in ("POST", "PUT", "DELETE", "PATCH")
        if needs_csrf:
            token = (request.META.get("HTTP_X_CSRF_TOKEN")
                     or (bridge.jbody(request) or {}).get("csrf"))
            secret = request.META.get("CSRF_COOKIE")
            ok_csrf = False
            if token and secret:
                try:
                    # _check_token_format 用抛异常表示非法，不是返回 bool；
                    # _does_token_match 会先给 masked token 解掩码再比。
                    _check_token_format(token)
                    ok_csrf = _does_token_match(token, secret)
                except InvalidTokenFormat:
                    ok_csrf = False
            if not ok_csrf:
                return bridge.jerr("CSRF 校验失败，请刷新页面", 403)

        # 长任务执行期间拒绝其它写操作
        if request.method not in ("GET", "HEAD", "OPTIONS") and bridge.WRITE_LOCK.locked():
            if not path.startswith("/api/task/") and path not in ("/api/login", "/api/logout", "/api/session"):
                return bridge.jerr("有任务正在执行（安装 / 升级 / 恢复），等它完成后再操作", 409)

        return self.get_response(request)


def _must_change():
    return bool(core.get_meta("admin_must_change"))


# CSP 只放 'unsafe-inline'：业务 SPA 与后台都是自包含页面内联脚本。
# 移除 SimpleUI 后后台回到 Django 原生模板（没有 Vue 运行时编译），
# 'unsafe-eval' 不再需要，整条去掉——少一个口子就少一类 XSS 利用面。


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        resp = self.get_response(request)
        resp.setdefault("X-Content-Type-Options", "nosniff")
        # 与 settings.X_FRAME_OPTIONS 保持一致（默认 DENY：没有任何嵌框需求）。
        resp.setdefault("X-Frame-Options", getattr(settings, "X_FRAME_OPTIONS", "DENY"))
        resp.setdefault("Referrer-Policy", "no-referrer")
        resp.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        resp.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; "
            "form-action 'self'; frame-ancestors 'none'")
        # request.is_secure 是方法，必须调用；漏掉 () 会把 bound method 当真值，
        # 每个纯 HTTP 响应都下发一年 HSTS，浏览器缓存后内网后台就再也打不开。
        # SECURE_PROXY_SSL_HEADER 只在 TRUST_LOCAL_PROXY 时配置，Django 的
        # is_secure() 已按它判断转发头；不要再手工信 X-Forwarded-Proto，
        # 否则直连端口伪造该头就能骗出 HSTS。
        if request.is_secure():
            resp.setdefault("Strict-Transport-Security", "max-age=31536000")
        ctype = resp.get("Content-Type", "")
        if "text/html" in ctype:
            resp["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp["Pragma"] = "no-cache"
            resp["Expires"] = "0"
        return resp
