#!/usr/bin/env python3
# scripts/contract_check.py
"""接入契约 v2 的机器可读检查器（CONTRACT.md 的判定实现）。

只用标准库 + AST 读源码，绝不 import Django settings：
  - 不触发 import 阶段的目录创建 / 环境变量校验
  - 目标机未装齐 Django 依赖时也能检查
  - 用 AST 而非正则判定裸 SQL / CSRF 绕过，避免测试字符串与注释误报

退出码：0 = 合规（可带 WARN），1 = 有 FAIL，或 --strict 下有 WARN。
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

CONTRACT_VERSION = "v2"
VALID_TIERS = (1, 2, 3)

TIER_TITLES = {
    1: "第 1 层 底座强制统一",
    2: "第 2 层 底座可选能力",
    3: "第 3 层 外部系统边界",
}

# 不做业务扫描的目录：虚拟环境、依赖包、构建产物、迁移记录、便携运行时、运行期数据
IGNORED_DIRS = {
    "__pycache__", "site-packages", "dist-packages", "node_modules",
    "build", "dist", ".git", "migrations",
    # 便携运行时与离线依赖、运行期数据目录，不属于源码
    "runtime", "vendor", "data", "staticfiles", "media", "logs",
}

CONNECT_MODULES = {
    "sqlite3", "pymysql", "psycopg2", "psycopg", "mysql.connector",
}
# 数据库游标/连接的常见变量名，只有这些对象上的 execute 才按裸 SQL 处理
DB_OBJECT_NAMES = {"cursor", "cur", "con", "conn", "connection", "db"}
DB_EXEC_METHODS = {"execute", "executemany", "executescript"}
SQL_PREFIXES = (
    "SELECT ", "INSERT ", "UPDATE ", "DELETE ", "CREATE ", "DROP ",
    "ALTER ", "REPLACE ", "WITH ", "PRAGMA ",
)

REAL_IP = "common.middleware.real_ip.RealIPMiddleware"
AUDITLOG_MW = "auditlog.middleware.AuditlogMiddleware"
AXES_MW = "axes.middleware.AxesMiddleware"


@dataclass(frozen=True)
class Finding:
    level: str  # PASS / INFO / WARN / FAIL
    tier: int
    code: str
    message: str
    hint: str = ""


class Report:
    def __init__(self, root: Path, marker: dict, tier: int):
        self.root = root
        self.marker = marker
        self.tier = tier
        self.findings: list[Finding] = []

    def check(self, ok: bool, level: str, tier: int, code: str,
              message: str, hint: str = "") -> None:
        self.findings.append(
            Finding("PASS" if ok else level, tier, code, message, "" if ok else hint)
        )

    def info(self, used: bool, tier: int, code: str,
             message: str, hint: str = "") -> None:
        self.findings.append(
            Finding("PASS" if used else "INFO", tier, code,
                    message if used else f"未启用：{message}", "" if used else hint)
        )

    def of(self, level: str) -> list[Finding]:
        return [f for f in self.findings if f.level == level]

    @property
    def fails(self) -> list[Finding]:
        return self.of("FAIL")

    @property
    def warns(self) -> list[Finding]:
        return self.of("WARN")

    @property
    def status(self) -> str:
        if self.fails:
            return "NONCOMPLIANT"
        if self.warns:
            return "COMPLIANT_WITH_NOTES"
        return "COMPLIANT"


# --------------------------------------------------------------------------
# 基础读取 / AST
# --------------------------------------------------------------------------
def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def parse_python(path: Path) -> tuple[ast.Module | None, str | None]:
    text = read_text(path)
    if text is None:
        return None, None
    try:
        return ast.parse(text), text
    except SyntaxError:
        return None, text


def list_sequence(tree: ast.Module | None, name: str) -> list[str] | None:
    """按源码顺序收集模块级 NAME = [...] / NAME += [...] 中的字符串常量。

    顺序有语义：校验中间件先后、simpleui 必须排在 admin 之前。
    返回 None 表示该变量没有模块级赋值；空列表表示有赋值但无字符串元素。
    """
    if tree is None:
        return None
    found: list[str] | None = None

    def absorb(value: ast.AST) -> None:
        nonlocal found
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            strings = [
                elt.value for elt in value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]
        elif isinstance(value, ast.Constant) and isinstance(value.value, str):
            strings = [value.value]
        else:
            return
        if found is None:
            found = []
        found.extend(strings)

    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                absorb(node.value)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                absorb(node.value)
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                absorb(node.value)
    return found


def assignment_node(tree: ast.Module | None, name: str) -> ast.AST | None:
    if tree is None:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                return node
    return None


def dotted_name(node: ast.AST) -> str:
    """把 Name / Attribute 链还原成点分字符串，失败返回空串。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


