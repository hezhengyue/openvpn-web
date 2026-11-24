# config/settings.py
"""
双模式统一底座配置

  STANDALONE=False  Docker 全家桶模式：
      Nginx + PostgreSQL + Redis + Celery/Beat + Gunicorn
  STANDALONE=True   裸机便携模式：
      便携 Python 3.12 + SQLite(WAL) + WhiteNoise + Waitress，零外部服务

两种模式共用同一份代码、同一套原生 Django Admin / Axes / 独立 audit app 统一审计。
"""

import os
import sys
import warnings
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote_plus

import environ
from django.core.exceptions import ImproperlyConfigured

# BASE_DIR = 源码根（manage.py、config/、core/、ovpn/ 所在层）。
# 2.6 起源码与交付同构为「项目根即 Django 根」，BASE_DIR 就是安装根；
# 仍兼容 base 模板的 django/ 子目录布局（那时 BASE_DIR=django/，安装根是其父）。
BASE_DIR = Path(__file__).resolve().parent.parent
# PROJECT_ROOT = 安装根（data/、bin/、system/、vendor/ 所在层）。
# 用 system/ 这个特权层标志判断：谁持有 system/，谁就是安装根。
# 根布局下 BASE_DIR 与 PROJECT_ROOT 重合；django/ 子目录布局下父目录才是安装根。
if (BASE_DIR / "system").is_dir():
    PROJECT_ROOT = BASE_DIR
else:
    PROJECT_ROOT = BASE_DIR.parent

env = environ.Env()
# 优先级：进程环境变量 > 项目根目录 .env > 代码默认值
environ.Env.read_env(PROJECT_ROOT / '.env')


# ===================== 📂 运行时目录策略 =====================
# RUNTIME_DIR 是「统一可写区」总开关：设置后，未单独指定的运行时目录自动落到它下面。
# 本项目为平铺布局，默认运行时目录在 BASE_DIR/{data,staticfiles,media,logs}；
# 单独设置 DATA_DIR / STATIC_ROOT / MEDIA_ROOT / LOG_DIR / CACHE_DIR 时以它为准。
RUNTIME_DIR = env('RUNTIME_DIR', default='').strip()


def _runtime_dir(var_name, subdir):
    explicit = env(var_name, default='').strip()
    if explicit:
        return Path(explicit)
    if RUNTIME_DIR:
        return Path(RUNTIME_DIR) / subdir
    # 运行时目录（data/staticfiles/media/logs）落在安装根，即 PROJECT_ROOT，
    # 与 bin/、system/、vendor/ 同级；源码布局下就是仓库根，交付包里就是 /opt/openvpn-web。
    return PROJECT_ROOT / subdir


def _ensure_dir(path, label):
    """仅 best-effort 预建目录，失败只告警，不中断 import。

    settings 在 import 阶段执行，任何命令（check/collectstatic/test）都会经过这里。
    「项目根 root:root 只读、只有运行时目录归服务用户可写」的部署模型下，
    直接 mkdir 会抛 PermissionError 导致服务根本起不来。目录预建的实际责任
    交给启动入口（install.sh / standalone:init）；这里失败只告警。
    """
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        warnings.warn(
            f'无法创建 {label} 目录 {path}（{exc}）；请确认已由启动入口预建且可写。',
            RuntimeWarning,
        )


# ===================== 🌟 运行模式开关 =====================
SECRET_KEY = env('SECRET_KEY', default='')
DEBUG = env.bool('DEBUG', default=False)
STANDALONE = env.bool('STANDALONE', default=False)
# 容器内检测仅作参考；STANDALONE 显式开启时一律按裸机模式处理
IN_DOCKER = os.path.exists('/.dockerenv') and not STANDALONE
# HTTPS 与 DEBUG 解耦：裸机内网 HTTP 时显式写 USE_HTTPS=false
USE_HTTPS = env.bool('USE_HTTPS', default=not DEBUG)
# 裸机默认按无 TLS 处理：直接调用 manage.py 时也生效，避免复用 Docker .env
# 导致 Secure Cookie / CSRF origin 在 HTTP 下失效。前置了 TLS 终止时显式开启。
STANDALONE_USE_HTTPS = env.bool('STANDALONE_USE_HTTPS', default=False)
# 裸机前置本机反代（Caddy/Nginx 做 TLS 终止）时置 True：仅信任来自
# 127.0.0.1/::1 的转发头，公网直连仍按 REMOTE_ADDR 处理。
TRUST_LOCAL_PROXY = env.bool('TRUST_LOCAL_PROXY', default=STANDALONE_USE_HTTPS)
if STANDALONE and USE_HTTPS and not STANDALONE_USE_HTTPS:
    USE_HTTPS = False
    warnings.warn(
        'STANDALONE=True 且未设置 STANDALONE_USE_HTTPS=true，已按 HTTP 处理；'
        '若前面有 TLS 终止请显式开启该开关。',
        RuntimeWarning,
    )
