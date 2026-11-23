# ovpn/models.py
"""OpenVPN 业务模型。

对应原 Flask 版 `core.py` 里用裸 SQL 维护的 14 张表。表名、列名全部沿用原库，
迁移用 `migrate --fake-initial` 对齐现网数据，不重建、不丢数据。

约定：
  * 时间列沿用原来的 `YYYY-MM-DD HH:MM:SS` 文本（前端直接展示，不做本地化/时区换算），
    所以是 CharField 而不是 DateTimeField；文本格式按字典序排序等价于按时间排序。
  * 账号主键 `username` 直接沿用原表的主键（ccd 文件名、ACL 主体、组成员都靠它），
    改名是一次主键更新，不是删旧建新。
  * 与后台管理员 `core.User` 严格分离：这里是「VPN 拨号账号」，不是 Django 登录账号。
"""
from django.db import models
from django.utils.translation import gettext_lazy as _


class SystemSetting(models.Model):
    """原 `meta` 键值表：配置项、指纹、首登标记、管理账户名都存在这里。"""

    k = models.CharField(_('键'), max_length=64, primary_key=True)
    v = models.TextField(_('值'), blank=True, default='')

    class Meta:
        db_table = 'meta'
        verbose_name = _('系统配置')
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.k


class VpnUser(models.Model):
    """VPN 隧道拨号账号（一人一账号一固定 IP），与后台登录账号无关。"""

    username = models.CharField(_('用户名'), max_length=64, primary_key=True)
    vpn_ip = models.CharField(_('固定 IP'), max_length=45, unique=True)
    enabled = models.BooleanField(_('启用'), default=True)
    password_hash = models.TextField(_('密码哈希'))
    note = models.TextField(_('备注'), blank=True, default='')
    real_name = models.CharField(_('姓名'), max_length=64, blank=True, default='')
    dept = models.CharField(_('部门'), max_length=64, blank=True, default='')
    created_at = models.CharField(_('创建时间'), max_length=32, blank=True, default='')
    updated_at = models.CharField(_('更新时间'), max_length=32, blank=True, default='')
    password_plain = models.TextField(_('明文密码'), blank=True, default='')
    expires_at = models.CharField(_('有效期'), max_length=32, blank=True, default='')

    class Meta:
        db_table = 'users'
        verbose_name = _('VPN 账号')
        verbose_name_plural = verbose_name
        ordering = ['vpn_ip']

    def __str__(self):
        return '%s(%s)' % (self.username, self.vpn_ip)


class AssetGroup(models.Model):
    """资产组（容器）：一个组可装多个「目标/协议/端口」条目，授权按组给。"""

    name = models.CharField(_('名称'), max_length=80, unique=True)
    note = models.TextField(_('备注'), blank=True, default='')

    class Meta:
        db_table = 'asset_groups'
        verbose_name = _('资产组')
        verbose_name_plural = verbose_name
        ordering = ['name']

    def __str__(self):
        return self.name


class AssetItem(models.Model):
    """资产条目：真正写进 iptables 的目标/协议/端口。"""

    group = models.ForeignKey(
        AssetGroup, on_delete=models.CASCADE,
        db_column='group_id', related_name='items', verbose_name=_('资产组'))
    cidr = models.CharField(_('目标'), max_length=64)
    proto = models.CharField(_('协议'), max_length=8, default='tcp')
    ports = models.CharField(_('端口'), max_length=128, blank=True, default='')
    note = models.TextField(_('备注'), blank=True, default='')

    class Meta:
        db_table = 'asset_items'
        verbose_name = _('资产条目')
        verbose_name_plural = verbose_name
        ordering = ['group_id', 'cidr', 'ports']
        indexes = [models.Index(fields=['group'], name='idx_items_group')]

    def __str__(self):
        return '%s %s %s' % (self.cidr, self.proto, self.ports)


class UserGroup(models.Model):
    """用户组：把若干账号划成一组，授权时按组给，加减人只改成员表。"""

    name = models.CharField(_('名称'), max_length=80, unique=True)
    note = models.TextField(_('备注'), blank=True, default='')

    class Meta:
        db_table = 'user_groups'
        verbose_name = _('用户组')
        verbose_name_plural = verbose_name
        ordering = ['name']

    def __str__(self):
        return self.name


class UserGroupMember(models.Model):
    """用户组成员。

    `username` 刻意用 CharField 而不是外键：账号改名在原实现里是一次
    「所有引用一起搬」的事务操作，用字符串列可以直接 UPDATE，语义与表结构都
    与现网一致；改成外键后主键改名会变成「删旧行 + 建新行」，反而更危险。
    """

    group = models.ForeignKey(
        UserGroup, on_delete=models.CASCADE,
        db_column='group_id', related_name='member_rows', verbose_name=_('用户组'))
    username = models.CharField(_('用户名'), max_length=64)

    class Meta:
        db_table = 'user_group_members'
        verbose_name = _('用户组成员')
        verbose_name_plural = verbose_name
        unique_together = (('group', 'username'),)
        indexes = [models.Index(fields=['group'], name='idx_members_group')]

    def __str__(self):
        return '%s/%s' % (self.group_id, self.username)


