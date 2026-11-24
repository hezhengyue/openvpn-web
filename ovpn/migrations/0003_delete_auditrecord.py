"""删除旧 audit 表（业务审计迁到 audit.AuditLog）。

旧表由 system/core.py 的裸 SQL 建在与主库同一个 sqlite 文件里（db_table='audit'），
历史数据只用于联调，切到统一审计后不再保留：
  * DeleteModel 让 Django 的迁移状态与模型定义一致；
  * RunSQL 兜底 DROP，覆盖「表在 Django 之外被建出来、迁移状态里没有」的存量库。
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('ovpn', '0002_upgrade_user_group_members'),
    ]

    operations = [
        migrations.DeleteModel(name='AuditRecord'),
        migrations.RunSQL(
            sql='DROP TABLE IF EXISTS audit;',
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
