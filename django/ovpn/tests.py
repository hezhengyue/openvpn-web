# ovpn/tests.py
"""业务层回归测试（Django TestCase）。

覆盖原 Flask 版 core.py 里最容易改坏的数据语义：
  * 账号改名要把「组成员 / ACL 主体 / 互访两端」一起搬
  * 资产条目按「规范化后的目标+协议+端口」去重
  * 用户组删除要连带清掉以「组名」为主体的授权
  * 授权展开只算启用账号、且过滤过期临时授权
  * 到期账号自动停用
"""
from datetime import datetime, timedelta

from django.test import Client, TestCase

from .services import core
from .models import (
    AclGrant, AssetGroup, AssetItem, PeerGrant, SystemSetting, UserGroup,
    UserGroupMember, VpnUser,
)


class MetaTest(TestCase):
    def test_meta_roundtrip_preserves_type(self):
        core.set_meta("port", 1234)
        core.set_meta("dns", ["1.1.1.1", "8.8.8.8"])
        self.assertEqual(core.get_meta("port"), 1234)
        self.assertEqual(core.get_meta("dns"), ["1.1.1.1", "8.8.8.8"])

    def test_meta_default_falls_back_to_default_cfg(self):
        self.assertEqual(core.get_meta("proto"), core.DEFAULT_CFG["proto"])

    def test_cfg_covers_every_default_key(self):
        # 未落库的键由 DEFAULT_CFG 兜底，cfg() 必须返回完整配置面
        self.assertEqual(set(core.cfg()), set(core.DEFAULT_CFG))
        self.assertEqual(core.cfg()["vpn_cidr"], core.DEFAULT_CFG["vpn_cidr"])
        core.set_meta("vpn_cidr", "10.9.0.0/24")
        self.assertTrue(SystemSetting.objects.filter(k="vpn_cidr").exists())
        self.assertEqual(core.cfg()["vpn_cidr"], "10.9.0.0/24")


class VpnUserTest(TestCase):
    def setUp(self):
        core.set_meta("vpn_cidr", "10.8.0.0/24")

    def test_add_user_assigns_free_ip_and_stores_plain(self):
        u = core.add_user("alice", real_name="爱丽丝", dept="研发")
        self.assertEqual(u["vpn_ip"], "10.8.0.2")
        self.assertEqual(u["password"], u["password"])
        row = VpnUser.objects.get(username="alice")
        self.assertEqual(row.real_name, "爱丽丝")
        self.assertEqual(len(row.password_plain), 16)
        self.assertTrue(core.verify_password(row.password_plain, row.password_hash))

    def test_duplicate_username_rejected(self):
        core.add_user("bob")
        with self.assertRaises(ValueError):
            core.add_user("bob")

    def test_duplicate_ip_rejected(self):
        core.add_user("alice", vpn_ip="10.8.0.9")
        with self.assertRaises(ValueError):
            core.add_user("bob", vpn_ip="10.8.0.9")

    def test_rename_moves_group_acl_and_peer_references(self):
        core.add_user("alice")
        core.add_user("bob")
        g = UserGroup.objects.create(name="运维组", note="")
        UserGroupMember.objects.create(group=g, username="alice")
        ag = AssetGroup.objects.create(name="内网", note="")
        AclGrant.objects.create(subject_type="user", subject="alice", asset_group=ag)
        PeerGrant.objects.create(src_username="alice", dst_username="bob")

        core.update_user("alice", new_username="alice-new")

        self.assertFalse(VpnUser.objects.filter(username="alice").exists())
        self.assertTrue(VpnUser.objects.filter(username="alice-new").exists())
        self.assertTrue(UserGroupMember.objects.filter(username="alice-new").exists())
        self.assertTrue(AclGrant.objects.filter(
            subject_type="user", subject="alice-new").exists())
        self.assertTrue(PeerGrant.objects.filter(src_username="alice-new").exists())
        self.assertFalse(PeerGrant.objects.filter(src_username="alice").exists())

    def test_delete_user_cleans_references(self):
        core.add_user("carol")
        g = UserGroup.objects.create(name="临时组", note="")
        UserGroupMember.objects.create(group=g, username="carol")
        core.delete_user("carol")
        self.assertFalse(VpnUser.objects.filter(username="carol").exists())
        self.assertFalse(UserGroupMember.objects.filter(username="carol").exists())

    def test_delete_missing_user_raises(self):
        with self.assertRaises(ValueError):
            core.delete_user("nobody")

    def test_expire_users_disables_past_due_only(self):
        today = datetime.now().date()
        past = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        future = (today + timedelta(days=30)).strftime("%Y-%m-%d")
        core.add_user("old", expires_at=past)
        core.add_user("new", expires_at=future)
        core.add_user("forever")

        names = core.expire_users()
        self.assertIn("old", names)
        self.assertFalse(VpnUser.objects.get(username="old").enabled)
        self.assertTrue(VpnUser.objects.get(username="new").enabled)
        self.assertTrue(VpnUser.objects.get(username="forever").enabled)