PROJECT_NAME = env('PROJECT_NAME', default='openvpn-web')


# ===================== 🌐 访问与安全 =====================
# HOSTS 可写 host 或 host:port（例如 10.1.0.90:8800）
_raw_hosts = env.list('HOSTS', default=[])
_raw_hosts = list(dict.fromkeys(['localhost', '127.0.0.1', *_raw_hosts]))
# ALLOWED_HOSTS 不带端口；CSRF_TRUSTED_ORIGINS 保留端口
ALLOWED_HOSTS = sorted({h.split(':', 1)[0] if ':' in h else h for h in _raw_hosts})
# DEBUG 同时放开 http/https 便于本地开发；非 DEBUG 时按 USE_HTTPS 收紧，
# 避免生产 HTTPS 还额外信任明文 http:// 来源。
if DEBUG:
    schemes = ['https', 'http']
elif USE_HTTPS:
    schemes = ['https']
else:
    schemes = ['http']
CSRF_TRUSTED_ORIGINS = [
    f'{scheme}://{host}'
    for host in _raw_hosts
    for scheme in schemes
]
CSRF_COOKIE_SECURE = USE_HTTPS
SESSION_COOKIE_SECURE = USE_HTTPS
# 后台使用 Django 原生模板（已移除 SimpleUI 的 iframe 菜单），
# 没有嵌框需求，保持 Django 默认的 DENY。
X_FRAME_OPTIONS = env('X_FRAME_OPTIONS', default='DENY')
# 前端沿用 Flask 版的 `X-CSRF-Token` 头。Django 默认读的是 `X-CSRFToken`
# （META 里是 HTTP_X_CSRFTOKEN，没有中间连字符），不同名会被当成没带 token。
# 这里对齐既有的前端契约，前端一行都不用改。
CSRF_HEADER_NAME = env('CSRF_HEADER_NAME', default='HTTP_X_CSRF_TOKEN')
# 前端按 JSON 解析所有响应：CSRF 失败也要回 JSON，不能给 Django 的 HTML 403
CSRF_FAILURE_VIEW = 'ovpn.csrf.csrf_failure'
CSRF_COOKIE_NAME = 'csrftoken'
CSRF_COOKIE_HTTPONLY = False      # 前端从 /api/session 的 JSON 取 token，不读 cookie，但保留可读便于排障
CSRF_USE_SESSIONS = False         # token 走 cookie，未登录也能拿到（登录页就要发写请求）
# Docker(Nginx) 或裸机 + 本机反代时信任 X-Forwarded-Proto；
# 裸机直连不信任任何代理头，避免伪造 https 标记与 IP。
SECURE_PROXY_SSL_HEADER = (
    ('HTTP_X_FORWARDED_PROTO', 'https')
    if (not STANDALONE or TRUST_LOCAL_PROXY)
    else None
)

# 真实 IP 解析策略：
#   Docker：Nginx 覆盖 X-Forwarded-For 为单个 $remote_addr，链中没有代理条目 → proxy_count=0
#   裸机  ：RealIPMiddleware 在 TRUST_LOCAL_PROXY 时从本机反代头解析并写回 REMOTE_ADDR，
#           其余情况只信 REMOTE_ADDR，因此这里始终按 REMOTE_ADDR 读取归一化结果。
IPWARE_HEADER_ORDER = ('REMOTE_ADDR',) if STANDALONE else ('HTTP_X_FORWARDED_FOR', 'REMOTE_ADDR')
IPWARE_PROXY_COUNT = 0
AXES_IPWARE_META_PRECEDENCE_ORDER = list(IPWARE_HEADER_ORDER)

AUTH_USER_MODEL = 'core.User'
# 模板只有 admin，没有 Django 默认的 accounts/profile 页面。
# 不设置 LOGIN_REDIRECT_URL 会导致登录成功后跳到 /accounts/profile/ 404。
LOGIN_URL = '/admin/login/'
LOGIN_REDIRECT_URL = '/admin/'
LOGOUT_REDIRECT_URL = '/admin/login/'


