#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenVPN Web (Django 版) 服务入口 —— systemd ExecStart 指向本文件。

单进程 + 线程池的 waitress 跑 Django WSGI（config.wsgi:application）。
与旧版保持一致：同一份进程内状态（公网 IP 缓存 / 二进制版本缓存 / 任务节流）
只有一份，SQLite 只有一个写入者，避免多进程抢锁。

环境变量：
  OVPN_WEB_PORT   监听端口，默认 8800
  OVPN_WEB_THREADS 线程数，默认 8
  OVPN_WEB_HTTPS   =1 时 session cookie 走 Secure
  OVPN_WEB_ROOT   安装根（core.py 数据目录）
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# serve.py 恒在安装根（与 bin/、system/、vendor/、data/ 同级）。
ROOT = _HERE
# Django 项目根：2.6 起源码与交付同构为「项目根即 Django 根」（config/ 在 ROOT 下），
# 兼容 base 模板的 django/ 子目录布局（ROOT/django/config）。
DJANGO_DIR = ROOT
if os.path.isdir(os.path.join(ROOT, "django", "config")):
    DJANGO_DIR = os.path.join(ROOT, "django")
sys.path.insert(0, ROOT)          # 让 system/ 包可导入（特权脚本边界）
sys.path.insert(0, DJANGO_DIR)    # 让 config/、core/、ovpn/ 可导入
PORT = int(os.environ.get("OVPN_WEB_PORT", "8800"))
try:
    THREADS = max(1, min(64, int(os.environ.get("OVPN_WEB_THREADS", "8"))))
except ValueError:
    THREADS = 8
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
# 这是单机便携交付：强制裸机模式（SQLite + WhiteNoise + Waitress，零外部服务）。
# 不读 .env 的 STANDALONE 开关——交付形态固定，避免误配成 Docker 模式连不上库。
os.environ["STANDALONE"] = "True"
os.environ.setdefault("USE_HTTPS", "false")
# 生产默认关 DEBUG；只有源码调试时显式设 OVPN_WEB_DEBUG=1 才打开
if os.environ.get("OVPN_WEB_DEBUG") == "1":
    os.environ["DEBUG"] = "true"
else:
    os.environ["DEBUG"] = "false"

# SECRET_KEY：优先环境变量（.env / systemd 注入）；否则自动生成并持久化到 data/web.secret，
# 保证「拷过去即用」——首次启动随机生成，之后重启沿用同一把，会话不会失效。
if not os.environ.get("SECRET_KEY"):
    secret_file = os.path.join(os.environ.get("OVPN_WEB_ROOT", ROOT), "data", "web.secret")
    try:
        os.makedirs(os.path.dirname(secret_file), exist_ok=True)
        if os.path.exists(secret_file):
            with open(secret_file, "rb") as f:
                key = f.read().decode("ascii", "replace").strip()
        else:
            import secrets
            key = secrets.token_urlsafe(48)
            with open(secret_file, "w", encoding="ascii") as f:
                f.write(key)
            os.chmod(secret_file, 0o600)
        if key:
            os.environ["SECRET_KEY"] = key
    except Exception as e:
        print("[serve] 生成 SECRET_KEY 失败: %s" % e)

# ALLOWED_HOSTS：把本机所有网卡 IPv4 + 端口都加进去，拷到任意机器用任意网卡 IP
# 都能访问管理页（Django 默认只信 ALLOWED_HOSTS，不配会 400）。
# 用户也可在 .env 里显式设 HOSTS 覆盖（settings 会读进程环境变量优先）。
if not os.environ.get("HOSTS"):
    hosts = ["localhost", "127.0.0.1"]
    try:
        import socket
        hn = socket.gethostname()
        hosts.append(hn)
        for info in socket.getaddrinfo(hn, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in hosts and not ip.startswith("127."):
                hosts.append(ip)
        # udp connect 取默认出口 IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip not in hosts:
                hosts.append(ip)
        except Exception:
            pass
        finally:
            s.close()
    except Exception:
        pass
    os.environ["HOSTS"] = ",".join(h + ":" + str(PORT) for h in hosts)

def _relabel_legacy_auth_app():
    """老版本的 Auth app 叫 accounts，后来改名 core（db_table 一直是 core_user）。

    升级时 django_migrations 里留着 `accounts.0001_initial`，新代码却按 `core`
    找迁移，于是报「admin.0001 先于 core.0001 应用」的历史不一致。表结构没变，
    把迁移记录与 content type 的 app_label 改成新名字即可，不重建任何表。
    """
    from django.db import connection
    with connection.cursor() as cur:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='django_migrations'")
        if not cur.fetchone():
            return False
        cur.execute("SELECT 1 FROM django_migrations WHERE app='accounts'")
        if not cur.fetchone():
            return False
        cur.execute("UPDATE django_migrations SET app='core' WHERE app='accounts'")
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='django_content_type'")
        if cur.fetchone():
            cur.execute("UPDATE django_content_type SET app_label='core' WHERE app_label='accounts'")
    return True


def _migrate():
    """迁移，并兼容两种历史库：

    1) 老库把 Auth app 记成 accounts（现名 core）→ 历史不一致；
    2) 老库的业务表是 core.py 用裸 SQL 建的，ORM 首次迁移会撞「表已存在」。
       这种正是 --fake-initial 的用途：只把初始迁移标记为已应用，不重建表。
    """
    from django.core.management import call_command
    try:
        call_command("migrate", "--noinput", verbosity=0)
    except Exception as exc:
        name = type(exc).__name__
        msg = str(exc)
        if name == "InconsistentMigrationHistory" and _relabel_legacy_auth_app():
            print("[serve] 检测到旧版 accounts 迁移记录，已改名 core 后重试")
            call_command("migrate", "--noinput", verbosity=0)
            return
        if "already exists" in msg:
            print("[serve] 业务表已存在（旧版裸 SQL 建的），改用 --fake-initial 对齐")
            call_command("migrate", "--fake-initial", "--noinput", verbosity=0)
            return
        raise


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    # 迁移 + Django 管理员引导（幂等）：确保拷过去装完就有可用后台
    import django
    django.setup()
    from django.core.management import call_command
    _migrate()
    call_command("ovpn_init", verbosity=0)

    from config.wsgi import application
    from waitress import serve
    print("=" * 60)
    print(" OpenVPN Web (Django) 监听 0.0.0.0:%s，waitress %d 线程" % (PORT, THREADS))
    print("=" * 60)
    serve(application, host="0.0.0.0", port=PORT, threads=THREADS,
          ident="openvpn-web", clear_untrusted_proxy_headers=True,
          max_request_body_size=80 * 1024 * 1024)


if __name__ == "__main__":
    main()
