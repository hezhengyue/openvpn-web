from django.apps import AppConfig


class OvpnConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ovpn'
    verbose_name = 'OpenVPN 管理'

    def ready(self):
        # 启动时初始化 core 数据库 + 引导管理账号（Django Auth）
        # 延后到 ready，避免在 settings 加载阶段访问 ORM
        from . import bootstrap  # noqa: F401
