# OpenVPN Web 一体化管理系统

一个目录、一个 systemd 服务、单进程。把「装 OpenVPN 服务端、账号密码、固定 IP、按人限制能访问哪些服务器、可开关的无 NAT 溯源、客户端互访 P2P、Excel 台账、版本与更新」全部收进浏览器，机房断网也能装。

> 交付定稿：x86_64 离线静态二进制、**Django 5.2 裸机便携模式（Waitress WSGI + SQLite WAL + WhiteNoise，零 Docker / 零外部服务）**、账号密码认证、全员一个 `company.ovpn`、ACL 默认拒绝 + 观察模式起步、**网络模式默认 NAT（无 NAT 溯源默认关闭、可一键开启）**、客户端互访走内核 FORWARD、手动上传更新。
>
> **运行模式边界**：底座代码保留 `STANDALONE=False` 的 Docker/PostgreSQL 能力，仅供底座自身开发测试；本项目正式运行**只允许 `STANDALONE=True` 的裸机 SQLite 模式**。特权层 `worker.py` / `auth-verify.py` / 备份恢复均固定读 `data/db.sqlite3`，切 PostgreSQL 会让 Web 台账、VPN 认证和规则下发看到不同的数据，属于明确不支持的部署形态。`.env.example` 与 `serve.py` 都已按此固定默认值。

> **从 GitHub clone 后想打包运行？先读 [docs/BUILD_FROM_SOURCE.md](docs/BUILD_FROM_SOURCE.md)。** 仓库刻意不含 `bin/openvpn`、`runtime/` 便携 Python、`vendor/` 大文件与 `dist/` 交付包（共约 500MB，均可由随仓库的脚本从零生成）。如果你拿到的已经是完整交付包（`dist/openvpn-web-x86_64.tar.gz`），直接从 [docs/OPERATIONS.md](docs/OPERATIONS.md) 部署开始即可。

---

## 开发者与 AI 必读规范索引

改代码前先读这四份，顺序即优先级：

1. [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md) — 目录职责与单向依赖（`django/` 内分层、`system/` 特权边界）
2. [docs/DJANGO_APP_GUIDE.md](docs/DJANGO_APP_GUIDE.md) — 新增 app / 视图 / 服务 / 模板的落位规则
3. [docs/CODING_STANDARDS.md](docs/CODING_STANDARDS.md) — 命名、分层、异常、日志、测试、提交
4. [CONTRACT.md](CONTRACT.md) — 接入契约 v3.0；改完必须 `python scripts/contract_check.py --strict` 0 FAIL / 0 WARN

**修 bug 之前额外读 [docs/INCIDENTS.md](docs/INCIDENTS.md)**（故障复盘库）——里面记的是「哪些现象最容易把人带向错方向、怎么用一条命令定性、靠哪条断言保证不复发」。**修完必须给它补一条记录（或挂上已有条目），且记录里的断言必须真实存在**——`tests/selftest.py` 有元检查会拦住「文档说防住了、断言其实被删了」。

验收命令（缺一不可）：

```bash
python scripts/contract_check.py --strict
python django/manage.py check
python django/manage.py makemigrations --check --dry-run
python django/manage.py test          # 期望 172 项；cwd 无关（TEST_RUNNER 已钉死发现根）
python tests/selftest.py              # 期望 472 项
python -m unittest system.tests.test_fullbackup_restore
```


## 0. 技术底座：Django 双模式底座（standalone）

本项目以 `django-web-base` 为底座重构，Web 框架从 Flask 迁移到 **Django 5.2**，沿用底座的安全基线与运维体系：

| 层 | 实现 |
| --- | --- |
| Web / HTTP | Django WSGI + Waitress（单进程多线程），无 Nginx / gunicorn |
| 数据库 | SQLite(WAL) + `busy_timeout=20000`；Django 用户库与 core 业务库都在 `data/` |
| 后台登录 | **Django Auth + django-axes**（失败 5 次锁 10 分钟），原生 Django 后台在 `/admin/` |
| 审计 | 独立 `audit` app（表 `audit_auditlog`，只追加、可导出、可清理）+ 特权侧 `data/state/ops-audit.jsonl`，同一日志页合并展示 |
| 静态文件 | WhiteNoise 托管，`collectstatic` 后离线可用 |
| 业务核心 | `system/core.py` / `system/fullbackup.py` 特权侧实现 + `ovpn/services/`（base/audit/users/userdata/assets/groups/grants/tasks/admin）的 Django ORM 门面（同名 API、零裸 SQL），`worker.py` root 提权 + 安装码闸门全部保留 |
| 前端 | `ovpn/templates/ovpn/index.html` 单页应用**零改动**，所有 REST API 路径与响应结构保持兼容 |

