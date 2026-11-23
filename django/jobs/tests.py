# jobs/tests.py
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from jobs.admin import JobAdmin, JobLogAdmin
from jobs.models import Job, JobLog
from jobs.services.job_service import JobService


class JobServiceTest(TestCase):
    """创建/状态流转必须更新 updated_at（auto_now 依赖 update_fields）。"""

    @staticmethod
    def _rewind_updated_at(job):
        """把库里的时间戳回拨 5 秒，避开时钟粒度导致的偶发相等。"""
        past = timezone.now() - timedelta(seconds=5)
        Job.objects.filter(pk=job.pk).update(updated_at=past)
        return past

    def test_create_and_lifecycle_updates_updated_at(self):
        job = JobService.create('mail', '测试任务', {'to': 'a@example.com'})
        self.assertEqual(job.status, 'pending')
        self.assertEqual(job.payload, {'to': 'a@example.com'})

        past = self._rewind_updated_at(job)
        JobService.start(job)
        job.refresh_from_db()
        self.assertEqual(job.status, 'running')
        self.assertIsNotNone(job.started_at)
        self.assertGreater(job.updated_at, past)

        past = self._rewind_updated_at(job)
        JobService.progress(job, 5, 10)
        job.refresh_from_db()
        self.assertEqual(job.progress, 50)
        self.assertGreater(job.updated_at, past)

        past = self._rewind_updated_at(job)
        JobService.success(job, {'sent': 1})
        job.refresh_from_db()
        self.assertEqual(job.status, 'success')
        self.assertEqual(job.result, {'sent': 1})
        self.assertIsNotNone(job.finished_at)
        self.assertGreater(job.updated_at, past)

    def test_failed_updates_updated_at_and_writes_log(self):
        job = JobService.create('mail', '失败任务')
        past = self._rewind_updated_at(job)
        JobService.failed(job, 'boom')
        job.refresh_from_db()
        self.assertEqual(job.status, 'failed')
        self.assertEqual(job.error_message, 'boom')
        self.assertGreater(job.updated_at, past)
        self.assertTrue(job.logs.filter(level='error').exists())

    def test_dispatch_sync_mode_runs_execute(self):
        job = JobService.create('mail', '同步任务')
        with self.settings(JOBS_SYNC_IN_TESTS=True):
            called = []
            original = JobService.execute_job

            def fake_execute(job_id):
                called.append(job_id)

            try:
                JobService.execute_job = staticmethod(fake_execute)
                JobService.dispatch(job)
            finally:
                JobService.execute_job = original
        self.assertEqual(called, [job.id])

    def test_dispatch_defers_until_transaction_commit(self):
        job = JobService.create('mail', '延迟派发任务')
        with self.settings(STANDALONE=False):
            with mock.patch('django.db.transaction.on_commit') as on_commit:
                JobService.dispatch(job)
            self.assertTrue(on_commit.called)
            launch = on_commit.call_args[0][0]
            with mock.patch('jobs.tasks.run_job.delay') as delay:
                launch()
        delay.assert_called_once_with(job.id)


class JobViewPermissionTest(TestCase):
    """任务详情与日志接口只允许 staff。"""

    def setUp(self):
        User = get_user_model()
        self.job = Job.objects.create(job_type='mail', name='任务')
        JobLog.objects.create(job=self.job, level='info', message='第一条')
        self.detail_url = reverse('job_detail', args=[self.job.id])
        self.logs_url = reverse('job_logs', args=[self.job.id])
        self.normal = User.objects.create_user('viewer', password='pass12345')

    def test_anonymous_redirected_to_admin_login(self):
        for url in (self.detail_url, self.logs_url):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn('/admin/login/', response['Location'])

    def test_non_staff_redirected(self):
        self.client.force_login(self.normal)
        for url in (self.detail_url, self.logs_url):
            self.assertEqual(self.client.get(url).status_code, 302)

    def test_staff_can_read_detail_and_logs(self):
        User = get_user_model()
        staff = User.objects.create_user('staff', password='pass12345', is_staff=True)
        self.client.force_login(staff)

        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        for field in ('started_at', 'finished_at', 'updated_at'):
            self.assertIn(field, data)

        response = self.client.get(self.logs_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['logs']), 1)

        last_id = JobLog.objects.latest('id').id
        JobLog.objects.create(job=self.job, level='info', message='第二条')
        response = self.client.get(self.logs_url, {'after_id': last_id})
        self.assertEqual([row['message'] for row in response.json()['logs']], ['第二条'])

    def test_logs_limit_and_has_more(self):
        User = get_user_model()
        staff = User.objects.create_user('staff2', password='pass12345', is_staff=True)
        self.client.force_login(staff)
        for i in range(5):
            JobLog.objects.create(job=self.job, level='info', message=f'第 {i} 条')

        response = self.client.get(self.logs_url, {'limit': 2})
        data = response.json()
        self.assertEqual(len(data['logs']), 2)
        self.assertTrue(data['has_more'])

        response = self.client.get(
            self.logs_url, {'limit': 2, 'after_id': data['logs'][-1]['id']}
        )
        data2 = response.json()
        self.assertEqual(len(data2['logs']), 2)
        self.assertNotEqual(data2['logs'][0]['id'], data['logs'][0]['id'])

        # 非法 limit 回落默认值，日志总量未超过 500
        response = self.client.get(self.logs_url, {'limit': 'bad'})
        self.assertFalse(response.json()['has_more'])


class JobAdminReadOnlyTest(TestCase):
    """Job / JobLog 管理页只读，防止构造 POST 改任务状态。"""

    def test_admin_permissions_are_read_only(self):
        for admin_cls, model in ((JobAdmin, Job), (JobLogAdmin, JobLog)):
            admin = admin_cls(model, None)
            self.assertFalse(admin.has_add_permission(None))
            self.assertFalse(admin.has_change_permission(None))
            self.assertFalse(admin.has_delete_permission(None))


class HandlerRegistryTest(TestCase):
    """派生项目通过 register_handler 扩展，无需改母版 registry.py。"""

    def test_register_handler_adds_and_overwrites(self):
        from jobs.handlers.registry import JOB_HANDLERS, register_handler

        class DummyHandler:
            pass

        key = 'test_dummy_handler'
        self.addCleanup(JOB_HANDLERS.pop, key, None)
        register_handler(key, DummyHandler)
        self.assertIs(JOB_HANDLERS[key], DummyHandler)

        class AnotherHandler:
            pass

        register_handler(key, AnotherHandler)
        self.assertIs(JOB_HANDLERS[key], AnotherHandler)
