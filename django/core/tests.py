import contextlib
import importlib.util
import io
import json
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
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from common.email.password import generate_secure_password, send_temp_password_email
from config.urls import health_check, media_download


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


class HealthCheckTest(TestCase):
    """健康检查必须执行真实查询；缓存连接不能冒充 DB 可用。"""

    def test_healthy_when_query_ok(self):
        request = RequestFactory().get('/health/')
        response = health_check(request)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"database": "ok"', response.content)

    def test_unhealthy_when_db_unavailable(self):
        request = RequestFactory().get('/health/')
        # 没有真实 DB 故障也能验证：cursor() 抛错即判定 unavailable。
        with patch('config.urls.connection') as mock_conn:
            mock_conn.cursor.side_effect = Exception('db down')
            response = health_check(request)
        self.assertEqual(response.status_code, 503)
        self.assertIn(b'"database": "unavailable"', response.content)


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


class ContractCheckTest(SimpleTestCase):
    """接入契约 v2 检查器：纯 AST 判定，不 import settings、不依赖数据库。"""

    SETTINGS = """\
SECRET_KEY = env('SECRET_KEY', default='')
LOGIN_URL = '/admin/login/'
AUTH_USER_MODEL = 'core.User'
INSTALLED_APPS = [
    'simpleui',
    'django.contrib.admin',
    'django.contrib.auth',
    'axes',
    'auditlog',
    'jobs',
    'import_export',
]
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'common.middleware.real_ip.RealIPMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'auditlog.middleware.AuditlogMiddleware',
    'axes.middleware.AxesMiddleware',
]
AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',
    'django.contrib.auth.backends.ModelBackend',
]
STANDALONE = True
"""

    WSGI = """\
import os
from django.core.wsgi import get_wsgi_application
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
application = get_wsgi_application()
"""

    URLS = """\
from django.contrib.admin.views.decorators import staff_member_required
from django.db import connection

def health_check(request):
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')

@staff_member_required
def media_download(request, path):
    return None
"""

    MODELS = """\
from django.db import models
from django.contrib.auth.models import AbstractUser

class User(AbstractUser):
    pass
"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        path = (
            Path(__file__).resolve().parent.parent.parent
            / 'scripts' / 'contract_check.py'
        )
        spec = importlib.util.spec_from_file_location('contract_check_under_test', path)
        cls.checker = importlib.util.module_from_spec(spec)
        # dataclass 在 3.13 会按 __module__ 回查 sys.modules，必须先注册
        sys.modules[spec.name] = cls.checker
        spec.loader.exec_module(cls.checker)

    @property
    def repo_root(self):
        return Path(__file__).resolve().parent.parent.parent

    def make_project(self, settings_text=None, tier=2, notes='', boundaries=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for folder in ('django/config', 'django/core', 'django/common', 'django/jobs/migrations'):
            (root / folder).mkdir(parents=True, exist_ok=True)
        (root / 'django' / 'manage.py').write_text('# manage\n', encoding='utf-8')
        (root / 'django' / 'config' / 'settings.py').write_text(
            settings_text if settings_text is not None else self.SETTINGS,
            encoding='utf-8',
        )
        (root / 'django' / 'config' / 'wsgi.py').write_text(self.WSGI, encoding='utf-8')
        (root / 'django' / 'config' / 'urls.py').write_text(self.URLS, encoding='utf-8')
        (root / 'django' / 'core' / 'models.py').write_text(self.MODELS, encoding='utf-8')
        (root / 'django' / 'jobs' / 'migrations' / '0001_initial.py').write_text(
            '# initial\n', encoding='utf-8'
        )
        marker = {
            'contract': 'v2', 'tier': tier, 'project': 'fixture',
            'role': 'test', 'notes': notes,
            'external_boundaries': boundaries or [],
        }
        (root / '.contract.json').write_text(json.dumps(marker), encoding='utf-8')
        return root

    def write_marker(self, root, **updates):
        marker = json.loads((root / '.contract.json').read_text(encoding='utf-8'))
        marker.update(updates)
        (root / '.contract.json').write_text(
            json.dumps(marker), encoding='utf-8'
        )

    def codes_at(self, report, level):
        return {f.code for f in report.of(level)}

    def test_reference_project_is_fully_compliant(self):
        report = self.checker.check_project(self.repo_root)
        self.assertEqual(report.status, 'COMPLIANT', self.checker.render_text(report))
        self.assertEqual(report.fails, [])
        self.assertEqual(report.warns, [])

    def _tier3_project(self, **kwargs):
        """tier 3 夹具：先放一个合法的 system/ 边界，避免边界项本身产生 WARN。"""
        root = self.make_project(tier=3, **kwargs)
        system_dir = root / 'system'
        system_dir.mkdir()
        (system_dir / 'hook.sh').write_text('#!/bin/sh\n', encoding='utf-8')
        self.write_marker(root, external_boundaries=[{
            'path': 'system/hook.sh', 'privilege': 'root',
            'reason': 'sudo 特权', 'interface': 'stdin/stdout',
        }])
        return root

    def test_tier1_stays_hard_in_tier3_project(self):
        root = self._tier3_project(
            settings_text=self.SETTINGS.replace("    'axes',\n", ''),
            notes='承载模式：特权脚本放在 system/',
        )
        report = self.checker.check_project(root)
        self.assertIn('axes_app', self.codes_at(report, 'FAIL'))
        self.assertNotIn('external_boundaries', self.codes_at(report, 'WARN'))

    def test_auth_user_model_required(self):
        root = self.make_project(
            settings_text=self.SETTINGS.replace("AUTH_USER_MODEL = 'core.User'\n", '')
        )
        self.assertIn(
            'auth_user_model',
            self.codes_at(self.checker.check_project(root), 'FAIL'),
        )

    def test_middleware_order_enforced(self):
        # Axes 不在最后 → middleware_order FAIL
        broken = self.SETTINGS.replace(
            "    'auditlog.middleware.AuditlogMiddleware',\n"
            "    'axes.middleware.AxesMiddleware',\n",
            "    'axes.middleware.AxesMiddleware',\n"
            "    'auditlog.middleware.AuditlogMiddleware',\n",
        )
        root = self.make_project(settings_text=broken)
        self.assertIn(
            'middleware_order',
            self.codes_at(self.checker.check_project(root), 'FAIL'),
        )

    def test_csrf_bypass_is_rejected(self):
        root = self.make_project()
        (root / 'django' / 'core' / 'evil.py').write_text(
            "class V:\n"
            "    def __init__(self, request):\n"
            "        request._dont_enforce_csrf_checks = True\n",
            encoding='utf-8',
        )
        self.assertIn(
            'no_csrf_bypass',
            self.codes_at(self.checker.check_project(root), 'FAIL'),
        )

    def test_csrf_exempt_warns(self):
        root = self.make_project()
        (root / 'django' / 'core' / 'views.py').write_text(
            "from django.views.decorators.csrf import csrf_exempt\n"
            "@csrf_exempt\ndef view(request):\n    pass\n",
            encoding='utf-8',
        )
        self.assertIn(
            'csrf_exempt_usage',
            self.codes_at(self.checker.check_project(root), 'WARN'),
        )

    def test_raw_sql_is_fail_and_test_strings_not_misreported(self):
        root = self.make_project()
        # 测试文件里的字符串常量不应被当成真实调用
        (root / 'django' / 'core' / 'tests.py').write_text(
            "SNIPPET = \"sqlite3.connect('x.db'); cursor.execute('DROP TABLE')\"\n",
            encoding='utf-8',
        )
        self.assertNotIn(
            'no_raw_sql_in_django',
            self.codes_at(self.checker.check_project(root), 'FAIL'),
        )
        (root / 'django' / 'core' / 'adapter.py').write_text(
            "import sqlite3\nsqlite3.connect('legacy.db')\n", encoding='utf-8'
        )
        self.assertIn(
            'no_raw_sql_in_django',
            self.codes_at(self.checker.check_project(root), 'FAIL'),
        )

    def test_pragma_and_health_select_are_exempt(self):
        root = self.make_project()
        (root / 'django' / 'common' / 'apps.py').write_text(
            "with connection.cursor() as cursor:\n"
            "    cursor.execute('PRAGMA journal_mode=WAL')\n",
            encoding='utf-8',
        )
        self.assertNotIn(
            'no_raw_sql_in_django',
            self.codes_at(self.checker.check_project(root), 'FAIL'),
        )

    def test_business_execute_is_not_raw_sql(self):
        root = self.make_project()
        (root / 'django' / 'core' / 'runner.py').write_text(
            "class Runner:\n"
            "    def execute(self, command):\n"
            "        return command\n"
            "\n"
            "runner = Runner()\n"
            "runner.execute('install')\n",
            encoding='utf-8',
        )
        self.assertNotIn(
            'no_raw_sql_in_django',
            self.codes_at(self.checker.check_project(root), 'FAIL'),
        )
        (root / 'django' / 'core' / 'adapter.py').write_text(
            "with connection.cursor() as cursor:\n"
            "    cursor.execute('SELECT * FROM users')\n",
            encoding='utf-8',
        )
        self.assertIn(
            'no_raw_sql_in_django',
            self.codes_at(self.checker.check_project(root), 'FAIL'),
        )

    def test_bulk_db_methods_and_strict_exempt_files(self):
        root = self.make_project()
        (root / 'django' / 'core' / 'bulk.py').write_text(
            "cursor.executemany('INSERT INTO t VALUES (?)', rows)\n",
            encoding='utf-8',
        )
        self.assertIn(
            'no_raw_sql_in_django',
            self.codes_at(self.checker.check_project(root), 'FAIL'),
        )
        # PRAGMA 只在 django/common/apps.py 豁免，换文件即 FAIL
        other = self.make_project()
        (other / 'django' / 'core' / 'db.py').write_text(
            "cursor.execute('PRAGMA journal_mode=WAL')\n", encoding='utf-8'
        )
        self.assertIn(
            'no_raw_sql_in_django',
            self.codes_at(self.checker.check_project(other), 'FAIL'),
        )

    def test_flat_layout_is_not_accepted(self):
        """平铺布局不再兼容：项目根有 manage.py 也不替代 django/ 结构。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / 'manage.py').write_text('# flat\n', encoding='utf-8')
        (root / 'config').mkdir()
        (root / 'core').mkdir()
        (root / 'common').mkdir()
        (root / '.contract.json').write_text(json.dumps({
            'contract': 'v2', 'tier': 2, 'project': 'flat', 'role': 'project',
            'notes': '', 'external_boundaries': [],
        }), encoding='utf-8')
        codes = self.codes_at(self.checker.check_project(root), 'FAIL')
        self.assertIn('layout_django_dir', codes)
        self.assertIn('settings_parse', codes)

    def test_external_boundary_must_be_a_real_relative_file(self):
        root = self._tier3_project()
        # 只登记目录名 → WARN
        (root / 'system').mkdir(exist_ok=True)
        self.write_marker(root, external_boundaries=[{
            'path': 'system', 'privilege': 'root',
            'reason': 'sudo 特权', 'interface': 'stdin',
        }])
        self.assertIn(
            'external_boundaries',
            self.codes_at(self.checker.check_project(root), 'WARN'),
        )
        # 包含 .. → WARN
        self.write_marker(root, external_boundaries=[{
            'path': 'system/../django/manage.py', 'privilege': 'root',
            'reason': 'sudo 特权', 'interface': 'stdin',
        }])
        self.assertIn(
            'external_boundaries',
            self.codes_at(self.checker.check_project(root), 'WARN'),
        )
        # POSIX 绝对路径在 Windows 上 is_absolute() 为 False，仍需拦截
        self.write_marker(root, external_boundaries=[{
            'path': '/etc/passwd', 'privilege': 'root',
            'reason': 'sudo 特权', 'interface': 'stdin',
        }])
        self.assertIn(
            'external_boundaries',
            self.codes_at(self.checker.check_project(root), 'WARN'),
        )

    def test_virtualenv_is_not_scanned(self):
        root = self.make_project()
        venv = root / 'django' / '.venv-check' / 'Lib' / 'site-packages' / 'django'
        venv.mkdir(parents=True)
        (venv / 'base.py').write_text(
            "import sqlite3\nsqlite3.connect('third-party.db')\n", encoding='utf-8'
        )
        self.assertEqual(self.checker.check_project(root).warns, [])
        self.assertEqual(self.checker.check_project(root).fails, [])

    def test_external_boundaries_validation(self):
        # 登记在 django/ 内 + 缺字段 → WARN
        bad = [{
            'path': 'django/core/views.py', 'privilege': 'root',
            'reason': '', 'interface': 'stdin',
        }]
        root = self.make_project(tier=3, notes='承载模式', boundaries=bad)
        self.assertIn(
            'external_boundaries',
            self.codes_at(self.checker.check_project(root), 'WARN'),
        )
        # 合法登记（system/ 下真实存在的文件）→ 无 WARN
        system_dir = root / 'system'
        system_dir.mkdir()
        script = system_dir / 'auth_hook.sh'
        script.write_text('#!/bin/sh\n', encoding='utf-8')
        good = [{
            'path': 'system/auth_hook.sh', 'privilege': 'root',
            'reason': 'sudo 特权', 'interface': 'stdin/stdout',
        }]
        self.write_marker(root, external_boundaries=good)
        self.assertNotIn(
            'external_boundaries',
            self.codes_at(self.checker.check_project(root), 'WARN'),
        )

    def test_tier3_requires_notes(self):
        root = self._tier3_project()
        self.assertIn(
            'external_boundaries',
            self.codes_at(self.checker.check_project(root), 'WARN'),
        )
        self.write_marker(root, notes='业务数据由自建库管理，审计走双轨')
        self.assertNotIn(
            'external_boundaries',
            self.codes_at(self.checker.check_project(root), 'WARN'),
        )

    def test_secret_key_literal_is_rejected(self):
        root = self.make_project(
            settings_text=self.SETTINGS.replace(
                "SECRET_KEY = env('SECRET_KEY', default='')",
                "SECRET_KEY = 'django-insecure-fixed'",
            )
        )
        self.assertIn('secret_key_literal', self.codes_at(self.checker.check_project(root), 'FAIL'))

    def test_contract_version_mismatch_is_rejected(self):
        root = self.make_project()
        self.write_marker(root, contract='v1')
        self.assertIn('contract_marker', self.codes_at(self.checker.check_project(root), 'FAIL'))

    def test_json_report_shape(self):
        report = self.checker.check_project(self.make_project())
        data = self.checker.build_json(report)
        self.assertEqual(data['contract'], 'v2')
        self.assertEqual(data['summary']['FAIL'], 0)
        self.assertIsInstance(data['findings'], list)

    def test_strict_mode_fails_on_warning(self):
        root = self.make_project()
        (root / 'django' / 'core' / 'views.py').write_text(
            "from django.views.decorators.csrf import csrf_exempt\n@csrf_exempt\ndef v(): pass\n",
            encoding='utf-8',
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.checker.main([str(root)]), 0)
            self.assertEqual(self.checker.main([str(root), '--strict']), 1)