# ===================== 📦 应用与中间件 =====================
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'core',                     # Django Auth 用户模型（AUTH_USER_MODEL = core.User）
    'ovpn',                     # OpenVPN 业务桥接（承载原 Flask 路由）
    'common.apps.CommonConfig',
    'jobs',
    'audit.apps.AuditConfig',   # 统一审计：唯一写入口 audit.services.audit_log()
    'axes',
]

# django_celery_beat 只在 Docker 全家桶模式注册；裸机模式不暴露定时任务菜单
if not STANDALONE:
    INSTALLED_APPS.append('django_celery_beat')

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise 替代 Nginx 托管 /static/，DEBUG=False 也不丢样式
    'whitenoise.middleware.WhiteNoiseMiddleware',
    # 必须在 AxesMiddleware / AuditContextMiddleware 之前修正 REMOTE_ADDR
    'common.middleware.real_ip.RealIPMiddleware',
    # 请求级审计上下文（request_id / 当前 request，ContextVar 实现）
    'audit.middleware.AuditContextMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # OpenVPN：安全头 + 白名单/登录/首登改密闸门。
    # CSRF 由上面的 CsrfViewMiddleware 负责（CSRF_HEADER_NAME 已对齐
    # 前端的 X-CSRF-Token），这里只补「带副作用 GET」的兜底校验。
    'ovpn.middleware.SecurityHeadersMiddleware',
    'ovpn.middleware.BootstrapMiddleware',
    'ovpn.middleware.OvpnGuardMiddleware',
    # Axes 官方文档要求放最后：它只在响应阶段改写锁定响应
    'axes.middleware.AxesMiddleware',
]

ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'templates'],
    'APP_DIRS': True,
    'OPTIONS': {
        'context_processors': [
            'django.template.context_processors.debug',
            'django.template.context_processors.request',
            'django.contrib.auth.context_processors.auth',
            'django.contrib.messages.context_processors.messages',
        ],
    },
}]


# ===================== 🌏 国际化 =====================
LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# ===================== 🗄️ 数据库：SQLite(WAL) / PostgreSQL =====================
if STANDALONE:
    DATA_DIR = _runtime_dir('DATA_DIR', 'data')
    _ensure_dir(DATA_DIR, 'DATA_DIR')
    # 特权脚本（system/worker.py、auth-verify.py）不读 .env，只认进程环境变量；
    # 把最终生效值回写，保证它们和 ORM 指向同一个数据目录/数据库。
    # 进程环境已有值时保持原样（优先级仍然最高）。
    os.environ.setdefault('DATA_DIR', str(DATA_DIR))
    if RUNTIME_DIR:
        os.environ.setdefault('RUNTIME_DIR', str(RUNTIME_DIR))
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': DATA_DIR / 'db.sqlite3',
            'OPTIONS': {
                'timeout': 20,
                # Django 5.1+：SQLite 立即事务，显著减少 "database is locked"
                'transaction_mode': 'IMMEDIATE',
                # PRAGMA journal_mode=WAL 由 common.apps 的 connection_created 信号设置，
                # SQLite 后端不支持 OPTIONS['init_command']（那是 MySQL 的参数）。
            },
        }
    }
else:
    POSTGRES_HOST = env('POSTGRES_HOST', default='db' if IN_DOCKER else 'localhost')
    POSTGRES_PORT = env.int('POSTGRES_PORT', default=5432)
    POSTGRES_USER = env('POSTGRES_USER', default='postgres')
    POSTGRES_PASSWORD = env('POSTGRES_PASSWORD', default='Postgres1234')
    POSTGRES_DB = env('POSTGRES_DB', default='db')
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': POSTGRES_DB,
            'USER': POSTGRES_USER,
            'PASSWORD': POSTGRES_PASSWORD,
            'HOST': POSTGRES_HOST,
            'PORT': POSTGRES_PORT,
            'CONN_MAX_AGE': 600,
            'CONN_HEALTH_CHECKS': True,
        }
    }


