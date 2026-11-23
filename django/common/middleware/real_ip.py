"""统一真实 IP 解析。

Docker 模式：Nginx 覆盖 X-Forwarded-For 为单个 $remote_addr，客户端自带的
             X-Forwarded-For 会被丢弃，因此只信任该头 + REMOTE_ADDR。
裸机模式  ：默认直接读 REMOTE_ADDR，天然不可伪造；若前面有本机反代
             （TRUST_LOCAL_PROXY=True），仅当请求确实来自 127.0.0.1/::1 时
             才从 X-Forwarded-For 取最右侧第一个合法 IP——反代用
             $proxy_add_x_forwarded_for 时会把真实客户端 IP 追加在最右，
             取最左会被客户端伪造的值绕过 Axes / 白名单。其余情况一律剥离代理头，
             包括 X-Forwarded-Proto（防止直连端口伪造 https 标记）。
"""

import ipaddress

from django.conf import settings
from django.utils.deprecation import MiddlewareMixin
from ipware import get_client_ip

PROXY_HEADERS = (
    'HTTP_X_FORWARDED_FOR',
    'HTTP_X_REAL_IP',
    'HTTP_X_FORWARDED_PORT',
)
# X-Forwarded-Proto 单独处理：回环本机反代需要保留它给 SECURE_PROXY_SSL_HEADER，
# 其余来源必须剥离，否则直连端口伪造的 https 标记会骗过 is_secure()。
FORWARDED_PROTO_HEADER = 'HTTP_X_FORWARDED_PROTO'


def _last_forwarded_for(value):
    """取 X-Forwarded-For 最右侧的合法 IP，取不到返回 None。

    本机反代用 $proxy_add_x_forwarded_for 时，真实客户端 IP 是反代追加的
    最后一段；左侧各段是客户端可伪造的值，必须从右往左取。
    仍做合法性校验，避免畸形值写回 REMOTE_ADDR 污染 Axes / 审计记录。
    """
    for candidate in reversed((value or '').split(',')):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return None


class RealIPMiddleware(MiddlewareMixin):

    def process_request(self, request):
        ip = None

        if getattr(settings, 'STANDALONE', False):
            remote_addr = request.META.get('REMOTE_ADDR')
            trust_local = getattr(settings, 'TRUST_LOCAL_PROXY', False)
            # 只有来自本机反代（回环地址）的请求才允许读转发头；
            # 公网客户端即使伪造 X-Forwarded-For 也走不到这个分支。
            if trust_local and remote_addr in ('127.0.0.1', '::1'):
                ip = _last_forwarded_for(request.META.get('HTTP_X_FORWARDED_FOR'))
            else:
                # 非本机反代直连：X-Forwarded-Proto 同样不可信，必须剥离，
                # 否则伪造的 https 会骗过 request.is_secure() 与 Secure cookie 判定。
                request.META.pop(FORWARDED_PROTO_HEADER, None)
            # 无论是否采信，都清空 IP 类代理头，避免下游插件绕过 REMOTE_ADDR。
            for header in PROXY_HEADERS:
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