class AssetTest(TestCase):
    def test_group_with_items_and_norm(self):
        r = core.add_asset_group("机房A", items=[("10.0.0.0/24", "tcp", "80,443")])
        self.assertEqual(r["count"], 1)
        g = core.list_asset_groups()[0]
        self.assertEqual(g["name"], "机房A")
        self.assertEqual(g["items"][0]["ports"], "80,443")

    def test_duplicate_item_rejected_across_groups(self):
        core.add_asset_group("组1", items=[("10.1.1.1", "tcp", "22")])
        with self.assertRaises(ValueError):
            core.add_asset_group("组2", items=[("10.1.1.1/32", "tcp", "22")])

    def test_update_item_excludes_self_from_dedupe(self):
        core.add_asset_group("组1", items=[("10.2.2.2", "tcp", "80")])
        item = AssetItem.objects.get()
        r = core.update_asset_item(item.id, note="改备注")
        # 单主机地址会被规范化成 /32，这是原有语义
        self.assertEqual(r["cidr"], "10.2.2.2/32")
        self.assertEqual(AssetItem.objects.get().note, "改备注")

    def test_delete_group_cascades_grants_and_items(self):
        core.add_asset_group("组1", items=[("10.3.3.3", "tcp", "80")])
        g = AssetGroup.objects.get()
        AclGrant.objects.create(subject_type="user", subject="x", asset_group=g)
        core.delete_asset_group(g.id)
        self.assertFalse(AssetItem.objects.exists())
        self.assertFalse(AclGrant.objects.exists())


class GrantTest(TestCase):
    def setUp(self):
        core.set_meta("vpn_cidr", "10.8.0.0/24")
        core.add_user("dave")
        core.add_user("erin")
        core.add_asset_group("资产A", items=[("10.9.9.0/24", "tcp", "443")])

    def test_expand_grants_produces_iptables_units(self):
        ag = AssetGroup.objects.get()
        core.add_grant("user", "dave", ag.id)
        units = core.expand_grants()
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["src"], "10.8.0.2")
        self.assertEqual(units[0]["ports"], "443")

    def test_expired_grant_excluded(self):
        ag = AssetGroup.objects.get()
        past = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        core.add_grant("user", "dave", ag.id, expires_at=past)
        self.assertEqual(core.expand_grants(), [])

    def test_group_grant_expands_to_members(self):
        ag = AssetGroup.objects.get()
        g = UserGroup.objects.create(name="小组", note="")
        UserGroupMember.objects.create(group=g, username="dave")
        UserGroupMember.objects.create(group=g, username="erin")
        core.add_grant("group", "小组", ag.id)
        units = core.expand_grants()
        self.assertEqual({u["username"] for u in units}, {"dave", "erin"})

    def test_delete_group_removes_group_grants(self):
        ag = AssetGroup.objects.get()
        g = UserGroup.objects.create(name="小组", note="")
        core.add_grant("group", "小组", ag.id)
        core.delete_user_group(g.id)
        self.assertEqual(core.list_grants(), [])

    def test_peer_grant_unique_by_tuple(self):
        core.add_peer_grant("dave", "erin", proto="tcp", ports="3389")
        core.add_peer_grant("dave", "erin", proto="tcp", ports="3389", note="第二次")
        self.assertEqual(PeerGrant.objects.count(), 1)
        self.assertEqual(PeerGrant.objects.get().note, "第二次")

    def test_peer_grant_rejects_same_account(self):
        with self.assertRaises(ValueError):
            core.add_peer_grant("dave", "dave")


