"""审计历史清理命令（无 celery beat 的部署方式用）。

    python manage.py audit_purge --days 180
    python manage.py audit_purge --days 180 --dry-run
"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from audit.models import AuditLog


class Command(BaseCommand):
    help = '清理过期审计记录（走唯一删除通道，保留 expire 自留痕）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=None,
            help='保留天数；缺省读取 settings.AUDIT_RETENTION_DAYS',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='只统计将删除的条数，不删除也不写自留痕',
        )

    def handle(self, *args, **options):
        days = options['days']
        if days is None:
            days = getattr(settings, 'AUDIT_RETENTION_DAYS', 180)
        days = int(days)
        if days < 0:
            raise CommandError('--days 不能为负数')
        before = timezone.now() - timedelta(days=days)
        count = AuditLog.objects.expirable(before).count()
        if options['dry_run']:
            self.stdout.write(f'[dry-run] 将清理 {count} 条 {before:%Y-%m-%d %H:%M} 之前的审计记录')
            return
        deleted = AuditLog.objects.expire_audit_logs(days=days)
        self.stdout.write(self.style.SUCCESS(f'已清理 {deleted} 条审计记录（保留 {days} 天）'))
