# ovpn/views/__init__.py
"""REST 视图层（按业务子域拆分）。

state / wizard / users / acl / settings_view / updates / backups / pages 各自只做
「解析请求 → 调 services/selectors → 封装响应」，跨子域的聚合查询在
ovpn/selectors.py。urls.py 统一从本包按名字取视图，所以这里要把它们全部导出。
"""
from .state import *  # noqa: F401,F403
from .wizard import *  # noqa: F401,F403
from .users import *  # noqa: F401,F403
from .acl import *  # noqa: F401,F403
from .settings_view import *  # noqa: F401,F403
from .updates import *  # noqa: F401,F403
from .backups import *  # noqa: F401,F403
from .pages import *  # noqa: F401,F403

# 下划线开头的内部函数不会被 import * 带出，urls.py 不引用它们，这里只补
# 需要跨模块使用的两个（保持 __all__ 之外的历史可导入性）。
from .state import _server_desc  # noqa: F401
from .wizard import _install_rollback  # noqa: F401
