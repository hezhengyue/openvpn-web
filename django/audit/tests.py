"""独立审计 app 单测：入口、脱敏、上下文、Admin、信号、清理。"""
import csv
import json
from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.db import IntegrityError, transaction

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.signals import (
    user_logged_in, user_logged_out, user_login_failed,
)
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from audit.admin import AuditLogAdmin, AuditModelAdmin
from audit.middleware import AuditContextMiddleware, get_current_request
from audit.models import (
    AUDIT_MODULE, AUTH_MODULE, AuditAction, AuditEventType,
    AuditLog, AuditLogImmutable,
)
from audit.services import (
    CSV_FIELDS, CSV_HEADER, audit_log, build_audit_queryset,
    export_audit_csv, query_audit_logs,
)
from audit.tasks import clean_old_audit_logs
from audit.utils import model_to_changes, sanitize_changes, sanitize_value, serialize_value


class AuditLogServiceTest(TestCase):

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='alice', password='pass12345', phone='13800000000'
        )
        self.factory = RequestFactory()

    def _request(self, **extra):
        request = self.factory.post('/admin/core/user/1/change/', **extra)
        request.user = self.user
        request._real_ip = '203.0.113.9'
        request.META['REMOTE_ADDR'] = '203.0.113.9'
        request.audit_request_id = 'req-001'
        return request

    def test_log_audit_extracts_actor_ip_and_request_id(self):
        entry = audit_log(
            request=self._request(), action='update', module='用户管理',
            instance=self.user, changes={'phone': {'old': '138', 'new': '139'}},
            message='修改手机号',
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor, self.user)
        self.assertEqual(entry.actor_name, 'alice')
        self.assertEqual(entry.remote_addr, '203.0.113.9')
        self.assertEqual(entry.request_id, 'req-001')
        self.assertEqual(entry.object_type, 'core.User')
        self.assertEqual(entry.object_id, str(self.user.pk))
        self.assertEqual(entry.changes['phone']['new'], '139')

    def test_log_audit_without_request(self):
        with override_settings(AUDIT_FALLBACK_ACTOR='admin'):
            entry = audit_log(action='system_task', module='任务中心', message='定时任务')
        self.assertIsNone(entry.actor)
        self.assertEqual(entry.actor_name, 'admin')
        self.assertEqual(entry.remote_addr, '')

    def test_default_fallback_actor_is_blank(self):
        with override_settings(AUDIT_FALLBACK_ACTOR=''):
            entry = audit_log(action='system_task')
        self.assertEqual(entry.actor_name, '')

    def test_string_actor_downgrades_to_snapshot(self):
        for value in ('admin', 42, object()):
            entry = audit_log(action='legacy', actor=value)
            self.assertIsNotNone(entry)
            self.assertIsNone(entry.actor)
            self.assertEqual(entry.actor_name, str(getattr(value, 'username', '') or value)[:150])

    def test_explicit_actor_name_wins(self):
        entry = audit_log(
            action='legacy', request=self._request(), actor='admin', actor_name='system',
        )
        self.assertIsNone(entry.actor)
        self.assertEqual(entry.actor_name, 'system')

    def test_write_failure_returns_none(self):
        from unittest.mock import patch
        with patch('audit.services.AuditLog.objects.create', side_effect=ValueError('bad actor')):
            self.assertIsNone(audit_log(action='x', actor='admin'))

    def test_long_message_is_not_truncated_at_2000(self):
        entry = audit_log(action='x', message='a' * 5000)
        self.assertEqual(len(entry.message), 5000)

    def test_sensitive_fields_are_masked(self):
        entry = audit_log(
            action='update', module=AUTH_MODULE,
            changes={
                'password': 'secret-hash',
                'phone': {'old': '138', 'new': '139'},
                'api_token': 'abc',
            },
            extra={'cookie': 'sid=1', 'count': 2},
        )
        self.assertEqual(entry.changes['password'], '***')
        self.assertEqual(entry.changes['api_token'], '***')
        self.assertEqual(entry.changes['phone']['new'], '139')
        self.assertEqual(entry.extra['cookie'], '***')
        self.assertEqual(entry.extra['count'], 2)

    def test_sanitize_helpers(self):
        self.assertEqual(sanitize_value({'ACCESS_KEY': 'x'}), {'ACCESS_KEY': '***'})
        self.assertEqual(sanitize_changes({'password': 'x', 'name': 'n'}), {'name': 'n'})
        changes = model_to_changes(None, self.user, ['phone', 'password'])
        self.assertIn('phone', changes)
        self.assertNotIn('password', changes)
        self.assertNotIn('last_login', changes)

    def test_serialize_value_keeps_json_structure_and_masks_nested_secrets(self):
        """JSONField 内的 password / token 键必须递归脱敏，不能先 json.dumps 逃逸。"""
        value = {
            'username': 'admin',
            'password': '123456',
            'nested': {'api_token': 'abcdef', 'level': 2},
            'items': [{'secret': 's3cr3t'}, 'plain'],
            'missing': None,
        }
        result = serialize_value(value)
        # 结构保留：字典仍是字典，列表仍是列表，数字/None 原样。
        self.assertIsInstance(result, dict)
        self.assertEqual(result['username'], 'admin')
        self.assertEqual(result['nested']['level'], 2)
        self.assertIsNone(result['missing'])
        self.assertIsInstance(result['items'], list)
        # 嵌套敏感键全部打码，明文不残留。
        self.assertEqual(result['password'], '***')
        self.assertEqual(result['nested']['api_token'], '***')
        self.assertEqual(result['items'][0]['secret'], '***')
        self.assertNotIn('123456', str(result))
        self.assertNotIn('abcdef', str(result))
        self.assertNotIn('s3cr3t', str(result))

    def test_audit_log_masks_nested_json_changes(self):
        """走统一入口写入的 changes 中，嵌套 JSON 敏感值不留明文。"""
        entry = audit_log(
            action='update', module='测试',
            changes={'profile': {
                'old': {'password': 'oldpass', 'city': 'bj'},
                'new': {'password': 'newpass', 'city': 'sh'},
            }},
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry.changes['profile']['old']['password'], '***')
        self.assertEqual(entry.changes['profile']['new']['password'], '***')
        self.assertNotIn('oldpass', str(entry.changes))
        self.assertNotIn('newpass', str(entry.changes))

    def test_event_type_defaults_to_business_without_warning(self):
        with patch('audit.services.logger.warning') as warn:
            default_entry = audit_log(action='x')
            none_entry = audit_log(action='x', event_type=None)
            blank_entry = audit_log(action='x', event_type='')
        warn.assert_not_called()
        for entry in (default_entry, none_entry, blank_entry):
            self.assertEqual(entry.event_type, AuditEventType.BUSINESS)
            self.assertNotIn('event_type', entry.extra)

    def test_illegal_event_type_warns_and_downgrades(self):
        with patch('audit.services.logger.warning') as warn:
            entry = audit_log(action='x', event_type='hacker', extra={'keep': 1})
        warn.assert_called_once()
        self.assertEqual(entry.event_type, AuditEventType.BUSINESS)
        self.assertEqual(entry.extra, {'keep': 1})
        self.assertNotIn('event_type', entry.extra)

    def test_event_type_check_constraint_rejects_unknown_value(self):
        # 绕过统一入口直接写非法值，必须由数据库 CHECK 拒绝。
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                AuditLog.objects.create(action='x', event_type='invalid')

    def test_non_finite_float_becomes_json_safe_string(self):
        result = sanitize_value({
            'nan': float('nan'),
            'inf': float('inf'),
            'ninf': float('-inf'),
            'normal': 1.5,
        })
        self.assertEqual(result['nan'], 'nan')
        self.assertEqual(result['inf'], 'inf')
        self.assertEqual(result['ninf'], '-inf')
        self.assertEqual(result['normal'], 1.5)
        # 字符串化后整棵结构可被 JSON 序列化，PG JSONB 不会拒绝。
        json.dumps(result)

    def test_admin_list_display_and_permissions(self):
        model_admin = AuditLogAdmin(AuditLog, admin.site)
        request = self._request()
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))
        self.assertIn('created_at', model_admin.list_display)
        self.assertIn('remote_addr', model_admin.list_display)
        self.assertIn('event_type', model_admin.list_display)
        self.assertIn('event_type', model_admin.list_filter)
        self.assertEqual(model_admin.ordering, ('-created_at',))


