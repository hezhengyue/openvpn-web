# jobs/services/job_service.py

import logging
import threading

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from jobs.models import Job
from jobs.models import JobLog

logger = logging.getLogger(__name__)


class JobService:

    @staticmethod
    def create(job_type, name, payload=None) -> Job:
        """统一创建任务记录，避免各业务直接写 Job 模型。"""
        return Job.objects.create(
            job_type=job_type,
            name=name,
            payload=payload or {},
        )

    @staticmethod
    def execute_job(job_id):
        """真正执行任务；Docker 的 Celery 任务与裸机线程共用这一实现。"""
        from jobs.handlers.registry import JOB_HANDLERS

        job = Job.objects.get(id=job_id)
        try:
            JobService.start(job)
            handler_class = JOB_HANDLERS[job.job_type]
            result = handler_class(job).handle()
            JobService.success(job, result)
        except Exception as exc:
            JobService.failed(job, exc)
            raise

    @staticmethod
    def dispatch(job: Job, sync=None):
        """把任务交给执行器：Docker 走 Celery，裸机走 daemon 线程。

        裸机不直接同步执行，避免长任务阻塞 HTTP 请求；线程内显式管理
        SQLite 连接生命周期。测试可用 sync=True 或 JOBS_SYNC_IN_TESTS
        强制同步，便于断言结果。
        """
        if sync is None:
            sync = bool(getattr(settings, 'JOBS_SYNC_IN_TESTS', False))
        if sync:
            JobService.execute_job(job.id)
            return

        def launch():
            # 事务提交后再派发，避免 Worker/线程在 create() 提交前读不到 Job
            if not getattr(settings, 'STANDALONE', False):
                from jobs.tasks import run_job

                run_job.delay(job.id)
                return

            thread = threading.Thread(
                target=JobService._run_in_thread,
                args=(job.id,),
                daemon=True,
                name=f'job-{job.id}',
            )
            thread.start()

        transaction.on_commit(launch)

    @staticmethod
    def _run_in_thread(job_id):
        close_old_connections()
        try:
            try:
                JobService.execute_job(job_id)
            except Exception:
                # execute_job 已把失败写进 Job / JobLog；这里只补日志，
                # 不再向线程顶层裸抛未捕获异常栈
                logger.exception('后台任务执行失败 job_id=%s', job_id)
        finally:
            close_old_connections()

    @staticmethod
    def start(job: Job):

        job.status = 'running'
        job.started_at = timezone.now()

        job.save(
            update_fields=[
                'status',
                'started_at',
                'updated_at',
            ]
        )

        JobService.log(job, '任务开始执行')

    @staticmethod
    def progress(job: Job, current: int, total: int):

        job.current = current
        job.total = total

        if total > 0:
            job.progress = int(current / total * 100)

        job.save(
            update_fields=[
                'current',
                'total',
                'progress',
                'updated_at',
            ]
        )

    @staticmethod
    def success(job: Job, result=None):

        job.status = 'success'
        job.progress = 100
        job.finished_at = timezone.now()
        job.result = result or {}

        job.save(
            update_fields=[
                'status',
                'progress',
                'finished_at',
                'result',
                'updated_at',
            ]
        )

        JobService.log(job, '任务执行成功')

    @staticmethod
    def failed(job: Job, error):

        job.status = 'failed'
        job.error_message = str(error)
        job.finished_at = timezone.now()

        job.save(
            update_fields=[
                'status',
                'error_message',
                'finished_at',
                'updated_at',
            ]
        )

        JobService.log(
            job,
            f'任务执行失败: {error}',
            level='error'
        )

    @staticmethod
    def log(job: Job, message: str, level='info'):

        JobLog.objects.create(
            job=job,
            level=level,
            message=message,
        )