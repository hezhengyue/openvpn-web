"""请求级审计上下文（仅 WSGI）。

本中间件继承 MiddlewareMixin，适用于底座默认的 Waitress/WSGI 同步线程池。
ASGI 下 MiddlewareMixin 的 process_request/process_response 会在不同 Context
拷贝里运行，ContextVar token 无法跨 Context reset；如将来切换 ASGI，需改写为
@sync_and_async_middleware 的标准 __call__ 形式。
业务代码在拿不到 request 时，audit_log() 会从这里回退读取当前请求。
"""
import uuid
from contextvars import ContextVar

from django.utils.deprecation import MiddlewareMixin

_current_request = ContextVar('audit_current_request', default=None)
_SAFE_CHARS = frozenset(
    'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.'
)


def get_current_request():
    return _current_request.get()


class AuditContextMiddleware(MiddlewareMixin):

    def process_request(self, request):
        request_id = (request.META.get('HTTP_X_REQUEST_ID') or '').strip()
        if (
            not request_id
            or len(request_id) > 64
            or any(ch not in _SAFE_CHARS for ch in request_id)
        ):
            request_id = uuid.uuid4().hex[:16]
        request.audit_request_id = request_id
        request._audit_ctx_token = _current_request.set(request)
        return None

    def _reset(self, request):
        token = getattr(request, '_audit_ctx_token', None)
        if token is not None:
            _current_request.reset(token)
            request._audit_ctx_token = None

    def process_response(self, request, response):
        self._reset(request)
        # 前置中间件短路时 process_request 未执行，此时 request_id 为空，
        # 不要写出一个空的 X-Request-ID 响应头。
        request_id = getattr(request, 'audit_request_id', '')
        if request_id:
            try:
                response.setdefault('X-Request-ID', request_id)
            except Exception:
                pass
        return response

    def process_exception(self, request, exception):
        self._reset(request)
        return None
