# django-web-base 接入契约 v2.3

凡从本模板派生的 Django 项目，均按本文件对齐。`./start.sh contract:check` 是它的机器可读形式；文档与检查器输出不一致时，以检查器为准。

**判定规则**：新项目从第 2 层起步；只接入底座强制能力的项目登记为 `tier: 1`；存在无法由 Web 进程承担的特权/守护组件时登记为 `tier: 3`。`tier` 表示接入定位，不是放宽第 1 层的等级——任何项目都必须满足第 1 层全部约束；`tier: 1` 与 `tier: 2` 的检查行为相同，区别只是第 2 层能力的接入承诺（前者不启用，后者默认全启用）。

检查器只读源码、不 import settings，因此不会触发目录创建或环境变量校验；目标机即使还没装齐 Django 依赖也能跑。

## 分层总览

| 层 | 定位 | 检查语义 |
| --- | --- | --- |
| 第 1 层 底座强制统一 | 目录结构、Auth/Axes/独立审计 app、中间件顺序、安全、ORM、裸 SQL | 不达标即 FAIL，退出码 1 |
| 第 2 层 底座可选能力 | jobs / 双模式 / 测试 | 软报告 PASS/INFO；`csrf_exempt` 记 WARN（`--strict` 下失败） |
| 第 3 层 外部系统边界 | Django 源码根之外的特权/守护组件 | 必须逐项登记；缺项记 WARN（`--strict` 下失败） |

## 第 1 层 · 底座强制统一（零例外）

| 编号 | 约束 | 机器判定 |
| --- | --- | --- |
| `layout_django_dir` | 保留标准目录结构 | 源码根下 `manage.py`、`config/`、`core/`、`common/` 均存在；源码根默认为 `django/`，项目根存在 `manage.py` 时即为项目根（本项目 2.6 起为此布局）；禁止把 app 摊平而缺 `config/`、`core/`、`common/` |
| `entry_wsgi` | 统一入口 `config.wsgi:application` | 源码根 `config/wsgi.py` 存在、AST 可解析，模块级定义 `application = get_wsgi_application()` |
| `settings_parse` | 配置可被静态解析 | 源码根 `config/settings.py` 存在且 AST 可解析 |
| `auth_user_model` | 用户模型固定在 `core.User` | settings 模块级显式 `AUTH_USER_MODEL = 'core.User'`；业务附属账号另建业务 App 模型，不得替换 `core.User` |
| `auth_app` / `auth_middleware` | Django Auth 是唯一认证体系 | `django.contrib.auth` 在 INSTALLED_APPS，`AuthenticationMiddleware` 在 MIDDLEWARE |
| `admin_app` | Admin 纳入底座 | `django.contrib.admin` 在 INSTALLED_APPS（由 v1 第 2 层升格） |
| `audit_app` / `audit_service` | 统一审计纳入底座 | `audit.apps.AuditConfig` 在 INSTALLED_APPS，源码根 `audit/models.py` 定义 `AuditLog`、`audit/services.py` 定义 `audit_log()`；禁止第三方审计插件 |
| `audit_single_entry` | 审计只走统一入口 | 业务代码（排除源码根 `audit/` 自身与测试）不得直接调 `AuditLog.objects.create/bulk_create/update/bulk_update/delete`，也不得 `AuditLog(...).save()/delete()` 或 `AuditLog.objects.filter(...).delete()` 等链式写法，只能 `audit_log()` |
| `audit_append_only` | 审计只追加不可改 | 源码根 `audit/models.py` 定义 `AuditLogQuerySet`（拦 update/bulk_update/delete）与 `AuditLogImmutable`；唯一删除通道为 `expire_audit_logs()` |
| `axes_app` / `axes_middleware` / `axes_backend` | 防爆破三件套齐全 | `axes`、`AxesMiddleware`、`AxesStandaloneBackend` 三处均在位 |
| `real_ip_middleware` | 统一真实 IP 中间件 | `common.middleware.real_ip.RealIPMiddleware` 在 MIDDLEWARE |
| `middleware_order` | 中间件顺序固定 | `RealIPMiddleware` → `AuditContextMiddleware` → `AuthenticationMiddleware`，且 `AxesMiddleware` 为最后一项 |
| `security_middleware` | 安全中间件不摘除 | `SecurityMiddleware` 与 `CsrfViewMiddleware` 在 MIDDLEWARE |
| `no_csrf_bypass` | 不得绕过 CSRF | 源码根内不得给 `_dont_enforce_csrf_checks` 赋值 |
| `login_url` | 登录跳转显式声明 | settings 模块级显式给 `LOGIN_URL` 赋值 |
| `secret_key_literal` | 密钥不硬编码 | settings 存在 `SECRET_KEY` 赋值且不是 `django-insecure...` 字面量 |
| `health_and_media_urls` | 健康检查与媒体鉴权就位 | 源码根 `config/urls.py` 存在含 `SELECT 1` 字面量的函数，且引用 `staff_member_required` 视图 |
| `orm_and_migrations` | 数据访问走 ORM | 源码根 `core/models.py` 使用 Django ORM 模型，源码根下存在 `migrations/0*.py` |
| `no_raw_sql_in_django` | Web 进程内禁止裸 SQL | 源码根内不得调用 `sqlite3/pymysql/psycopg2/psycopg/mysql.connector` 的 `connect()`；不得调用 `executemany` / `executescript`；游标/连接对象上的 `execute` 或首参为 SQL 语句字面量的 `execute` 同样禁止；ORM 逃生舱 `RawSQL()` / `expressions.RawSQL()` 也禁止；`.raw()` / `.extra()` 仅在 ORM 形态（`objects` / `_default_manager` / `qs` / `queryset` 链、SQL 字面量首参、`extra(select=.../where=.../tables=...)` 等）命中（`client.raw()`、`parser.extra()` 等普通业务方法不算）。AST 判定，内置豁免**仅限** 源码根 `config/urls.py` 的 `SELECT 1` 与 `common/apps.py` 的 `PRAGMA` 初始化；**不提供业务白名单** |
| `contract_marker` | 契约登记合法 | 项目根 `.contract.json` 合法，`contract == "v2.3"`，`tier` 属于 1/2/3 |

