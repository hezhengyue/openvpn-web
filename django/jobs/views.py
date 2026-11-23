# jobs/views.py

from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404

from jobs.models import Job


def _iso(value):
    return value.isoformat() if value is not None else None


@staff_member_required
def job_detail(request, job_id):
    """任务进度轮询接口：仅后台 staff 可访问，防止 result/error_message 泄露。"""
    job = get_object_or_404(Job, id=job_id)

    return JsonResponse({
        'id': job.id,
        'status': job.status,
        'progress': job.progress,
        'current': job.current,
        'total': job.total,
        'result': job.result,
        'error_message': job.error_message,
        'started_at': _iso(job.started_at),
        'finished_at': _iso(job.finished_at),
        'updated_at': _iso(job.updated_at),
    })


@staff_member_required
def job_logs(request, job_id):
    """任务日志增量轮询：?after_id=<int> 只返回该 ID 之后的日志。

    ?limit= 控制单页条数（默认 500，上限 2000），响应带 has_more，
    避免一次拉全量日志撑爆内存与响应体。
    """
    job = get_object_or_404(Job, id=job_id)
    queryset = job.logs.all().order_by('id')

    after_id = request.GET.get('after_id')
    if after_id is not None and after_id.isdigit():
        queryset = queryset.filter(id__gt=int(after_id))

    try:
        limit = int(request.GET.get('limit', 500))
    except (TypeError, ValueError):
        limit = 500
    if limit < 1:
        limit = 500
    limit = min(limit, 2000)

    rows = list(queryset[:limit + 1])
    has_more = len(rows) > limit
    logs = [
        {
            'id': log.id,
            'level': log.level,
            'message': log.message,
            'created_at': _iso(log.created_at),
        }
        for log in rows[:limit]
    ]
    return JsonResponse({'job_id': job.id, 'logs': logs, 'has_more': has_more})