class AuditRealWorldRegressionTest(TestCase):
    """用真实 ORM/Admin 请求路径覆盖评审指出的隐藏 Bug，不再用 Mock 伪造场景。"""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='frank', password='pass12345', phone='13800000000',
            is_staff=True, is_superuser=True,
        )

    def test_audit_write_inside_transaction_does_not_break_outer_tx(self):
        """审计写入失败被 savepoint 隔离，外层事务仍可继续执行业务 SQL。"""
        with transaction.atomic():
            from unittest.mock import patch
            with patch('audit.services.AuditLog.objects.create', side_effect=RuntimeError('db down')):
                self.assertIsNone(audit_log(action='x', message='will fail'))
            # 外层事务未 broken：这条写入必须成功。
            get_user_model().objects.create(username='after-failure')
        self.assertTrue(get_user_model().objects.filter(username='after-failure').exists())

    def test_existing_pk_save_cannot_overwrite_audit(self):
        entry = audit_log(action='original', message='keep')
        forged = AuditLog(id=entry.id, action='tampered', message='hacked')
        # savepoint 隔离 IntegrityError，避免污染 TestCase 外层事务。
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                forged.save()
        self.assertEqual(AuditLog.objects.get(pk=entry.pk).action, 'original')

    def test_bulk_create_update_conflicts_blocked(self):
        with self.assertRaises(Exception):
            AuditLog.objects.bulk_create([AuditLog(action='x')], update_conflicts=True)

    def test_password_change_via_admin_view_is_audited(self):
        """真实走 /admin/core/user/<id>/password/ 改密，pre_save 必须留下 password_change。"""
        self.client.force_login(self.user)
        url = f'/admin/core/user/{self.user.pk}/password/'
        response = self.client.post(url, {'password1': 'NewPass12345', 'password2': 'NewPass12345'})
        self.assertEqual(response.status_code, 302)
        entry = AuditLog.objects.get(action=AuditAction.PASSWORD_CHANGE)
        self.assertEqual(entry.actor, self.user)
        self.assertEqual(entry.actor_name, 'frank')
        self.assertEqual(entry.object_id, str(self.user.pk))

    @override_settings(AUDIT_FALLBACK_ACTOR='')
    def test_password_change_via_set_password_is_audited(self):
        """无请求上下文的改密不能把目标用户错记成操作人。"""
        entry_user = get_user_model().objects.get(pk=self.user.pk)
        with patch('audit.signals.get_current_request', return_value=None), \
                patch('audit.services.get_current_request', return_value=None):
            entry_user.set_password('AnotherPass123')
            entry_user.save()
        entry = AuditLog.objects.get(action=AuditAction.PASSWORD_CHANGE)
        self.assertIsNone(entry.actor)
        self.assertEqual(entry.actor_name, '')
        self.assertEqual(entry.object_id, str(self.user.pk))

    def test_non_password_update_fields_do_not_query_or_audit(self):
        """登录等只更新 last_login 的保存不再多查 password，也不写改密日志。"""
        self.user.set_password('ShouldNotBeSaved123')
        self.user.save(update_fields=['last_login'])
        self.assertFalse(AuditLog.objects.filter(action='password_change').exists())

    def test_failed_password_update_leaves_no_audit(self):
        """pre_save 只打标；UPDATE 失败时 post_save 不触发，不能留下假日志。"""
        self.user.set_password('FailPass123')
        # savepoint 隔离模拟的 UPDATE 失败，避免污染 TestCase 外层事务。
        with transaction.atomic():
            with patch.object(get_user_model(), '_save_table', side_effect=RuntimeError('db fail')):
                with self.assertRaises(RuntimeError):
                    self.user.save()
        self.assertFalse(AuditLog.objects.filter(action='password_change').exists())

    def test_until_date_includes_whole_day(self):
        entry = audit_log(action='today', message='x')
        # _parse_time 用本地时区解释纯日期；UTC 日期在北京时间凌晨会差一天。
        today = timezone.localdate()
        # until=今天必须包含当天任意时刻的记录（旧实现按午夜截断会漏查）。
        self.assertIn(entry, list(query_audit_logs(until=today, limit=2000)))
        # 最常见的入参形态是字符串日期，同样要覆盖整天。
        self.assertIn(entry, list(query_audit_logs(until=today.isoformat(), limit=2000)))
        # since=明天才排除今天的记录。
        tomorrow = today + timedelta(days=1)
        self.assertNotIn(entry, list(query_audit_logs(since=tomorrow, limit=2000)))

    def test_m2m_changes_record_actual_pks(self):
        from django.contrib.auth.models import Group
        from audit.utils import m2m_changes
        group = Group.objects.create(name='ops')
        old_pks = set(self.user.groups.values_list('pk', flat=True))
        self.user.groups.add(group)
        delta = m2m_changes(old_pks, self.user, 'groups')
        self.assertIn(str(group.pk), delta.get('added', []))

    def test_save_model_does_not_emit_garbage_m2m_value(self):
        """M2M 字段不得被 model_to_changes 序列化成 'auth.Group.None'。"""
        admin_instance = AuditModelAdmin(get_user_model(), admin.site)
        request = RequestFactory().post(f'/admin/core/user/{self.user.pk}/change/')
        request.user = self.user
        form = Mock(changed_data=['groups', 'phone'])
        admin_instance.save_model(request, self.user, form, True)
        entry = AuditLog.objects.filter(action='update').first()
        self.assertNotIn('groups', entry.changes)
        self.assertNotIn('auth.Group', str(entry.changes))


