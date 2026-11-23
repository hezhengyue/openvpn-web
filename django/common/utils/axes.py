"""Axes 锁定响应：同一套底座同时服务 admin 表单登录与前端 JSON API。

由 settings.AXES_LOCKOUT_CALLABLE 指向；Axes 判定锁定时回调本函数。
"""

from django.conf import settings
from django.http import HttpResponse, JsonResponse


def _wants_json(request):
    """API 路径或显式要求 JSON 响应时返回 True。"""
    if request.path.startswith('/api/'):
        return True
    accept = request.META.get('HTTP_ACCEPT', '')
    content_type = request.META.get('CONTENT_TYPE', '')
    return 'application/json' in accept or 'application/json' in content_type


def axes_lockout_response(request, credentials=None, *args, **kwargs):
    """锁定后返回 JSON（API）或友好中文 403 HTML（admin 表单）。"""
    minutes = getattr(settings, 'AXES_COOLOFF_MINUTES', 60)
    if _wants_json(request):
        return JsonResponse(
            {'ok': False, 'msg': f'失败次数过多，请 {minutes} 分钟后再试'},
            status=403,
        )
    return HttpResponse(
        f'<h3>登录失败次数过多</h3>'
        f'<p>请 {minutes} 分钟后再试，或联系管理员解锁账号。</p>',
        status=403,
        content_type='text/html; charset=utf-8',
    )
