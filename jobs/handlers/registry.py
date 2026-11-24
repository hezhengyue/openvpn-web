# jobs/handlers/registry.py

from jobs.handlers.mail import MailHandler


JOB_HANDLERS = {
    'mail': MailHandler,
}


def register_handler(job_type: str, handler_cls):
    """注册任务处理器，派生项目在自己的 AppConfig.ready() 中调用即可。

    这样业务 App 无需改母版的 registry.py，升级 jobs 模块时零合并冲突。
    """
    JOB_HANDLERS[job_type] = handler_cls