def iter_python_files(root: Path) -> list[tuple[str, Path, ast.Module]]:
    """遍历 django/ 下可扫描的 .py（项目根相对路径, 绝对路径, AST）。"""
    src_root = root / "django"
    if not src_root.is_dir():
        return []
    files: list[tuple[str, Path, ast.Module]] = []
    for path in sorted(src_root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        parts = Path(rel).parts
        if any(p in IGNORED_DIRS for p in parts):
            continue
        if any(p.startswith(".venv") or p == "venv" for p in parts):
            continue
        tree, _ = parse_python(path)
        if tree is not None:
            files.append((rel, path, tree))
    return files


def is_test_file(rel: str) -> bool:
    name = Path(rel).name
    return name == "tests.py" or name.startswith("test_") or name.endswith("_test.py")


# --------------------------------------------------------------------------
# 专用 AST 扫描
# --------------------------------------------------------------------------
def _is_db_exec_call(node: ast.Call, method: str) -> bool:
    """判断 execute/executemany/executescript 是否为数据库调用，避免误伤业务类。

    executemany / executescript 是数据库 API 专有方法，直接命中。
    execute 需要满足以下任一条件：
      - 调用对象名属于游标/连接常见名（cursor / cur / con / conn / connection / db）
      - 形如 *.cursor().execute(...)
      - 第一个参数是 SQL 语句字面量
    """
    if method in ("executemany", "executescript"):
        return True

    head, _, _ = dotted_name(node.func).rpartition(".")
    if head.rpartition(".")[2] in DB_OBJECT_NAMES:
        return True

    value = node.func.value
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "cursor"
    ):
        return True

    first = node.args[0] if node.args else None
    return (
        isinstance(first, ast.Constant)
        and isinstance(first.value, str)
        and first.value.strip().upper().startswith(SQL_PREFIXES)
    )


def scan_raw_sql(root: Path) -> list[str]:
    """AST 判定 django/ 内的数据库连接/执行调用。

    命中规则：
      - <sqlite3|pymysql|psycopg2|psycopg|mysql.connector>.connect(...)
      - executemany / executescript 调用
      - 游标/连接对象上的 execute，或第一个参数为 SQL 语句字面量的 execute
    内置豁免严格按文件：
      - django/common/apps.py 的 PRAGMA 连接初始化
      - django/config/urls.py 的 SELECT 1 健康检查（大小写不敏感）
    业务对象上的 runner.execute('install') 之类不算裸 SQL。
    """
    apps_rel = "django/common/apps.py"
    health_rel = "django/config/urls.py"
    hits: list[str] = []
    for rel, _path, tree in iter_python_files(root):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            full = dotted_name(node.func)
            head, _, method = full.rpartition(".")

            if method == "connect" and head in CONNECT_MODULES:
                hits.append(f"{rel}:{node.lineno} ({full}())")
                continue
            if method not in DB_EXEC_METHODS:
                continue

            first = node.args[0] if node.args else None
            sql = None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                sql = " ".join(first.value.split()).upper()
            if sql is not None:
                if sql.startswith("PRAGMA ") and rel == apps_rel:
                    continue
                if sql == "SELECT 1" and rel == health_rel:
                    continue

            if _is_db_exec_call(node, method):
                hits.append(f"{rel}:{node.lineno} ({method})")
    return sorted(set(hits))


def scan_csrf_bypass(root: Path) -> list[str]:
    """查找给 _dont_enforce_csrf_checks 属性赋值的位置（第 1 层 FAIL）。"""
    hits: list[str] = []
    for rel, _path, tree in iter_python_files(root):
        for node in ast.walk(tree):
            targets: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "_dont_enforce_csrf_checks":
                    hits.append(f"{rel}:{node.lineno}")
    return sorted(set(hits))


def scan_csrf_exempt(root: Path) -> list[str]:
    """查找非测试文件里的 csrf_exempt 装饰器/调用（第 2 层 WARN）。"""
    hits: list[str] = []
    for rel, _path, tree in iter_python_files(root):
        if is_test_file(rel):
            continue
        for node in ast.walk(tree):
            is_exempt = (
                (isinstance(node, ast.Name) and node.id == "csrf_exempt")
                or (isinstance(node, ast.Attribute) and node.attr == "csrf_exempt")
            )
            if is_exempt:
                hits.append(f"{rel}:{node.lineno}")
    return sorted(set(hits))


