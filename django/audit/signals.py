"""认证与安全事件接入：登录、退出、失败、Axes 锁定。

只记录事件类型与用户名/IP，绝不记录密码等凭据。
业务侧自己写同类事件时，用 settings.AUDIT_CAPTURE_EVENTS 关掉对应自动捕获，避免双写。
"""
from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .middleware import get_current_request
from .models import AUTH_MODULE, AuditAction, AuditEventType
from .services import audit_log

ALL_EVENTS = ('login', 'logout', 'login_failed', 'lockout')


def _captures(event):
    configured = getattr(settings, 'AUDIT_CAPTURE_EVENTS', ALL_EVENTS)
    if isinstance(configured, str):
        # 允许 .env 里的逗号分隔字符串；'lockout' 必须解析为 ('lockout',)，
        # 不能按字符拆开导致四个事件全匹配不上。
        configured = [part.strip() for part in configured.split(',') if part.strip()]
    else:
        configured = tuple(configured or ())
    return event in configured


@receiver(user_logged_in, dispatch_uid='audit.user_logged_in')
def on_user_logged_in(sender, request, user, **kwargs):
    if not _captures('login'):
        return
    audit_log(
        request=request, action=AuditAction.LOGIN,
        event_type=AuditEventType.SECURITY, module=AUTH_MODULE,
        actor=user, instance=user, message='登录成功',
    )


@receiver(user_logged_out, dispatch_uid='audit.user_logged_out')
def on_user_logged_out(sender, request, user, **kwargs):
    if not _captures('logout'):
        return
    audit_log(
        request=request, action=AuditAction.LOGOUT,
        event_type=AuditEventType.SECURITY, module=AUTH_MODULE,
        actor=user, instance=user, message='退出登录',
    )


@receiver(user_login_failed, dispatch_uid='audit.user_login_failed')
def on_user_login_failed(sender, credentials, request=None, **kwargs):
    if not _captures('login_failed'):
        return
    username = ''
    if isinstance(credentials, dict):
        # credentials 里含 password，只读 username。
        username = str(credentials.get('username', '') or '')[:150]
    audit_log(
        request=request, action=AuditAction.LOGIN_FAILED,
        event_type=AuditEventType.SECURITY, module=AUTH_MODULE,
        object_type='User', object_id='', object_repr=username,
        message='登录失败', extra={'username': username},
    )


def _connect_axes_lockout():
    try:
        from axes.signals import user_locked_out
    except Exception:  # Axes 未安装或配置未就绪时静默跳过
        return

    # weak=False：接收者是函数内局部对象，弱引用会在函数返回后被回收。
    @receiver(user_locked_out, dispatch_uid='audit.axes.user_locked_out', weak=False)
    def on_user_locked_out(sender, request, username, ip_address, **kwargs):
        if not _captures('lockout'):
            return
        audit_log(
            request=request, action=AuditAction.LOCKOUT,
            event_type=AuditEventType.SECURITY, module=AUTH_MODULE,
            object_type='User', object_repr=str(username or ''),
            remote_addr=str(ip_address or ''), message='账号被锁定',
        )


_connect_axes_lockout()


@receiver(pre_save, sender=settings.AUTH_USER_MODEL, dispatch_uid='audit.user.password_change.mark')
def mark_user_password_change(sender, instance, **kwargs):
    """只比对并打标，真正写日志推迟到 post_save。

    Django UserAdmin 编辑页的 password 是只读哈希框，永远不会进 changed_data，
    因此只能比对哈希；但 pre_save 阶段直接写审计会在 UPDATE 失败时留下假日志。
    """
    # 上一次保存若在 pre_save 打标后失败，标记会残留在实例上；重新保存时先清掉。
    instance.__dict__.pop('_audit_password_changed', None)
    if kwargs.get('raw') or instance._state.adding or instance.pk is None:
        return
    update_fields = kwargs.get('update_fields')
    if update_fields is not None and 'password' not in update_fields:
        # 登录只更新 last_login；这类保存没必要多查一次 password。
        return
    old = sender._default_manager.filter(pk=instance.pk).only('password').first()
    if old is None or old.password == instance.password:
        return
    instance._audit_password_changed = True


@receiver(post_save, sender=settings.AUTH_USER_MODEL, dispatch_uid='audit.user.password_change')
def on_user_password_change(sender, instance, **kwargs):
    """UPDATE 成功后再留痕，覆盖管理员改他人密码、自助改密码、set_password 三条路径。"""
    if not getattr(instance, '_audit_password_changed', False):
        return
    delattr(instance, '_audit_password_changed')
    request = get_current_request()
    actor = getattr(request, 'user', None) if request is not None else None
    if actor is None or not getattr(actor, 'is_authenticated', False):
        # 无登录操作人时不能把目标用户记成 actor：后台任务/CLI 改密没有操作人，
        # 目标用户已由 instance=instance 落到对象三元组，actor 交给 fallback 规则处理。
        actor = None
    audit_log(
        request=request, action=AuditAction.PASSWORD_CHANGE,
        event_type=AuditEventType.SECURITY, module=AUTH_MODULE,
        actor=actor, instance=instance, message='密码被修改',
        extra={'admin': bool(request is not None and getattr(request, 'path', '').startswith('/admin/'))},
    )