# ===================== 🚀 缓存 / Session / Celery =====================
if STANDALONE:
    STANDALONE_CACHE = env('STANDALONE_CACHE', default='locmem').strip().lower()
    if STANDALONE_CACHE == 'file':
        CACHE_DIR = _runtime_dir('CACHE_DIR', 'data/cache')
        _ensure_dir(CACHE_DIR, 'CACHE_DIR')
        CACHES = {
            'default': {
                'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache',
                'LOCATION': str(CACHE_DIR),
                'TIMEOUT': 300,
            }
        }
    else:
        CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
    SESSION_ENGINE = 'django.contrib.sessions.backends.db'
    # 无 Redis：Celery 用内存 broker + 同步执行，代码里的 .delay() 依旧可用
    CELERY_BROKER_URL = 'memory://'
    CELERY_RESULT_BACKEND = 'cache+memory://'
    CELERY_TASK_ALWAYS_EAGER = True
    CELERY_TASK_EAGER_PROPAGATES = False
else:
    REDIS_HOST = env('REDIS_HOST', default='redis' if IN_DOCKER else 'localhost')
    REDIS_PORT = env.int('REDIS_PORT', default=6379)
    REDIS_PASSWORD = env('REDIS_PASSWORD', default='Redis1234')
    REDIS_DB = 0
    _redis_auth = f':{quote_plus(REDIS_PASSWORD)}@' if REDIS_PASSWORD else ''
    REDIS_URL = f'redis://{_redis_auth}{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}'
    CELERY_BROKER_DB = 1
    CELERY_RESULT_BACKEND_DB = 2
    CELERY_BROKER_URL = f'redis://{_redis_auth}{REDIS_HOST}:{REDIS_PORT}/{CELERY_BROKER_DB}'
    CELERY_RESULT_BACKEND = f'redis://{_redis_auth}{REDIS_HOST}:{REDIS_PORT}/{CELERY_RESULT_BACKEND_DB}'
    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': REDIS_URL,
            'OPTIONS': {
                'CLIENT_CLASS': 'django_redis.client.DefaultClient',
                'SOCKET_CONNECT_TIMEOUT': 5,
                'SOCKET_TIMEOUT': 5,
            },
        }
    }
    SESSION_ENGINE = 'django.contrib.sessions.backends.cached_db'

CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TIME_LIMIT = 300
# 会话时长：默认 1 小时，与前端「无操作 1 小时自动退出」保持一致。
# 不全局开 SESSION_SAVE_EVERY_REQUEST：前端 60 秒 /api/session、长任务 700ms
# /api/task/<tid>/log 都是只读轮询，全局续期会把每个轮询变成一次 SQLite 写，
# 还会让绕过前端的客户端会话永不过期。改为由 ovpn.middleware 对「真实请求」
# 显式置 session.modified=True 实现滑动续期，轮询与静态资源不碰 session 表。
SESSION_COOKIE_AGE = env.int('SESSION_COOKIE_AGE', default=3600)
SESSION_SAVE_EVERY_REQUEST = False


# ===================== 🔐 密码校验 & Axes 防爆破 =====================
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',
    'django.contrib.auth.backends.ModelBackend',
]
# 锁定阈值与冷却时间可环境变量化：本项目默认 5 次 / 10 分钟（比底座 60 分钟短，
# 与旧 Flask 版「失败 5 次锁 10 分钟」保持一致）。
AXES_FAILURE_LIMIT = env.int('AXES_FAILURE_LIMIT', default=5)
AXES_COOLOFF_MINUTES = env.int('AXES_COOLOFF_MINUTES', default=10)
if AXES_COOLOFF_MINUTES <= 0:
    raise ImproperlyConfigured('AXES_COOLOFF_MINUTES 必须为正整数')
AXES_COOLOFF_TIME = timedelta(minutes=AXES_COOLOFF_MINUTES)
AXES_LOCKOUT_PARAMETERS = [['ip_address', 'username']]
AXES_RESET_ON_SUCCESS = True
AXES_ENABLE_ADMIN = True
AXES_HTTP_RESPONSE_CODE = 403
AXES_CACHE = 'default'
AXES_IPWARE_PROXY_COUNT = IPWARE_PROXY_COUNT
# 锁定响应同时兼容 admin 表单登录与前端 JSON API（见 common/utils/axes.py）
AXES_LOCKOUT_CALLABLE = 'common.utils.axes.axes_lockout_response'


