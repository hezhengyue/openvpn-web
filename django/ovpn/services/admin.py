# ovpn/services/admin.py
"""管理账户

管理账户：救场重置凭据（Django Auth 接管后只保留入口）。
"""
from .base import *          # noqa: F401,F403  共享命名空间（常量/纯逻辑/ORM 模型）
from .audit import log_audit




# --------------------------------------------------------------------------- 管理账户（Django Auth 接管）

def admin_reset_credentials(*args, **kwargs):
    """重新生成一组管理凭据（账户名 + 一次性明文密码）。

    口令本体由调用方写进 Django User（`ovpn_cli reset-admin` 或后台按钮），
    这里只负责生成、落「管理账户名」标记、清掉首登强改并写救场凭据文件。
    """
    name = "admin-" + secrets.token_hex(3)
    pw = rand_password(18)
    set_meta("admin_user", name)
    set_meta("admin_must_change", 0)
    log_audit("admin", "admin.reset", "管理账户与密码已重新生成")
    # 第一行必须是纯密码：bootstrap 只取第一行
    try:
        with open(os.path.join(DATA, "admin_password.txt"), "w") as f:
            f.write(pw + "\n")
            f.write("# 管理账户: %s（重置于 %s）\n" % (name, now()))
        os.chmod(os.path.join(DATA, "admin_password.txt"), 0o600)
    except Exception:
        pass
    return {"username": name, "password": pw}

# 只导出本模块自己实现的函数：`from .base import *` 带进来的同名函数
# （如 base 继承的 list_users）不能借 import * 反向覆盖别的子模块。
__all__ = [
    "admin_reset_credentials",
]
