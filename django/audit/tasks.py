"""审计历史清理：默认不注册到 Beat，由运维显式触发。

删除统一走 AuditLogQuerySet.expire_audit_logs()（唯一合法删除通道），
它会绕过只追加拦截并写一条 (system, 审计, expire) 自留痕。
"""
from celery import shared_task
from django.conf import settings

from .models import AuditLog


@shared_task
def clean_old_audit_logs(days=None):
    days = getattr(settings, 'AUDIT_RETENTION_DAYS', 180) if days is None else days
    return AuditLog.objects.expire_audit_logs(days=days)
