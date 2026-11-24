# Django App 开发指南（openvpn-web 3.0）

面向「在现有底座上加一个 app / 一个接口 / 一个特权动作」的实际操作。目录职责见
[PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md)，契约判定见 [CONTRACT.md](../CONTRACT.md)。

---

## 一、新增一个底座 app（很少需要）

底座 app 只有四个：`accounts` / `common` / `audit` / `jobs`。再加一个之前先问：
**它是通用基础设施吗？** 不是就放进 `ovpn`，不要为了「整齐」拆 app。

必须做的事：

1. `python django/manage.py startapp <name> django/<name>`
2. `apps.py` 定义 `<Name>Config`，`name = '<name>'`，给中文 `verbose_name`
3. `config/settings.py` 的 `BASE_APPS` 里加**完整路径**：`'<name>.apps.<Name>Config'`
4. 有模型就 `makemigrations <name>`，迁移文件随 app 走
5. 写 `tests.py`（至少覆盖模型约束与一条主流程）
6. `python scripts/contract_check.py --strict` 必须 0 FAIL / 0 WARN

**禁止**：`common` / `accounts` / `audit` / `jobs` 里 `import ovpn`（契约 `layering` 会 FAIL）。

---

## 二、新增一个 REST 接口

前端是单页应用，没有 DRF，全部手写视图 + JSON 响应。

### 1. 写接口（有副作用）

```python
# ovpn/views/users.py
def users_lock(request, name):
    d = jbody(request)                      # 统一 JSON body 解析
    core.set_user_enabled(name, False, actor=actor(request))
    core.log_audit(actor(request), "user.lock", name)
    return ok({"msg": "已停用"})
```

```python
# ovpn/urls.py
path("api/users/<name>/lock",
     write_only(views.users_lock), name="user_lock"),
```

- **必须** `write_only(...)`：非 POST/PUT/PATCH/DELETE 直接 405。GET 带副作用是最常见
  的 CSRF 提权入口，契约与安全审查都盯着这条。
- 参数非法就 `raise ValueError("...")`，`ovpn/middleware.py` 会统一转成 400 JSON
  `{ok: false, msg}`；不要自己 `HttpResponse(status=400)`。
- 成功统一 `ok({...})`，字段名与前端 `index.html` 对齐；**不要改既有字段名**。

### 2. 只读接口

简单查询直接调 `services`；跨多个服务聚合的放 `selectors.py`：

```python
# ovpn/selectors.py
def user_detail(name):
    user = core.user_by_name(name)
    return {"user": user, "grants": core.grants_of_user(name),
            "online": bool(core.online_users().get(name))}
```

```python
def api_user_detail(request, name):
    return ok(selectors.user_detail(name))
```

### 3. 前端对接

`index.html` 里所有请求都带 `X-CSRF-Token`（来自 `/api/session` 的 `csrf` 字段）。
新接口不需要额外改 CSRF 配置。

---

## 三、新增业务逻辑（services）

`ovpn/services/` 按子域拆分，**不要再新建 `core.py` 这种巨型文件**。

- 新函数加到最贴近的子域模块（`users` / `assets` / `grants` / `userdata` …）。
- 需要被外部调用：在模块 `__all__` 里登记，`services/__init__.py` 已 `import *`。
- 需要被 `system/core.py` 内部纯函数回调：加入 `_PATCH_INTO_BASE` 列表，否则
  `system.core` 里的 `online_users()` 之类仍走 SQLite 版本。
- 子模块间依赖只允许 `base ← audit ← users ← userdata`、`assets ← grants` 这个方向；
  新加反向依赖要重新设计，不要用函数内 import 绕。
- 数据访问只用 Django ORM。源码根内禁止 `sqlite3.connect` / `cursor.execute` /
  `RawSQL` / `.raw()` / `.extra()`（契约 `no_raw_sql_in_django`）。

---

## 四、新增特权动作（要 root）

特权动作一律走 `system/worker.py`，Web 侧只投递任务文件：

```python
# system/worker.py
ACTIONS = {..., "sync-crl": action_sync_crl}

def action_sync_crl(p):
    run([core.OPENSSL, "ca", "-gencrl", ...], check=True)
```

Web 侧：

```python
tid = uuid.uuid4().hex
sudo_worker(tid, "sync-crl")          # 由 bridge 写 data/tasks/<tid>.json 并 sudo
```

规则：

- 动作参数从 `data/tasks/<tid>.json` 读，**不接受命令行自由参数**（sudoers 只放行
  `worker.py <task_id>`）。
- 只要能写 `bin/openvpn`，动作必须进 `GATED`（要一次性安装码），否则等于给 Web 开了
  一个「装 root 程序」的后门。
- 结果写回 `data/tasks/<tid>.json`（`status` / `msg` / `progress`），前端轮询
  `/api/task/<tid>`。
- 需要新系统命令时同步 `system/precheck.py`（安装前能查的）与 `install.sh`（要装的包）。

---

## 五、新增长任务（不需要 root）

用 `jobs` 底座，不要自己 `threading.Thread`：

```python
from jobs.services import JobService

def dispatch_export(actor_name):
    return JobService.dispatch("export", actor=actor_name, payload={...})
```

handler 放 `jobs/handlers/`，在 `jobs/handlers/registry.py` 注册。需要即时反馈的长动作
（向导、上传、编译）仍走 `bridge.start_task`（带全局写锁与实时日志）。

---

## 六、模板与静态资源

- 页面模板放 `ovpn/templates/ovpn/`，视图里用 `render(request, "ovpn/xxx.html")`。
- 需要原样输出（含前端 `{{ }}` 模板语法）时按 `self_page()` 的方式直接读文件。
- 项目级静态资源放 `django/static/`，`collectstatic` 后由 WhiteNoise 提供。

---

## 七、配置项

- 业务配置一律进 `config/settings.py`，用 `OVPN_` 前缀，并在 `.env.example` 登记：

```python
OVPN_SESSION_IDLE = env.int('OVPN_SESSION_IDLE', default=3600)
```

- 业务代码用 `settings.OVPN_xxx` 或 `getattr(settings, 'OVPN_xxx', default)`，
  **不要** `os.environ.get(...)`（测试无法覆盖、契约也不认）。
- 特权层（`system/`）是独立进程，读进程环境变量是允许的（它拿不到 Django settings）。

---

## 八、提交前自检

```bash
python scripts/contract_check.py --strict
python django/manage.py check
python django/manage.py makemigrations --check --dry-run
python django/manage.py test
python tests/selftest.py
```

`manage.py test` 不带标签时由 `common/test_runner.py` 的 `BaseDirDiscoverRunner` 把发现根
钉死为 `BASE_DIR`（`django/`），因此在仓库根还是 `django/` 下执行，结果都是同一套 172 项。
不要改回默认 runner —— 默认行为以 cwd 为扫描起点，在仓库根只会扫到 `system/tests` 的 4 项并
“静默通过”。

新增接口要在 `ovpn/tests.py` 补一条「GET 被 405 挡住」，新增模型要在 `tests.py` 补约束测试。
