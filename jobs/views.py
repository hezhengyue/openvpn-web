# jobs/views.py

from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404

from jobs.models import Job


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
    })
