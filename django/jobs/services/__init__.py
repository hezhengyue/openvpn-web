# jobs/services/__init__.py
"""任务中心的服务层入口。

业务代码统一 `from jobs.services import JobService` 再 `JobService.dispatch(...)`，
不要直接操作 Job / JobLog 模型，也不要自建线程池发任务。
"""
from .job_service import JobService  # noqa: F401

__all__ = ["JobService"]