def has_orm_models(root: Path) -> bool:
    tree, _ = parse_python(root / "django" / "core" / "models.py")
    if tree is None:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            name = dotted_name(base)
            if name == "AbstractUser" or name.endswith(".Model"):
                return True
    return False


def check_urls_security(root: Path) -> bool:
    """urls.py 必须同时含 SELECT 1 健康检查与 staff_member_required 媒体视图。"""
    tree, _ = parse_python(root / "django" / "config" / "urls.py")
    if tree is None:
        return False
    has_select = any(
        isinstance(n, ast.Constant) and isinstance(n.value, str) and "SELECT 1" in n.value
        for n in ast.walk(tree)
    )
    has_staff = any(
        (isinstance(n, ast.Name) and n.id == "staff_member_required")
        or (isinstance(n, ast.Attribute) and n.attr == "staff_member_required")
        for n in ast.walk(tree)
    )
    return has_select and has_staff


def check_auth_user_model(settings_tree: ast.Module | None) -> bool:
    node = assignment_node(settings_tree, "AUTH_USER_MODEL")
    return (
        isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and node.value.value == "core.User"
    )


def check_middleware_order(middleware_seq: list[str] | None) -> bool:
    seq = middleware_seq or []
    try:
        real_ip_at = seq.index(REAL_IP)
        auditlog_at = seq.index(AUDITLOG_MW)
        axes_at = seq.index(AXES_MW)
    except ValueError:
        return False
    return real_ip_at < auditlog_at and real_ip_at < axes_at and axes_at == len(seq) - 1


def check_external_boundaries(root: Path, marker: dict, tier: int) -> list[str]:
    """返回第 3 层 WARN 列表；非 tier 3 项目也校验已登记条目的合法性。"""
    problems: list[str] = []
    entries = marker.get("external_boundaries", [])
    if not isinstance(entries, list):
        return ["external_boundaries 必须是数组"]

    required = ("path", "privilege", "reason", "interface")
    for idx, item in enumerate(entries):
        if not isinstance(item, dict):
            problems.append(f"external_boundaries[{idx}] 必须是对象")
            continue
        for field in required:
            if not isinstance(item.get(field), str) or not item[field].strip():
                problems.append(f"external_boundaries[{idx}].{field} 缺失或为空")
        path = item.get("path")
        if isinstance(path, str) and path.strip():
            normalized = path.replace("\\", "/")
            parts = Path(normalized).parts
            if Path(path).is_absolute():
                problems.append(f"external_boundaries[{idx}].path 必须是相对路径：{path}")
            if ".." in parts:
                problems.append(f"external_boundaries[{idx}].path 不得包含 ..：{path}")
            stripped = normalized.lstrip("/")
            if stripped == "django" or stripped.startswith("django/"):
                problems.append(
                    f"external_boundaries[{idx}].path 不得落在 django/ 内：{path}"
                )
            if not (root / path).is_file():
                problems.append(f"external_boundaries[{idx}].path 不存在或不是文件：{path}")

    if tier == 3:
        if not entries:
            problems.append("tier 3 必须登记 external_boundaries")
        if not str(marker.get("notes") or "").strip():
            problems.append("tier 3 必须填写 notes（设计决策与技术债）")
    return problems


def load_marker(root: Path) -> tuple[dict, str | None]:
    path = root / ".contract.json"
    text = read_text(path)
    if text is None:
        return {}, "缺少项目根 .contract.json"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f".contract.json 不是合法 JSON：{exc.msg}"
    if not isinstance(data, dict):
        return {}, ".contract.json 顶层必须是对象"
    if data.get("contract") != CONTRACT_VERSION:
        return data, (
            f"契约版本不匹配：声明 {data.get('contract')!r}，"
            f"当前检查器为 {CONTRACT_VERSION}"
        )
    if data.get("tier") not in VALID_TIERS:
        return data, f"tier 必须是 {VALID_TIERS} 之一，当前为 {data.get('tier')!r}"
    return data, None