class AuditAdminMixinTest(TestCase):

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='bob', password='pass12345', phone='13800000000',
            is_staff=True, is_superuser=True,
        )
        self.factory = RequestFactory()
        self.request = self.factory.post('/admin/core/user/1/change/')
        self.request.user = self.user
        self.request._real_ip = '10.0.0.1'
        self.request.audit_request_id = 'req-admin'
        from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
        self.model_admin = AuditModelAdmin(get_user_model(), admin.site)
        self.model_admin.audit_module = '用户管理'
        self.assertTrue(issubclass(BaseUserAdmin, admin.ModelAdmin))

    def test_save_model_records_changes_and_security_events(self):
        self.user.phone = '13900000000'
        self.user.set_password('NewPass12345')
        self.user.is_staff = False
        form = Mock(changed_data=['phone', 'password', 'is_staff'])
        self.model_admin.save_model(self.request, self.user, form, True)

        audit = AuditLog.objects.filter(action='update')
        self.assertTrue(audit.exists())
        self.assertIn('phone', audit.first().changes)
        self.assertNotIn('password', audit.first().changes)
        self.assertTrue(AuditLog.objects.filter(action='password_change').exists())
        self.assertTrue(AuditLog.objects.filter(action='permission_change').exists())

    def test_scalar_permission_changes_are_split_from_business(self):
        self.user.phone = '13900000000'
        self.user.is_staff = False
        form = Mock(changed_data=['phone', 'is_staff'])
        self.model_admin.save_model(self.request, self.user, form, True)

        update = AuditLog.objects.get(action=AuditAction.UPDATE)
        self.assertEqual(update.event_type, AuditEventType.BUSINESS)
        self.assertEqual(update.module, '用户管理')
        self.assertEqual(set(update.changes), {'phone'})

        permission = AuditLog.objects.get(action=AuditAction.PERMISSION_CHANGE)
        self.assertEqual(permission.event_type, AuditEventType.SECURITY)
        self.assertEqual(permission.module, '用户管理')
        self.assertEqual(set(permission.changes), {'is_staff'})
        self.assertEqual(permission.changes['is_staff']['old'], True)
        self.assertEqual(permission.changes['is_staff']['new'], False)
        self.assertEqual(permission.extra['fields'], ['is_staff'])

    def test_permission_only_change_has_no_business_update(self):
        self.user.is_staff = False
        form = Mock(changed_data=['is_staff'])
        self.model_admin.save_model(self.request, self.user, form, True)
        self.assertFalse(AuditLog.objects.filter(action=AuditAction.UPDATE).exists())
        self.assertTrue(AuditLog.objects.filter(
            action=AuditAction.PERMISSION_CHANGE,
            event_type=AuditEventType.SECURITY,
        ).exists())

    def test_mixed_m2m_splits_security_and_business(self):
        from django.contrib.auth.models import Group
        group = Group.objects.create(name='ops')

        class FakeField:
            def __init__(self, name):
                self.name = name

        fields = [FakeField('groups'), FakeField('tags')]
        form = Mock(changed_data=[], instance=self.user)

        def fake_m2m(old_pks, obj, name, exclude_fields):
            return {'added': [str(group.pk)]} if name in {'groups', 'tags'} else {}

        with patch.object(get_user_model()._meta, 'many_to_many', fields), \
                patch('audit.admin.m2m_changes', side_effect=fake_m2m):
            self.model_admin.save_related(self.request, form, [], True)

        permission = AuditLog.objects.get(action=AuditAction.PERMISSION_CHANGE)
        self.assertEqual(permission.event_type, AuditEventType.SECURITY)
        self.assertEqual(permission.module, '用户管理')
        self.assertIn('groups', permission.changes)
        self.assertNotIn('tags', permission.changes)
        self.assertEqual(permission.message, '权限关联被修改')

        business = AuditLog.objects.get(action=AuditAction.UPDATE)
        self.assertEqual(business.event_type, AuditEventType.BUSINESS)
        self.assertIn('tags', business.changes)
        self.assertNotIn('groups', business.changes)
        self.assertEqual(business.message, '多对多关联被修改')

    def test_create_with_m2m_records_added_groups(self):
        """Admin 新建时勾选 M2M 也要记 audit，不能只在 change=True 时记。"""
        from django.contrib.auth.models import Group
        group = Group.objects.create(name='ops')
        new_user = get_user_model()(username='m2m-create')
        form = Mock(changed_data=['groups'], instance=new_user)
        self.model_admin.save_model(self.request, new_user, form, False)
        new_user.groups.add(group)  # 模拟 form.save_m2m()
        self.model_admin.save_related(self.request, form, [], False)
        entry = AuditLog.objects.filter(action='permission_change').first()
        self.assertIsNotNone(entry)
        self.assertIn(str(group.pk), entry.changes['groups']['added'])

    def test_m2m_only_change_has_no_empty_update(self):
        """纯改 M2M 不应先写一条 changes={} 的 update。"""
        from django.contrib.auth.models import Group
        group = Group.objects.create(name='ops')
        form = Mock(changed_data=[], instance=self.user)
        self.model_admin.save_model(self.request, self.user, form, True)
        self.user.groups.add(group)
        self.model_admin.save_related(self.request, form, [], True)
        self.assertFalse(AuditLog.objects.filter(action='update', changes={}).exists())
        self.assertTrue(AuditLog.objects.filter(action='permission_change').exists())

    def test_create_and_delete_records(self):
        new_user = get_user_model()(username='carol')
        self.model_admin.save_model(self.request, new_user, Mock(changed_data=[]), False)
        self.assertTrue(AuditLog.objects.filter(action='create').exists())

        self.model_admin.delete_model(self.request, new_user)
        self.assertTrue(AuditLog.objects.filter(action='delete').exists())

    def test_delete_queryset_records_total_and_capped_snapshots(self):
        users = [
            get_user_model().objects.create(username=f'bulk-{i}')
            for i in range(55)
        ]
        queryset = get_user_model().objects.filter(pk__in=[u.pk for u in users])
        self.model_admin.delete_queryset(self.request, queryset)

        entry = AuditLog.objects.get(action=AuditAction.BULK_DELETE)
        self.assertEqual(entry.event_type, AuditEventType.BUSINESS)
        self.assertEqual(entry.module, '用户管理')
        self.assertEqual(entry.message, '批量删除 55 条')
        self.assertEqual(entry.extra['count'], 55)
        self.assertEqual(len(entry.extra['objects']), 50)
        self.assertFalse(get_user_model().objects.filter(pk__in=[u.pk for u in users]).exists())


