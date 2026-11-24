# accounts/tests.py
"""用户模型与 app 配置的单元测试。

公共基础设施（邮件、真实 IP、Axes 响应、健康检查、媒体下载、契约检查）的测试
在 `common/tests.py`；这里只覆盖 `accounts` 自己的模型契约。
"""
from django.conf import settings
from django.contrib import admin as django_admin
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from unittest.mock import Mock, patch

from .apps import AccountsConfig
from .models import User
from .services import generate_secure_password, send_temp_password_email


class AccountsConfigTest(SimpleTestCase):
    def test_app_config_is_explicit(self):
        """INSTALLED_APPS 用完整 AppConfig 路径，app_label 必须是 accounts。"""
        self.assertEqual(AccountsConfig.name, 'accounts')
        self.assertIn('accounts.apps.AccountsConfig', settings.INSTALLED_APPS)

    def test_auth_user_model(self):
        self.assertEqual(settings.AUTH_USER_MODEL, 'accounts.User')

    def test_user_model_is_registered_in_admin(self):
        self.assertIn(User, django_admin.site._registry)


class UserModelTest(TestCase):
    def test_table_name_stays_core_user(self):
        """app_label 从 core 改成 accounts 后，表名保持不变（历史库免迁移）。"""
        self.assertEqual(User._meta.db_table, 'core_user')

    def test_phone_validator_accepts_mobile(self):
        user = User(username='alice', phone='13800138000')
        user.full_clean(exclude=['password'])

    def test_phone_validator_rejects_invalid(self):
        user = User(username='bob', phone='12345')
        with self.assertRaises(ValidationError) as ctx:
            user.full_clean(exclude=['password'])
        self.assertIn('phone', ctx.exception.message_dict)

    def test_phone_is_optional(self):
        user = User(username='carol', phone=None)
        user.full_clean(exclude=['password'])

    def test_display_name_prefers_full_name_then_username(self):
        user = User(username='dave', first_name='Dave', last_name='Wang')
        self.assertEqual(user.display_name, 'Dave Wang')
        user.first_name = user.last_name = ''
        self.assertEqual(user.display_name, 'dave')

    def test_str_falls_back_to_username(self):
        self.assertEqual(str(User(username='erin')), 'erin')


class PasswordMailTest(TestCase):
    """账户域：随机口令与临时密码邮件投递（不发真实邮件、不用真实邮箱）。"""

    def test_generate_secure_password_meets_policy(self):
        pwd = generate_secure_password()
        self.assertEqual(len(pwd), 10)
        self.assertTrue(any(c.isupper() for c in pwd))
        self.assertTrue(any(c.islower() for c in pwd))
        self.assertTrue(any(c.isdigit() for c in pwd))

    def test_send_password_email_enqueues_task(self):
        password = generate_secure_password()
        with patch(
            'accounts.services.send_password_email_task.delay',
            return_value=Mock(id='task-test-001'),
        ) as delay_mock:
            task_id = send_temp_password_email(
                user_email='test@example.com',
                password=password,
                username='测试用户',
            )
        self.assertEqual(task_id, 'task-test-001')
        delay_mock.assert_called_once_with(
            user_email='test@example.com',
            password=password,
            username='测试用户',
        )

    def test_send_password_email_without_address_is_noop(self):
        self.assertIsNone(send_temp_password_email('', 'x', 'y'))

