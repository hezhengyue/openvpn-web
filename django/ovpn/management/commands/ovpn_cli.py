#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
manage.py ovpn_cli —— 命令行救场工具（对应旧 app.py 的 --allow / --reset-admin / --restore-full）

白名单配错把自己关在门外、忘记管理凭据、全备份回滚，都 SSH 上来跑本命令。
"""
import os
import re
import sys

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model

from ovpn import bridge

core = bridge.core


class Command(BaseCommand):
    help = "OpenVPN Web 命令行救场：allow / reset-admin / restore-full"

    def add_arguments(self, parser):
        parser.add_argument("op", choices=["allow", "allow-add", "allow-reset",
                                           "reset-admin", "restore-full"])
        parser.add_argument("value", nargs="?", default="")

    def handle(self, *args, **opts):
        core.init_db()
        op, value = opts["op"], opts["value"]

        if op in ("allow", "allow-add", "allow-reset"):
            if op == "allow-reset":
                items = core.local_subnets()
            else:
                if not value:
                    raise CommandError("用法: manage.py ovpn_cli allow 10.20.30.0/24[,10.0.0.0/8]")
                items = list(core.allow_list()) if op == "allow-add" else []
                items += re.split(r"[,，\s]+", value)
            self.stdout.write("新白名单: %s" % ", ".join(core.set_allow_list(items)))
            return

        if op == "reset-admin":
            r = core.admin_reset_credentials()
            # 同步到 Django User
            User = get_user_model()
            u, _ = User.objects.get_or_create(username=r["username"],
                                              defaults={"is_staff": True, "is_superuser": True})
            u.is_staff = True
            u.is_superuser = True
            u.set_password(r["password"])
            u.save()
            core.set_meta("admin_must_change", 1)
            self.stdout.write("管理账户: %s" % r["username"])
            self.stdout.write("管理密码: %s" % r["password"])
            self.stdout.write("请重启服务后使用新凭据登录: systemctl restart openvpn-web")
            return

        if op == "restore-full":
            if not value:
                raise CommandError("用法: manage.py ovpn_cli restore-full /path/to/backup.tar.gz")
            r = bridge.fullbackup.restore(value, audit=False, with_bin=True)
            self.stdout.write("已从 %s 恢复：openvpn %d / web %d（Web %s）"
                              % (r["name"], r["openvpn_files"], r["web_files"], r["web_version"]))
            self.stdout.write("请重启: systemctl restart openvpn-web && systemctl restart openvpn")
            return
