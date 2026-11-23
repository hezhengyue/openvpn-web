# core/admin.py
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _

from audit.admin import AuditModelAdmin

from .models import User

admin.site.site_header = _('后台管理')
admin.site.site_title = _('后台管理')
admin.site.index_title = _('控制台')


@admin.register(User)
class UserAdmin(AuditModelAdmin, BaseUserAdmin):
    """管理账号后台。

    继承 AuditModelAdmin：新增 / 改资料 / 改权限分别落 business 与 security 两类
    审计；密码变更由 audit.signals 的 pre_save 统一捕获，这里不重复记。
    """

    audit_module = '管理账号'

    list_display = ('username', 'email', 'phone', 'is_staff', 'is_active', 'date_joined')
    list_filter = ('is_staff', 'is_superuser', 'is_active', 'groups', 'date_joined')
    search_fields = ('username', 'email', 'phone', 'first_name', 'last_name')
    ordering = ('username',)

    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        (_('Personal info'), {'fields': ('first_name', 'last_name', 'email', 'phone')}),
        (_('Permissions'), {
            'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions'),
        }),
        (_('Important dates'), {'fields': ('last_login', 'date_joined')}),
    )

    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'email', 'phone', 'password1', 'password2'),
        }),
    )

    readonly_fields = ('last_login', 'date_joined')

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        form.base_fields['phone'].required = False
        return form
