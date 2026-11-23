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
        self.assertEqual(audit[0]["action"], "unit.test")
        self.assertEqual(audit[0]["actor"], "tester")
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
    """CSP 要分路径：业务页收紧（不许 eval），后台放宽（SimpleUI 需要 eval）。

    后台登录页是 new Vue({el:'.login-main'})，Vue 运行时编译器用 new Function
    生成渲染函数；CSP 里没有 'unsafe-eval' 就会被拒，挂载点被替换成空注释节点，
    表现就是「页面一片空白」。这条用例就是防它再退化。
    """

    def test_business_page_csp_has_no_unsafe_eval(self):
        resp = Client().get("/api/session")
        csp = resp["Content-Security-Policy"]
        self.assertIn("script-src 'self' 'unsafe-inline'", csp)
        self.assertNotIn("unsafe-eval", csp)

    def test_admin_page_csp_allows_eval(self):
        resp = Client().get("/admin/login/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("'unsafe-eval'", resp["Content-Security-Policy"])


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
                     "SystemSetting", "AuditRecord", "AuthLog",
                     "OvpnTask", "BackupRecord"):
            self.assertIn(getattr(models, name), dj_admin.site._registry)

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

    def test_auditlog_records_business_change(self):
        from auditlog.models import LogEntry

        VpnUser.objects.create(username="audit_probe", vpn_ip="10.8.0.99",
                               password_hash="x", note="")
        row = VpnUser.objects.get(username="audit_probe")
        row.note = "改备注"
        row.save()
        self.assertTrue(LogEntry.objects.filter(
            content_type__model="vpnuser", object_pk="audit_probe").exists())

    def test_auditlog_registered_for_business_models_only(self):
        from auditlog.registry import auditlog
        from . import models

        registered = set(auditlog.get_models())
        for name in ("VpnUser", "AssetGroup", "AssetItem", "UserGroup",
                     "UserGroupMember", "AclGrant", "PeerGrant"):
            self.assertIn(getattr(models, name), registered, name)
        # 日志/高频表不该登记，否则 auditlog 会被自己的写入刷满
        for name in ("SystemSetting", "AuditRecord", "AuthLog",
                     "OvpnTask", "BackupRecord"):
            self.assertNotIn(getattr(models, name), registered, name)
