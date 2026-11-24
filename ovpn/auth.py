#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ovpn/auth.py —— 管理端登录（Django Auth + Axes）与用户自助改密

前端契约保持原样：
  /api/session  -> {ok, logged, csrf, initialized, web_version, mock, must_change, admin_user, src_ip, allow}
  /api/login    -> {ok, csrf, initialized, must_change, admin_user}
  /api/logout   -> {ok}
  /api/password -> {ok, msg, must_change}
  /api/admin-user -> {ok, username, msg}
"""
import re

from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.middleware.csrf import get_token, rotate_token

from . import bridge
from .bridge import core, ok, jerr, jbody, client_ip, actor, admin_user

User = get_user_model()


def _csrf(request):
    """CSRF token 统一交给 Django 管理。

    get_token() 会同时把 token 写进 csrftoken cookie，前端从 /api/session 的 JSON
    取到后放在 X-CSRF-Token 头里回传；CsrfViewMiddleware 用同一个 cookie 校验。
    不再自己生成 session['csrf']，避免两套 token 各说各话。
    """
    return get_token(request)


def _must_change():
    return bool(core.get_meta("admin_must_change"))


def api_session(request):
    return ok({
        "logged": request.user.is_authenticated,
        "csrf": _csrf(request),
        "initialized": bool(core.get_meta("initialized")),
        "web_version": core.WEB_VERSION,
        "mock": bridge.MOCK,
        "must_change": _must_change() if request.user.is_authenticated else False,
        "admin_user": admin_user() if request.user.is_authenticated else "",
        "src_ip": client_ip(request),
        "allow": core.allow_list(),
    })


def api_login(request):
    ip = client_ip(request)
    d = jbody(request)
    username = (d.get("username") or "").strip()
    password = d.get("password") or ""
    user = authenticate(request, username=username, password=password)
    if user is None:
        core.log_audit("", "login.fail", "账户或密码错误 (%s)" % username, ip=ip)
        return jerr("账户或密码错误", 401)
    auth_login(request, user)
    # 会话有效期由 settings.SESSION_COOKIE_AGE 控制；permanent 是 Flask 写法，
    # 在 Django 的 SessionBase 上赋值不会有任何效果。
    # 登录成功后轮换 token（防 session fixation），与 Django 官方推荐一致
    rotate_token(request)
    core.set_meta("admin_user", user.username)
    forced = _must_change()
    core.log_audit(user.username, "login", "登录成功", ip=ip)
    return ok({
        "csrf": get_token(request),
        "initialized": bool(core.get_meta("initialized")),
        "must_change": forced,
        "admin_user": user.username,
    })


def api_logout(request):
    core.log_audit(actor(request) or admin_user(), "logout", "退出登录", ip=client_ip(request))
    auth_logout(request)
    # 退出后旧 token 立即作废，避免拿着旧页面继续发写请求
    rotate_token(request)
    return ok()


def api_password(request):
    d = jbody(request)
    new = d.get("new", "")
    if new != d.get("confirm", ""):
        return jerr("两次输入的新密码不一致")
    forced = _must_change()
    if not forced:
        if not request.user.check_password(d.get("old", "")):
            return jerr("原密码错误")
    try:
        validate_password(new, user=request.user)
    except ValidationError as e:
        return jerr("；".join(e.messages))
    request.user.set_password(new)
    request.user.save()
    core._write_admin_password_note("# 管理密码已修改，明文不再保存（Django Auth）。\n")
    # Django 改密后会轮换 session key，重新登录以保住会话（update_session_auth_hash 等价物）
    from django.contrib.auth import update_session_auth_hash
    update_session_auth_hash(request, request.user)
    if forced:
        core.set_meta("admin_must_change", 0)
    core.log_audit(actor(request), "password.change",
                   "首次登录已强制修改默认密码" if forced else "管理密码已修改",
                   ip=client_ip(request))
    return ok(msg="管理密码已修改", must_change=False)


def api_admin_user(request):
    """改管理账户名：忘了密码时也要能改，因此不校验当前密码。"""
    d = jbody(request)
    name = (d.get("username") or "").strip()
    if not name:
        return jerr("账户名不能为空")
    # 与 core.ADMIN_USER_RE 一致：3-32 位字母/数字/下划线/连字符，拒绝空格等
    if not re.match(r"^[A-Za-z0-9_\-]{3,32}$", name):
        return jerr("账户名只能包含字母、数字、下划线和连字符（3-32 位）")
    u = request.user
    old = u.username
    u.username = name
    try:
        u.save(update_fields=["username"])
    except Exception:
        return jerr("账户名已存在或非法")
    core.set_meta("admin_user", name)
    core.log_audit(actor(request), "admin.rename", "%s -> %s" % (old, name), ip=client_ip(request))
    return ok({"username": name, "msg": "管理账户名已改为 " + name})


# --------------------------------------------------------------------------- 自助改密页
SELF_PAGE = ""  # 由 views.py 从内联 HTML 常量提供（保持与原版一致）


def api_self_whoami(request):
    ip = client_ip(request)
    u = core.user_by_vpn_ip(ip)
    if not u:
        return ok({"identified": False, "src_ip": ip,
                   "msg": "没有从来源地址认出账号：请先连上 VPN 再打开本页"})
    return ok({"identified": True, "vpn_ip": u["vpn_ip"], "username": u["username"],
               "real_name": u.get("real_name") or "", "dept": u.get("dept") or "",
               "enabled": bool(u["enabled"])})


def api_self_password(request):
    ip = client_ip(request)
    key = "self:" + ip
    if bridge._fails_take(key) >= 5:
        return jerr("失败次数过多，10 分钟后再试", 429)
    # CSRF 由 CsrfViewMiddleware 统一校验（X-CSRF-Token 对 csrftoken cookie），
    # 这里不再重复比对，避免两套 token 不同源。
    d = jbody(request)
    u = core.user_by_vpn_ip(ip)
    if not u:
        bridge._fails_add(key)
        return jerr("没识别到你的账号，请先连上 VPN 再打开本页；已经连着 VPN 请联系管理员核对固定 IP", 401)
    try:
        r = core.self_change_password(u["vpn_ip"], d.get("old", ""), d.get("new", ""), src_ip=ip)
    except ValueError as e:
        bridge._fails_add(key)
        return jerr(str(e), 401)
    bridge._fails_clear(key)
    return ok({"username": r["username"], "msg": "密码已修改，下次拨号请使用新密码"})
