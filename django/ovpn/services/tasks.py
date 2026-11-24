# ovpn/services/tasks.py
"""长任务状态

长任务（向导 / 上传 / 编译）的状态表读写。
"""
from .base import *          # noqa: F401,F403  共享命名空间（常量/纯逻辑/ORM 模型）




# --------------------------------------------------------------------------- 长任务

def task_create(tid, kind, actor_name="admin", msg="排队中"):
    OvpnTask.objects.create(id=tid, kind=kind, status="running", progress=0,
                            msg=msg, created_at=now(), actor=actor_name)



def task_update(tid, **kw):
    OvpnTask.objects.filter(pk=tid).update(**kw)



def task_get(tid):
    row = OvpnTask.objects.filter(pk=tid).values().first()
    return row

# 只导出本模块自己实现的函数：`from .base import *` 带进来的同名函数
# （如 base 继承的 list_users）不能借 import * 反向覆盖别的子模块。
__all__ = [
    "task_create",
    "task_update",
    "task_get",
]