关键文件（全部在 `django/` 下）：

```
manage.py           Django 管理入口（migrate / ovpn_init / ovpn_cli / shell）
serve.py            systemd 入口：migrate + 建管理员 + waitress 跑 config.wsgi
config/             settings / urls / wsgi / celery（STANDALONE 模式）
accounts/           Django Auth 用户模型（AUTH_USER_MODEL=accounts.User，db_table 仍是 core_user）
ovpn/               OpenVPN 业务层：urls / auth / middleware / bridge / models / selectors
  views/            REST 视图包：state / wizard / users / acl / settings_view / updates / backups / pages
  services/         业务门面包（按子域拆分，对外仍是 ovpn/services/__init__.py 一个入口）
  templates/ovpn/   index.html + self_page.html
common/ jobs/ audit/ 底座公共模块、任务中心、独立审计
static/             项目级静态资源（collectstatic 输入）
```

`system/` 是 Django 源码根之外的特权层：`core.py` / `fullbackup.py` / `worker.py` /
`install.sh` / `setup.py` / `auth-verify.py` / `precheck.py`。

运行时与离线依赖（跟随底座模板 v1.1.0）：

- `start.sh` 按 `PYTHON_BIN` → `runtime/python/bin/python3.1x`（底座新约定）→ `python/bin/python3.1x`（本包交付目录）→ `runtime/bin/python3.1x`（源码开发）→ `django/python3.12`（旧路径）的顺序找解释器；只看文件存在、不依赖执行位，执行前会校验并提示 `chmod`。
- `./start.sh wheel:download` 在联网构建机上按目标平台下载 wheel（只发 sdist 的包会自动构建成 wheel）；`./start.sh standalone:install` 检测到 `WHEEL_DIR` 下的 `*.whl` 就走 `--no-index` 离线安装，pip 缺失时用 `ensurepip` 恢复。本项目 `.env.example` 把 `WHEEL_DIR` 设为 `vendor/wheels`，与交付包自带的 wheel 仓库共用。
- `STANDALONE=True` 时 `standalone:*` 命令强制使用本地解释器，不会误进 Docker 容器。
- **模式归一化**：`standalone:*` 命令强制 `STANDALONE=True` 且默认按纯 HTTP 处理（忽略 `.env` 里残留的 `USE_HTTPS=true`），并把探测到的本机内网 IP 临时加入 `HOSTS`；裸机前面若已有 TLS 终止，显式设 `STANDALONE_USE_HTTPS=true` 才会保留 Secure Cookie。Docker 生命周期命令（`up/init/update/dev*`）则强制 `STANDALONE=False`，`migrate/shell` 等通用命令仍完全尊重 `.env`。
- **项目根只读的部署**：`settings.py` 在 import 阶段对运行时目录只做 best-effort 创建（失败仅告警、不中断启动），真正的预建由 `install.sh` / `standalone:init` 承担。若项目根归 root 只读、只有专属目录归服务用户可写，设 `RUNTIME_DIR=/var/lib/openvpn-web` 即可把 data/staticfiles/media/logs 统一重定向到可写区；也可单独设 `DATA_DIR` / `STATIC_ROOT` / `MEDIA_ROOT` / `LOG_DIR` / `CACHE_DIR`（单项优先级更高）。
- **Axes 参数可配**：`AXES_FAILURE_LIMIT`（默认 5）与 `AXES_COOLOFF_MINUTES`（本项目默认 10）可环境变量化；锁定响应保持 JSON（前端统一解析），不会返回 Django 的 HTML 403。
- **上传文件下载**：`/media/<path>` 统一走 staff 鉴权视图（`safe_join` 拦截 `../` 穿越），匿名/非 staff 跳 admin 登录，DEBUG 下也不裸奔。

命令行救场（SSH 到服务器）：