# ===================== 🧾 统一审计 =====================
# 审计记录保留天数，清理命令：manage.py audit_purge --days N [--dry-run]
AUDIT_RETENTION_DAYS = env.int('AUDIT_RETENTION_DAYS', default=180)
# 无操作人时的兜底名字；业务侧 50 处调用都显式传 actor，这里留空即可。
AUDIT_FALLBACK_ACTOR = env('AUDIT_FALLBACK_ACTOR', default='')
# 认证事件自动捕获白名单（字符串或元组均可，逗号分隔）。
# 登录 / 退出 / 失败 / 锁定由业务侧 core.log_audit 记（带中文动作码与失败原因），
# 这里只留 lockout：Axes 锁定没有业务入口，必须靠信号补。
AUDIT_CAPTURE_EVENTS = env('AUDIT_CAPTURE_EVENTS', default='lockout')


# ===================== 📁 静态 / 媒体 / 上传 =====================
# 允许 compose 通过环境变量统一覆盖路径；使用默认 StaticFilesStorage，
# 不用 ManifestStaticFilesStorage，避免离线包个别 .map 文件缺失报错。
STATIC_ROOT = _runtime_dir('STATIC_ROOT', 'staticfiles')
MEDIA_ROOT = _runtime_dir('MEDIA_ROOT', 'media')
# 项目自带的公共静态资源（例如 Logo）放这里，collectstatic 后由
# Docker(Nginx) 与裸机(WhiteNoise) 两种模式的 /static/ 统一提供。
# 这是源码内的只读输入目录，不随 RUNTIME_DIR 重定向。
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_URL = '/static/'
MEDIA_URL = '/media/'
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}
# best-effort 预建（失败只告警，真正预建交给启动入口）。
# static/ 也包含在内：即便使用者删掉 static/img/.gitkeep，首次运行也不会触发 staticfiles.W004。
for _p in (STATIC_ROOT, MEDIA_ROOT, BASE_DIR / 'static'):
    _ensure_dir(_p, str(_p))

MAX_UPLOAD_MB = env.int('MAX_UPLOAD_MB', default=50)
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_MB * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_MB * 1024 * 1024


# ===================== 📝 日志 =====================
# Docker：只打 stdout/stderr，由 compose 的 json-file 轮转，避免 7 个进程
#         同时切割同一个 TimedRotatingFileHandler 造成丢日志/FileNotFoundError。
# 裸机  ：单进程 Waitress，保留按天轮转文件便于本地排查。
LOG_DIR = _runtime_dir('LOG_DIR', 'logs')
_ensure_dir(LOG_DIR, 'LOG_DIR')
LOG_LEVEL = 'DEBUG' if DEBUG else 'INFO'
LOG_FILE_LEVEL = 'INFO' if DEBUG else 'WARNING'
LOG_RETENTION_DAYS = env.int('LOG_RETENTION_DAYS', default=90)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'dev': {
            '()': 'colorlog.ColoredFormatter',
            'format': '%(log_color)s%(asctime)s [%(levelname)s] %(name)s:%(lineno)d%(reset)s - %(message)s',
            'log_colors': {
                'DEBUG': 'cyan', 'INFO': 'green', 'WARNING': 'yellow',
                'ERROR': 'red', 'CRITICAL': 'bold_red',
            },
        },
        'prod': {
            '()': 'pythonjsonlogger.json.JsonFormatter',
            'format': '%(asctime)s %(name)s %(levelname)s %(message)s %(pathname)s %(lineno)d %(funcName)s',
            'static_fields': {'project': PROJECT_NAME},
        },
        'file': {
            'format': '%(asctime)s [%(levelname)s] %(name)s:%(lineno)d - %(message)s',
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'dev' if DEBUG else 'prod',
            'stream': sys.stdout,
            'level': LOG_LEVEL,
        },
        'app_file': {
            'class': 'logging.handlers.TimedRotatingFileHandler',
            'formatter': 'file',
            'filename': LOG_DIR / 'app.log',
            'when': 'midnight', 'interval': 1,
            'backupCount': LOG_RETENTION_DAYS,
            'encoding': 'utf-8', 'level': LOG_FILE_LEVEL, 'delay': True,
        },
        'error_file': {
            'class': 'logging.handlers.TimedRotatingFileHandler',
            'formatter': 'file',
            'filename': LOG_DIR / 'error.log',
            'when': 'midnight', 'interval': 1,
            'backupCount': LOG_RETENTION_DAYS,
            'encoding': 'utf-8', 'level': 'ERROR', 'delay': True,
        },
        'django_file': {
            'class': 'logging.handlers.TimedRotatingFileHandler',
            'formatter': 'file',
            'filename': LOG_DIR / 'django.log',
            'when': 'midnight', 'interval': 1,
            'backupCount': LOG_RETENTION_DAYS,
            'encoding': 'utf-8',
            'level': 'INFO' if DEBUG else 'WARNING', 'delay': True,
        },
    },
}

