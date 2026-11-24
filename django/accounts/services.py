# accounts/services.py
"""账户域服务：随机口令生成与「临时密码」通知邮件。

这两件事属于账户业务（文案、首次登录必须改密），所以放在 accounts 而不是
common —— common 只保留通用基础设施（真实 IP、邮件发送本身、Axes 响应、
Admin 改名），不再携带任何账户业务文案。

实际发信仍复用 Django 的邮件后端；裸机模式 EMAIL_ENABLED=false 时后端是
console，不会真的发出去（也便于 selftest 断言）。
"""
import logging
import secrets
from html import escape

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def generate_secure_password(length=10):
    """生成含大小写字母 + 数字的随机口令（至少各一个）。"""
    chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
    while True:
        pwd = ''.join(secrets.choice(chars) for _ in range(length))
        if (any(c.isupper() for c in pwd)
                and any(c.islower() for c in pwd)
                and any(c.isdigit() for c in pwd)):
            return pwd


@shared_task(bind=True)
def send_password_email_task(self, user_email: str, password: str, username: str = ''):
    """异步发送临时密码邮件（单次执行，不重试：口令类通知重试没有意义）。"""
    safe_username = escape(username or '用户')
    safe_password = escape(password)

    subject = '【OpenVPN】您的初始密码已生成'
    message = (f'您好 {username or "用户"}，\n\n您的临时登录密码为：{password}\n\n'
               '为保障安全，请在首次登录后立即修改密码。')
    html_message = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 20px;">
        <h2>您好 {safe_username}，</h2>
        <p>您的临时登录密码已生成：</p>
        <div style="background: #f4f4f4; padding: 15px; font-size: 24px; font-weight: bold; letter-spacing: 2px; border-radius: 5px; text-align: center;">
            {safe_password}
        </div>
        <p style="color: #666; font-size: 14px; margin-top: 20px;">
            为保障账户安全，请在首次登录后立即修改密码。<br>
            如非本人操作，请忽略此邮件或联系管理员。
        </p>
    </div>
    """
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user_email],
            html_message=html_message,
            fail_silently=False,
        )
        logger.info("临时密码邮件已发送: %s", user_email)
        return True
    except Exception as exc:  # noqa: BLE001 —— 通知失败不能影响主流程
        logger.error("临时密码邮件发送失败 %s: %s", user_email, exc, exc_info=True)
        return False


def send_temp_password_email(user_email: str, password: str, username: str = ''):
    """投递临时密码邮件，返回 task_id；邮箱为空时直接返回 None。"""
    if not user_email:
        return None
    task = send_password_email_task.delay(
        user_email=user_email, password=password, username=username
    )
    return task.id