```bash
cd /opt/openvpn-web
python3 django/manage.py ovpn_cli allow 10.20.30.0/24     # 白名单加网段
python3 django/manage.py ovpn_cli allow-reset              # 重置白名单为本机网卡网段
python3 django/manage.py ovpn_cli reset-admin              # 重置管理凭据（等同 reset-admin.sh）
python3 django/manage.py ovpn_cli restore-full <备份包>    # 全备份回滚（CLI 允许连二进制一起）
```

---

## 1. 它到底解决什么

| 需求 | 实现 |
|---|---|
| 所有人账号密码登录 | `auth-user-pass-verify` via-file + PBKDF2 哈希；无客户端证书、无 CRL 过期风险 |
| 每人固定 IP、管理员可改 | `ccd/<用户名>` 里的 `ifconfig-push`，服务端下发，客户端改不了 |
| 所有人同一个 ovpn | `company.ovpn` 内联 CA + tls-crypt + `auth-user-pass`，不含任何个人凭据 |
| Excel 新建/删除用户 | 全量同步：表里没有的账号 = 删除；预览变更 → 点按钮确认 → 自动备份 |
| 内网服务器知道客户端 IP | **可选开关**：「无 NAT 溯源」默认关闭（默认 NAT，开箱可用）；一键开启后 tun/subnet 路由模式让源 IP 变成账号固定 IP |
| 指定用户只能访问指定服务器 | 服务端 FORWARD 自定义链 `OVPN_ACL`，按「源 IP → 目标/端口/协议」白名单，末尾 DROP |
| 客户端之间互访（P2P） | 不走 `client-to-client`，流量仍经过内核 FORWARD，按「账号固定 IP → 账号固定 IP」纳入同一套 ACL / 日志 / 溯源 |
| 全在 Web 上操作，包括安装与更新 | 首次一行 `install.sh` 起 Web，之后安装向导、账号、授权、P2P、网络模式、日志、更新、回滚、自检全在浏览器 |

**溯源链（仅在无 NAT 溯源模式下）**：内网服务器日志里的 `10.8.0.x` → 反查账号台账 → 认证日志里该时刻的来源公网 IP 与连接时间。默认 NAT 模式下内网日志只能看到 VPN 服务器自己，溯源靠认证日志。两种模式都请给 VPN 服务器和所有内网服务器开 NTP。

---

## 2. 网络模式：默认 NAT，溯源按需开启（先看这个）

「无 NAT 溯源」做成了**默认关闭、可一键开启**的开关，因为不做 NAT 是有代价的——机房网关必须配回程路由。默认 NAT 让系统开箱可用，不用惊动网管。

| | NAT 模式（默认） | 无 NAT 溯源 |
|---|---|---|
| 回程静态路由 | **不需要** | **必须**，否则客户端连上隧道也 ping 不通内网 |
| 内网服务器日志源 IP | 全是 VPN 服务器自己 | 各账号的固定 VPN IP |
| 溯源到具体账号 | 只能靠认证日志（账号 + 来源公网 IP + 时间） | 内网日志 + 认证日志双链 |
| 开箱可用 | 是 | 需网管配合 |
| 切换代价 | 只重建防火墙规则，**不重启 OpenVPN、不断线、配置指纹不变、ovpn 不用重发** | 同左 |

开启时 Web 会弹确认框、点按钮即可，并提示先配回程路由；「回程路由 / 自检」页会按当前模式自动生成命令：

```bash
ip route add 10.8.0.0/24 via <VPN服务器内网IP>        # Linux 网关
ip route-static 10.8.0.0 24 <VPN服务器内网IP>         # H3C / 华为
```

网关改不了时，退一步给每台内网服务器都加同一条主机路由。中间有 LB / 反向代理 / NAT 网关时源 IP 会变，ACL 与溯源都会错位，那层必须透传。

**别再手工执行 `firewall-cmd --add-masquerade` 或另加 NAT 规则。** 本系统用自建 `OVPNWEB_NAT` 链接管（带 `OVPNWEB_NAT` comment 标记），开启溯源时会自动清掉针对 VPN 网段的裸 MASQUERADE/SNAT；系统还装了每 5 分钟重放规则的 systemd timer，firewalld reload 冲掉 FORWARD 后能自动恢复。

