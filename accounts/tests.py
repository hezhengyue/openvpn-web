from unittest.mock import Mock, patch

from django.test import TestCase

from common.email.password import generate_secure_password, send_temp_password_email


class EmailTest(TestCase):
    """模板测试：不发送真实邮件，也不使用任何个人邮箱。"""

    def test_send_password_email(self):
        password = generate_secure_password()

        with patch(
            'common.email.password.send_password_email_task.delay',
            return_value=Mock(id='task-test-001'),
        ) as delay_mock:
            task_id = send_temp_password_email(
                user_email='test@example.com',
                password=password,
                username='测试用户',
            )

        self.assertEqual(task_id, 'task-test-001')
        delay_mock.assert_called_once_with(
            user_email='test@example.com',
            password=password,
            username='测试用户',
        )
