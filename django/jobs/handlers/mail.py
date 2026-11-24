# jobs/handlers/mail.py

import re

from django.conf import settings
from django.core.mail import send_mail

from jobs.handlers.base import BaseHandler


class MailHandler(BaseHandler):

    def handle(self):
        payload = getattr(self.job, 'payload', None) or {}
        raw = payload.get('to') or payload.get('recipients')
        # 任务参数里传 list 时 str(raw) 会带 [] 和引号，正则拆出来的地址是坏的。
        # 这里分类型处理；没有收件人直接报错，不再偷偷发到 test@test.com。
        if isinstance(raw, (list, tuple)):
            recipients = [str(x).strip() for x in raw if str(x).strip()]
        else:
            recipients = [x.strip() for x in re.split(r'[,;，；\s]+', str(raw or '')) if x.strip()]
        if not recipients:
            raise ValueError("邮件任务缺少收件人：请在参数里填 to 或 recipients")
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