> 顺带修了一个真实前提：`net.ipv4.ip_forward=1`。路由模式与客户端互访（tun0 → tun0）都依赖它，现在由 worker 持久化到 `/etc/sysctl.d/99-openvpn-web.conf`，重建规则时自动设置。

### 2.1 网关是爱快（iKuai）

爱快默认会对命中静态路由的非直连网段做 SNAT，典型症状是「能 ping、能握手、发数据就断」。
回程路由 + NAT 过滤的完整步骤见 [docs/NETWORK_IKUAI.md](docs/NETWORK_IKUAI.md)。

---

## 3. 目录结构

3.0 起按 `django-web-base v3.0` 规范收敛：**Django 源码全部在 `django/`**，项目根只放
外围设施，交付包与源码树保持同一布局。

```
openvpn-web/
├── django/                   Django 源码根（manage.py / serve.py / config / accounts /
│                             common / audit / jobs / ovpn / templates / static / requirements）
├── system/                   特权层（root 执行）：install.sh / worker.py / setup.py /
│                             auth-verify.py / precheck.py / fullbackup.py / core.py
├── build/                    打包与静态编译脚本（不进交付包）
├── vendor/                   离线依赖：src（源码）/ static（预编译库）/ wheels
├── bin/                      预编译静态二进制（openvpn / openssl，不进 Git）
├── runtime/                  源码开发用便携 Python（交付包里叫 python/）
├── scripts/                  接入契约检查器与 start.sh 子命令
├── tests/                    端到端自检（selftest.py / selftest_django.py / 前端脚本）
├── docs/                     规范与运维文档（本目录是唯一权威入口）
├── data/ logs/ media/        运行数据（唯一可写区，不入 Git）
└── dist/                     打包产物
```

目录职责、单向依赖与新增文件该放哪，见
[docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md)；契约的机器判定见
`.contract.json` 与 `scripts/contract_check.py`。

### 3.1 装机后（/opt/openvpn-web）

```
/opt/openvpn-web/
├── python/                   随包便携解释器（glibc 2.17 即可）
├── bin/openvpn bin/openssl   静态二进制
├── django/                   Django 源码（含 serve.py，systemd 直接拉起）
├── system/                   特权脚本（root 所有，Web 不可写）
├── tests/                    包内自检（安装尾声自动跑）
├── vendor/static/            目标机「上传源码自动编译」用的预编译静态库
└── data/                     唯一可写区：SQLite / 证书 / ccd / ovpn / 日志 / backups
```

进程与权限：

- systemd 只拉 `ovpnweb` 用户的一个 Web 进程：**waitress 单进程 8 线程**；长任务另有一把全局写锁，任何时刻只有一个执行者在改系统
- 生产入口固定 `django/serve.py` → Waitress；依赖缺失时从 `vendor/wheels` 离线补齐，运行期绝不退回系统 python3
- 长任务（安装、更新、重启）走独立后台线程 + `task_id` 轮询 + 实时日志；任务在跑时其它写接口 409
- `sudoers` 只放行一条精确命令：`python3 system/worker.py <task_id>`，动作与参数从 `data/tasks/<id>.json` 读并做白名单
- 装外部代码（替换二进制 / 本机编译 / 更新 Web）需要**一次性安装码**：只能在本机 `sudo /opt/openvpn-web/install-code.sh` 生成
- `data/` 归 `ovpnweb` 可写；`bin/`、`django/`、`system/` 归 `root:root`，Web 进程只读

---

## 4. 三步跑起来

| 步骤 | 命令 | 文档 |
| --- | --- | --- |
| 1. 构建离线包（有网构建机） | `python build/build-offline.py`（Windows）或 `bash build/build-offline.sh`（Linux） | [docs/BUILD_FROM_SOURCE.md](docs/BUILD_FROM_SOURCE.md) |
| 2. 部署（目标机 root） | `tar xf openvpn-web-x86_64.tar.gz && cd openvpn-web-x86_64 && ./install.sh` | [docs/OPERATIONS.md](docs/OPERATIONS.md) |
| 3. 打开 `http://<IP>:8800` | 按向导 5 步走完（环境自检 → 二进制 → 网段 → 证书 → 完成） | [docs/OPERATIONS.md](docs/OPERATIONS.md) |

装完 `openvpn` 与 Web 都在跑，安装脚本末尾自动跑一遍包内自检并打印结果。

