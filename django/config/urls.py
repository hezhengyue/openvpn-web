# config/urls.py
from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import SuspiciousFileOperation
from django.http import Http404, JsonResponse
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.db import connection
from django.views.static import serve as static_serve


def health_check(request):
    """健康检查端点：同时检查 Web 与数据库连通性。

    用 SELECT 1 真发一条语句，而不是只 ensure_connection()：连接池/文件句柄
    里的失效连接只有真正查询时才会暴露，只建连会把坏连接判成健康。
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        db_ok = True
    except Exception:
        db_ok = False
    return JsonResponse({
        "status": "healthy" if db_ok else "unhealthy",
        "service": getattr(settings, 'PROJECT_NAME', 'unknown'),
        "database": "ok" if db_ok else "unavailable",
    }, status=200 if db_ok else 503)


@staff_member_required
def media_download(request, path):
    """带权限校验的媒体文件访问：匿名/非 staff 跳 admin 登录。

    django.views.static.serve 内部用 safe_join 拦截 ../ 目录穿越。
    """
    try:
        return static_serve(
            request,
            path,
            document_root=str(settings.MEDIA_ROOT),
            show_indexes=False,
        )
    except SuspiciousFileOperation as exc:
        # Django 5.2 的 static.serve 直接抛 SuspiciousFileOperation；
        # 转成 404，避免把绝对路径/目录结构暴露给客户端。
        raise Http404('Invalid media path') from exc


urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health_check, name="health"),
    # 上传文件统一走 staff 鉴权视图，DEBUG 下也不裸奔
    path("media/<path:path>", media_download, name="media-download"),
    path("jobs/", include("jobs.urls")),
    # OpenVPN 管理 SPA + 全部 REST API（根路径就是登录页）
    path("", include("ovpn.urls")),
]

if settings.DEBUG:
    # 媒体文件已由上面的鉴权视图提供，此处只补充静态文件
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
