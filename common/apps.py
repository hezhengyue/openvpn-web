from django.apps import AppConfig


def _configure_sqlite_connection(sender, connection, **kwargs):
    """
    SQLite 单机模式：每次建立连接时开启 WAL，降低多读单写场景的锁冲突。
    Django 的 SQLite OPTIONS 不支持 init_command，必须用 connection_created 信号。
    """
    if connection.vendor != 'sqlite':
        return
    with connection.cursor() as cursor:
        cursor.execute('PRAGMA journal_mode=WAL')
        cursor.execute('PRAGMA synchronous=NORMAL')
        cursor.execute('PRAGMA busy_timeout=20000')


class CommonConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'common'
    verbose_name = '公共模块'

    def ready(self):
        from django.db.backends.signals import connection_created

        connection_created.connect(
            _configure_sqlite_connection,
            dispatch_uid='common.sqlite_wal_pragmas',
        )

        from .admin.rename import rename_third_party_apps
        rename_third_party_apps()
