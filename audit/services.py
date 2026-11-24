"""统一审计服务：audit_log() 是唯一公开写入口，另有查询与 CSV 导出助手。

业务代码不写 AuditLog.objects.create()，只调用本函数。
"""
import csv
import io
import logging
from datetime import date, datetime

from django.conf import settings
from django.db import transaction
from django.db.models import Q, QuerySet
from django.http import StreamingHttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from .middleware import get_current_request
from .models import AUDIT_MODULE, AuditAction, AuditEventType, AuditLog
from .utils import object_info, sanitize_value

logger = logging.getLogger('audit')

MAX_MESSAGE_LENGTH = 8000
MAX_QUERY_LIMIT = 2000
MAX_EXPORT_ROWS = 50000
CSV_HEADER = ('时间', '操作人', '来源IP', '事件分类', '动作', '模块', '对象', '描述')
CSV_FIELDS = (
    'created_at', 'actor_name', 'remote_addr', 'event_type', 'action',
    'module', 'object_repr', 'message',
)


def _as_aware(value):
    """统一补齐时区：naive 时间按当前时区解释，避免 USE_TZ 下的告警与 8 小时偏差。"""
    if value is not None and timezone.is_naive(value):
        return timezone.make_aware(value)
    return value


def _parse_time(value, *, end=False):
    # 未传或全空白字符串视为「不加该筛选条件」；其他非空值必须能解析，
    # 否则静默返回 None 会让查询退化成无条件的全表扫描/导出。
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return _as_aware(value)
    if isinstance(value, date):
        return _as_aware(datetime.combine(value, datetime.max.time() if end else datetime.min.time()))
    text = str(value).strip()
    # 纯日期（YYYY-MM-DD）必须先识别：parse_datetime 会把它当成 00:00:00，
    # end=True 时查不到当天任何数据。
    day = parse_date(text)
    if day is not None:
        return _as_aware(datetime.combine(day, datetime.max.time() if end else datetime.min.time()))
    parsed = parse_datetime(text)
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(f'无法解析时间参数: {value!r}') from None
    return _as_aware(parsed)


def build_audit_queryset(*, since=None, until=None, actor='', action='',
                         module='', event_type='', q=''):
    """构造筛选后的未切片 QuerySet，调用方可继续 .filter() / 分页 / 导出。"""
    qs = AuditLog.objects.all()
    start = _parse_time(since)
    end = _parse_time(until, end=True)
    if start is not None:
        qs = qs.filter(created_at__gte=start)
    if end is not None:
        qs = qs.filter(created_at__lte=end)
    if actor:
        qs = qs.filter(Q(actor_name__icontains=actor) | Q(actor__username__icontains=actor))
    if action:
        qs = qs.filter(action__icontains=action)
    if module:
        qs = qs.filter(module__icontains=module)
    if event_type:
        qs = qs.filter(event_type=event_type)
    if q:
        qs = qs.filter(
            Q(actor_name__icontains=q) | Q(remote_addr__icontains=q) |
            Q(action__icontains=q) | Q(module__icontains=q) |
            Q(object_type__icontains=q) | Q(object_id__icontains=q) |
            Q(object_repr__icontains=q) | Q(message__icontains=q) |
            Q(request_id__icontains=q)
        )
    return qs


def query_audit_logs(*, since=None, until=None, actor='', action='', module='',
                     event_type='', q='', limit=200):
    """按条件查询并按 -created_at 排序，返回已限制条数的 QuerySet。

    需要继续 filter / 分页 / 导出时用 build_audit_queryset()。
    仅提供 limit（UUID 主键不支持 id < last_id 游标），上限 2000。
    """
    qs = build_audit_queryset(
        since=since, until=until, actor=actor, action=action, module=module,
        event_type=event_type, q=q,
    )
    return qs[:max(1, min(int(limit), MAX_QUERY_LIMIT))]


def _csv_cell(value):
    text = '' if value is None else str(value)
    if text and text[0] in '=+-@\t\r':
        return "'" + text
    return text


def _csv_stream(queryset, exclude_pk=None):
    # Admin 的 get_queryset 带 select_related('actor')；CSV 用不到 actor，
    # 去掉关联并把 changes/extra 等大 JSON 字段排除在 SELECT 之外。
    # 已切片的 QuerySet 不能再改字段，保持原样即可。
    if (
        isinstance(queryset, QuerySet)
        and not (queryset.query.low_mark or queryset.query.high_mark)
    ):
        queryset = queryset.select_related(None).only(*CSV_FIELDS)

    yield '\ufeff'  # UTF-8 BOM
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)
    yield buf.getvalue()
    buf.seek(0)
    buf.truncate(0)
    written = 0
    for entry in queryset.iterator(chunk_size=1000):
        # export 自留痕在流式响应返回前已落库；按主键跳过刚写的那条，
        # 防止「跨页全选」导出把自己混进结果。
        if exclude_pk is not None and getattr(entry, 'pk', None) == exclude_pk:
            continue
        if written >= MAX_EXPORT_ROWS:
            break
        writer.writerow([
            _csv_cell(
                timezone.localtime(getattr(entry, field)).strftime('%Y-%m-%d %H:%M:%S')
                if field == 'created_at' and getattr(entry, field, None) is not None
                else getattr(entry, field, '')
            )
            for field in CSV_FIELDS
        ])
        written += 1
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)


