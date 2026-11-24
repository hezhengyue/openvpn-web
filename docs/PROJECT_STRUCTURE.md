# 项目结构与依赖规则（openvpn-web 3.0）

本文件是仓库的**目录权威说明**。改代码前先按这里的职责表判断「这段逻辑该放哪」，
再动手。机器判定见 [CONTRACT.md](../CONTRACT.md) 与 `scripts/contract_check.py`。

---

## 一、顶层目录

| 目录 | 职责 | 谁能改 |
| --- | --- | --- |
| `django/` | **Django 源码根**：`manage.py`、`serve.py`、`config/`、`accounts/`、`common/`、`audit/`、`jobs/`、`ovpn/`、`templates`（随 app）、`static/`、`requirements*.txt` | 开发者 |
| `system/` | **特权层**（root 执行）：安装、内核/防火墙、PKI、OpenVPN 二进制、worker、认证钩子、备份 | 开发者；运行期 Web 只读 |
| `build/` | 打包（`build-offline.py` / `.sh`）与静态编译（`build-static-openvpn.sh`） | 开发者 |
| `vendor/` | 离线依赖：`src/` 源码包、`static/prefix.tar.gz` 预编译库、`wheels/` | 构建 |
| `bin/` | 预编译静态二进制（`openvpn` / `openssl`），不进 Git | 构建 |
| `runtime/` | 源码开发用的便携 Python（交付包里叫 `python/`） | 构建 |
| `scripts/` | 接入契约检查器、`start.sh` 子命令库 | 开发者 |
| `tests/` | 端到端自检（`selftest.py` / `selftest_django.py`）与前端脚本 | 开发者 |
| `docs/` | 规范与运维文档（本文件所在） | 所有人 |
| `data/ logs/ media/ staticfiles/` | 运行数据，唯一可写区，不入 Git | 运行期 |
| `dist/` | 打包产物 | 构建 |

**红线**：`system/` 永远在 Django 源码根之外。任何「下次会以 root 执行」的文件都不能
落在 `django/` 里，否则 Web 进程被打穿就等于 root。

---

## 二、`django/` 内部结构

```
django/
├── manage.py            Django CLI（migrate / ovpn_init / ovpn_cli / test）
├── serve.py             systemd 入口：读 .env → migrate → ovpn_init → waitress
├── config/              settings.py / urls.py / wsgi.py / asgi.py / celery.py
├── accounts/            用户模型（AUTH_USER_MODEL=accounts.User）、账户服务、迁移
├── common/              公共层：真实 IP、邮件发送、Axes 响应、Admin 改名、工具函数
├── audit/               独立审计 app：唯一写入口 audit_log()、只追加模型、清理命令
├── jobs/                任务中心：Job / JobLog 模型、JobService.dispatch、handler 注册表
├── ovpn/                OpenVPN 业务 app
│   ├── urls.py          全部 REST 路由（app_name="ovpn"）
│   ├── views/           视图包：state / wizard / users / acl / settings_view / updates / backups / pages
│   ├── selectors.py     只读查询层（跨多个服务的聚合，可单测）
│   ├── services/        业务门面包：base / audit / users / userdata / assets / groups / grants / tasks / admin
│   ├── bridge.py        调特权层的唯一适配器（响应封装、长任务、sudo worker、安装码）
│   ├── middleware.py    白名单 / 登录 / 首登强改 / 到期踢人 / 安全头
│   ├── auth.py          登录、退出、改密（Django Auth + Axes）
│   ├── csrf.py          CSRF 失败 JSON 化
│   ├── models.py        VPN 业务模型（VpnUser / AssetGroup / AclGrant …）
│   ├── bootstrap.py     Django Auth 与业务 meta 的引导同步
│   ├── admin.py         Django 后台注册
│   ├── management/commands/  ovpn_init / ovpn_cli
│   ├── migrations/
│   └── templates/ovpn/  index.html（单页前端）+ self_page.html（自助改密）
└── static/              项目级静态资源
```

