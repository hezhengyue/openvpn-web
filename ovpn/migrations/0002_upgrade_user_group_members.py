# ovpn/migrations/0002_upgrade_user_group_members.py
"""把旧版「裸 SQL 建表」的 user_group_members 升级成 ORM 可用的形状。

历史原因：Flask 时代的 core.py 用 `CREATE TABLE user_group_members(group_id,
username, UNIQUE(group_id, username))` 建表——没有自增主键。Django ORM 的模型
默认带一个隐式 `id` AutoField，于是 `UserGroupMember.objects.all()` 会查一个
不存在的列，报 `no such column: user_group_members.id`。

`migrate --fake-initial` 只把 0001 标记为已应用、不会补列，所以这里显式重建一次：
SQLite 不支持给已有表加 AUTOINCREMENT 主键，只能「建新表 → 搬数据 → 换名」。

全新库由 Django 建表，本来就有 id，本迁移检测到就直接跳过（幂等）。
"""
from django.db import migrations


OLD_TABLE = 'user_group_members'
NEW_TABLE = 'user_group_members__new'


def _columns(cursor, table):
    return [row[1] for row in cursor.execute('PRAGMA table_info(%s)' % table)]


def forwards(apps, schema_editor):
    cursor = schema_editor.connection.cursor()
    cols = _columns(cursor, OLD_TABLE)
    if not cols or 'id' in cols:
        return  # 表不存在（全新库会由 Django 建）或已经是新结构
    cursor.execute(
        'CREATE TABLE %s ('
        ' id integer NOT NULL PRIMARY KEY AUTOINCREMENT,'
        ' group_id integer NOT NULL REFERENCES user_groups(id) DEFERRABLE INITIALLY DEFERRED,'
        ' username varchar(64) NOT NULL,'
        ' UNIQUE (group_id, username))' % NEW_TABLE
    )
    cursor.execute(
        'INSERT INTO %s (group_id, username) SELECT group_id, username FROM %s'
        % (NEW_TABLE, OLD_TABLE)
    )
    cursor.execute('DROP TABLE %s' % OLD_TABLE)
    cursor.execute('ALTER TABLE %s RENAME TO %s' % (NEW_TABLE, OLD_TABLE))
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_members ON %s (group_id)' % OLD_TABLE
    )


def backwards(apps, schema_editor):
    """回退无意义：旧结构缺主键，退回只会把问题带回去。"""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('ovpn', '0001_initial_ovpn_business'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
