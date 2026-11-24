"""审计数据脱敏与对象信息提取。

所有进入 changes / extra 的数据都要经过这里，避免密码、Token、Cookie 落库。
"""
import math

SENSITIVE_KEYWORDS = (
    'password', 'passwd', 'pwd', 'token', 'secret', 'cookie',
    'session', 'csrf', 'authorization', 'api_key', 'apikey', 'access_key',
    'private_key', 'credential', 'bearer', 'jwt', 'salt',
)
MAX_SANITIZE_DEPTH = 8
MAX_SANITIZE_ITEMS = 200
MAX_SANITIZE_STRING = 2000


def is_sensitive_key(key):
    lowered = str(key).lower()
    return any(word in lowered for word in SENSITIVE_KEYWORDS)


def sanitize_value(value, _depth=0):
    """递归脱敏；不可序列化对象退化为字符串，保证 JSONField 永不写失败。

    限制递归深度、容器条目数和字符串长度：循环引用或超大 dict 不会递归爆栈，
    也不会把单行 JSONField 撑爆。截断统一加 '…' 标记。
    """
    if _depth >= MAX_SANITIZE_DEPTH:
        return '…(truncated)'
    if isinstance(value, dict):
        return {
            str(k): (
                '***' if is_sensitive_key(k)
                else sanitize_value(v, _depth + 1)
            )
            for k, v in list(value.items())[:MAX_SANITIZE_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_value(v, _depth + 1) for v in list(value)[:MAX_SANITIZE_ITEMS]]
    # NaN / Infinity 不是合法 JSON 数值，PG JSONB 会拒绝写入；先退化成字符串。
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return value[:MAX_SANITIZE_STRING]
    return str(value)[:MAX_SANITIZE_STRING]


def sanitize_changes(changes, exclude_fields=()):
    """过滤敏感键与排除字段后返回可安全入库的字典。"""
    if not isinstance(changes, dict):
        return {}
    excluded = set(exclude_fields or ())
    return {
        str(key): sanitize_value(value)
        for key, value in changes.items()
        if not is_sensitive_key(key) and key not in excluded
    }


def serialize_value(value):
    """字段值入库前统一递归脱敏，保留 JSON 结构。

    不能再先把 dict/list json.dumps 成字符串：那样 JSON 内部的 password / token
    键会逃过 sanitize_value 的递归识别。这里直接复用 sanitize_value，字典仍是
    字典、列表仍是列表、敏感键递归打码、None 保留为 None。
    """
    return sanitize_value(value)


def _field_of(obj, name):
    """取模型字段定义，取不到返回 None（非模型字段/注解忽略）。"""
    meta = getattr(obj, '_meta', None)
    if meta is None:
        return None
    try:
        return meta.get_field(name)
    except Exception:
        return None


def object_info(instance):
    """从模型实例提取 object_type / object_id / object_repr，不建立外键。"""
    if instance is None:
        return '', '', ''
    meta = getattr(instance, '_meta', None)
    object_type = getattr(meta, 'label', type(instance).__name__) if meta else type(instance).__name__
    object_id = str(getattr(instance, 'pk', '') or '')
    try:
        object_repr = str(instance)[:255]
    except Exception:
        object_repr = object_type
    return object_type, object_id, object_repr


def model_to_changes(old_obj, new_obj, changed_fields, exclude_fields=()):
    """把 form.changed_data 转成 {field: {old, new}} 结构。

    - 跳过多对多字段：save_model 阶段 M2M 尚未写入，getattr 只能拿到
      ManyRelatedManager，str() 后是无意义的 "auth.Group.None"，由 Admin 的
      save_related 单独记录增删；
    - 外键取 <field>_id，避免触发额外查询且记录稳定。
    """
    changes = {}
    excluded = set(exclude_fields or ())
    for name in changed_fields or ():
        field_name = str(name)
        if field_name in excluded or is_sensitive_key(field_name):
            continue
        field = _field_of(new_obj, field_name)
        if field is not None and getattr(field, 'many_to_many', False):
            continue
        attr = f'{field_name}_id' if field is not None and getattr(field, 'many_to_one', False) else field_name
        if not hasattr(new_obj, attr):
            continue
        old_value = serialize_value(getattr(old_obj, attr, None)) if old_obj is not None else None
        new_value = serialize_value(getattr(new_obj, attr, None))
        changes[field_name] = {'old': old_value, 'new': new_value}
    return changes


def m2m_changes(old_pks, new_obj, field_name, exclude_fields=()):
    """对比已物化的旧 pk 集合与实例当前的 M2M pk 集合，返回增删字典。

    old_pks 必须是集合（在写库前 get() 出来），不能是模型实例——M2M manager
    是延迟查询，改动后旧实例再查会得到新值，导致 added/removed 永远为空。
    """
    field_name = str(field_name)
    if field_name in set(exclude_fields or ()) or is_sensitive_key(field_name):
        return {}
    if not hasattr(new_obj, field_name):
        return {}
    try:
        new_ids = set(getattr(new_obj, field_name).values_list('pk', flat=True))
    except Exception:
        return {}
    old_ids = set(old_pks or ())
    added = sorted(str(pk) for pk in (new_ids - old_ids))
    removed = sorted(str(pk) for pk in (old_ids - new_ids))
    return {'added': added, 'removed': removed} if added or removed else {}
