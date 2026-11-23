import os
import subprocess
import sys
import tempfile
import warnings
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import Http404
from django.test import RequestFactory, TestCase, override_settings

from common.email.password import generate_secure_password, send_temp_password_email
from config.urls import media_download


class EmailTest(TestCase):
    """模板测试：不发送真实邮件，也不使用任何个人邮箱。"""

    def test_send_password_email(self):
        password = generate_secure_password()

        with patch(
            'common.email.password.send_password_email_task.delay',
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


class AuthRedirectTest(TestCase):
    """模板只有 admin，登录相关跳转不能落到 Django 默认的 accounts 路由。"""

    def test_admin_redirects(self):
        self.assertEqual(settings.LOGIN_URL, '/admin/login/')
        self.assertEqual(settings.LOGIN_REDIRECT_URL, '/admin/')
        self.assertEqual(settings.LOGOUT_REDIRECT_URL, '/admin/login/')


class RuntimeConfigTest(TestCase):
    """settings 运行时目录与 Axes 参数：import 阶段不崩、路径可重定向、参数可配。"""

    def test_ensure_dir_only_warns_when_unwritable(self):
        from config.settings import _ensure_dir

        blocker = tempfile.NamedTemporaryFile()
        self.addCleanup(blocker.close)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            _ensure_dir(Path(blocker.name) / 'child', 'TEST_DIR')
        self.assertTrue(any(issubclass(w.category, RuntimeWarning) for w in caught))

    def test_runtime_dir_precedence(self):
        from config import settings as settings_module

        # RUNTIME_DIR 是 import 期常量，用 patch.object 模拟；独立 *DIR 走环境变量。
        with patch.object(settings_module, 'RUNTIME_DIR', '/tmp/rt'):
            with patch.dict(os.environ):
                os.environ.pop('DATA_DIR', None)
                self.assertEqual(
                    settings_module._runtime_dir('DATA_DIR', 'data'),
                    Path('/tmp/rt/data'),
                )
                with patch.dict(os.environ, {'DATA_DIR': '/custom/data'}):
                    self.assertEqual(
                        settings_module._runtime_dir('DATA_DIR', 'data'),
                        Path('/custom/data'),
                    )

    def test_axes_cooldown_from_env(self):
        script = (
            'from django.conf import settings; '
            'print(settings.AXES_FAILURE_LIMIT, int(settings.AXES_COOLOFF_TIME.total_seconds()/60))'
        )
        env = {
            **os.environ,
            'DJANGO_SETTINGS_MODULE': 'config.settings',
            'SECRET_KEY': 'test',
            'DEBUG': 'false',
            'STANDALONE': 'True',
            'USE_HTTPS': 'false',
            'EMAIL_ENABLED': 'false',
            'AXES_FAILURE_LIMIT': '3',
            'AXES_COOLOFF_MINUTES': '10',
        }
        result = subprocess.run(
            [sys.executable, '-c', script],
            cwd=str(Path(__file__).resolve().parent.parent),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '3 10')


class MediaDownloadTest(TestCase):
    """上传文件下载视图：必须登录且为 staff，并拦截目录穿越。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.media_root = Path(self.tmp.name) / 'media'
        self.media_root.mkdir()
        (self.media_root / 'hello.txt').write_text('secret', encoding='utf-8')
        # 放在 media 目录外，用于验证 ../ 穿越被拦截
        Path(self.tmp.name, 'outside.txt').write_text('outside', encoding='utf-8')

        User = get_user_model()
        self.normal_user = User.objects.create_user(username='normal', password='pass12345')
        self.staff_user = User.objects.create_user(
            username='staff', password='pass12345', is_staff=True
        )

    def _get(self, path):
        with override_settings(MEDIA_ROOT=self.media_root):
            return self.client.get(path)

    def test_anonymous_is_redirected(self):
        response = self._get('/media/hello.txt')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response['Location'])

    def test_non_staff_is_redirected(self):
        self.client.force_login(self.normal_user)
        response = self._get('/media/hello.txt')
        self.assertEqual(response.status_code, 302)

    def test_staff_can_download(self):
        self.client.force_login(self.staff_user)
        response = self._get('/media/hello.txt')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), b'secret')

    def test_path_traversal_is_blocked(self):
        request = RequestFactory().get('/media/../outside.txt')
        request.user = self.staff_user
        with override_settings(MEDIA_ROOT=self.media_root):
            with self.assertRaises(Http404):
                media_download(request, '../outside.txt')