---

## 5. 文档索引

| 文档 | 内容 | 读者 |
| --- | --- | --- |
| [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md) | 目录职责、单向依赖、新增文件该放哪 | 开发者 / AI |
| [docs/DJANGO_APP_GUIDE.md](docs/DJANGO_APP_GUIDE.md) | 新增 app / 视图 / 服务的落地步骤与边界 | 开发者 |
| [docs/CODING_STANDARDS.md](docs/CODING_STANDARDS.md) | 命名、分层、异常、日志、测试、提交规范 | 开发者 |
| [CONTRACT.md](CONTRACT.md) | 接入契约 v3.0 的机器判定规则 | 开发者 / 审计 |
| [docs/BUILD_FROM_SOURCE.md](docs/BUILD_FROM_SOURCE.md) | 从零补齐依赖并构建交付包 | 构建 / 运维 |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | 部署、卸载、向导、日常操作、备份恢复 | 运维 |
| [docs/NETWORK_IKUAI.md](docs/NETWORK_IKUAI.md) | 爱快网关下的无 NAT 溯源配置 | 网络 |
| [docs/SECURITY.md](docs/SECURITY.md) | 特权隔离、安装码、上线安全清单 | 安全 / 运维 |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | 排障清单（按现象查） | 运维 |
| [docs/INCIDENTS.md](docs/INCIDENTS.md) | 故障复盘库（按事件查：误判过程、定性方法、防复发断言） | 开发者 / 运维 |
| [docs/VERIFICATION.md](docs/VERIFICATION.md) | 已验证项、部署环境矩阵、已知限制 | 验收 |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | 完整版本历史 | 所有人 |

---

## 6. 最近版本

## 12. 版本信息（截至 2026-11-25）

