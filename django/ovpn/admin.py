# ovpn/admin.py
"""OpenVPN 业务台账接入 Django Admin（只读为主）。

设计原则：**后台不是第二个写入口**。

这些表的写入几乎都带副作用——新建账号要签证书、写 ccd、下发路由；改资产或
授权要重算 iptables；改端口要重写 server.conf 并重载 OpenVPN。Django Admin 的
`save_model` 只会写库，不会触发那一串动作，所以在这里放开编辑等于亲手制造
「库里改了、实际规则没变」的漂移。

因此：
  * 全部模型默认只读（可浏览、可搜索、可筛选）；
  * `VpnUser` 只放开不产生副作用的四个字段：姓名 / 部门 / 备注 / 有效期；
  * 新增与删除一律禁止，统一走管理端 SPA 的接口（那里才有证书与规则重算）。

要判断「该不该用 admin」时，这就是分界线：**纯展示与检索用 admin 很划算，
带副作用的写入留在业务接口**。
"""
from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import (AclGrant, AssetGroup, AssetItem, AuditRecord, AuthLog,
                     BackupRecord, OvpnTask, PeerGrant, SystemSetting,
                     UserGroup, UserGroupMember, VpnUser)


class ReadOnlyAdmin(admin.ModelAdmin):
    """只读台账：能看、能搜、能筛，不能增删改。"""

    actions = None  # 去掉「删除所选」这类批量动作

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(VpnUser)
class VpnUserAdmin(admin.ModelAdmin):
    """VPN 账号台账。

    只允许改「资料类」字段；用户名、固定 IP、启用状态、密码都必须走 SPA——
    它们牵动证书、ccd 文件和路由，光改库不会生效。
    """

    list_display = ('username', 'vpn_ip', 'real_name', 'dept', 'enabled',
                    'expires_at', 'updated_at')
    list_filter = ('enabled', 'dept')
    search_fields = ('username', 'vpn_ip', 'real_name', 'dept', 'note')
    ordering = ('vpn_ip',)
    list_per_page = 50

    fields = ('username', 'vpn_ip', 'enabled', 'real_name', 'dept',
              'expires_at', 'note', 'password_state', 'created_at', 'updated_at')
    readonly_fields = ('username', 'vpn_ip', 'enabled', 'created_at',
                       'updated_at', 'password_state')

    @admin.display(description=_('密码状态'))
    def password_state(self, obj):
        if not obj or not obj.pk:
            return '-'
        bits = []
        if obj.password_hash:
            bits.append('哈希已设置')
        if obj.password_plain:
            bits.append('含明文（便于交付）')
        return '、'.join(bits) or '未设置'

    def has_add_permission(self, request):
        # 新增要走 SPA：签证书 + 建 ccd + 下发路由都在那边
        return False

    def has_delete_permission(self, request, obj=None):
        # 删除同理：要回收证书与规则，统一走 SPA 的「停用/删除」
        return False

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        changed = ', '.join(form.changed_data) or '无字段变化'
        try:
            from .bridge import core
            core.log_audit(request.user.username, 'admin.user.edit',
                           '后台修改账号 %s 资料（%s）' % (obj.username, changed))
        except Exception:
            pass


@admin.register(AssetGroup)
class AssetGroupAdmin(ReadOnlyAdmin):
    list_display = ('name', 'item_count', 'note')
    search_fields = ('name', 'note')
    ordering = ('name',)

    @admin.display(description=_('条目数'))
    def item_count(self, obj):
        return obj.items.count()


@admin.register(AssetItem)
class AssetItemAdmin(ReadOnlyAdmin):
    list_display = ('group', 'cidr', 'proto', 'ports', 'note')
    list_filter = ('group', 'proto')
    search_fields = ('cidr', 'ports', 'note')
    ordering = ('group_id', 'cidr')


@admin.register(UserGroup)
class UserGroupAdmin(ReadOnlyAdmin):
    list_display = ('name', 'member_count', 'note')
    search_fields = ('name', 'note')

    @admin.display(description=_('成员数'))
    def member_count(self, obj):
        return obj.member_rows.count()


@admin.register(UserGroupMember)
class UserGroupMemberAdmin(ReadOnlyAdmin):
    list_display = ('group', 'username')
    list_filter = ('group',)
    search_fields = ('username',)


@admin.register(AclGrant)
class AclGrantAdmin(ReadOnlyAdmin):
    list_display = ('subject_type', 'subject', 'asset_group', 'expires_at', 'note')
    list_filter = ('subject_type', 'asset_group')
    search_fields = ('subject', 'note')
    ordering = ('subject_type', 'subject')


@admin.register(PeerGrant)
class PeerGrantAdmin(ReadOnlyAdmin):
    list_display = ('src_username', 'dst_username', 'proto', 'ports',
                    'expires_at', 'note')
    list_filter = ('proto',)
    search_fields = ('src_username', 'dst_username', 'note')


@admin.register(SystemSetting)
class SystemSettingAdmin(ReadOnlyAdmin):
    list_display = ('k', 'short_value')
    search_fields = ('k', 'v')
    ordering = ('k',)

    @admin.display(description=_('值'))
    def short_value(self, obj):
        v = obj.v or ''
        return v if len(v) <= 80 else v[:80] + '…'


@admin.register(AuditRecord)
class AuditRecordAdmin(ReadOnlyAdmin):
    list_display = ('ts', 'actor', 'action', 'ip', 'short_detail')
    list_filter = ('action',)
    search_fields = ('actor', 'action', 'detail', 'ip')
    ordering = ('-id',)
    list_per_page = 100

    @admin.display(description=_('详情'))
    def short_detail(self, obj):
        return (obj.detail or '')[:60]


@admin.register(AuthLog)
class AuthLogAdmin(ReadOnlyAdmin):
    list_display = ('ts', 'username', 'src_ip', 'result', 'short_reason')
    list_filter = ('result',)
    search_fields = ('username', 'src_ip', 'reason')
    ordering = ('-id',)
    list_per_page = 100

    @admin.display(description=_('原因'))
    def short_reason(self, obj):
        return (obj.reason or '')[:60]


@admin.register(OvpnTask)
class OvpnTaskAdmin(ReadOnlyAdmin):
    list_display = ('id', 'kind', 'status', 'progress', 'actor',
                    'created_at', 'finished_at')
    list_filter = ('status', 'kind')
    search_fields = ('id', 'kind', 'actor', 'msg')
    ordering = ('-created_at',)


@admin.register(BackupRecord)
class BackupRecordAdmin(ReadOnlyAdmin):
    list_display = ('ts', 'kind', 'path', 'note')
    list_filter = ('kind',)
    search_fields = ('path', 'note')
    ordering = ('-id',)
