import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone


class AuditLogImmutable(Exception):
    """审计记录不可变：任何修改或删除尝试都抛出此异常。"""


class AuditEventType(models.TextChoices):
    """强约束事件分类：business / security / system，数据库 CHECK 同步拦截。"""

    BUSINESS = 'business', '业务操作'
    SECURITY = 'security', '安全事件'
    SYSTEM = 'system', '系统事件'


class AuditAction:
    """标准动作常量（弱约束）：供复用，业务派生动词不受限制。"""

    CREATE = 'create'
    UPDATE = 'update'
    DELETE = 'delete'
    BULK_DELETE = 'bulk_delete'
    LOGIN = 'login'
    LOGOUT = 'logout'
    LOGIN_FAILED = 'login_failed'
    LOCKOUT = 'lockout'
    PASSWORD_CHANGE = 'password_change'
    PERMISSION_CHANGE = 'permission_change'
    EXPORT = 'export'
    EXECUTE = 'execute'
    EXPIRE = 'expire'


AUDIT_MODULE = '审计'
AUTH_MODULE = '认证'


class AuditLogQuerySet(models.QuerySet):
    """审计记录查询集：update/bulk_update/delete 及 upsert 一律禁止。

    唯一合法的删除通道是 expire_audit_logs()，它直接调父类 delete 绕过拦截，
    并写一条 (system, 审计, expire) 自留痕。
    """

    def update(self, **kwargs):
        raise AuditLogImmutable('审计记录不可 update')

    def bulk_update(self, *args, **kwargs):
        raise AuditLogImmutable('审计记录不可 bulk_update')

    def bulk_create(self, objs, *args, **kwargs):
        # update_conflicts=True 是 UPDATE 伪装成 INSERT，能改写旧审计记录。
        if kwargs.get('update_conflicts'):
            raise AuditLogImmutable('审计记录不可 bulk_create(update_conflicts=True)')
        return super().bulk_create(objs, *args, **kwargs)

    def delete(self):
        raise AuditLogImmutable('审计记录不可 delete，清理请用 expire_audit_logs()')

    def expirable(self, before):
        """过期候选集合：仅豁免审计模块自己的 expire 自留痕。

        三元组同时命中 (system, 审计, expire) 才豁免，业务层自己写的
        (business, 令牌, expire) 仍按普通记录清理。
        """
        return self.filter(created_at__lt=before).exclude(
            event_type=AuditEventType.SYSTEM,
            module=AUDIT_MODULE,
            action=AuditAction.EXPIRE,
        )

    def expire_audit_logs(self, *, before=None, days=None):
        """清理过期审计记录，并留一条 (system, 审计, expire) 自留痕。

        绕过本 QuerySet 的 delete 拦截走底层删除；自留痕本身排除在清理范围外，
        避免下次清理把自己的痕迹吃掉。返回实际删除条数。
        """
        if before is None:
            days = getattr(settings, 'AUDIT_RETENTION_DAYS', 180) if days is None else days
            days = int(days)
            if days < 0:
                raise ValueError('审计保留天数 days 不能为负数')
            before = timezone.now() - timedelta(days=days)
        qs = self.expirable(before)
        # 删除与留痕放在同一事务：留痕失败时回滚删除，避免出现「日志已删但无清理记录」。
        with transaction.atomic():
            # 直接调父类实现，绕过子类 future 的 delete 拦截。
            deleted, _ = models.QuerySet.delete(qs)

            # 局部导入避免 services -> models 循环导入。
            from .services import audit_log
            entry = audit_log(
                action=AuditAction.EXPIRE, event_type=AuditEventType.SYSTEM,
                module=AUDIT_MODULE,
                message=f'清理 {deleted} 条 {before:%Y-%m-%d %H:%M} 之前的审计记录',
                extra={'deleted': deleted, 'before': before.isoformat()},
            )
            if entry is None:
                raise RuntimeError('审计清理留痕失败，已回滚本次清理')
        return deleted


class AuditLog(models.Model):
    """统一操作审计记录。

    设计要点：
    - actor 可为空（系统任务 / 匿名请求），actor_name 保存操作人快照；
    - 对象用 object_type + object_id 记录，不使用 GenericForeignKey；
    - changes / extra 用 JSONField，敏感字段在写入前统一脱敏；
    - 不记录密码、Token、Secret、Cookie。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(default=timezone.now, db_index=True, verbose_name='操作时间')
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
        db_index=True,
        verbose_name='操作人',
    )
    actor_name = models.CharField(max_length=150, blank=True, default='', db_index=True, verbose_name='操作人快照')
    event_type = models.CharField(
        max_length=20,
        choices=AuditEventType.choices,
        default=AuditEventType.BUSINESS,
        verbose_name='事件分类',
    )
    # event_type / action / module / object_type 均已落入下面的复合索引，
    # 不再单独建索引，避免每次写审计多维护无用 B-Tree。
    action = models.CharField(max_length=50, verbose_name='动作')
    module = models.CharField(max_length=100, blank=True, default='', verbose_name='模块')
    object_type = models.CharField(max_length=100, blank=True, default='', verbose_name='对象类型')
    object_id = models.CharField(max_length=64, blank=True, default='', db_index=True, verbose_name='对象 ID')
    object_repr = models.CharField(max_length=255, blank=True, default='', verbose_name='对象名称')
    remote_addr = models.CharField(max_length=64, blank=True, default='', verbose_name='来源 IP')
    request_id = models.CharField(max_length=64, blank=True, default='', db_index=True, verbose_name='请求 ID')
    changes = models.JSONField(default=dict, blank=True, verbose_name='变更内容')
    message = models.TextField(blank=True, default='', verbose_name='操作描述')
    extra = models.JSONField(default=dict, blank=True, verbose_name='扩展信息')

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        verbose_name = '操作审计'
        verbose_name_plural = '操作审计'
        ordering = ['-created_at', '-id']
        indexes = [
            # 最左前缀要匹配最常用的过滤列：先 action 再时间，
            # 让 WHERE action=? ORDER BY created_at DESC 吃得到索引。
            models.Index(fields=['action', 'created_at'], name='audit_action_created_idx'),
            models.Index(fields=['module', 'created_at'], name='audit_module_created_idx'),
            # object_type / object_id 几乎总是联合查，合并为单一复合索引。
            models.Index(fields=['object_type', 'object_id', 'created_at'], name='audit_obj_created_idx'),
            models.Index(fields=['event_type', 'created_at'], name='audit_type_created_idx'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(event_type__in=AuditEventType.values),
                name='audit_event_type_valid',
            ),
        ]

    def save(self, *args, **kwargs):
        # 仅放行新增，且强制 INSERT：即使有人构造 AuditLog(id=旧主键).save()，
        # _state.adding 也是 True，此时靠 force_insert 撞 PK 抛 IntegrityError，
        # 而不是让 Django 查到旧行后执行 UPDATE 覆盖历史。
        if not self._state.adding:
            raise AuditLogImmutable('审计记录不可修改')
        if kwargs.get('force_update'):
            raise AuditLogImmutable('审计记录不可 force_update')
        kwargs.pop('force_insert', None)
        return super().save(*args, force_insert=True, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditLogImmutable('审计记录不可删除，清理请用 expire_audit_logs()')

    def __str__(self):
        return f'{self.created_at:%Y-%m-%d %H:%M:%S} {self.actor_name or "-"} {self.action} {self.object_repr}'
