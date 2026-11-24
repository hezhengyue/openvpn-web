"""测试发现根固定为 Django 源码根（BASE_DIR）。

背景：本项目源码在仓库的 ``django/`` 子目录下，而 ``system/`` ``tests/`` 等
仍留在仓库根。Django ``manage.py test``（不带 app 标签）的行为是：

    test_labels or ["."]  ->  对 "." 做 unittest.discover(start_dir=".")

也就是说**扫描起点是当前工作目录**，同一个命令在不同 cwd 下跑出的用例数完全不同：

    cd django && python manage.py test   ->  172 项（全部业务用例）
    python django/manage.py test         ->    4 项（只有 system/tests）

后者会「静默通过」，在 CI 或人工验收里极易被误判为全套通过。``top_level``
参数只管模块名的相对基准，改不了扫描起点，所以这里直接重写 ``build_suite``，
把「未指定标签」翻译成 ``BASE_DIR`` 这个绝对路径标签，任何 cwd 都得到同一套用例。

特例：``system/tests`` 依赖 root 权限与真实 openvpn 二进制，仍由
``python -m unittest system.tests.test_fullbackup_restore`` 单独执行；
它不在 BASE_DIR 之下，本来也不该进 Django 测试发现范围。显式传标签时行为不变，
例如 ``manage.py test ovpn`` 依然只跑 ovpn。
"""

from django.conf import settings
from django.test.runner import DiscoverRunner


class BaseDirDiscoverRunner(DiscoverRunner):
    """无标签时从 BASE_DIR 开始发现的 DiscoverRunner。"""

    def build_suite(self, test_labels=None, **kwargs):
        labels = list(test_labels or [])
        # "." 等价于 cwd，是最容易被误用的默认值；替换成源码根的绝对路径。
        if not labels or labels == ["."]:
            labels = [str(settings.BASE_DIR)]
        return super().build_suite(labels, **kwargs)