class AuditMiddlewareTest(TestCase):

    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = AuditContextMiddleware(lambda r: HttpResponse())

    def test_request_id_from_header_and_cleanup(self):
        request = self.factory.get('/', HTTP_X_REQUEST_ID='abc-123')
        self.middleware.process_request(request)
        self.assertEqual(request.audit_request_id, 'abc-123')
        self.assertIs(get_current_request(), request)

        response = self.middleware.process_response(request, HttpResponse())
        self.assertEqual(response['X-Request-ID'], 'abc-123')
        self.assertIsNone(get_current_request())

    def test_bad_header_gets_generated_id(self):
        request = self.factory.get('/', HTTP_X_REQUEST_ID='bad id!<script>')
        self.middleware.process_request(request)
        self.assertNotEqual(request.audit_request_id, 'bad id!<script>')
        self.assertEqual(len(request.audit_request_id), 16)
        self.middleware.process_response(request, HttpResponse())


class AuditSignalTest(TestCase):

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='dave', password='pass12345'
        )
        self.request = RequestFactory().post('/admin/login/')
        self.request.audit_request_id = 'req-login'

    # 本项目 settings 用 AUDIT_CAPTURE_EVENTS='lockout'（登录/退出/失败由业务侧
    # core.log_audit 记，带中文动作码与失败原因），这里单独打开三个认证事件，
    # 验证信号实现本身。
    @override_settings(AUDIT_CAPTURE_EVENTS=('login', 'logout', 'login_failed'))
    def test_login_logout_failed_signals(self):
        user_logged_in.send(sender=self.__class__, request=self.request, user=self.user)
        login_log = AuditLog.objects.get(action=AuditAction.LOGIN)
        self.assertEqual(login_log.actor, self.user)
        self.assertEqual(login_log.actor_name, 'dave')
        self.assertEqual(login_log.event_type, AuditEventType.SECURITY)
        self.assertEqual(login_log.module, AUTH_MODULE)

        user_logged_out.send(sender=self.__class__, request=self.request, user=self.user)
        logout_log = AuditLog.objects.get(action=AuditAction.LOGOUT)
        self.assertEqual(logout_log.actor, self.user)
        self.assertEqual(logout_log.actor_name, 'dave')
        self.assertEqual(logout_log.event_type, AuditEventType.SECURITY)
        self.assertEqual(logout_log.module, AUTH_MODULE)

        user_login_failed.send(
            sender=self.__class__,
            credentials={'username': 'dave', 'password': 'must-not-be-logged'},
            request=self.request,
        )
        failed = AuditLog.objects.get(action=AuditAction.LOGIN_FAILED)
        self.assertEqual(failed.object_repr, 'dave')
        self.assertNotIn('must-not-be-logged', failed.message)
        self.assertNotIn('must-not-be-logged', str(failed.extra))

    @override_settings(AUDIT_CAPTURE_EVENTS=('logout', 'login_failed', 'lockout'))
    def test_capture_events_can_disable_login(self):
        user_logged_in.send(sender=self.__class__, request=self.request, user=self.user)
        self.assertFalse(AuditLog.objects.filter(action='login').exists())
        user_logged_out.send(sender=self.__class__, request=self.request, user=self.user)
        self.assertTrue(AuditLog.objects.filter(action='logout').exists())

    @override_settings(AUDIT_CAPTURE_EVENTS='lockout')
    def test_capture_events_accepts_comma_separated_string(self):
        user_logged_in.send(sender=self.__class__, request=self.request, user=self.user)
        self.assertFalse(AuditLog.objects.filter(action='login').exists())
        from axes.signals import user_locked_out
        user_locked_out.send(
            sender=self.__class__, request=self.request,
            username='dave', ip_address='203.0.113.7',
        )
        self.assertTrue(AuditLog.objects.filter(action='lockout').exists())

    @override_settings(AUDIT_CAPTURE_EVENTS='logout,login_failed')
    def test_capture_events_string_with_multiple_items(self):
        user_logged_in.send(sender=self.__class__, request=self.request, user=self.user)
        user_logged_out.send(sender=self.__class__, request=self.request, user=self.user)
        self.assertFalse(AuditLog.objects.filter(action='login').exists())
        self.assertTrue(AuditLog.objects.filter(action='logout').exists())

    def test_axes_lockout_signal(self):
        from axes.signals import user_locked_out

        user_locked_out.send(
            sender=self.__class__, request=self.request,
            username='dave', ip_address='203.0.113.7',
        )
        locked = AuditLog.objects.get(action=AuditAction.LOCKOUT)
        self.assertEqual(locked.remote_addr, '203.0.113.7')
        self.assertEqual(locked.event_type, AuditEventType.SECURITY)
        self.assertEqual(locked.module, AUTH_MODULE)
        self.assertNotIn('event_type', locked.extra)


