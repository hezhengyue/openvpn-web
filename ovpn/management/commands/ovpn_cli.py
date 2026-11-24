#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
manage.py ovpn_cli —— 命令行救场工具（替代旧 app.py 的 --allow / --reset-admin / --restore-full）

白名单配错把自己关在门外、忘记管理凭据、全备份回滚，都 SSH 上来跑本命令。
"""
import os
import re
import sys
import uuid

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
            r = core.admin_reset_credentials()   # 生成凭据、写 meta、清 must_change
            User = get_user_model()
            # 必须「原地改名」而不是另建一个随机名超管：
            #   1) 新建会让旧超管和旧密码继续有效，凭据泄露时等于没重置；
            #   2) bootstrap.sync_admin_username() 取 id 最小的超管，重启后会把
            #      meta.admin_user 同步回旧名字，界面又显示旧账户。
            # 口径与 bootstrap 保持一致：主超管 = id 最小的那个。
            supers = list(User.objects.filter(is_superuser=True).order_by("id"))
            if supers:
                u = supers[0]
                # 新名字被别的账号占着（例如上一次重置的残留）：先改名停用，
                # 否则 Django 的 username UNIQUE 会让本次重置直接失败。
                clash = User.objects.filter(username=r["username"]).exclude(pk=u.pk).first()
                if clash:
                    clash.username = "disabled-" + uuid.uuid4().hex[:16]
                    clash.is_active = False
                    clash.is_staff = False
                    clash.is_superuser = False
                    clash.set_unusable_password()
                    clash.save()
            else:
                u = User(username=r["username"])
            u.username = r["username"]
            u.is_active = True
            u.is_staff = True
            u.is_superuser = True
            u.set_password(r["password"])
            u.save()
            # 其它超管一律停用并降权，避免留下第二条能用旧密码进后台的路。
            for other in supers[1:]:
                other.is_active = False
                other.is_staff = False
                other.is_superuser = False
                other.set_unusable_password()
                other.save()
            # must_change 由 core.admin_reset_credentials() 清成 0：一次性随机
            # 凭据无需再走首登强改，不能在这里又设回 1。
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