# --------------------------------------------------------------------------
# 主检查
# --------------------------------------------------------------------------
def check_project(root: Path) -> Report:
    root = Path(root).resolve()
    marker, marker_error = load_marker(root)
    tier = marker.get("tier") if marker.get("tier") in VALID_TIERS else 2
    report = Report(root, marker, tier)

    src_root = root / "django"
    settings_path = src_root / "config" / "settings.py"
    settings_tree, settings_source = parse_python(settings_path)
    apps = list_sequence(settings_tree, "INSTALLED_APPS") or []
    middleware = list_sequence(settings_tree, "MIDDLEWARE") or []
    backends = list_sequence(settings_tree, "AUTHENTICATION_BACKENDS") or []

    # ================= 第 1 层：底座强制统一 =================
    required_layout = ["manage.py", "config", "core", "common"]
    missing_layout = [p for p in required_layout if not (src_root / p).exists()]
    report.check(
        not missing_layout, "FAIL", 1, "layout_django_dir",
        "保留标准 django/ 目录结构（manage.py / config / core / common）",
        "缺失或改名：" + ", ".join("django/" + p for p in missing_layout),
    )

    wsgi_tree, wsgi_source = parse_python(src_root / "config" / "wsgi.py")
    wsgi_node = assignment_node(wsgi_tree, "application") if wsgi_tree else None
    report.check(
        wsgi_tree is not None and wsgi_node is not None
        and "get_wsgi_application" in (wsgi_source or ""),
        "FAIL", 1, "entry_wsgi",
        "统一入口 django/config/wsgi.py 暴露 application",
        "保留模板的 config.wsgi:application，不要改用自定义 server 入口",
    )

    report.check(
        settings_tree is not None, "FAIL", 1, "settings_parse",
        "settings.py 存在且可静态解析",
        f"检查 {settings_path} 是否存在、语法是否正确",
    )

    report.check(
        check_auth_user_model(settings_tree), "FAIL", 1, "auth_user_model",
        "AUTH_USER_MODEL 显式固定为 'core.User'",
        "业务附属账号另建业务 App 模型，不得替换 core.User",
    )

    report.check("django.contrib.auth" in apps, "FAIL", 1, "auth_app",
                 "INSTALLED_APPS 含 django.contrib.auth")
    report.check("django.contrib.auth.middleware.AuthenticationMiddleware" in middleware,
                 "FAIL", 1, "auth_middleware", "MIDDLEWARE 含 AuthenticationMiddleware")
    report.check("django.contrib.admin" in apps, "FAIL", 1, "admin_app",
                 "INSTALLED_APPS 含 django.contrib.admin")
    report.check("auditlog" in apps, "FAIL", 1, "auditlog_app",
                 "INSTALLED_APPS 含 auditlog")
    report.check(AUDITLOG_MW in middleware, "FAIL", 1, "auditlog_middleware",
                 "MIDDLEWARE 含 AuditlogMiddleware")
    report.check("axes" in apps, "FAIL", 1, "axes_app", "INSTALLED_APPS 含 axes")
    report.check(AXES_MW in middleware, "FAIL", 1, "axes_middleware",
                 "MIDDLEWARE 含 AxesMiddleware")
    report.check("axes.backends.AxesStandaloneBackend" in backends,
                 "FAIL", 1, "axes_backend", "AUTHENTICATION_BACKENDS 含 AxesStandaloneBackend")
    report.check(REAL_IP in middleware, "FAIL", 1, "real_ip_middleware",
                 "MIDDLEWARE 含 RealIPMiddleware")
    report.check(
        check_middleware_order(middleware), "FAIL", 1, "middleware_order",
        "RealIP 在 Auditlog/Axes 之前，且 AxesMiddleware 为最后一项",
        f"当前顺序：{' → '.join(middleware) if middleware else '(空)'}",
    )
    report.check(
        {"django.middleware.security.SecurityMiddleware",
         "django.middleware.csrf.CsrfViewMiddleware"} <= set(middleware),
        "FAIL", 1, "security_middleware",
        "SecurityMiddleware 与 CsrfViewMiddleware 均在 MIDDLEWARE",
    )

    csrf_bypass = scan_csrf_bypass(root)
    report.check(
        not csrf_bypass, "FAIL", 1, "no_csrf_bypass",
        "django/ 内无 _dont_enforce_csrf_checks 绕过",
        "发现位置：" + ", ".join(csrf_bypass) if csrf_bypass else "",
    )

    report.check(
        assignment_node(settings_tree, "LOGIN_URL") is not None,
        "FAIL", 1, "login_url", "LOGIN_URL 已显式声明",
        "settings 中显式设置 LOGIN_URL（模板为 /admin/login/）",
    )

    secret_node = assignment_node(settings_tree, "SECRET_KEY")
    secret_literal = (
        isinstance(secret_node, ast.Assign)
        and isinstance(secret_node.value, ast.Constant)
        and isinstance(secret_node.value.value, str)
        and secret_node.value.value.startswith("django-insecure")
    )
    report.check(
        secret_node is not None and not secret_literal, "FAIL", 1,
        "secret_key_literal", "SECRET_KEY 由环境变量提供，未硬编码 insecure 值",
    )

    report.check(
        check_urls_security(root), "FAIL", 1, "health_and_media_urls",
        "urls.py 含 SELECT 1 健康检查与 staff_member_required 媒体鉴权视图",
    )

    report.check(
        has_orm_models(root) and any(src_root.glob("*/migrations/0*.py")),
        "FAIL", 1, "orm_and_migrations",
        "core/models.py 使用 ORM 且存在 migrations/0*.py",
    )

    raw_sql = scan_raw_sql(root)
    report.check(
        not raw_sql, "FAIL", 1, "no_raw_sql_in_django",
        "django/ 内无业务裸 SQL（仅内置豁免 SELECT 1 / PRAGMA）",
        "命中：" + ", ".join(raw_sql) if raw_sql else "",
    )

    report.check(
        not marker_error, "FAIL", 1, "contract_marker",
        "契约标记合法且版本匹配",
        marker_error or "",
    )

    # ================= 第 2 层：底座可选能力（软报告） =================
    report.info("jobs" in apps, 2, "jobs_app", "jobs 已启用")
    simpleui_at = apps.index("simpleui") if "simpleui" in apps else -1
    admin_at = apps.index("django.contrib.admin") if "django.contrib.admin" in apps else -1
    report.info(
        simpleui_at >= 0 and admin_at >= 0 and simpleui_at < admin_at,
        2, "simpleui_app", "simpleui 已启用且排在 admin 之前",
    )
    report.info("import_export" in apps, 2, "import_export_app", "import_export 已启用")
    report.info("STANDALONE" in (settings_source or ""), 2, "dual_mode", "保留双模式开关")
    report.info(
        any(is_test_file(rel) for rel, _path, _tree in iter_python_files(root)),
        2, "tests", "存在自动化测试",
    )

    csrf_exempt_hits = scan_csrf_exempt(root)
    report.check(
        not csrf_exempt_hits, "WARN", 2, "csrf_exempt_usage",
        "未使用 csrf_exempt（建议改走 CSRF_HEADER_NAME 对齐前端）",
        "发现位置：" + ", ".join(csrf_exempt_hits) if csrf_exempt_hits else "",
    )

    # ================= 第 3 层：外部系统边界（软报告） =================
    boundary_problems = check_external_boundaries(root, marker, tier)
    report.check(
        not boundary_problems, "WARN", 3, "external_boundaries",
        "外部边界登记完整",
        "；".join(boundary_problems),
    )

    return report


