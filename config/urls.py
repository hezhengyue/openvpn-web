# config/urls.py
from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse
from django.conf import settings
from django.conf.urls.static import static
from django.db import connection


def health_check(request):
    """健康检查端点：同时检查 Web 与数据库连通性。"""
    try:
        connection.ensure_connection()
        db_ok = True
    except Exception:
        db_ok = False
    return JsonResponse({
        "status": "healthy" if db_ok else "unhealthy",
        "service": getattr(settings, 'PROJECT_NAME', 'unknown'),
        "database": "ok" if db_ok else "unavailable",
    }, status=200 if db_ok else 503)


urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health_check, name="health"),
    path("jobs/", include("jobs.urls")),
    # OpenVPN 管理 SPA + 全部 REST API（根路径就是登录页）
    path("", include("ovpn.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