class AclGrant(models.Model):
    """授权：主体（单个账号 / 用户组）× 资产组。"""

    SUBJECT_USER = 'user'
    SUBJECT_GROUP = 'group'
    SUBJECT_CHOICES = [(SUBJECT_USER, _('账号')), (SUBJECT_GROUP, _('用户组'))]

    subject_type = models.CharField(
        _('主体类型'), max_length=8, choices=SUBJECT_CHOICES, default=SUBJECT_USER)
    subject = models.CharField(_('主体'), max_length=80)
    asset_group = models.ForeignKey(
        AssetGroup, on_delete=models.CASCADE,
        db_column='asset_group_id', related_name='grants', verbose_name=_('资产组'))
    expires_at = models.CharField(_('到期时间'), max_length=32, blank=True, default='')
    note = models.TextField(_('备注'), blank=True, default='')

    class Meta:
        db_table = 'acl_grants'
        verbose_name = _('访问授权')
        verbose_name_plural = verbose_name
        unique_together = (('subject_type', 'subject', 'asset_group'),)
        indexes = [models.Index(fields=['asset_group'], name='idx_aclgrants_group')]

    def __str__(self):
        return '%s:%s -> %s' % (self.subject_type, self.subject, self.asset_group_id)


class PeerGrant(models.Model):
    """客户端互访（P2P）授权。"""

    src_username = models.CharField(_('源账号'), max_length=64)
    dst_username = models.CharField(_('目标账号'), max_length=64)
    proto = models.CharField(_('协议'), max_length=8, default='any')
    ports = models.CharField(_('端口'), max_length=128, blank=True, default='')
    expires_at = models.CharField(_('到期时间'), max_length=32, blank=True, default='')
    note = models.TextField(_('备注'), blank=True, default='')

    class Meta:
        db_table = 'peer_grants'
        verbose_name = _('互访授权')
        verbose_name_plural = verbose_name
        unique_together = (('src_username', 'dst_username', 'proto', 'ports'),)

    def __str__(self):
        return '%s -> %s' % (self.src_username, self.dst_username)


class AuditRecord(models.Model):
    """操作审计（Web 侧写入）。字段与前端 `/api/logs?kind=audit` 的返回一一对应。"""

    ts = models.CharField(_('时间'), max_length=32, blank=True, default='')
    actor = models.CharField(_('操作者'), max_length=64, blank=True, default='')
    action = models.CharField(_('动作'), max_length=64, blank=True, default='')
    detail = models.TextField(_('详情'), blank=True, default='')
    ip = models.CharField(_('来源 IP'), max_length=45, blank=True, default='')

    class Meta:
        db_table = 'audit'
        verbose_name = _('操作审计')
        verbose_name_plural = verbose_name
        ordering = ['-id']
        indexes = [models.Index(fields=['-id'], name='idx_audit_id')]

    def __str__(self):
        return '%s %s' % (self.actor, self.action)


class AuthLog(models.Model):
    """拨号认证日志（认证脚本写入，Web 只读）。"""

    ts = models.CharField(_('时间'), max_length=32, blank=True, default='')
    username = models.CharField(_('用户名'), max_length=64, blank=True, default='')
    src_ip = models.CharField(_('来源 IP'), max_length=45, blank=True, default='')
    result = models.CharField(_('结果'), max_length=16, blank=True, default='')
    reason = models.TextField(_('原因'), blank=True, default='')

    class Meta:
        db_table = 'auth_log'
        verbose_name = _('认证日志')
        verbose_name_plural = verbose_name
        ordering = ['-id']
        indexes = [models.Index(fields=['-id'], name='idx_authlog_id')]

    def __str__(self):
        return '%s %s' % (self.username, self.result)


class OvpnTask(models.Model):
    """长任务台账（`/api/task/<tid>` 与日志页读它；日志正文在 data/tasks/<tid>.log）。"""

    id = models.CharField(_('任务 ID'), max_length=40, primary_key=True)
    kind = models.CharField(_('类型'), max_length=64, blank=True, default='')
    status = models.CharField(_('状态'), max_length=16, blank=True, default='')
    progress = models.IntegerField(_('进度'), default=0)
    msg = models.TextField(_('消息'), blank=True, default='')
    created_at = models.CharField(_('创建时间'), max_length=32, blank=True, default='')
    finished_at = models.CharField(_('完成时间'), max_length=32, blank=True, default='')
    actor = models.CharField(_('操作者'), max_length=64, blank=True, default='')

    class Meta:
        db_table = 'tasks'
        verbose_name = _('长任务')
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return '%s %s' % (self.id, self.status)


class BackupRecord(models.Model):
    """备份点台账（账号 Excel 自动备份 + 全量备份包）。"""

    ts = models.CharField(_('时间'), max_length=32, blank=True, default='')
    kind = models.CharField(_('类型'), max_length=32, blank=True, default='')
    path = models.TextField(_('路径'))
    note = models.TextField(_('备注'), blank=True, default='')

    class Meta:
        db_table = 'backups'
        verbose_name = _('备份记录')
        verbose_name_plural = verbose_name
        ordering = ['-id']

    def __str__(self):
        return '%s %s' % (self.kind, self.ts)