def render_text(report: Report) -> str:
    lines = [
        f"{report.root.name} — 接入契约 {CONTRACT_VERSION} "
        f"（tier {report.tier} / {report.marker.get('role', 'unspecified')}）",
        "",
    ]
    for tier, title in TIER_TITLES.items():
        items = [f for f in report.findings if f.tier == tier]
        if not items:
            continue
        passed = sum(1 for f in items if f.level == "PASS")
        actionable = sum(1 for f in items if f.level in ("FAIL", "WARN"))
        lines.append(f"[{title}]  {passed} PASS / {actionable} 待处理")
        for finding in items:
            lines.append(f"  {finding.level:<5} {finding.code:<24} {finding.message}")
            if finding.hint:
                lines.append(f"          ↳ {finding.hint}")
        lines.append("")

    status_labels = {
        "COMPLIANT": "COMPLIANT  合规",
        "COMPLIANT_WITH_NOTES": "COMPLIANT WITH NOTES  合规，但有标注项",
        "NONCOMPLIANT": "NONCOMPLIANT  不合规",
    }
    lines.append(
        f"结论：{status_labels[report.status]} — "
        f"{len(report.fails)} FAIL / {len(report.warns)} WARN"
    )
    return "\n".join(lines)


def build_json(report: Report) -> dict:
    return {
        "contract": CONTRACT_VERSION,
        "project": report.root.name,
        "root": str(report.root),
        "tier": report.tier,
        "role": report.marker.get("role", ""),
        "status": report.status,
        "summary": {
            level: len(report.of(level))
            for level in ("PASS", "INFO", "WARN", "FAIL")
        },
        "findings": [asdict(f) for f in report.findings],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="检查 Django 项目是否符合 django-web-base 接入契约 v2"
    )
    parser.add_argument("root", nargs="?", default=".", help="项目根目录（默认当前目录）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--strict", action="store_true", help="WARN 也视为失败")
    args = parser.parse_args(argv)

    report = check_project(Path(args.root))
    if args.json:
        print(json.dumps(build_json(report), ensure_ascii=False, indent=2))
    else:
        print(render_text(report))

    if report.fails or (args.strict and report.warns):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