def export_audit_csv(queryset, filename='audit-logs.csv', request=None):
    """流式导出审计查询集：BOM + 本地时区 + 公式注入中和 + 行数上限。

    使用 StreamingHttpResponse + iterator 避免一次性把几万行拼进内存。
    导出属于敏感操作，调用方传入 request 时会记一条 (security, 审计, export)；
    刚写入的这条会按主键排除，不会混进本次导出结果。
    """
    audit_entry = None
    if request is not None:
        audit_entry = audit_log(
            request=request, action=AuditAction.EXPORT,
            event_type=AuditEventType.SECURITY, module=AUDIT_MODULE,
            message=f'导出审计记录，上限 {MAX_EXPORT_ROWS} 行',
        )
    response = StreamingHttpResponse(
        _csv_stream(queryset, exclude_pk=audit_entry.pk if audit_entry is not None else None),
        content_type='text/csv; charset=utf-8',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def _user_model():
    """延迟获取 AUTH_USER_MODEL，避免模块导入期 apps 未就绪。"""
    try:
        from django.contrib.auth import get_user_model
        return get_user_model()
    except Exception:
        return None


def _name_from(value):
    if value is None:
        return ''
    return str(getattr(value, 'username', '') or value)


def _resolve_actor(request, actor, actor_name=''):
    """返回 (actor_obj, actor_name)。

    - 只有 AUTH_USER_MODEL 实例才写外键；
    - 字符串/任意对象一律降级为 actor_name 快照，绝不抛 ValueError；
    - 显式 actor_name 优先级最高（派生项目从老签名迁移时用），且不再写外键；
    - 无上下文或匿名请求回落到 AUDIT_FALLBACK_ACTOR。
    """
    user_cls = _user_model()
    actor_obj = None
    name = ''

    if actor is not None:
        if user_cls is not None and isinstance(actor, user_cls):
            actor_obj = actor
            name = _name_from(actor)
        else:
            name = _name_from(actor)
    else:
        user = getattr(request, 'user', None) if request is not None else None
        if user is not None and getattr(user, 'is_authenticated', False):
            if user_cls is not None and isinstance(user, user_cls):
                actor_obj = user
            name = _name_from(user)
        else:
            name = str(getattr(settings, 'AUDIT_FALLBACK_ACTOR', '') or '')

    if actor_name:
        name = _name_from(actor_name)
        actor_obj = None
    return actor_obj, name[:150]


def _resolve_remote_addr(request, remote_addr):
    if remote_addr:
        return str(remote_addr)[:64]
    if request is None:
        return ''
    # RealIPMiddleware 已把可信 IP 写回 REMOTE_ADDR，并记录在 _real_ip。
    value = getattr(request, '_real_ip', None) or request.META.get('REMOTE_ADDR', '')
    return str(value or '')[:64]


def audit_log(
    *,
    action,
    module='',
    request=None,
    instance=None,
    changes=None,
    message='',
    object_type=None,
    object_id=None,
    object_repr=None,
    actor=None,
    actor_name='',
    remote_addr=None,
    extra=None,
    event_type=AuditEventType.BUSINESS,
):
    """写一条审计记录，成功返回 AuditLog，写库失败只记运行日志并返回 None。

    禁止把 password / token / secret / cookie 传进 changes 或 extra：
    sanitize_value 会把敏感键的值替换为 ***。
    不接收 **kwargs：拼错参数名（msg= / ip=）必须显式报错，不能静默丢数据。
    event_type 只接受 AuditEventType 的三个值；None/空串静默回落，非法值告警后降级。
    """
    if request is None:
        request = get_current_request()

    if event_type is None or event_type == '':
        event_type = AuditEventType.BUSINESS.value
    else:
        candidate = str(event_type)
        if candidate not in AuditEventType.values:
            logger.warning('非法审计事件分类 event_type=%r，已降级为 business', event_type)
            candidate = AuditEventType.BUSINESS.value
        event_type = candidate

    actor_obj, resolved_name = _resolve_actor(request, actor, actor_name)

    auto_type, auto_id, auto_repr = object_info(instance)
    extras = sanitize_value(extra) if isinstance(extra, dict) else {}

    request_id = ''
    if request is not None:
        request_id = getattr(request, 'audit_request_id', '') or request.META.get('HTTP_X_REQUEST_ID', '')

    try:
        # 独立 savepoint：外层业务在 transaction.atomic() 中时，审计写入失败
        # 只回滚这个 savepoint，不会把整个外层事务标记为 broken（PG 下尤其致命）。
        with transaction.atomic():
            return AuditLog.objects.create(
                actor=actor_obj,
                actor_name=resolved_name,
                action=str(action)[:50],
                event_type=event_type,
                module=str(module)[:100],
                object_type=str(object_type or auto_type)[:100],
                object_id=str(object_id or auto_id)[:64],
                object_repr=str(object_repr or auto_repr)[:255],
                remote_addr=_resolve_remote_addr(request, remote_addr),
                request_id=str(request_id)[:64],
                changes=sanitize_value(changes) if isinstance(changes, dict) else {},
                message=str(message)[:MAX_MESSAGE_LENGTH],
                extra=extras,
            )
    except Exception:
        # 审计失败绝不能拖垮业务：任何异常（含 ValueError/类型错误）都只记运行日志。
        logger.exception('审计日志写入失败 action=%s module=%s', action, module)
        return None
