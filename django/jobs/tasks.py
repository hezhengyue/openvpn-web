# jobs/tasks.py
# Celery 任务只做薄包装，执行逻辑集中在 JobService.execute_job，
# 供 Docker Worker 与裸机 daemon 线程共用。

from celery import shared_task

from jobs.services.job_service import JobService


@shared_task(bind=True)
def run_job(self, job_id):
    JobService.execute_job(job_id)
