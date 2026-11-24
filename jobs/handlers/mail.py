# jobs/handlers/mail.py

import re

from django.conf import settings
from django.core.mail import send_mail

from jobs.handlers.base import BaseHandler


class MailHandler(BaseHandler):

    def handle(self):
        payload = getattr(self.job, 'payload', None) or {}
        raw = payload.get('to') or payload.get('recipients') or 'test@test.com'
        recipients = [x.strip() for x in re.split(r'[,;，；\s]+', str(raw)) if x.strip()]
        subject = payload.get('subject') or '测试邮件'
        message = payload.get('message') or '这是一个测试任务邮件'

        self.log('开始发送邮件给 %d 位收件人' % len(recipients))
        self.progress(0, 1)
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipients,
            fail_silently=False,
        )
        self.progress(1, 1)
        self.log('邮件发送完成')
        return {'message': '邮件发送成功', 'recipients': recipients}
