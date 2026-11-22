"""统一真实 IP 解析。

Docker 模式：Nginx 覆盖 X-Forwarded-For 为单个 $remote_addr，客户端自带的
             X-Forwarded-For 会被丢弃，因此只信任该头 + REMOTE_ADDR。
裸机模式  ：没有反向代理，直接读 REMOTE_ADDR，天然不可伪造；同时彻底剥离
             客户端发来的代理头——django-auditlog 3.4.2 的 _get_remote_addr()
             默认优先读 X-Forwarded-For 最左侧值，不清掉仍会污染审计日志。
"""

from django.conf import settings
from django.utils.deprecation import MiddlewareMixin
from ipware import get_client_ip


class RealIPMiddleware(MiddlewareMixin):

    def process_request(self, request):
        ip = None

        if getattr(settings, 'STANDALONE', False):
            # 裸机无反代：剥离全部客户端可伪造的代理头，避免 auditlog 等
            # 下游插件绕过 REMOTE_ADDR 记录假 IP。REMOTE_ADDR 由 WSGI
            # 服务器写入，请求头无法伪造。
            for header in (
                'HTTP_X_FORWARDED_FOR',
                'HTTP_X_REAL_IP',
                'HTTP_X_FORWARDED_PORT',
            ):
                request.META.pop(header, None)
        else:
            ip, _is_routable = get_client_ip(
                request,
                proxy_count=getattr(settings, 'IPWARE_PROXY_COUNT', 0),
                proxy_order='left-most',
                request_header_order=tuple(
                    getattr(settings, 'IPWARE_HEADER_ORDER', ('REMOTE_ADDR',))
                ),
            )

        ip = ip or request.META.get('REMOTE_ADDR') or '0.0.0.0'
        request._real_ip = ip
        # Axes / Auditlog / 业务代码统一从 REMOTE_ADDR 取真实 IP
        request.META['REMOTE_ADDR'] = ip
