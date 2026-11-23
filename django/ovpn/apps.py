from django.apps import AppConfig


# 纳入 auditlog 留痕的模型：都是「管理员会改、且改了必须能追溯谁改的」业务对象。
# 刻意不登记这几类：
#   SystemSetting —— 登录一次就写 admin_user，日志会被刷满；
#   AuditRecord / AuthLog —— 它们本身就是日志，再包一层没有意义；
#   OvpnTask / BackupRecord —— 高频状态流转，属于运行日志不是人工变更。
_AUDITED_MODELS = (
    'VpnUser',
    'AssetGroup',
    'AssetItem',
    'UserGroup',
    'UserGroupMember',
    'AclGrant',
    'PeerGrant',
)


class OvpnConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ovpn'
    verbose_name = 'OpenVPN 管理'

    def ready(self):
        # 启动时初始化 core 数据库 + 引导管理账号（Django Auth）
        # 延后到 ready，避免在 settings 加载阶段访问 ORM
        from . import bootstrap  # noqa: F401
        self._register_auditlog()

    def _register_auditlog(self):
        """把业务模型的增删改记进 auditlog_logentry。

        与自建 `audit` 表的分工：`audit` 记「业务动作」（登录、下发规则、跑任务），
        是给 SPA 的日志页看的；auditlog 记「ORM 层面的字段级变更」，是给后台追溯
        「谁在哪一刻把这行改成什么」用的。两者互补，不重复。
        """
        try:
            from auditlog.registry import auditlog
            from . import models
        except Exception:
            return
        for name in _AUDITED_MODELS:
            model = getattr(models, name, None)
            if model is None:
                continue
            try:
                auditlog.register(model)
            except Exception:
                # 重复注册（如测试里多次 ready）不算错误
                pass
