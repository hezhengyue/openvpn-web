# ovpn/services/base.py
"""业务门面 · 基础设施层。

本包是所有 services 子模块的共享命名空间：这里 import 的 json/os/settings/transaction
等对象会被 `from .base import *` 带到子模块，子模块不需要重复 import。

分工：
  * `system/core.py`  —— 纯逻辑层（网络计算、配置渲染、iptables 渲染、PKI、版本校验、
    密码哈希……）与特权脚本共用的 SQLite 实现。**零 Django 依赖**，给
    `system/worker.py`、`system/auth-verify.py`、`system/setup.py` 用。
  * 本模块 —— 把纯逻辑整体继承过来，再**用 Django ORM 重新实现全部数据访问**，
    供 Web 进程使用。源码根内不出现任何 `sqlite3.connect` 或业务 `cursor.execute`。

对外函数名、参数、返回结构与原 Flask 版 `core.py` 完全一致，`ovpn/views/` 与
`ovpn/services/` 的其他子模块都按这套 API 调用。
"""
import copy
import json
import os
import sys
import time
import threading
import ipaddress
from datetime import datetime

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

# 纯逻辑层：安装根下的 system 包（交付包与源码布局都成立）
_ROOT = str(getattr(settings, 'PROJECT_ROOT', '') or '')
if _ROOT and _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from system import core as _base  # noqa: E402

from ..models import (  # noqa: E402
    SystemSetting, VpnUser, AssetGroup, AssetItem, UserGroup, UserGroupMember,
    AclGrant, PeerGrant, AuthLog, OvpnTask, BackupRecord,
)

from audit.models import AuditEventType, AuditLog  # noqa: E402
from audit.services import audit_log as _audit_write  # noqa: E402

# --------------------------------------------------------------------------- 继承纯逻辑
# 常量（ROOT/DATA/BIN/DEFAULT_CFG/WEB_VERSION…）、纯函数（hash_password、vpn_network、
# render_server_conf、render_rules、gen_pki、online_users、allow_list…）全部直接继承，
# 不做第二份实现。数据访问函数在下面逐个覆盖。
for _k, _v in list(vars(_base).items()):
    if not _k.startswith('__'):
        globals()[_k] = _v

# 下面这些原实现依赖裸 SQL 或已由 Django 迁移接管，统一覆盖掉。
_PREVIEW_CACHE_PREFIX = 'ovpn:preview:'
_PREVIEW_TTL = 300



def db():
    """原实现返回 sqlite3 连接。Web 侧已全部改走 ORM，这里显式拒绝，避免有人绕回来。"""
    raise RuntimeError(
        "Web 侧不再直接持有数据库连接：请使用 ovpn/models.py 里的模型。"
        "特权脚本（system/）仍可用 system.core.db()。")



def init_db():
    """建目录 + 收紧权限；建表交给 Django migrations。"""
    ensure_dirs()
    secure_data_perms()



def _enabled_int(v):
    """原库 enabled 是 0/1 整数，前端按原样渲染。这里保持返回 int，不返回 bool。"""
    return 1 if v else 0



# --------------------------------------------------------------------------- 配置项

def get_meta(key, default=None, con=None):
    row = SystemSetting.objects.filter(k=key).values_list('v', flat=True).first()
    if row is None:
        return copy.deepcopy(DEFAULT_CFG.get(key, default))
    try:
        return json.loads(row)
    except Exception:
        return row



def set_meta(key, value, con=None):
    SystemSetting.objects.update_or_create(
        k=key, defaults={'v': json.dumps(value, ensure_ascii=False)})



def cfg():
    return {k: get_meta(k) for k in DEFAULT_CFG}