class LogTest(TestCase):
    def test_audit_and_auth_logs_are_readable(self):
        core.log_audit("tester", "unit.test", "单元测试")
        core.log_auth("alice", "1.2.3.4", "OK", "通过")
        audit = core.get_logs("audit", 10)
        auth = core.get_logs("auth", 10)
        # 安装期会往 JSONL 写真实 setup.auto 审计，不能假设自己写的在第一行；
        # 按 action + actor 精确定位本用例写入的那条。
        mine = [r for r in audit if r["action"] == "unit.test" and r["actor"] == "tester"]
        self.assertTrue(mine)
        self.assertEqual(mine[0]["action"], "unit.test")
        self.assertEqual(mine[0]["actor"], "tester")
        self.assertTrue(any(r["username"] == "alice" for r in auth))

    def test_task_record_lifecycle(self):
        core.task_create("t1", "selftest", "tester")
        core.task_update("t1", status="done", progress=100, msg="完成")
        row = core.task_get("t1")
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["progress"], 100)
        self.assertIsNone(core.task_get("nope"))


class CsrfFailureTest(TestCase):
    """CSRF 失败必须回 JSON：前端 api() 统一按 JSON 解析，HTML 403 会让它抛解析错。"""

    def test_api_post_without_token_returns_json(self):
        client = Client(enforce_csrf_checks=True)
        resp = client.post(
            "/api/login", data="{}", content_type="application/json"
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["ok"], False)
        self.assertTrue(resp.json()["msg"])


