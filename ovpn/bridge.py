#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ovpn/bridge.py —— Flask -> Django 桥接工具层

业务逻辑仍由项目根的 core.py / fullbackup.py 提供（零改动）。
本模块把原 app.py 里那些「与 Web 框架无关」的公共能力集中起来：
  * JSON 响应封装（ok / jerr），保持与前端既有契约一致
  * 请求解析（JSON body / query / form / 上传文件）
  * 真实来源 IP、审计上下文、管理账户名
  * 长任务（全局写锁、任务日志、sudo worker、任务线程）
  * 登录失败节流（自助改密页仍用；管理端交给 Axes）
  * 公网 IP 探测缓存
"""
import io
import os
import sys
import re
import json
import time
import uuid
import threading
import subprocess
import traceback
from datetime import timedelta

from django.http import JsonResponse, HttpResponse, FileResponse
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import close_old_connections

ROOT = str(getattr(settings, 'PROJECT_ROOT', '') or
           os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 业务门面：纯逻辑继承自 system/core.py，数据访问全部是 Django ORM（源码根内零裸 SQL）。
from .services import core                      # noqa: E402
from .services import backup as fullbackup      # noqa: E402

PORT = int(getattr(settings, 'OVPN_PORT', 8800))
MOCK = os.environ.get("OVPN_WEB_MOCK") == "1"

WRITE_LOCK = threading.Lock()


class TaskBusy(RuntimeError):
    """已有长任务在执行：新写请求直接 409，不排队、不抢锁。"""


TASKS_DIR = os.path.join(core.DATA, "tasks")
CTX = {"tid": ""}

# 自助改密 / 的失败节流（管理端登录改由 Django Axes 负责）
LOGIN_FAILS = {}
LOGIN_LOCK = threading.Lock()

_PUBIP = {"ts": 0.0, "ip": "", "src": ""}
_PUBIP_LOCK = threading.Lock()
# 常态化维护：以前这些只在启动时跑一次，连续运行几个月不重启 JSONL 仍会膨胀。
_MAINT_LAST = 0.0
_MAINT_LOCK = threading.Lock()


# --------------------------------------------------------------------------- 响应
def ok(data=None, **kw):
    """构造与前端约定的 JSON：默认带 ok=True，dict 展开到顶层。"""
    d = {"ok": True}
    if data is not None:
        d.update(data if isinstance(data, dict) else {"data": data})
    d.update(kw)
    return JsonResponse(d)


def jerr(msg, code=400, **kw):
    d = {"ok": False, "msg": str(msg)}
    d.update(kw)
    return JsonResponse(d, status=code)


def err(e, code=400):
    return jerr(e, code)


def text_response(text, content_type="text/html; charset=utf-8", status=200):
    return HttpResponse(text, content_type=content_type, status=status)


def file_response(path_or_bytes, mimetype, download_name=None, attachment=True):
    """等价 Flask send_file：磁盘路径用 FileResponse，字节用 HttpResponse。"""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        resp = HttpResponse(bytes(path_or_bytes), content_type=mimetype)
    elif hasattr(path_or_bytes, "read") and not isinstance(path_or_bytes, str):
        # BytesIO / 文件对象：读到内存返回（与 Flask send_file(BytesIO) 一致）
        try:
            path_or_bytes.seek(0)
        except Exception:
            pass
        resp = HttpResponse(path_or_bytes.read(), content_type=mimetype)
    else:
        resp = FileResponse(open(path_or_bytes, "rb"), content_type=mimetype)
    if attachment and download_name:
        # 文件名走 ASCII 安全编码，避免非 ASCII 破坏 header
        from urllib.parse import quote
        resp["Content-Disposition"] = "attachment; filename*=UTF-8''%s" % quote(download_name)
    return resp


# --------------------------------------------------------------------------- 请求解析
def jbody(request):
    """等价 request.get_json(silent=True) or {}"""
    ct = (request.content_type or "").lower()
    if "application/json" not in ct:
        return {}
    try:
        data = json.loads(request.body or b"{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def client_ip(request):
    """真实来源 IP：统一走底座 RealIPMiddleware 归一化后的结果。

    RealIPMiddleware 已在链首把可信来源的 X-Forwarded-For 写回 REMOTE_ADDR
    并剥离其余代理头，所以这里直接用 common.utils.ip.get_client_ip，
    不再自己做「回环才信 XFF」的判断（否则两套规则会打架）。
    """
    from common.utils.ip import get_client_ip
    return get_client_ip(request)


def actor(request):
    """审计操作者：Django 已登录用户的用户名；未登录为空。"""
    u = getattr(request, "user", None)
    if u is not None and u.is_authenticated:
        return u.username
    return ""


def admin_user():
    """与 core 审计体系一致的管理账户名（由 bootstrap 同步自 Django User）。"""
    return core.admin_user()


# --------------------------------------------------------------------------- 失败节流
def _fails_take(key):
    with LOGIN_LOCK:
        if len(LOGIN_FAILS) > 500:
            now = time.time()
            for k in [k for k, v in list(LOGIN_FAILS.items())
                      if not any(now - t < 600 for t in v)]:
                LOGIN_FAILS.pop(k, None)
        fails = [t for t in LOGIN_FAILS.get(key, []) if time.time() - t < 600]
        LOGIN_FAILS[key] = fails
        return len(fails)


def _fails_add(key):
    with LOGIN_LOCK:
        LOGIN_FAILS.setdefault(key, []).append(time.time())


def _fails_clear(key):
    with LOGIN_LOCK:
        LOGIN_FAILS.pop(key, None)


# --------------------------------------------------------------------------- 长任务
def tlog(tid, msg):
    core.ensure_dirs()
    with open(os.path.join(TASKS_DIR, tid + ".log"), "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))


def task_update(tid, **kw):
    core.task_update(tid, **kw)


def periodic_maintenance(force=False):
    """每小时一次的常态化清理，挂在所有特权动作前，彻底免重启维护。

    启动时 startup_maintenance 会先跑一轮；之后只要有 sudo_worker 调用
    （改配置、装包、重建规则等日常操作）就会按小时节流补跑。
    """
    global _MAINT_LAST
    now = time.time()
    if not force and now - _MAINT_LAST < 3600:
        return
    with _MAINT_LOCK:
        now = time.time()
        if not force and now - _MAINT_LAST < 3600:
            return
        _MAINT_LAST = now
    try:
        core.maybe_prune_tasks()
    except Exception:
        pass
    try:
        if core.rotate_openvpn_log():
            print("[定期维护] openvpn.log 超过 20MB，已转存并截断")
    except Exception:
        pass
    for fn in ("ops-audit.jsonl", "auth.jsonl", "deny.jsonl"):
        try:
            if core.rotate_jsonl(os.path.join(core.STATE, fn)):
                print("[定期维护] %s 超过 10MB，已转存为 %s.1" % (fn, fn))
        except Exception:
            pass
    try:
        n = core.prune_auth_logs(keep=5000)
        if n:
            print("[定期维护] 清理了 %d 条过期认证日志" % n)
    except Exception:
        pass


def sudo_worker(tid, action, params=None, timeout=300):
    """特权动作：Web 只写任务文件，worker 从固定目录读，命令行不传自由参数。"""
    core.ensure_dirs()
    periodic_maintenance()
    with open(os.path.join(TASKS_DIR, tid + ".json"), "w", encoding="utf-8") as f:
        json.dump({"action": action, "params": params or {}}, f, ensure_ascii=False)
    # 特权执行器在 system/（安装根下，零 Django 依赖）
    worker = os.path.join(ROOT, "system", "worker.py")
    if not os.path.exists(worker):
        worker = os.path.join(ROOT, "worker.py")
    if MOCK:
        cmd = [sys.executable, worker, tid]
    else:
        cmd = ["sudo", "-n", core.PY, worker, tid]
    try:
        with core.SYS_LOCK:
            wenv = dict(os.environ, OVPN_WEB_ROOT=core.ROOT,
                        OVPN_WEB_MOCK="1" if MOCK else "0",
                        OVPN_WEB_SELFTEST="1" if MOCK else "0")
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=wenv)
    except subprocess.TimeoutExpired:
        raise RuntimeError("特权动作超时: %s" % action)
    if r.returncode != 0:
        tail = (r.stdout + r.stderr).strip()[-600:]
        raise RuntimeError("特权动作失败(%s): %s" % (action, tail or "见任务日志"))
    return True


def start_task(kind, steps, actor_name="admin", on_error=None):
    """steps: [(label, fn)]；fn 返回字符串写入日志。全程持全局写锁。

    锁必须在主线程里先拿到：旧实现让子线程去抢锁，两个同时到达的高危请求
    都可能先通过中间件的 WRITE_LOCK.locked() 检查，然后一个拿锁、一个排队，
    排队那个的 HTTP 已经返回 tid，用户会以为两个任务都启动了。
    """
    if not WRITE_LOCK.acquire(blocking=False):
        raise TaskBusy("有任务正在执行（安装 / 升级 / 恢复），等它完成后再操作")
    tid = uuid.uuid4().hex
    try:
        core.ensure_dirs()
        core.task_create(tid, kind, actor_name)
    except Exception:
        WRITE_LOCK.release()
        raise

    def runner():
        try:
            # 工作线程复用主线程留下的 ORM 连接不安全：用前关掉旧连接，
            # 结束时再关一次，别把 SQLite 连接泄漏在 daemon 线程里。
            close_old_connections()
            CTX["tid"] = tid
            core.audit_ctx_set(actor_name, "")
            total = len(steps)
            for i, (label, fn) in enumerate(steps):
                task_update(tid, msg=label, progress=int(i * 100 / total))
                tlog(tid, "▶ " + label)
                res = fn()
                if res:
                    tlog(tid, "  " + str(res))
                task_update(tid, progress=int((i + 1) * 100 / total))
            task_update(tid, status="done", progress=100, msg="完成", finished_at=core.now())
            tlog(tid, "✔ 全部完成")
        except Exception as e:
            tlog(tid, "✘ 失败: %s" % e)
            tlog(tid, traceback.format_exc()[-1500:])
            task_update(tid, status="failed", msg=str(e), finished_at=core.now())
            if on_error:
                try:
                    on_error(tid)
                except Exception as ee:
                    tlog(tid, "回滚也失败: %s" % ee)
        finally:
            close_old_connections()
            WRITE_LOCK.release()

    try:
        threading.Thread(target=runner, daemon=True).start()
    except Exception as e:
        # 线程创建失败时 runner 的 finally 不会执行，锁必须在这里释放，
        # 否则整个 Web 永远卡在「有任务正在执行」。
        try:
            tlog(tid, "✘ 启动任务线程失败: %s" % e)
            task_update(tid, status="failed", msg="启动任务线程失败: %s" % e,
                        finished_at=core.now())
        finally:
            WRITE_LOCK.release()
        raise
    return tid


def after_change(kick=False, username=None):
    """账号变更统一动作：全量重建 ccd + 规则，可选踢下线。"""
    with core.SYS_LOCK:
        core.write_all_config()
        tid = uuid.uuid4().hex
        sudo_worker(tid, "apply-rules")
        if kick and username:
            sudo_worker(tid, "kick-user", {"username": username})


def install_code(request):
    """安装码：只在这一次请求里透传给 worker，不落库、不写日志。"""
    return (request.POST.get("install_code") or "").strip()


# --------------------------------------------------------------------------- 公网 IP
def pubip_get(force=False):
    if not force and time.time() - _PUBIP["ts"] < 600:
        return _PUBIP["ip"], _PUBIP["src"], True
    with _PUBIP_LOCK:
        if not force and time.time() - _PUBIP["ts"] < 600:
            return _PUBIP["ip"], _PUBIP["src"], True
        ip, src = core.detect_public_ip()
        _PUBIP.update({"ts": time.time(), "ip": ip, "src": src})
        return ip, src, False


# --------------------------------------------------------------------------- 维护
def startup_maintenance():
    """服务启动一次的维护动作，等价原 app.main() 里的启动段。"""
    core.init_db()
    # 与日常特权动作共用同一套清理逻辑：去重，并顺手刷新 _MAINT_LAST，
    # 避免启动后的第一次 sudo_worker 立刻再扫一遍同样的目录/日志。
    try:
        periodic_maintenance(force=True)
    except Exception as e:
        print("[启动维护] 跳过: %s" % e)
    try:
        be = core.check_build_env(force=True)
        line = ("可用（gcc / make / pkg-config 齐备）" if be["ok"]
                else "不完整 · 缺 " + "、".join(be["missing"]))
    except Exception as e:
        line = "检测失败: %s" % e
    print("=" * 60)
    print(" OpenVPN Web 一体化管理系统 (Django)  v%s" % core.WEB_VERSION)
    print(" 数据  %s" % core.DB_PATH)
    print(" 本机编译环境: %s" % line)
    print("=" * 60)


# --------------------------------------------------------------------------- 方法分派
def write_only(fn):
    """有副作用的接口一律拒绝 GET/HEAD/OPTIONS（405）。

    只靠前端用 POST 调用不够：管理员点开一个链接就能用 GET 触发重启、
    删授权、重建 CA。这里在路由层兜底，新增写接口必须显式挂上这个包装。
    CSRF 仍由外层中间件统一校验（所有非 GET 路径）。
    """
    import functools

    @functools.wraps(fn)
    def wrapper(request, *args, **kwargs):
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return jerr("该接口不接受 GET，请用 POST 提交", 405)
        return fn(request, *args, **kwargs)
    return wrapper


def method_split(**views):
    """
    多个 HTTP 方法共用同一 URL 时按 method 分派（对应 Flask 同名路由多装饰器）。
    用法：method_split(GET=users_get, POST=users_add, DELETE=users_delete)
    """
    def wrapper(request, *args, **kwargs):
        fn = views.get(request.method)
        if fn is None:
            return jerr("方法不允许", 405)
        return fn(request, *args, **kwargs)
    return wrapper