- Web **3.0.2**：**修交付链路三处「静默错误」——不报错，但装出来的机器是错的**。① `install.sh` 的依赖探针把清单写成空格分隔（`import django axes …`），Python 不认空格，探针**恒假**，于是每台机器都白跑一遍 fallback 并永远报「依赖仍不完整」，而同一份日志里 `precheck` 又说「依赖已就位」。② `setup.py` 的 `firewall()` 在 ACL/NAT 下发**之后**执行 `firewall-cmd --reload`，把 `OVPN_ACL` 跳转和 `OVPNWEB_NAT` 整个冲掉——服务 active、页面 200、自检全过，但 `iptables -S | grep OVPN` 是 0（observe 模式下等于全员放行）。③ `mask_units()` 在 systemd 219 上 `systemctl mask openvpn@` 必然报错，改为建 `/dev/null` 软链。新增 4 条回归断言；复盘见 [INC-0005](docs/INCIDENTS.md#inc-0005) / [INC-0006](docs/INCIDENTS.md#inc-0006)。
- Web **3.0.1**：**修掉「内网访问 VPN 客户端 ping 通、TCP 全不通」**。根因不在客户端，而在服务端 firewalld：它的 zone 链只无条件追加 `-p icmp -j ACCEPT`，其余转发流量全部落到 `FORWARD` 末尾的 `-j REJECT`，而 `tun0` 不在任何 zone 里——于是 LAN→客户端方向天生只有 ICMP 能过。更坑的是客户端侧抓包**一条报文都看不到**（SYN 停在服务端 FORWARD，没上 tun0），极易被误判成客户端防火墙/安全软件在丢包。修复：`render_rules()` 与 `setup.py firewall()` 都会幂等地补三条 `--direct` 规则（`-s 10/8|172.16/12|192.168/16 -o tun0 -j ACCEPT`，只放私网源，公网仍拦），**不 `--reload`**（reload 会重建 FORWARD 链、冲掉 `OVPN_ACL` 跳转）；worker 采样 `FORWARD_direct`，自检页新增「firewalld 转发放行」一项。顺带修：`openvpn-web-replay.service` 恢复指向 root 独占的 `/etc/openvpn-web/rules.sh`（原先指向 Web 可写的 `data/state/rules.sh`，等于每 5 分钟以 root 执行 Web 可控内容）。新增 3 条回归断言。
- Web **3.0.0**：**按 django-web-base v3.0 规范重整结构**。新增 `django/` 子目录收拢 `manage.py` / `config` / `accounts` / `common` / `audit` / `jobs` / `ovpn` / `templates` / `requirements*`，顶层只留 `system/ build/ vendor/ scripts/ tests/ docs/`；`core` app 改名 `accounts`（`db_table` 仍 `core_user`，免数据迁移）；`ovpn/services/core.py` 拆成 9 个语义化子模块，`ovpn/views.py` 拆包并抽出 `selectors.py`；`INSTALLED_APPS` 改四段式 + 完整 `AppConfig` 路径；契约升 v3.0（新增 `app_config` / `layering` 检查）。
- Web **2.0.0**：**正式版标签**。功能与 1.3.30 一致，仅将 Web 版本标签提升为 2.0.0（`core.WEB_VERSION` 与 `index.html PAGE_VERSION` 同步），标识 Web 框架已完成从 Flask 到 Django 5.2 standalone 的迁移。
- Web **1.3.30**：**Web 框架整体迁到 Django 裸机底座，并同步底座 v1.1.0 的运行时/离线 wheel 能力**。Web 由 Flask 换成 **Django 5.2 standalone**（Waitress + SQLite WAL + WhiteNoise），**不用 Docker**；`core.py` / `fullbackup.py` 与 `index.html` 前端零改动，60+ REST 路径与 `X-CSRF-Token` 契约不变。管理登录改为 **Django Auth + django-axes**（失败 5 次锁 10 分钟、锁定返回 JSON 403），底座 `core` app 改名 `accounts` 以避开业务 `core.py`；启动入口改为 `serve.py`（自动 migrate / 建管理员 / 生成 `data/web.secret` / 收集 HOSTS），救场命令改为 `manage.py ovpn_cli {allow|allow-add|allow-reset|reset-admin|restore-full}`。同步底座 v1.1.0：解释器探测同时兼容 `runtime/python/`、`python/`（交付包）、`runtime/`（源码开发）与旧 `django/python3.12/`；新增 `./start.sh wheel:download` 与 `standalone:install` 的 `--no-index` 离线安装（pip 缺失走 `ensurepip`），`STANDALONE=True` 时 `standalone:*` 不再误进容器。selftest 经兼容层用 Django `Client` 跑原 424 项，全通过；新增 `.gitignore` 对 `.env` / `logs/` / `staticfiles/` / `wheelhouse/` 的忽略。
- Web **1.3.29**：**「拷过去跑一下就好」补齐环境侧**。三处改动：① **强制只用随包便携版 Python**——删掉 `install.sh` 里「系统 python3 有 flask 就用系统的」兜底分支，包内运行时不可用就直接报「交付包不完整」并退出；目标机 Python 版本/依赖千差万别（CentOS 7 只有 3.6、最小化系统压根没装），不碰它才有确定性。② 新增 `precheck.py`（纯标准库，随包分发），在动 `/etc`、运行账号、systemd **之前**一次报齐：CPU 架构、glibc 版本、TUN 设备、init 系统、随包 Python 可执行性、Web 依赖/离线 wheel、**直接试跑包内 openvpn ELF**（glibc/架构不匹配当场发现，不等装完报 `GLIBC_2.xx not found`）、8800/1194 端口、磁盘、**系统时钟**、防火墙/SELinux。硬错误整批中止，警告只提示；时钟只做「RTC 与系统时间差 8 小时」「没开 NTP」这类诊断，**绝不自动改系统时间**（离线机器没有可信基准）。无 systemd 的系统会明确拦住并说明原因（本版不做 cron 降级）。③ 装完自动跑包内 `selftest.py` 并把结果打在「安装完成」里，交付即验收；它自己把数据目录指到临时目录并开 MOCK，不碰生产 `data/`。重装场景端口占用只警告（后面会 restart），首装才硬检查。新增 14 条回归断言（含「安装包不再引用系统 python3」「预检必须在写 sudoers 之前」「precheck.collect() 在 Windows 上也能安全执行」）。
- Web **1.3.28**：**堵住两条「改配置 / 恢复备份就能拿到 root」的注入路径**（安全审查发现）。① `tls_min` 是 `validate_cfg` 的盲区——绕过前端直接打 `/api/settings`，一个带换行的字符串被原样写进以 root 运行的 `server.conf`（`tls-version-min` 行），即可注入 `up`/`plugin` 等任意指令，`settings_save` 随即重启 OpenVPN = root 命令执行；现加白名单（只认 1.2 / 1.3），渲染前再 fail-closed 拦一次。② 全备份恢复会整库覆盖 meta 并重新渲染配置，而 manifest 哈希只证明「包自洽」不证明「包没被改过」——改库重算哈希就能借「恢复」把脏值送进 root 执行的 `rules.sh`（`dns`/`ntp` 未加引号直接拼进 iptables 行）；现在恢复前先复核包内配置，不合法**整包拒绝**（在任何生产文件被覆盖之前）。同一次加固把渲染面全部收口：`rules.sh` 里网段取规范化 IPv4Network、账号 IP/资产网段/端口/协议渲染时再校验（脏值宁可让 apply-rules 失败也不生成脚本）、ccd 生成前校验账号固定 IP；带副作用的 `GET /api/logs?kind=deny` 与 `GET /api/selfcheck` 纳入 CSRF（防跨站 `<img>` 白嫖触发 root worker）。新增 13 条回归断言（含「篡改 meta + 重算哈希 → 恢复必须拒绝」）。
- Web **1.3.27**：**堵住一个能绕过安装码的提权洞**。1.3.23 给「上传即 root」加了安装码闸门，但闸门判断写成了 `if act in GATED and not MOCK`，而 `install.sh` 的 sudoers 又用 `env_keep` 保留了 `OVPN_WEB_MOCK`——任何能调面板 API 的人都能在自己进程里设这个变量、经 sudo 传进 worker，跳过安装码把任意 ELF 装成 root 服务（测试机实测确认：无码时 `replace-binary` 被拦，带 `MOCK=1` 则直接进动作）。现在闸门改看**专用自检开关 `OVPN_WEB_SELFTEST`**（只有直接跑 worker.py 的自检进程才有；sudoers 加 `env_reset` 且只保留 `OVPN_WEB_ROOT`，该开关经 sudo 必被清空），`OVPN_WEB_MOCK` 退回到只管「是否碰真实系统」的职责，并加了 3 条回归断言钉死。顺带修：界面上「24 小时有效」文案与实际 15 分钟/2 小时不符；`install.sh` 探活只靠 curl（最小化系统没有 curl 会误报 Web 没起来，改成优先用包内 python 的 urllib，curl/wget 兜底）。
- Web **1.3.26**：**Windows 上也能打包**。之前只能在 Linux 构建机跑 `build-offline.sh`——它要用包内的 Linux `python` 执行 `pip install`，Windows 上根本跑不了。新增 `build/build-offline.py`：只做「分层 → 平铺」的搬运，不执行任何包内程序，Windows 的 Git Bash 里一条 `python build/build-offline.py` 就出包。代价是运行时的 `site-packages` 是干净的，所以 `install.sh` 第 2 步改成「缺依赖就从 `vendor/wheels` 离线补」（先 `pip --no-index --find-links`，pip 不可用就直接把 wheel 当 zip 解开）。同时把三个 Windows 坑堵在打包环节：软链存根在 tar 里写成真符号链接（实测 745 个）、权限按路径规则写死（仓库里 `bin/openvpn` 记的是 644）、`os.symlink` 失败时回退存根。实测：Windows 打出 134.6MB 包，Linux 上解包 `python3 -V` 正常、依赖补装成功、包内自检 394 项全过。
- Web **1.3.25**：**编译工具不再是拦路虎**。上一版让「缺二进制就地编译」成立，但最小化系统常见地没装 gcc（这台测试机就是：make 有、gcc/perl/pkg-config 全无），真到要编的时候还是卡住。现在 `prepare-openvpn.sh` 在缺工具时会先按本机包管理器（yum / dnf / apt-get）自动补装，补不上才报错，并给出对应发行版的准确包名（CentOS 7 的 pkg-config 包叫 `pkgconfig`，容易踩）。`install.sh` 新增 `--with-build-tools`：装完顺手把工具链备好，以后在「升级」页上传源码就能直接编；不加这个参数则完全不碰包管理器（生产机不该为了用不到的能力多装 100MB）。离线机器上自动补装会失败，但只影响「本机编译」这一条路，不影响安装本身。

完整历史（含 1.3.x 全部条目）见 [docs/CHANGELOG.md](docs/CHANGELOG.md)。
