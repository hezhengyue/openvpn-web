# ovpn/views/pages.py
"""前端页面。"""
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




# =========================================================== 前端页面
def index(request):
    """单页前端：ovpn/templates/ovpn/index.html

    走 Django 模板引擎只是为了把版本号从 `core.WEB_VERSION` 注入进去——页面里
    其余 116KB 是纯 HTML/JS，一个 `{{ }}` / `{% %}` 都没有，所以渲染结果与原文件
    逐字节等价。这样版本号只有一处定义，不再需要改前端时同步手改两遍。
    """
    return render(request, "ovpn/index.html", {"page_version": core.WEB_VERSION})



def favicon(request):
    return HttpResponse(status=204)



def self_page(request):
    """用户自助改密页（不需要后台登录；拨着 VPN 时按来源 IP 自动认人）

    原样读取文件而不走模板引擎：页面里的 `{{ }}` 是前端 JS 模板语法，交给
    Django 渲染会被当成变量吃掉。文件位置随 app 走（django/ovpn/templates/ovpn/）。
    """
    p = os.path.join(str(settings.BASE_DIR), "ovpn", "templates", "ovpn", "self_page.html")
    with open(p, "rb") as f:
        return HttpResponse(f.read(), content_type="text/html; charset=utf-8")


__all__ = [
    "index",
    "favicon",
    "self_page",
]
