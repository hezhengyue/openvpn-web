# core/resources.py

from import_export import resources, fields
from import_export.widgets import BooleanWidget

from django.contrib.auth.hashers import make_password, identify_hasher
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from .models import User


class UserResource(resources.ModelResource):
    """
    用户导入导出 Resource

    功能：
    - Excel / CSV 导入导出
    - 自动密码加密
    - Django 强密码校验
    - username 存在则更新
    - password 为空则不修改密码
    - 自动跳过空行
    - 不导出 password hash
    """

    is_active = fields.Field(
        column_name='is_active',
        attribute='is_active',
        widget=BooleanWidget()
    )

    is_staff = fields.Field(
        column_name='is_staff',
        attribute='is_staff',
        widget=BooleanWidget()
    )

    class Meta:
        model = User

        # 允许导入导出的字段
        fields = (
            'username',
            'email',
            'phone',
            'password',
            'is_active',
            'is_staff',
        )

        # 导出字段顺序
        export_order = (
            'username',
            'email',
            'phone',
            'password',
            'is_active',
            'is_staff',
        )

        # 使用 username 作为更新依据
        import_id_fields = ('username',)

        # 跳过未变化数据
        skip_unchanged = True

        # 显示跳过报告
        report_skipped = True

        # 使用事务
        use_transactions = True

    def before_import(self, dataset, **kwargs):
        """
        导入前先把全空行从 dataset 删除。

        django-import-export 4.4.6 没有 resources.SkipRow，在 before_import_row
        里抛该异常会直接 AttributeError；且 tablib 空单元格多为 None，
        str(None) == 'None' 会把空行误判成有效行。从后往前删避免索引错位。
        """
        for idx in range(len(dataset) - 1, -1, -1):
            if all(not str(value or '').strip() for value in dataset[idx]):
                del dataset[idx]

    def before_import_row(self, row, **kwargs):
        """
        导入前处理每一行（空行已在 before_import 中剔除）
        """

        # =====================================
        # username
        # =====================================
        username = str(row.get('username') or '').strip()

        if not username:
            raise ValidationError('username 不能为空')

        row['username'] = username

        # =====================================
        # email
        # =====================================
        email = str(row.get('email') or '').strip().lower()

        row['email'] = email

        # =====================================
        # phone
        # =====================================
        phone = str(row.get('phone') or '').strip()

        row['phone'] = phone

        # =====================================
        # password
        # =====================================
        # 先 strip 再判空：Excel 密码格里误敲空格等价于未填密码
        password = str(row.get('password') or '').strip()

        # 密码为空 -> 不修改现有用户密码；新用户保存前设为不可用密码
        if not password:
            row.pop('password', None)
            return

        # 临时用户对象
        temp_user = User(
            username=username,
            email=email,
        )

        try:
            # 已经是 hash
            identify_hasher(password)

        except Exception:

            # Django 强密码验证
            validate_password(password, user=temp_user)

            # hash 密码
            row['password'] = make_password(password)

    def before_save_instance(self, instance, row, **kwargs):
        """
        保存前处理
        """

        # email 小写
        if instance.email:
            instance.email = str(instance.email).lower().strip()

        # phone 去空格
        if instance.phone:
            instance.phone = str(instance.phone).strip()

        # 新用户导入时密码留空：设为不可用密码，只能由管理员重置
        if not instance.password:
            instance.set_unusable_password()

    def dehydrate_password(self, user):
        """
        导出 password 字段

        企业级：
        不导出真实 hash
        """

        return ''

    def get_instance(self, instance_loader, row):
        """
        username 存在 -> 更新
        username 不存在 -> 创建
        """

        username = str(row.get('username') or '').strip()

        if not username:
            return None

        try:
            return User.objects.get(username=username)

        except User.DoesNotExist:
            return None