class AuditQueryAndExportTest(TestCase):

    def setUp(self):
        self.user = get_user_model().objects.create_user(username='erin', password='pass12345')
        audit_log(
            action=AuditAction.LOGIN, event_type=AuditEventType.SECURITY,
            module=AUTH_MODULE, actor=self.user, message='登录成功',
        )
        audit_log(
            action=AuditAction.UPDATE, event_type=AuditEventType.BUSINESS,
            module='用户管理', actor='admin', message='=1+1 危险公式',
        )

    def test_query_filters(self):
        self.assertEqual(query_audit_logs(action=AuditAction.LOGIN).count(), 1)
        self.assertEqual(query_audit_logs(actor='admin').count(), 1)
        self.assertEqual(query_audit_logs(module='认证').count(), 1)
        self.assertEqual(query_audit_logs(q='危险').count(), 1)
        self.assertEqual(query_audit_logs(since='2999-01-01').count(), 0)
        self.assertEqual(len(query_audit_logs(limit=9999)), 2)

    def test_event_type_filters(self):
        self.assertEqual(query_audit_logs(event_type=AuditEventType.SECURITY).count(), 1)
        self.assertEqual(build_audit_queryset(event_type=AuditEventType.BUSINESS).count(), 1)
        self.assertEqual(build_audit_queryset(event_type=AuditEventType.SYSTEM).count(), 0)

    def test_invalid_time_argument_raises_instead_of_full_scan(self):
        with self.assertRaises(ValueError):
            query_audit_logs(since='not-a-date')
        with self.assertRaises(ValueError):
            build_audit_queryset(until='2026/13/99')
        # 未传或全空白仍表示不加该筛选条件。
        self.assertEqual(query_audit_logs(since='').count(), 2)
        self.assertEqual(build_audit_queryset(since=None).count(), 2)
        self.assertEqual(build_audit_queryset(until='   ').count(), 2)

    def test_csv_bom_and_formula_neutralized(self):
        response = export_audit_csv(query_audit_logs())
        raw = b''.join(response.streaming_content)
        content = raw.decode('utf-8-sig')
        self.assertIn('操作人', content)
        self.assertIn("'=1+1 危险公式", content)
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))

    def test_export_logs_itself_when_request_given(self):
        request = RequestFactory().post('/admin/audit/auditlog/')
        request.user = self.user
        request.audit_request_id = 'req-export'
        response = export_audit_csv(query_audit_logs(), request=request)
        content = b''.join(response.streaming_content).decode('utf-8-sig')
        entry = AuditLog.objects.get(action=AuditAction.EXPORT)
        self.assertEqual(entry.event_type, AuditEventType.SECURITY)
        self.assertEqual(entry.module, AUDIT_MODULE)
        # 自审计不能混进本次跨页全选导出的结果。
        self.assertNotIn(AuditAction.EXPORT, content)

    def test_csv_has_aligned_event_type_column(self):
        request = RequestFactory().post('/admin/audit/auditlog/')
        request.user = self.user
        request.audit_request_id = 'req-export-align'
        response = export_audit_csv(query_audit_logs(), request=request)
        content = b''.join(response.streaming_content).decode('utf-8-sig')
        rows = list(csv.reader(StringIO(content)))
        self.assertEqual(tuple(rows[0]), CSV_HEADER)
        self.assertEqual(len(CSV_HEADER), len(CSV_FIELDS))
        for row in rows[1:]:
            self.assertEqual(len(row), len(CSV_HEADER))
        type_idx = CSV_HEADER.index('事件分类')
        action_idx = CSV_HEADER.index('动作')
        self.assertIn(AuditEventType.SECURITY, [row[type_idx] for row in rows[1:]])
        self.assertNotIn(AuditAction.EXPORT, [row[action_idx] for row in rows[1:]])

    def test_csv_time_is_localized(self):
        from django.utils import timezone as tz
        entry = audit_log(action='localized', message='x')
        response = export_audit_csv(AuditLog.objects.filter(pk=entry.pk))
        content = b''.join(response.streaming_content).decode('utf-8-sig')
        local = tz.localtime(entry.created_at).strftime('%Y-%m-%d %H:%M:%S')
        self.assertIn(local, content)