if IN_DOCKER:
    _root_handlers = ['console']
    _django_handlers = ['console']
    _core_handlers = ['console']
else:
    _root_handlers = ['console', 'app_file', 'error_file']
    _django_handlers = ['console', 'django_file']
    _core_handlers = ['console', 'app_file', 'error_file']

LOGGING['root'] = {'handlers': _root_handlers, 'level': LOG_LEVEL}
LOGGING['loggers'] = {
    'django': {
        'handlers': _django_handlers,
        'level': 'INFO' if DEBUG else 'WARNING',
        'propagate': False,
    },
    'django.db.backends': {
        'handlers': ['console'] if DEBUG else [],
        'level': 'DEBUG' if DEBUG else 'CRITICAL',
        'propagate': False,
    },
    'ovpn': {'handlers': _core_handlers, 'level': 'DEBUG', 'propagate': False},
    # 审计写库失败只记运行日志（audit.services 内部 logger.exception），
    # 业务不能因为审计故障 500，排查入口放在这里。
    'audit': {'handlers': _core_handlers, 'level': 'INFO', 'propagate': False},
}


# ===================== 📧 邮件：默认关闭，彻底解耦启动 =====================
EMAIL_ENABLED = env.bool('EMAIL_ENABLED', default=False)
EMAIL_PROTOCOL = env.str('EMAIL_PROTOCOL', default='ssl').strip().lower()
VALID_PROTOCOLS = {'ssl', 'tls', 'none'}
if EMAIL_PROTOCOL not in VALID_PROTOCOLS:
    raise ImproperlyConfigured(f"EMAIL_PROTOCOL 必须是 {VALID_PROTOCOLS} 之一，当前值: '{EMAIL_PROTOCOL}'")

if EMAIL_ENABLED:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_HOST = env('EMAIL_HOST', default='')
    EMAIL_PORT = env.int('EMAIL_PORT', default=465)
    EMAIL_HOST_USER = env('EMAIL_HOST_USER', default='')
    EMAIL_HOST_PASSWORD = env('EMAIL_HOST_PASSWORD', default='')
    if not EMAIL_HOST or not EMAIL_HOST_USER or not EMAIL_HOST_PASSWORD:
        raise ImproperlyConfigured(
            'EMAIL_ENABLED=True 时必须同时设置 EMAIL_HOST / EMAIL_HOST_USER / EMAIL_HOST_PASSWORD'
        )
    EMAIL_USE_SSL = EMAIL_PROTOCOL == 'ssl'
    EMAIL_USE_TLS = EMAIL_PROTOCOL == 'tls'
    DEFAULT_FROM_EMAIL = EMAIL_HOST_USER
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
    EMAIL_HOST = ''
    EMAIL_PORT = 25
    EMAIL_HOST_USER = ''
    EMAIL_HOST_PASSWORD = ''
    EMAIL_USE_SSL = False
    EMAIL_USE_TLS = False
    DEFAULT_FROM_EMAIL = f'noreply@{ALLOWED_HOSTS[0] if ALLOWED_HOSTS else "localhost"}'


# ===================== 🛡️ 生产环境启动拦截 =====================
if not DEBUG:
    if not SECRET_KEY or SECRET_KEY.startswith('django-insecure'):
        raise ImproperlyConfigured('生产环境 SECRET_KEY 必须更换且不能为空！')
    if '*' in ALLOWED_HOSTS:
        raise ImproperlyConfigured('生产环境 ALLOWED_HOSTS 禁止使用 * ！')
    if not STANDALONE:
        if POSTGRES_PASSWORD == 'Postgres1234' or REDIS_PASSWORD == 'Redis1234':
            raise ImproperlyConfigured(
                '生产环境禁止使用默认弱口令 Postgres1234 / Redis1234，请先修改 .env！'
            )
    if not USE_HTTPS:
        warnings.warn(
            'USE_HTTPS=false 时 Cookie 不带 Secure 标志，仅建议内网裸机 HTTP 使用；'
            '公网环境请显式设置 USE_HTTPS=true 并配置 HTTPS。',
            RuntimeWarning,
        )
