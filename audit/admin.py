"""审计后台：AuditModelAdmin 供业务 Admin 继承；AuditLogAdmin 严格只读。"""
from django.contrib import admin

from .models import AuditAction, AuditEventType, AuditLog
from .services import audit_log, export_audit_csv
from .utils import m2m_changes, model_to_changes, object_info


class AuditModelAdmin(admin.ModelAdmin):
    """业务 ModelAdmin 基类：自动记录 create/update/delete/bulk_delete。

    用法：
        from audit.admin import AuditModelAdmin

        @admin.register(MyModel)
        class MyModelAdmin(AuditModelAdmin):
            audit_exclude_fields = ('password', 'secret')
    """

    audit_exclude_fields = ('password', 'last_login')
    audit_module = None

    def _module_name(self):
        return str(self.audit_module or self.model._meta.verbose_name)

    def save_model(self, request, obj, form, change):
        changed = list(getattr(form, 'changed_data', []) or [])
        old_obj = None
        if change and obj.pk:
            old_obj = type(obj)._default_manager.filter(pk=obj.pk).first()
        # save_related 阶段才写 M2M；M2M manager 是延迟查询，必须在改动前
        # 把旧 pk 集合物化下来，否则对比时两边查到的都是新值。
        request._audit_m2m_old_pks = {
            f.name: set(getattr(old_obj, f.name).values_list('pk', flat=True))
            for f in self.model._meta.many_to_many
        } if old_obj is not None else {}
        super().save_model(request, obj, form, change)

        module = self._module_name()
        all_changes = model_to_changes(old_obj, obj, changed, self.audit_exclude_fields)
        if not change:
            # 新建只写一条 business/create，changes 保留全部标量变更。
            audit_log(
                request=request, action=AuditAction.CREATE,
                event_type=AuditEventType.BUSINESS, module=module, instance=obj,
                changes=all_changes,
                message=f'{AuditAction.CREATE} {self.model._meta.verbose_name}',
                extra={'admin': True},
            )
            return

        # 密码变更由 audit.signals 的 pre_save 统一捕获（含自助改密/代码改密），
        # 此处不再用 form.changed_data——UserAdmin 的 password 是只读哈希框，永远不进 changed_data。
        # 权限标量单独拆成 security/permission_change，避免同一字段被两条日志重复记录。
        scalar_permission_fields = {'is_superuser', 'is_staff', 'is_active'}
        business_changes = {
            name: value for name, value in all_changes.items()
            if name not in scalar_permission_fields
        }
        security_changes = {
            name: value for name, value in all_changes.items()
            if name in scalar_permission_fields
        }
        if business_changes:
            audit_log(
                request=request, action=AuditAction.UPDATE,
                event_type=AuditEventType.BUSINESS, module=module, instance=obj,
                changes=business_changes,
                message=f'{AuditAction.UPDATE} {self.model._meta.verbose_name}',
                extra={'admin': True},
            )
        if security_changes:
            audit_log(
                request=request, action=AuditAction.PERMISSION_CHANGE,
                event_type=AuditEventType.SECURITY, module=module, instance=obj,
                changes=security_changes, message='权限被修改',
                extra={'admin': True, 'fields': sorted(security_changes)},
            )

    def save_related(self, request, form, formsets, change):
        """M2M 在 save_related 阶段才落库，前后对比 pks 记录真实增删。"""
        super().save_related(request, form, formsets, change)
        # 新建对象勾选 M2M 时 old_pks_map 为空，有勾选自然记 added；
        # 没有勾选时 delta 为空，不会多写日志，因此无需跳过 change=False。
        obj = form.instance
        old_pks_map = getattr(request, '_audit_m2m_old_pks', {})
        m2m_fields = [f.name for f in self.model._meta.many_to_many]
        collected = {}
        for name in m2m_fields:
            delta = m2m_changes(old_pks_map.get(name, set()), obj, name, self.audit_exclude_fields)
            if delta:
                collected[name] = delta
        if not collected:
            return
        # 权限组/权限项按 security 记录，普通多对多按 business 记录，互不混杂。
        security_fields = {'groups', 'user_permissions'}
        security_changes = {
            name: delta for name, delta in collected.items()
            if name in security_fields
        }
        business_changes = {
            name: delta for name, delta in collected.items()
            if name not in security_fields
        }
        if security_changes:
            audit_log(
                request=request, action=AuditAction.PERMISSION_CHANGE,
                event_type=AuditEventType.SECURITY, module=self._module_name(),
                instance=obj, changes=security_changes,
                message='权限关联被修改',
                extra={'admin': True, 'm2m': True},
            )
        if business_changes:
            audit_log(
                request=request, action=AuditAction.UPDATE,
                event_type=AuditEventType.BUSINESS, module=self._module_name(),
                instance=obj, changes=business_changes,
                message='多对多关联被修改',
                extra={'admin': True, 'm2m': True},
            )

    def delete_model(self, request, obj):
        object_type, object_id, object_repr = object_info(obj)
        module = self._module_name()
        super().delete_model(request, obj)
        audit_log(
            request=request, action=AuditAction.DELETE,
            event_type=AuditEventType.BUSINESS, module=module,
            object_type=object_type, object_id=object_id, object_repr=object_repr,
            message=f'delete {self.model._meta.verbose_name}', extra={'admin': True},
        )

    def delete_queryset(self, request, queryset):
        module = self._module_name()
        # 总数走 COUNT(*)，只把前 50 条物化成快照，避免大批量删除时全量拉实例进内存。
        total = queryset.count()
        snapshots = [object_info(obj) for obj in queryset[:50]]
        super().delete_queryset(request, queryset)
        audit_log(
            request=request, action=AuditAction.BULK_DELETE,
            event_type=AuditEventType.BUSINESS, module=module,
            message=f'批量删除 {total} 条',
            extra={
                'admin': True,
                'count': total,
                'objects': [
                    {'object_type': t, 'object_id': i, 'object_repr': r}
                    for t, i, r in snapshots
                ],
            },
        )


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """审计记录后台：只读 + 筛选 + 分页，不允许任何人改历史。"""

    list_display = (
        'created_at', 'actor_name', 'event_type', 'action', 'module',
        'object_repr', 'remote_addr', 'short_message',
    )
    list_filter = ('event_type', 'action', 'module', 'created_at')
    search_fields = (
        'actor_name', 'action', 'module', 'object_type',
        'object_id', 'object_repr', 'message', 'request_id', 'remote_addr',
    )
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    list_per_page = 50
    readonly_fields = [field.name for field in AuditLog._meta.fields]
    actions = ['export_selected_csv']

    @admin.action(description='导出选中记录为 CSV')
    def export_selected_csv(self, request, queryset):
        return export_audit_csv(queryset, request=request)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('actor')

    @admin.display(description='操作描述')
    def short_message(self, obj):
        return obj.message[:80] + ('…' if len(obj.message) > 80 else '')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