class SecurityHeadersTest(TestCase):
    """CSP 全站收紧：移除 SimpleUI 后后台是 Django 原生模板，不需要 'unsafe-eval'。

    这条用例防的是「有人为了图方便把 unsafe-eval 加回来」——之前 SimpleUI 的
    Vue 运行时编译必须它，代价是任何注入点都能 new Function，等于关掉一半 CSP。
    """

    def test_business_page_csp_has_no_unsafe_eval(self):
        resp = Client().get("/api/session")
        csp = resp["Content-Security-Policy"]
        self.assertIn("script-src 'self' 'unsafe-inline'", csp)
        self.assertNotIn("unsafe-eval", csp)

    def test_admin_page_csp_has_no_unsafe_eval(self):
        resp = Client().get("/admin/login/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("unsafe-eval", resp["Content-Security-Policy"])

    def test_admin_page_not_framable(self):
        resp = Client().get("/admin/login/")
        self.assertEqual(resp["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", resp["Content-Security-Policy"])


class IndexPageTest(TestCase):
    """版本号必须由服务端注入：页面里只有 {{ page_version }}，值来自 core.WEB_VERSION。"""

    def test_page_version_injected_from_web_version(self):
        resp = Client().get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn("const PAGE_VERSION='%s'" % core.WEB_VERSION, html)
        self.assertNotIn("{{ page_version }}", html)


class AdminRegistrationTest(TestCase):
    """业务台账要能在后台看到；但带副作用的写入不许从后台走。"""

    def test_business_models_registered(self):
        from django.contrib import admin as dj_admin
        from . import models

        for name in ("VpnUser", "AssetGroup", "AssetItem", "UserGroup",
                     "UserGroupMember", "AclGrant", "PeerGrant",
                     "SystemSetting", "AuthLog",
                     "OvpnTask", "BackupRecord"):
            self.assertIn(getattr(models, name), dj_admin.site._registry)

    def test_audit_log_registered_by_audit_app(self):
        from audit.models import AuditLog
        from django.contrib import admin as dj_admin

        self.assertIn(AuditLog, dj_admin.site._registry)
        ma = dj_admin.site._registry[AuditLog]
        self.assertFalse(ma.has_add_permission(None))
        self.assertFalse(ma.has_change_permission(None))
        self.assertFalse(ma.has_delete_permission(None))

    def test_password_columns_never_rendered(self):
        from django.contrib import admin as dj_admin
        from .models import VpnUser

        ma = dj_admin.site._registry[VpnUser]
        self.assertNotIn("password_hash", ma.fields)
        self.assertNotIn("password_plain", ma.fields)
        self.assertIn("password_state", ma.readonly_fields)

    def test_admin_cannot_create_or_delete_accounts(self):
        from django.contrib import admin as dj_admin
        from django.test import RequestFactory
        from django.contrib.auth import get_user_model
        from .models import VpnUser

        request = RequestFactory().get("/admin/")
        request.user = get_user_model()(username="root", is_superuser=True,
                                        is_staff=True, is_active=True)
        ma = dj_admin.site._registry[VpnUser]
        self.assertFalse(ma.has_add_permission(request))
        self.assertFalse(ma.has_delete_permission(request))

    def test_grants_are_read_only(self):
        from django.contrib import admin as dj_admin
        from django.test import RequestFactory
        from django.contrib.auth import get_user_model
        from .models import AclGrant

        request = RequestFactory().get("/admin/")
        request.user = get_user_model()(username="root", is_superuser=True,
                                        is_staff=True, is_active=True)
        ma = dj_admin.site._registry[AclGrant]
        self.assertFalse(ma.has_add_permission(request))
        self.assertFalse(ma.has_change_permission(request))
        self.assertFalse(ma.has_delete_permission(request))

class AuditIntegrationTest(TestCase):
    """业务门面 core.log_audit → 统一审计表的契约（50 个调用点全靠它）。"""

    def test_log_audit_writes_unified_record(self):
        from audit.models import AuditEventType, AuditLog

        core.log_audit("alice", "user.add", "新建账号 bob", ip="10.9.9.9")
        row = AuditLog.objects.filter(action="user.add").first()
        self.assertIsNotNone(row)
        self.assertEqual(row.actor_name, "alice")
        self.assertIsNone(row.actor)          # 字符串操作人不建外键
        self.assertEqual(row.remote_addr, "10.9.9.9")
        self.assertEqual(row.message, "新建账号 bob")
        self.assertEqual(row.module, "VPN 账号")
        self.assertEqual(row.event_type, AuditEventType.BUSINESS)

    def test_login_like_action_is_security_event(self):
        from audit.models import AuditEventType, AuditLog

        core.log_audit("admin", "login.fail", "密码错误", ip="10.0.0.1")
        row = AuditLog.objects.filter(action="login.fail").first()
        self.assertEqual(row.event_type, AuditEventType.SECURITY)
        self.assertEqual(row.module, "登录")

    def test_get_logs_maps_back_to_legacy_contract(self):
        core.log_audit("bob", "settings.save", "改了监听端口", ip="1.2.3.4")
        rows = [r for r in core.get_logs("audit", 200) if r["action"] == "settings.save"]
        self.assertTrue(rows)
        row = rows[0]
        # 前端只用这 5 个键，字段名必须与 Flask 版一致
        self.assertEqual(row["actor"], "bob")
        self.assertEqual(row["ip"], "1.2.3.4")
        self.assertEqual(row["detail"], "改了监听端口")
        self.assertRegex(row["ts"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_audit_records_are_append_only(self):
        from audit.models import AuditLog, AuditLogImmutable

        core.log_audit("bob", "user.delete", "删了账号")
        row = AuditLog.objects.filter(action="user.delete").first()
        row.message = "篡改"
        with self.assertRaises(AuditLogImmutable):
            row.save()
        with self.assertRaises(AuditLogImmutable):
            AuditLog.objects.filter(pk=row.pk).delete()


class RawAuditSinkTest(TestCase):
    """特权脚本（root）写 JSONL，Web 侧合并展示，不能丢也不能重复算。"""

    def setUp(self):
        import os
        self.path = os.path.join(core.STATE, core.AUDIT_JSONL)
        self.existed = os.path.exists(self.path)
        if self.existed:
            self.backup = open(self.path, "r", encoding="utf-8").read()

    def tearDown(self):
        import os
        if self.existed:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(self.backup)
        elif os.path.exists(self.path):
            os.remove(self.path)

    def test_jsonl_rows_merged_into_logs(self):
        import json
        import os

        os.makedirs(core.STATE, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-01-02 03:04:05", "actor": "installer",
                                "action": "setup.auto", "detail": "安装完成",
                                "ip": "10.1.1.1"}, ensure_ascii=False) + "\n")
        rows = [r for r in core.get_logs("audit", 200)
                if r["action"] == "setup.auto" and r["actor"] == "installer"
                and r["ts"] == "2026-01-02 03:04:05"]
        self.assertTrue(rows)
