# common/tasks/__init__.py
# 审计清理任务实现在独立 audit app 里（唯一删除通道 + 自留痕），
# 这里只做 re-export，避免出现两份实现。
from audit.tasks import clean_old_audit_logs  # noqa: F401
