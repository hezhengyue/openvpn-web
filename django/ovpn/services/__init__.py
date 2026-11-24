# ovpn/services/__init__.py
"""OpenVPN 业务门面（对外唯一入口）。

按业务子域拆成多个模块，禁止再出现巨型 `core.py`：

    base.py      基础设施：system.core 纯逻辑继承、meta 配置、预览缓存
    audit.py     审计与认证日志
    users.py     VPN 账号（含到期停用、自助改密）
    userdata.py  台账备份 / 快照 / Excel 全量同步
    assets.py    资产组与条目
    groups.py    用户组
    grants.py    ACL 授权与客户端互访
    tasks.py     长任务状态
    admin.py     管理账户凭据重置

调用方约定：
    from . import services        # 本包；services.list_users(...)
    from .services import users   # 只在确实要用子域时

system.core 的纯逻辑只继承一份（base.py），数据访问全部用 Django ORM。导入本包
时会把 ORM 版本就地 patch 回 system.core —— system.core 内部那些互相调用的纯函数
（online_users / allow_list / users_rows …）在 Web 进程里因此也走 ORM，而 worker /
auth-verify / setup 是独立进程，各自的 sqlite3 实现互不影响。
"""
from .base import *          # noqa: F401,F403
from .audit import *         # noqa: F401,F403
from .users import *         # noqa: F401,F403
from .userdata import *      # noqa: F401,F403
from .assets import *        # noqa: F401,F403
from .groups import *        # noqa: F401,F403
from .grants import *        # noqa: F401,F403
from .tasks import *         # noqa: F401,F403
from .admin import *         # noqa: F401,F403

# `import *` 不带下划线名；patch 列表与调用方（bridge/views/auth/middleware）需要它们。
from .base import _base                                    # noqa: F401
# 继承自 system.core 的私有工具（进程存活探测、口令提示文件），对外仍按 core._xxx 调用
from .base import _vpn_alive, _write_admin_password_note   # noqa: F401
# 预览缓存键前缀与审计 JSONL 文件名：模块级常量，供测试与运维脚本引用
from .base import _PREVIEW_CACHE_PREFIX, _PREVIEW_TTL      # noqa: F401
from .audit import AUDIT_JSONL                             # noqa: F401
from .audit import _audit_module, _audit_rows_from_db, _audit_rows_from_jsonl  # noqa: F401
from .users import _add_user                               # noqa: F401
from .userdata import _apply_import                        # noqa: F401
from .assets import _asset_group_map, _find_dup_item, _insert_item  # noqa: F401
from .groups import _clean_members                         # noqa: F401

# --------------------------------------------------------------------------- 打通继承链
# system.core 里继承来的纯函数（online_users / stale_ip_users / allow_list /
# set_allow_list / users_rows …）内部是按「自己模块的全局名」调用 get_meta /
# list_users / log_audit 的。只把函数复制到本包不会改变它们的查找路径，
# 所以这里把 system.core 里那批数据访问函数就地替换成 ORM 版本：
#   * 纯逻辑只有一份（system/core.py），不复制；
#   * Web 进程里任何人调 services.xxx() 走的都是 ORM；
#   * system/worker.py、auth-verify.py、setup.py 是独立进程，各自仍是 SQLite 实现。
_PATCH_INTO_BASE = [
    "init_db", "get_meta", "set_meta", "cfg",
    "log_audit", "log_auth", "get_logs", "expire_users",
    "list_users", "next_free_ip", "validate_vpn_ip",
    "add_user", "_add_user", "update_user", "delete_user",
    "user_by_vpn_ip", "self_change_password",
    "backup_users", "prune_user_backups", "snapshot", "restore_snapshot", "list_backups",
    "preview_import", "apply_import", "_apply_import",
    "list_asset_groups", "_asset_group_map", "_find_dup_item", "_insert_item",
    "add_asset_group", "update_asset_group", "delete_asset_group",
    "add_asset_items", "add_asset_item", "update_asset_item", "delete_asset_item",
    "list_user_groups", "_clean_members", "add_user_group", "update_user_group",
    "delete_user_group",
    "list_grants", "add_grant", "delete_grant", "active_grants", "expand_grants",
    "list_peer_grants", "add_peer_grant", "delete_peer_grant",
]
for _name in _PATCH_INTO_BASE:
    setattr(_base, _name, globals()[_name])
