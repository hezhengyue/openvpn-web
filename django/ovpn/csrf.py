#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CSRF 失败响应：API 回 JSON，后台表单仍用 Django 自带的 HTML 页。

settings.CSRF_FAILURE_VIEW 指向本函数。前端 api() 统一按 JSON 解析响应，
Django 默认的 HTML 403 会让它抛「Unexpected token <」，用户看不到
「页面已过期，请刷新后重试」这种可操作提示。
"""
from django.http import JsonResponse
from django.views.csrf import csrf_failure as _default_failure


def csrf_failure(request, reason=""):
    if request.path.startswith("/api/"):
        return JsonResponse(
            {"ok": False, "msg": "页面已过期，请刷新后重试", "need_refresh": True},
            status=403,
        )
    return _default_failure(request, reason=reason)