---

## 三、单向依赖（硬规则）

```
                     ┌──────────────┐
                     │   config/    │  settings / urls
                     └──────┬───────┘
                            │ 只能被 import，不 import 业务
          ┌─────────────────┼──────────────────┐
          ▼                 ▼                  ▼
    ┌──────────┐      ┌──────────┐      ┌────────────┐
    │ common/  │◄─────│ ovpn/    │      │  jobs/     │
    │ audit/   │      │ views    │      │  audit/    │
    │ accounts/│      │ services │      └────────────┘
    └──────────┘      │ selectors│
          ▲           └────┬─────┘
          │                │ bridge.py（唯一出口）
          └────────────────┼──────────────► system/（root 特权层，独立进程）
                           │
                      Django ORM ──► data/db.sqlite3
```

规则（`contract_check.py` 的 `layering` 项会 FAIL）：

1. `common` / `accounts` / `audit` / `jobs` **不得** `import ovpn`。底座不认识业务。
2. `ovpn` 可以依赖底座；`ovpn.views` 只依赖 `selectors` / `services` / `bridge`，不直接写 ORM 查询。
3. `views` 不直接读写模型，也不拼 SQL；聚合查询进 `selectors`，写操作进 `services`。
4. 任何 `system/` 调用只能经 `ovpn/bridge.py`（`sudo_worker` / `start_task`），
   Web 进程不 `subprocess` 调特权脚本、不直连 `data/db.sqlite3`。
5. `config/` 只被 import；不得反向 import 任何 app 的视图或服务。

---

## 四、运行期目录与路径推导

- `settings.BASE_DIR` = `django/`（源码根）
- `settings.PROJECT_ROOT` = 安装根（`django/` 的父目录，靠「谁持有 `system/`」判断）
- 运行数据一律经 `_runtime_dir()`：`RUNTIME_DIR` 统管，单项 `DATA_DIR` / `STATIC_ROOT` /
  `MEDIA_ROOT` / `LOG_DIR` / `CACHE_DIR` 优先。
- 源码树与交付包布局一致：`/opt/openvpn-web/django/serve.py`、`/opt/openvpn-web/system/...`。

---

## 五、新增文件放哪（决策表）

| 你要加的东西 | 放这里 |
| --- | --- |
| 新业务接口 | `ovpn/urls.py` + `ovpn/views/<子域>.py`（写接口必须 `write_only(...)` 包一层） |
| 跨服务聚合的只读查询 | `ovpn/selectors.py` |
| 业务写逻辑 / 数据访问 | `ovpn/services/<子域>.py`，并在 `__init__.py` 导出 |
| VPN 业务模型 | `ovpn/models.py` + `migrations/` |
| 可在别的项目复用的工具 | `common/utils/` 或 `common/middleware/` |
| 用户/账户相关 | `accounts/`（模型、services、admin） |
| 审计动作 | 调 `audit.services.audit_log()`，不要直接写 `AuditLog` |
| 长任务 | `jobs/` 的 handler + `JobService.dispatch`，不新起线程 |
| 需要 root 的动作 | `system/worker.py` 的 action + 加入 `GATED`（若能写 `bin/`），Web 侧只发任务 |
| 前端页面 | `ovpn/templates/ovpn/`（单页应用只改 `index.html`） |
| 运维脚本 | `scripts/`（`start.sh` 子命令）或 `system/`（root 特权） |

---

## 六、交付包结构

`build/build-offline.py` 按目录搬运，包内与源码树一致：

```
openvpn-web-x86_64/
├── python/   bin/   vendor/static/    运行时与二进制
├── django/                            源码（含 serve.py、templates、requirements）
├── system/  tests/  scripts/          特权层、包内自检、契约检查
├── install.sh  README.md  CONTRACT.md  .contract.json  version.txt
└── data/（运行期生成）
```

`django/` 与 `system/` 在安装后归 `root:root`，只有 `data/` 归 `ovpnweb` 可写。
