#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""manage.py ovpn_init —— 初始化 core 库 + Django 管理账号（幂等）"""
import os

from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model

from ovpn import bridge

core = bridge.core


class Command(BaseCommand):
    help = "初始化 OpenVPN Web：core 数据库 + Django 管理员（默认 admin/admin，首登强改）"

    def handle(self, *args, **opts):
        core.init_db()
        # 初始化 core 库的业务配置（cfg/meta 默认值）
        core.set_meta("admin_user", core.get_meta("admin_user") or "admin")

        User = get_user_model()
        # 初始管理密码：沿用 data/admin_password.txt（安装脚本写入），否则 admin
        pw_file = os.path.join(core.DATA, "admin_password.txt")
        pw = "admin"
        if os.path.exists(pw_file):
            lines = [x.strip() for x in open(pw_file, encoding="utf-8").read().splitlines()
                     if x.strip() and not x.startswith("#")]
            if lines:
                pw = lines[0]
        # 账户名沿用 core.meta（可能被改过）
        username = core.get_meta("admin_user") or "admin"
        if not User.objects.filter(username=username).exists():
            u = User(username=username, is_staff=True, is_superuser=True)
            u.set_password(pw)
            u.save()
            core.set_meta("admin_must_change", 1 if pw == "admin" else 0)
            self.stdout.write("已创建 Django 管理员 %s" % username)
        else:
            self.stdout.write("Django 管理员 %s 已存在" % username)