class RealIPMiddlewareTest(SimpleTestCase):
    """裸机 + 本机反代：只信来自回环地址的转发头。"""

    def _request(self, remote_addr, forwarded=None):
        request = RequestFactory().get('/')
        request.META['REMOTE_ADDR'] = remote_addr
        if forwarded:
            request.META['HTTP_X_FORWARDED_FOR'] = forwarded
        return request

    def test_local_proxy_header_is_honoured(self):
        from common.middleware.real_ip import RealIPMiddleware

        with override_settings(STANDALONE=True, TRUST_LOCAL_PROXY=True):
            # 反代用 $proxy_add_x_forwarded_for：真实客户端 IP 在最右侧
            request = self._request('127.0.0.1', '203.0.113.7, 10.0.0.1')
            RealIPMiddleware(lambda r: None).process_request(request)
            self.assertEqual(request.META['REMOTE_ADDR'], '10.0.0.1')
            self.assertNotIn('HTTP_X_FORWARDED_FOR', request.META)

    def test_spoofed_left_ip_is_ignored(self):
        from common.middleware.real_ip import RealIPMiddleware

        with override_settings(STANDALONE=True, TRUST_LOCAL_PROXY=True):
            # 左侧是客户端伪造值，取反代追加的最右侧真实 IP
            request = self._request('127.0.0.1', '198.51.100.77, 203.0.113.7')
            RealIPMiddleware(lambda r: None).process_request(request)
            self.assertEqual(request.META['REMOTE_ADDR'], '203.0.113.7')

    def test_direct_client_header_is_stripped(self):
        from common.middleware.real_ip import RealIPMiddleware

        with override_settings(STANDALONE=True, TRUST_LOCAL_PROXY=True):
            request = self._request('198.51.100.9', '203.0.113.7')
            request.META['HTTP_X_FORWARDED_PROTO'] = 'https'
            RealIPMiddleware(lambda r: None).process_request(request)
            self.assertEqual(request.META['REMOTE_ADDR'], '198.51.100.9')
            # 非回环直连时伪造的 https 标记必须剥离
            self.assertNotIn('HTTP_X_FORWARDED_PROTO', request.META)

    def test_local_proxy_proto_header_is_kept(self):
        from common.middleware.real_ip import RealIPMiddleware

        with override_settings(STANDALONE=True, TRUST_LOCAL_PROXY=True):
            request = self._request('127.0.0.1', '203.0.113.7')
            request.META['HTTP_X_FORWARDED_PROTO'] = 'https'
            RealIPMiddleware(lambda r: None).process_request(request)
            # 回环本机反代的 proto 头要留给 SECURE_PROXY_SSL_HEADER
            self.assertEqual(request.META['HTTP_X_FORWARDED_PROTO'], 'https')

    def test_no_proxy_setting_strips_headers(self):
        from common.middleware.real_ip import RealIPMiddleware

        with override_settings(STANDALONE=True, TRUST_LOCAL_PROXY=False):
            request = self._request('127.0.0.1', '203.0.113.7')
            RealIPMiddleware(lambda r: None).process_request(request)
            self.assertEqual(request.META['REMOTE_ADDR'], '127.0.0.1')

    def test_malformed_forwarded_for_falls_back_to_remote_addr(self):
        from common.middleware.real_ip import RealIPMiddleware

        with override_settings(STANDALONE=True, TRUST_LOCAL_PROXY=True):
            request = self._request('127.0.0.1', 'not-an-ip, 203.0.113.7')
            RealIPMiddleware(lambda r: None).process_request(request)
            self.assertEqual(request.META['REMOTE_ADDR'], '203.0.113.7')

            # 最右侧非法时继续往左找合法值；全非法才回退 REMOTE_ADDR
            request = self._request('127.0.0.1', 'not-an-ip')
            RealIPMiddleware(lambda r: None).process_request(request)
            self.assertEqual(request.META['REMOTE_ADDR'], '127.0.0.1')

            request = self._request('127.0.0.1', '203.0.113.7, not-an-ip')
            RealIPMiddleware(lambda r: None).process_request(request)
            self.assertEqual(request.META['REMOTE_ADDR'], '203.0.113.7')


class AxesLockoutResponseTest(SimpleTestCase):
    """锁定响应：API 路径/JSON 请求返回 JSON，admin 表单返回 HTML。"""

    @override_settings(AXES_COOLOFF_MINUTES=60)
    def test_api_returns_json(self):
        from common.utils.axes import axes_lockout_response

        request = RequestFactory().post('/api/login/', HTTP_ACCEPT='application/json')
        response = axes_lockout_response(request)
        self.assertEqual(response.status_code, 403)
        self.assertIn(b'"ok": false', response.content)
        self.assertIn(b'60', response.content)

    def test_admin_returns_html(self):
        from common.utils.axes import axes_lockout_response

        request = RequestFactory().post('/admin/login/')
        response = axes_lockout_response(request)
        self.assertEqual(response.status_code, 403)
        self.assertIn(b'\xe7\x99\xbb\xe5\xbd\x95', response.content)  # 登录
