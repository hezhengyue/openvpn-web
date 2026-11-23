from django.apps import AppConfig


class AuditConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'audit'
    verbose_name = '统一审计'

    def ready(self):
        # 认证与安全事件钩子统一在这里接线，业务代码不感知。
        from . import signals  # noqa: F401