class AuditAppendOnlyTest(TestCase):

    def setUp(self):
        self.entry = audit_log(action='x', message='original')

    def test_queryset_mutations_blocked(self):
        with self.assertRaises(AuditLogImmutable):
            AuditLog.objects.update(action='y')
        with self.assertRaises(AuditLogImmutable):
            AuditLog.objects.bulk_update([self.entry], ['action'])
        with self.assertRaises(AuditLogImmutable):
            AuditLog.objects.all().delete()

    def test_instance_mutations_blocked(self):
        with self.assertRaises(AuditLogImmutable):
            self.entry.save()
        with self.assertRaises(AuditLogImmutable):
            self.entry.delete()
        self.assertEqual(AuditLog.objects.get(pk=self.entry.pk).message, 'original')


class AuditPurgeCommandTest(TestCase):

    def test_dry_run_does_not_delete(self):
        audit_log(action='old', message='x')
        out = StringIO()
        call_command('audit_purge', days=0, dry_run=True, stdout=out)
        self.assertIn('[dry-run]', out.getvalue())
        self.assertEqual(AuditLog.objects.count(), 1)

    def test_negative_days_rejected_even_in_dry_run(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('audit_purge', days=-1, dry_run=True)

    def test_purge_deletes_and_leaves_marker(self):
        audit_log(action='old', message='x')
        out = StringIO()
        call_command('audit_purge', days=0, stdout=out)
        self.assertFalse(AuditLog.objects.filter(action='old').exists())
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.EXPIRE).exists())


