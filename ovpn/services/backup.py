# ovpn/services/backup.py
"""备份恢复门面（过渡实现）。

当前仍复用 `system/fullbackup.py` 的 tar.gz 打包与 manifest 校验：它内部通过
`system.core` 的 SQLite 接口做整库热备，属于特权/系统边界动作，不在 Django 源码根里
直接持有数据库连接。Web 进程里 `system.core` 的数据访问函数已被替换成 ORM 版本，
但 `fullbackup` 走的是 sqlite3 的备份 API（只读源库），与 Django 的 ORM 连接互不干扰。

下一步要把 `web/` 部分改成「导出 ovpn 业务模型的 JSON」（web/ovpn-data.json），
恢复时在 transaction.atomic() 内回灌，且不覆盖 core_user / django_session /
审计 / jobs。届时本模块只保留 openvpn/ 目录与 manifest 的处理。
"""
from system import fullbackup as _impl  # noqa: E402

# 常量（KIND / FORMAT / FULL / MANIFEST…）与函数全部透传，保持与原模块同名可调用
for _k, _v in list(vars(_impl).items()):
    if not _k.startswith('__'):
        globals()[_k] = _v
