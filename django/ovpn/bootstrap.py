#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ovpn/bootstrap.py —— Django Auth 与 core 业务层的引导对接

设计：
  * 管理登录完全走 Django User（django.contrib.auth），防爆破走 Axes。
  * core.py 里与「管理账户名」相关的地方（审计日志 actor、/api/state 返回等）
    只用到一个名字字符串，这里把 Django 管理员的用户名同步到 core.meta，
    保证两套体系展示一致，却不必改动 core.py 任何一行。
  * 首登强改密标记仍落在 core.meta（admin_must_change），前端契约不变。
"""
import os

from django.conf import settings
from django.db import connection
from django.contrib.auth import get_user_model

from . import bridge

core = bridge.core


def _table_exists(name):
    with connection.cursor() as cur:
        return name in connection.introspection.table_names()


def sync_admin_username():
    """把 Django 超级用户的用户名同步到 core.meta，供审计/接口展示。"""
    User = get_user_model()
    # 取第一个 superuser（正常安装只会有一个 admin）
    su = User.objects.filter(is_superuser=True).order_by("id").first()
    if not su:
        return
    if (core.get_meta("admin_user") or "") != su.username:
        core.set_meta("admin_user", su.username)


def ensure_bootstrap():
    """
    在 core 数据库就绪后调用。幂等：
      - 初始化 core 库（建表 / 迁移）
      - 首次：设置 admin_must_change=1（前端首登强改密）
      - 同步 Django 管理员名到 core.meta
    """
    core.init_db()
    # 首次安装：默认 admin/admin，前端强制改密（与旧版行为一致）
    if core.get_meta("admin_must_change") is None and not core.get_meta("initialized"):
        core.set_meta("admin_must_change", 1)
    # Django 用户表可能还没迁移（首次 migrate 之前），跳过同步
    if _table_exists(User_TABLE()):
        sync_admin_username()


def User_TABLE():
    return get_user_model()._meta.db_table


# apps.ready() 在 migrate 之前也会触发，此时表不存在；真正的初始化放在
# 第一次 Web 请求前的中间件里做（见 ovpn/middleware.py）。