class CleanOldAuditLogsTest(TestCase):

    def test_cleanup_respects_days(self):
        AuditLog.objects.create(
            action='old', created_at=timezone.now() - timedelta(days=200)
        )
        AuditLog.objects.create(
            action='recent', created_at=timezone.now() - timedelta(days=10)
        )
        deleted = clean_old_audit_logs(days=180)
        self.assertEqual(deleted, 1)
        self.assertTrue(AuditLog.objects.filter(action='recent').exists())
        self.assertFalse(AuditLog.objects.filter(action='old').exists())
        marker = AuditLog.objects.get(action=AuditAction.EXPIRE)
        self.assertEqual(marker.event_type, AuditEventType.SYSTEM)
        self.assertEqual(marker.module, AUDIT_MODULE)
        self.assertEqual(marker.extra['deleted'], 1)

    def test_business_expire_records_are_cleaned_but_audit_marker_kept(self):
        old = timezone.now() - timedelta(days=2)
        AuditLog.objects.create(
            action=AuditAction.EXPIRE, event_type=AuditEventType.BUSINESS,
            module='令牌', created_at=old,
        )
        AuditLog.objects.create(
            action=AuditAction.EXPIRE, event_type=AuditEventType.SYSTEM,
            module=AUDIT_MODULE, created_at=old, message='旧清理留痕',
        )
        deleted = AuditLog.objects.expire_audit_logs(days=1)
        self.assertEqual(deleted, 1)
        self.assertFalse(AuditLog.objects.filter(module='令牌', action=AuditAction.EXPIRE).exists())
        self.assertTrue(AuditLog.objects.filter(message='旧清理留痕').exists())

    @override_settings(AUDIT_RETENTION_DAYS=30)
    def test_default_days_from_settings(self):
        AuditLog.objects.create(
            action='old', created_at=timezone.now() - timedelta(days=60)
        )
        self.assertEqual(clean_old_audit_logs(), 1)

    def test_rollback_when_audit_marker_fails(self):
        """留痕写入失败时删除必须回滚，不留下「日志已删但无清理记录」。"""
        AuditLog.objects.create(
            action='old', created_at=timezone.now() - timedelta(days=200)
        )
        self.assertTrue(AuditLog.objects.filter(action='old').exists())
        with patch('audit.services.audit_log', return_value=None):
            with self.assertRaises(RuntimeError):
                AuditLog.objects.expire_audit_logs(days=180)
        # 事务回滚后旧记录仍在。
        self.assertTrue(AuditLog.objects.filter(action='old').exists())
