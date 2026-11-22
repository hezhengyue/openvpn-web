#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest 的 Flask -> Django 兼容层。

src/selftest.py 原本 `import app as web`，用 Flask 的 app.test_client() 跑 HTTP 用例。
这里提供同名接口（web.app.test_client() / web.bootstrap_admin() / web._PUBIP /
web.WRITE_LOCK），底层换成 Django 的 django.test.Client + Django Auth，
让 1542 行、424 项业务断言几乎不用改就能验证新底座。
"""
import os
import sys
import json as _json
import io

# selftest.py 在 import 本模块前已设好 OVPN_WEB_ROOT / OVPN_WEB_MOCK / OVPN_WEB_PORT
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = _HERE
if not os.path.isdir(os.path.join(_ROOT, "config")):
    _ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ["STANDALONE"] = "True"
os.environ.setdefault("USE_HTTPS", "false")
if not os.environ.get("SECRET_KEY"):
    os.environ["SECRET_KEY"] = "selftest-only-secret-key-not-for-production-0123456789"
os.environ["DEBUG"] = "true"
os.environ["HOSTS"] = "testserver,localhost,127.0.0.1"
# Django 的库也放进 selftest 的临时目录，与 core 用同一个 TMP，做到每次运行全隔离
os.environ["DATA_DIR"] = os.path.join(os.environ["OVPN_WEB_ROOT"], "data")

import django  # noqa: E402
django.setup()

# 临时库是全新的，先建表（selftest 每次用独立 TMP）
from django.core.management import call_command  # noqa: E402
call_command("migrate", "--noinput", verbosity=0)

from django.conf import settings as _settings  # noqa: E402
# selftest 会用同一 IP 反复试错误口令做业务断言，放宽 Axes 阈值避免误锁
# （防爆破本身由集成测试单独验证；这里只验证业务流程）
_settings.AXES_FAILURE_LIMIT = 999
from django.contrib.auth import get_user_model  # noqa: E402
from ovpn import bridge  # noqa: E402

core = bridge.core


# --------------------------------------------------------------------------- 响应包装
class _Resp:
    """把 Django HttpResponse 包装成 Flask test_client 响应的常用接口。"""

    def __init__(self, dj_resp):
        self._r = dj_resp
        self.status_code = dj_resp.status_code
        # FileResponse 是流式的，没有 .content；拼起来模拟 Flask 的 .data
        if hasattr(dj_resp, "content"):
            self.data = dj_resp.content
        else:
            self.data = b"".join(dj_resp.streaming_content)
        self.headers = dj_resp.headers

    def get_json(self):
        try:
            return _json.loads(self.data.decode("utf-8", "replace"))
        except Exception:
            return None


def _to_dj_headers(extra):
    """Flask headers dict -> Django WSGI 环境变量（X-CSRF-Token -> HTTP_X_CSRF_TOKEN）"""
    out = {}
    for k, v in (extra or {}).items():
        out["HTTP_" + k.replace("-", "_").upper()] = v
    return out


class _Client:
    """Flask test_client 接口 -> django.test.Client。"""

    def __init__(self):
        from django.test import Client
        self._c = Client()

    def _call(self, method, path, json=None, data=None, headers=None,
              content_type=None, environ_base=None, **kw):
        json_body = json  # Flask 的 json= 参数
        extra = _to_dj_headers(headers)
        if environ_base:
            for k, v in environ_base.items():
                extra[k] = v
        if json_body is not None:
            resp = self._c.generic(method, path, data=_json.dumps(json_body),
                                   content_type="application/json", **extra, **kw)
        elif content_type == "multipart/form-data":
            # Flask 的 (BytesIO, filename) 元组 -> Django SimpleUploadedFile
            from django.core.files.uploadedfile import SimpleUploadedFile
            djdata = {}
            for k, v in (data or {}).items():
                if isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], io.BytesIO):
                    djdata[k] = SimpleUploadedFile(v[1], v[0].read())
                else:
                    djdata[k] = v
            # Django 的 post() 在 data 为 dict 时自动用 multipart 编码（含文件）
            fn = getattr(self._c, method.lower(), None)
            resp = (fn(path, data=djdata, **extra, **kw) if fn else
                    self._c.generic(method, path, data=djdata, **extra, **kw))
        else:
            resp = self._c.generic(method, path, data=data, **extra, **kw)
        return _Resp(resp)

    def get(self, path, **kw):
        return self._call("GET", path, **kw)

    def post(self, path, **kw):
        return self._call("POST", path, **kw)

    def put(self, path, **kw):
        return self._call("PUT", path, **kw)

    def delete(self, path, **kw):
        return self._call("DELETE", path, **kw)


class _App:
    def test_client(self):
        return _Client()

    def __call__(self, environ, start_response):
        """作为 WSGI callable 供 waitress 并发用例直接托管。"""
        from config.wsgi import application
        return application(environ, start_response)


class _Web:
    app = _App()
    WRITE_LOCK = bridge.WRITE_LOCK
    _PUBIP = bridge._PUBIP

    @staticmethod
    def bootstrap_admin():
        """兼容旧 selftest：初始化 core 默认 admin/admin 与强改标记，并同步 Django 用户。"""
        core.init_db()
        if not core.get_meta("admin_hash"):
            core.set_meta("admin_user", "admin")
            core.set_meta("admin_hash", core.hash_password("admin"))
            core.set_meta("admin_must_change", 1)
            # 旧版 bootstrap 会写凭据文件，selftest 有断言依赖它
            pwfile = os.path.join(core.DATA, "admin_password.txt")
            if not os.path.exists(pwfile):
                with open(pwfile, "w", encoding="utf-8") as f:
                    f.write("admin\n")
        User = get_user_model()
        # core 体系当前默认口令是 admin（新装）：确保 Django 侧 admin 密码也是 admin
        if core.admin_verify("admin"):
            u = User.objects.filter(username="admin").first()
            if not u:
                u = User(username="admin", is_staff=True, is_superuser=True)
            if not u.check_password("admin"):
                u.set_password("admin")
            u.is_staff = True
            u.is_superuser = True
            u.save()


# selftest 里 `import app as web` -> 换成 `import selftest_django as web`，
# 因此本模块需要直接暴露 app / bootstrap_admin / WRITE_LOCK / _PUBIP 这些名字。
app = _App()
bootstrap_admin = _Web.bootstrap_admin
WRITE_LOCK = bridge.WRITE_LOCK
_PUBIP = bridge._PUBIP