自己写登录、自己起 server、改目录结构、在源码根内直连数据库 —— 一律不合规。

## 第 2 层 · 底座可选能力（软报告）

只报告「已用/未用」，不拦截交付：`jobs_app`、`dual_mode`、`tests`。

**启用同类能力时必须复用底座模块，禁止自造轮子**：任务走 `jobs` + `JobService.dispatch`，邮件走 `common.email`，IP 统一用 `common.utils.ip.get_client_ip`，审计走独立 `audit` app 的 `audit.services.audit_log()`。此项是设计条款，检查器只报告能力启用状态，不自动判定是否自造轮子。

`csrf_exempt_usage` 是 WARN 项：扫描源码根内非测试文件的 `csrf_exempt` 装饰器/调用，出现即提醒改用标准 CSRF（如通过 `CSRF_HEADER_NAME` 对齐既有前端），`--strict` 下退出码 1。

## 第 3 层 · 外部系统边界（仅针对 Django 源码根之外）

凡因**安全隔离、特权执行（sudo）、守护进程钩子或依赖前预检**而物理上无法由 Django 进程承担的组件，必须放在 Django 源码根之外（推荐 `system/`）。本层判定只看组件位置与登记信息，与业务领域无关。

这些外部组件必须在 `.contract.json` 的 `external_boundaries` 数组中逐项登记，每项包含：

| 字段 | 含义 |
| --- | --- |
| `path` | 相对项目根路径，不得落在 Django 代码目录内（子目录布局为 `django/`，根布局为 `config/`、`core/`、`ovpn/` 等 app 目录）、不得包含 `..`，且必须是真实存在的文件（不是仅登记目录名） |
| `privilege` | 运行特权，如 `root` / `daemon` / `unprivileged` |
| `reason` | 为何不能进 Django 进程 |
| `interface` | 与 Django 的通信方式 |

声明 `tier: 3` 但 `external_boundaries` 为空、缺必填字段、`notes` 为空，或登记路径为绝对路径、含 `..`、落在 Django 代码目录内部、不存在或只是目录 —— 记 WARN（`--strict` 下退出码 1）。非 tier 3 项目登记了非法条目同样记 WARN。

## 使用

```bash
./start.sh contract:check                  # 检查当前项目
./start.sh contract:check /path/to/proj    # 检查指定项目
./start.sh contract:check --json           # 机器可读报告
./start.sh contract:check --strict         # WARN 也视为失败
```

退出码：`0` = 无 FAIL（可带 WARN，`--strict` 下 WARN 也算失败），`1` = 存在 FAIL，或 `--strict` 下存在 WARN。

## 契约登记文件

```json
{
  "contract": "v2.3",
  "tier": 2,
  "project": "django-web-base",
  "role": "reference",
  "notes": "",
  "external_boundaries": []
}
```

v1 的 `raw_sql_allowlist` 已取消：源码根内裸 SQL 一律 FAIL，只有检查器内置的两处例外。

## 版本演进

`contract` 值与本文件头部版本一致。**新增或收紧检查项升次版本**（v1 → v2 → v2.1 → v2.2 → v2.3），已登记项目不会因模板升级被静默判为不合规；措辞与示例修正不升版本。v2.3 收紧了 `RawSQL` 属性链与审计链式删除检测，同时修正 `.raw()` / `.extra()` 对普通业务方法的误报。
