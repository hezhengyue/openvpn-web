# OpenVPN Web 一体化管理系统

一个目录、一个 systemd 服务、单进程。把「装 OpenVPN 服务端、账号密码、固定 IP、按人限制能访问哪些服务器、可开关的无 NAT 溯源、客户端互访 P2P、Excel 台账、版本与更新」全部收进浏览器，机房断网也能装。

> 交付定稿：x86_64 离线静态二进制、**Django 5.2 裸机便携模式（Waitress WSGI + SQLite WAL + WhiteNoise，零 Docker / 零外部服务）**、账号密码认证、全员一个 `company.ovpn`、ACL 默认拒绝 + 观察模式起步、**网络模式默认 NAT（无 NAT 溯源默认关闭、可一键开启）**、客户端互访走内核 FORWARD、手动上传更新。

> **从 GitHub clone 后想打包运行？先读 [BUILD-FROM-SOURCE.md](BUILD-FROM-SOURCE.md)。** 仓库刻意不含 `bin/openvpn`、`runtime/` 便携 Python、`vendor/` 大文件与 `dist/` 交付包（共约 500MB，均可由随仓库的脚本从零生成）；本页第 4 节是构建原理与细节，从零准备手把手步骤在那份指南里。如果你拿到的已经是完整交付包（`dist/openvpn-web-x86_64.tar.gz`），直接从第 5 节部署开始即可。

---

## 0. 技术底座：Django 双模式底座（standalone）

本项目以 `django-web-base` 为底座重构，Web 框架从 Flask 迁移到 **Django 5.2**，沿用底座的安全基线与运维体系：

| 层 | 实现 |
| --- | --- |
| Web / HTTP | Django WSGI + Waitress（单进程多线程），无 Nginx / gunicorn |
| 数据库 | SQLite(WAL) + `busy_timeout=20000`；Django 用户库与 core 业务库都在 `data/` |
| 后台登录 | **Django Auth + django-axes**（失败 5 次锁 10 分钟），原生 Django 后台在 `/admin/` |
| 审计 | 独立 `audit` app（表 `audit_auditlog`，只追加、可导出、可清理）+ 特权侧 `data/state/ops-audit.jsonl`，同一日志页合并展示 |
| 静态文件 | WhiteNoise 托管，`collectstatic` 后离线可用 |
| 业务核心 | `system/core.py` / `system/fullbackup.py` 特权侧实现 + `ovpn/services/core.py` 的 Django ORM 门面（同名 API、零裸 SQL），`worker.py` root 提权 + 安装码闸门全部保留 |
| 前端 | `index.html` 单页应用**零改动**，所有 REST API 路径与响应结构保持兼容 |

关键文件：

```
manage.py           Django 管理入口（migrate / ovpn_init / ovpn_cli / shell）
serve.py            systemd 入口：migrate + 建管理员 + waitress 跑 config.wsgi
config/             settings / urls / wsgi / celery（STANDALONE 模式）
ovpn/               Flask→Django 桥接：views / urls / auth / middleware / bridge
  views.py          原 app.py 的全部 REST 路由（业务逻辑调 core.py）
  auth.py           管理登录改用 Django Auth + Axes，自助改密仍走 core
  middleware.py     白名单 / 登录 / CSRF（X-CSRF-Token）/ 安全头 / 错误 JSON 化
  bridge.py         core 加载 + 长任务线程 + sudo worker + 响应封装
core/               Django Auth 用户模型（底座 app 由 accounts 改名，db_table 仍是 core_user）
common/ jobs/ audit/ 底座公共模块、任务中心、独立审计
system/             特权层：core.py / fullbackup.py / worker.py / install.sh / setup.py
templates/          单文件前端 index.html + 自助页
tests/selftest_django.py  selftest 的 Flask→Django 兼容层（当前 461 项自检原样运行）
```

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
python3 manage.py ovpn_cli allow 10.20.30.0/24     # 白名单加网段
python3 manage.py ovpn_cli allow-reset              # 重置白名单为本机网卡网段
python3 manage.py ovpn_cli reset-admin              # 重置管理凭据（等同 reset-admin.sh）
python3 manage.py ovpn_cli restore-full <备份包>    # 全备份回滚（CLI 允许连二进制一起）
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

### 2.1 网关是爱快（iKuai）时，无 NAT 溯源怎么配

上面说的「网关配回程路由」只是第一步。如果机房核心网关是**爱快 iKuai**，还要多做一步 NAT 过滤，否则会出现一个很迷惑的故障：**`ping` 内网服务器通、`ping -f -l 1372` 大包也不丢，但 HTTPS 卡在 `Trying...` 超时、SSH 连上后立刻 `10053` 断开**。原因是爱快默认会对命中静态路由的非直连网段（`10.8.0.0/24`）做 SNAT，把回程 TCP 数据包的源 IP 改写成了网关自己的 IP；Linux `conntrack` 对 ICMP 与 TCP `SYN-ACK` 跳过 NAT、只对建连后的数据包触发，所以恰好「能 ping、能握手、发数据就断」。

只配回程路由不够，**必须再配 NAT 过滤规则**。好处是不用逐台服务器加路由。

**适用**：希望内网业务服务器（Nginx / SSH / 数据库）日志里记的是真实 VPN 客户端 IP（`10.8.0.x`）而不是 OpenVPN 服务器的 LAN IP，且机房网关是爱快。

**参数示例**（按实际替换）：VPN 网段 `10.8.0.0/24`、OpenVPN 内网 IP `192.168.9.223`、所在接口 `lan1`、内网网段 `192.168.9.0/24`（或多网段 `192.168.0.0/16`）。

#### 步骤一：回程静态路由

爱快后台 → **网络设置 → 静态路由 → 添加**：

| 配置项 | 填写内容 |
|---|---|
| 线路 | `lan1`（OpenVPN 所在内网口，按实际选） |
| 目的地址 | `10.8.0.0` |
| 子网掩码 | `255.255.255.0`（`/24`，与 OpenVPN server 网段一致） |
| 网关 | `192.168.9.223`（OpenVPN 服务器内网 IP） |
| 优先级 | 默认（`0`） |
| 备注 | `OpenVPN 回程路由` |

保存并确认状态为「已启用」。

#### 步骤二：NAT 过滤规则（关键，别漏）

爱快后台 → **网络设置 → NAT规则 → 添加**。新增两条**动作 = 过滤**的规则（**不需要改全局「上网模式」**）：

**规则 1：回程流量跳过 NAT（必配）** —— 防止内网服务器回给 VPN 客户端的包被 SNAT 成网关 IP。

| 配置项 | 填写内容 |
|---|---|
| 动作 | `过滤` |
| 进接口 | `任意` |
| 出接口 | `任意` |
| 源地址 | 留空（任意） |
| 目的地址 | `10.8.0.0/24` |
| 协议 | `任意` |

**规则 2：去程流量跳过 NAT（推荐）** —— 防止 VPN 客户端跨网段访问爱快下其他内网 VLAN 时，去程源 IP `10.8.0.x` 被改写成目标网段网关 IP，让全内网溯源都生效。

| 配置项 | 仅访问内网 / 分流模式 | VPN 全局上网模式 |
|---|---|---|
| 动作 | `过滤` | `过滤` |
| 进接口 | `任意` | `任意` |
| 出接口 | `任意` | 仅选内网接口（如 `lan1`），**勿选 WAN 口** |
| 源地址 | `10.8.0.0/24` | `10.8.0.0/24` |
| 目的地址 | 留空 | 内网私有网段（如 `192.168.0.0/16`） |
| 协议 | `任意` | `任意` |

> 若 OpenVPN 开了全局流量转发（`redirect-gateway def1`），规则 2 务必按右列限制「出接口」或「目的地址」，**不要把 WAN 出口的 NAT 也过滤掉**，否则 VPN 客户端上不了公网。注意：本项目 `server.conf` 刻意**不加** `redirect-gateway`（客户端只走内网网段），通常按左列填即可。

保存后规则**立即生效**，无需重启路由。

#### 步骤三：验证

VPN 客户端连上后：

1. **连通性**：`ping <内网服务器IP>` 正常无丢包；`curl -v https://<内网服务器IP>`、SSH 登录都不再超时 / 秒断。
2. **溯源**：看目标服务器的 `access.log` / `/var/log/secure`，来源 IP 应是 **`10.8.0.x`**，而不是 VPN 服务器 IP 或爱快网关 IP。

#### 排障对照

| 现象 | 可能原因 | 处理 |
|---|---|---|
| ping 和 TCP 都不通 | 静态路由没配，或 OpenVPN 没开 `ip_forward` / 防火墙拦截 | 查步骤一是否指向正确的 OpenVPN 内网 IP；查 `net.ipv4.ip_forward=1` 与 `FORWARD` 链 |
| **ping 通、HTTPS 超时 / SSH 10053 秒断** | **没配 NAT 过滤，回程 TCP 被 SNAT 成网关 IP** | 查步骤二「规则 1」是否启用、目的地址是否为 `10.8.0.0/24` |
| 服务器日志记的是 VPN 服务器 IP | OpenVPN 服务器仍有 iptables SNAT 残留 | `iptables -t nat -L POSTROUTING -nv`，本系统用 `OVPNWEB_NAT` 链自管，点「重建规则」清残留 |
| 同网段正常、跨网段日志显示网关 IP | 没配「规则 2」，去程跨网段被 SNAT | 补步骤二的规则 2 |
| 配完后 VPN 客户端上不了公网 | 全局隧道下规则 2 把 WAN 口也过滤了 | 把规则 2 目的地址限定内网网段，或出接口指定内网 LAN 口 |

> **进阶**：若 OpenVPN 服务器与业务服务器在同一二层网段（如都在 `192.168.9.0/24`），路径是「去程二层直连、回程经爱快转发」的非对称路由，配完 NAT 过滤后通常就稳定了。生产环境想彻底消除非对称隐患，可把 OpenVPN 服务器划到独立网段 / VLAN（网关仍在爱快），让来回流量都对称经过爱快。

---

## 3. 目录与文件

### 3.1 仓库（开发机）

源码、构建、测试分开，根目录只留分类目录和一份 README：

```
openvpn-web/                  2.6 起「源码 = 交付包 = 标准 Django 根布局」
├── manage.py serve.py start.sh   Django 管理入口 / systemd 入口 / 解释器探测
├── config/                   settings / urls / wsgi（STANDALONE 裸机模式）
├── ovpn/                     views / urls / auth / middleware / bridge + services（ORM 门面）+ models
├── core/ common/ jobs/ audit/  底座应用：Auth（core_user）、公共模块、任务中心、独立审计
├── templates/ static/        单文件前端与静态资源
├── system/                   特权层（root）：core.py / fullbackup.py / worker.py / setup.py /
│                              auth-verify.py / install.sh / install-code.sh / build-static-openvpn.sh
├── bin/                      预编译静态二进制（openvpn / openssl）
├── runtime/                  离线 Python 3.12（源码开发用，交付包里叫 python/）
├── vendor/                   src/ static/prefix.tar.gz wheels/ SHA256SUMS fetch.sh
├── build/ scripts/           构建打包、契约检查等工具（不进交付包）
├── tests/                    selftest.py（461 项）/ selftest_django.py / smoke.js / shots.js
├── dist/                     产物：openvpn-web-x86_64.tar.gz
└── README.md BUILD-FROM-SOURCE.md CONTRACT.md
```

**为什么不再分层**：2.0 之前仓库里是 `src/` 分层、包内平铺；2.6 起把源码、交付包统一成同一个 Django 根布局，`build/build-offline.py`（Windows）与 `build/build-offline.sh`（Linux）只做文件搬运，不再拍平目录。`system/` 始终是 root 特权层，Web 进程不可写。

### 3.2 装机后（/opt/openvpn-web）

```
/opt/openvpn-web/     与仓库根同构（唯一例外：runtime/ 在包内叫 python/）
├── python/           随包便携解释器 + Django 全家桶（glibc 2.17 即可）
├── bin/openvpn bin/openssl   静态二进制（第三方库全静态）
├── serve.py manage.py        systemd 入口 / Django 管理入口
├── config/ ovpn/ core/ common/ jobs/ audit/ templates/ staticfiles/
├── system/           特权脚本：worker.py / setup.py / auth-verify.py / install.sh /
│                     install-code.sh / build-static-openvpn.sh（root 所有，Web 不可写）
├── selftest.py       包内自检（安装尾声自动跑，461 项）
├── vendor/static/    目标机「上传源码自动编译」用的预编译静态库
└── data/             唯一可写区：SQLite / 证书 / ccd / ovpn / 日志 / backups /
                      full-backups（统一备份包）/ snapshots（仅兼容旧包）
```

进程与权限：

- systemd 只拉 `ovpnweb` 用户的一个 Web 进程：**waitress 单进程 8 线程**（纯 Python WSGI 服务器，离线 wheel 随包走）。线程只用来并行处理 HTTP，慢请求不会再把整页拖住；长任务另有一把全局写锁，任何时刻只有一个执行者在改系统，杜绝规则重建竞态
- 生产入口固定是 `serve.py` → Waitress（单进程多线程）；依赖缺失时安装脚本会从 `vendor/wheels` 离线补齐，不再退回开发服务器。调试时才用 `./start.sh standalone:dev` 跑 Django `runserver`
- 长任务（安装、更新、重启）走独立后台线程 + `task_id` 轮询 + 实时日志；HTTP 请求本身不执行几十秒的系统动作。任务在跑时，其它写接口直接 409 拒绝
- **三层锁各管一段**：进程内 `core.SYS_LOCK`（同一进程别同时写配置、别同时起两个 worker）→ `worker.py` 的 `flock`（跨进程别同时改系统，定时重放也算）→ 生成的 `rules.sh` 自己也 `flock`（重放与手工重建串行）。iptables 一律带 `-w 5` 等 xtables 锁
- Web 低权限，`sudoers` 只放行一个精确命令：`python3 worker.py <task_id>`，命令行不传自由参数，动作与参数从 `data/tasks/<id>.json` 读并做白名单
- **装外部代码要人工放行**：替换 openvpn 二进制、本机编译源码、更新 Web 自身这三个动作，worker 额外要求一个**一次性安装码**——只能在本机 `sudo /opt/openvpn-web/install-code.sh` 生成（8 位，默认 15 分钟，一码一次，填错 5 次作废），令牌 600 `root:root` 存在 `/etc/openvpn-web/install.token`，Web 进程既造不出也读不到。装完系统会把首装码（2 小时）印在「安装完成」里；日常改配置、建号、启停服务都不需要它
- `data/` 归 `ovpnweb` 可写；**`bin/` 归 `root:root 755`**（只能执行、不能改），`serve.py`/`core.py`/`worker.py`/`index.html`/`install-code.sh` 与 Django 项目目录归 root 且低权用户只读
- 因此**备份恢复不碰 `bin/openvpn`**（包里的二进制会跳过并提示），要换二进制只有两条带安装码的路：上传替换、本机编译
- 安装时 mask 掉发行版自带的 `openvpn@.service`，避免包管理器拉起第二个实例抢 1194

---

## 4. 构建离线包（在一台有网的 x86_64 构建机上跑一次）

目标最低 **CentOS 7（glibc 2.17）**，往上任意发行版都能跑。**必须在老 glibc 环境构建**：在 Ubuntu 26 上 pip 会拉 manylinux_2_28+ 的 wheel、链出来的 openvpn 会要 `GLIBC_2.28`，拷到老系统直接 `GLIBC_2.28 not found`。

本仓库自带的 `bin/openvpn` 与 `runtime/` 都是在 **CentOS 7.9 / glibc 2.17 / gcc 4.8.5** 的 chroot 里编的，
`objdump -T bin/openvpn | grep -o 'GLIBC_[0-9.]*' | sort -Vu | tail -1` → `GLIBC_2.17`。
`build/build-static-openvpn.sh` 结尾会自检这一条，超限直接报错退出，不会悄悄产出不兼容的包。

要在别的机器上复现同样的 ABI，用 `dnf --installroot` 造一个 CentOS 7 chroot 即可（不需要 docker）：

```bash
mkdir -p /opt/c7
dnf -y --installroot=/opt/c7 --releasever=7 --nogpgcheck --disablerepo='*' \
  --repofrompath=c7os,https://mirrors.aliyun.com/centos-vault/7.9.2009/os/x86_64/ \
  --repofrompath=c7up,https://mirrors.aliyun.com/centos-vault/7.9.2009/updates/x86_64/ \
  --enablerepo=c7os,c7up install basesystem glibc glibc-devel gcc make perl pkgconfig \
  perl-IPC-Cmd perl-Data-Dumper perl-Digest-SHA perl-Time-Piece perl-Getopt-Long tar gzip
mknod -m 666 /opt/c7/dev/null c 1 3
cp -a vendor /opt/c7/build-vendor && cp build/build-static-openvpn.sh /opt/c7/
chroot /opt/c7 /bin/bash -lc 'VENDOR_ONLY=1 PREFIX_OUT=/out/prefix.tar.gz \
  bash /build-static-openvpn.sh /out/openvpn'
```

第一次要 1~2 分钟（OpenSSL 编得慢），之后想换 openvpn 版本只编本体：
`STATIC_LIBS=1 OPENVPN_SRC=/path/openvpn-2.6.24.tar.gz ./build/build-static-openvpn.sh` —— **14 秒**出结果（实测）。

**构建依赖本地优先，不靠云**。三样东西各有一到三级来源，前面能用就绝不联网：

| 依赖 | ① 本地 | ② 仓库快照 | ③ 联网（最后兜底） |
|---|---|---|---|
| Python 运行时 | 仓库根 `runtime/`（已自带，零下载） | `vendor/python/*.tar.gz` | python-build-standalone |
| Django 全家桶 / openpyxl | — | `vendor/wheels/*.whl`（`pip --no-index`） | PyPI |
| 静态 OpenVPN | `bin/openvpn` 已存在则直接用 | `vendor/src/*.tar.gz`（带 sha256 校验） | 各上游镜像按序回退，下到后**回填 vendor/src** |

所以日常路径是：**本地保留的 `python/` + `vendor/` 里 18M 源码和 wheel → 构建机连网线都不用插**。
（GitHub clone 不带这些大文件，按 [BUILD-FROM-SOURCE.md](BUILD-FROM-SOURCE.md) 第 1、2 步补齐后即可达到同样状态。）

**而且拿到完整交付包后连构建都不是必须的**：本地已编好的 `bin/openvpn`（2.6.23 静态）和装好依赖的 `python/`，
`./build/build-offline.sh` 检测到 `bin/openvpn` 会直接复用、跳过编译（想重编：`FORCE_BUILD_OPENVPN=1 ./build/build-offline.sh`），
只做「拷文件 + 离线装 wheel + 打包」，几十秒出 `dist/openvpn-web-x86_64.tar.gz`。

**Windows 开发机也能打包**（不用装 WSL、不用容器）：

```bash
python build/build-offline.py              # 产出 dist/openvpn-web-x86_64/ + .tar.gz
python build/build-offline.py --tar-only   # 只要 tar.gz
```

两个脚本做的是同一件事（分层仓库 → 平铺包），差别只在「要不要执行 Linux 的 pip」：

| 脚本 | 在哪跑 | 依赖 | 说明 |
|---|---|---|---|
| `build-offline.sh` | Linux 构建机 | 会调用包内 `python` 真装一遍 wheel | 产物 site-packages 是热的 |
| `build-offline.py` | Windows / Linux 都行 | **不执行任何 Linux 程序**，只搬文件 | 运行时保持"干净"，安装时从包内 wheel 补（几秒，离线） |

`.py` 版本专门处理三个 Windows 坑：软链被拍成 10 字节文本（tar 里改写成符号链接，本机实测 745 个）、
没有可执行位（权限按路径规则写死：`install.sh` 755 / `install-code.sh` 700 / `bin/*` 755）、
`os.symlink` 默认没权限（建不了就写回存根，tar 阶段再还原）。

```bash
./build/build-offline.sh              # 本地优先；缺什么才去下什么
./build/build-offline.sh --skip-openvpn   # 不预编二进制：目标机装的时候就地编译（要 gcc/make/perl）
VENDOR_ONLY=1 ./build/build-offline.sh     # 严格离线：缺件直接失败，一个字节都不下载
./vendor/fetch.sh                     # 在有网机器上补齐 vendor/（幂等）+ 重算 SHA256SUMS
```

最省事的可复现方式（Docker，镜像里已把源码与工具链备好）：

```bash
docker build -f build/Dockerfile.build -t ovpn-build .
docker run --rm -v "$PWD/dist:/out" ovpn-build cp -r /build/dist/openvpn-web-x86_64 /out/
# 产物：./dist/openvpn-web-x86_64/  （含 python/ 与 bin/openvpn）
```

也可以直接在 CentOS 7 / manylinux2014 容器里跑 `./build/build-offline.sh`。版本号用环境变量覆盖：`OPENVPN_VER` / `OPENSSL_VER` / `LZO_VER` / `LZ4_VER` / `PBS_URL`。

构建脚本做三件事：

1. 准备 python 运行时（优先复用仓库自带 `runtime/` → 包内 `python/`），并用 `vendor/wheels` 离线装 `requirements-standalone.txt`（Django 全家桶 + openpyxl）
2. 在 manylinux2014（glibc 2.17 ABI）里编译 **OpenVPN 2.6.23 + OpenSSL 3 + LZO/LZ4/libcap-ng 全部静态链接**（`--disable-dco`、关掉 PAM / down-root / systemd / pkcs11 全部插件，我们只用脚本认证）
3. 打包并校验 `ldd bin/openvpn` 只出现 glibc 一家

**上游链接是会坏的**（2026-11-18 实测：OpenVPN 官方 `swupdate.openvpn.org` 连不上，只能走 GitHub tag 归档，而它不带 `configure`、要先 `autoreconf`）。`vendor/` 就是为这件事存在的——见 `vendor/README.md`。镜像打包后放共享盘，用 `VENDOR=/data/vendor ./build/build-offline.sh` 还能让多个项目共用一份。

**`--skip-openvpn` 打出来的包**不带二进制，安装时会在目标机上就地编译（源码和依赖都在 `vendor/` 里，不联网）。
目标机得先有 `gcc / make / pkg-config`（没有预编译静态库时还要 `perl`）；缺了会**先试着用 yum/dnf/apt 自动补装**，装不上才报错。编不出来安装会明确失败，不会留下「Web 起来了但 VPN 没起」的半成品。
常规交付不用加这个参数——仓库自带 `bin/openvpn`，装完就是完整的。

> 说明：交付包里**已经含编好的静态 openvpn、静态 openssl 和装好依赖的离线 python**，
> 所以「构建」只是可选项：想换版本、换 ABI 时才需要跑脚本。

---

## 5. 部署：一条命令装完（机房目标机，root 一次）

包里该带的全带了：离线 python（glibc 2.17）、静态 openvpn、静态 openssl、CA 生成所需的一切。
目标机**不联网、不装包、不编译**，跑完 `openvpn` 和 `web` 两个都在跑。

```bash
tar xzf openvpn-web-x86_64.tar.gz -C /opt/
cd /opt/openvpn-web-x86_64 && ./install.sh
```

跑完就**能直接连**，不需要再点安装向导：

```bash
./install.sh                                   # 默认：1194/tcp + NAT，对外地址自动探测
./install.sh /srv/ovpn                         # 换安装目录
./install.sh -- --public-addr 1.2.3.4 --port 1194 --lan 10.10.0.0/16
./install.sh -- --user alice:Passw0rd          # 顺手建账号（可重复）
./install.sh -- --nat-mode no-nat              # 无 NAT 溯源模式（需网关回程路由）
./install.sh --with-build-tools                 # 顺手装 gcc/make/perl/pkgconfig，以后能在本机编 openvpn
```

`install.sh` 做 9 件事（全程幂等，重跑不会签第二套 CA），**首尾各加一道检查**：动系统之前跑 `precheck.py` 环境预检（一次报齐所有不满足的条件，硬错误直接中止，不会装到一半留半套环境），装完自动跑 `selftest.py` 并把结果打印在「安装完成」里。

| 步骤 | 内容 |
|---|---|
| 1 | 释放程序文件（`tar` 管道，不依赖 rsync；`data/` 不覆盖） |
| 2 | 选解释器：**强制只用包内 `python/` 便携版**，绝不退回目标机系统 python3；包内运行时不可用即判「交付包不完整」并退出 |
| 3 | 建 `ovpnweb` 用户、目录权限（`data/` 归它可写，**`bin/` 归 root 只可执行**；`install-code.sh` 700 root）；**修复被 Windows/网盘拍平的 python 软链与可执行位** |
| 4 | sudoers 白名单（只放行 `python3 worker.py *`）+ 建 `/etc/openvpn-web`（700 root） |
| 5 | systemd unit（保守语法，兼容 CentOS 7 的 systemd 219） |
| 6 | 初始管理账户 / 密码（默认 `admin` / `admin`，600 权限）；**首次登录强制改密** |
| 7 | **备好 openvpn 二进制**（`prepare-openvpn.sh`）：包内有就用；没有就用包内源码当场编（`VENDOR_ONLY=1`，不联网）；都失败就明确报错，绝不「装到一半走人」 |
| 8 | 调 `setup.py`：签 CA + 服务端证书 + tls-crypt → `server.conf` / `ccd` / `company.ovpn` / ACL 脚本 → 开 IPv4 转发 → 下发 NAT+ACL → mask 发行版 unit → 装规则重放 timer → 启动 OpenVPN →（可选）建账号 → 开 firewalld/ufw 端口 |
| 9 | 启动 Web 并自检 `GET /api/session`；OpenVPN 没起来则以 `exit 3` 结束（Web 仍然在跑，方便登录排障） |

浏览器打开 `http://<服务器IP>:8800`，用**账户 + 密码**登录：新装默认账户 `admin`、默认密码 `admin`（也写在 `data/admin_password.txt`，600）。**登录页不预填账户、不显示任何默认口令提示**，避免被人一眼看到就登进去；凭据从安装完成后的终端输出或凭据文件里取。
**第一次登录会进入独立的强制改密页**，不设新密码无法访问后台任何功能；改完自动进入系统。老机器沿用原有密码，不会被强行重置。客户端配置是 `data/company.ovpn`（全员共用，含 CA 与 tls-crypt）。

`setup.py` 也可以单独重跑（改对外地址、换网段、补账号）：`./setup.py --help`。

### 卸载 / 删干净重装

```bash
./install.sh --purge        # 在安装目录里执行（它知道自己装在哪）
```

`--purge` 按这个顺序拆：① 把 `data/` 打包到 `/root/openvpn-web-data-<时间>.tar.gz`（**先备走再删**，失败就中止）→ ② 停并删 `openvpn-web` 与 `openvpn-web-replay.*` 单元 → ③ 结束 openvpn 进程 → ④ **先删引用再删链**，拆掉 `OVPN_ACL` / `OVPNWEB_NAT`（引用规则里带 VPN 网段，网段可能被改过，所以是照 `iptables -S` 实际输出逐条 `-D`，不猜参数）→ ⑤ 撤 firewalld 放行 → ⑥ 删安装目录、sudoers、sysctl 配置、`/etc/openvpn-web`（安装码）、`ovpnweb` 账号 → ⑦ 自查残留（unit / 两条链 / 目录）并报告「干净」。

一条命令删干净再重装：

```bash
./install.sh --purge && tar xzf /root/openvpn-web-x86_64.tar.gz -C /opt/ \
  && /opt/openvpn-web-x86_64/install.sh
```

> 兼容性：`bin/openvpn` 与 `runtime/` 的 glibc 底线都是 **2.17**，所以 CentOS 7 / RHEL 7 起、到 Ubuntu 26 全都能跑。
> 自查：`objdump -T bin/openvpn | grep -o 'GLIBC_[0-9.]*' | sort -Vu | tail -1` → `GLIBC_2.17`。

---

## 6. 安装向导（5 步，全部在 Web 上）

> 向导只在**还没装完**时出现在侧栏（`initialized` 标记）。装完之后它就自动隐藏，日常运维不会误点进去重跑一遍。需要重装/换参数请用「服务端设置」；真要重走向导，把该标记清掉即可。

1. **环境自检**：x86_64、`/dev/net/tun`、静态二进制、端口占用、openssl、iptables、firewalld
2. **放置二进制**：离线包已自带；没有就直接上传（与更新页同一套校验：x86_64 ELF → SHA256 可选 → 版本安全线 → 分支闸门 → ldd 仅 glibc）
3. **填参数**：对外地址/域名、端口、隧道协议（默认 TCP）、VPN 网段、附加推送路由、tun-mtu（默认 1400）、加密套件，以及**网络模式**（默认 NAT 开箱可用 / 无 NAT 溯源需先配回程路由）。推送路由是**标签式列表**，一条一个标签，写 20 条也不会糊成一行。**动态地址池（`ifconfig-pool`）、DNS / NTP 例外、最低 TLS（下拉选 1.2 / 1.3）与压缩都收在「高级选项」折叠区**（默认收起）—— 这些默认值适用于绝大多数部署，摆在外面只会让人纠结。高级区内部按「地址池 / DNS·NTP 例外 / 协议与兼容」三组划分

> **VPN 网段会自动下发**：`push "route <VPN 网段>"` 由系统自己加（客户端要访问 VPN 网关自身、要和别的客户端互访都靠它），不需要在「附加推送路由」里再写一遍。写了也不会有害——与它重叠的目标会先做减法拆成不重叠的子网再推，只是排序上会并成一条。这条自动路由不计入配置指纹（它在 `ifconfig` 里本来就有），所以不会因为它要求全员重发 ovpn。

> **改 VPN 网段要自己改账号地址**：系统**不会**替你把账号的固定 IP 搬过去。保存新网段后，「认证账号」页会红字列出所有地址还停在旧网段的账号，逐个【编辑】改成新网段里的地址即可（建议保持原来的末位，如 `10.8.0.2` → `10.8.1.2`）。只改网段不会让配置指纹变化，不需要重新分发 `company.ovpn`；启用动态地址池时，池子的起止地址也要一并改到新网段，否则保存会被拒绝并提示具体是哪一段越界。
4. **执行**：签 CA + 服务端证书 + tls-crypt → 写 `server.conf` / `ccd` / `company.ovpn` / ACL 脚本 → mask 发行版 unit → 装规则重放 timer → 开启 IPv4 转发 → 下发 NAT 与 ACL 规则 → 启动 OpenVPN → 自检。点按钮并在弹窗确认，实时日志与进度条
5. **交付**：下载 `company.ovpn`；若选了无 NAT 溯源，复制回程路由命令给网管（默认 NAT 模式不需要）

每步状态落库，刷新/断电重跑会跳过已完成步骤，不会签出第二套 CA。任何一步失败都会回滚到备份点。

**ACL 默认「观察模式」起步**：只记录「谁本该被拦」，不真拦。等授权配齐了，在「资产与授权」页点按钮确认才切到默认拒绝，避免一键装完把所有业务访问都挡掉。

---

## 7. 日常操作

> **全按钮化**：所有高危 / 不可逆操作一律是「点按钮 → 弹窗说明 → 再点一次确认按钮」，**没有任何需要手动输入确认词的地方**（早期版本要输 `INSTALL` / `yes` / `ENFORCE` / `NO-NAT` / `REINIT` / `DEFAULT` / `RESTORE`，已全部去掉）。防误触靠弹窗与醒目的红色确认按钮，不靠打字；登录 + CSRF + 操作审计仍在。

统一收口后的按钮清单：

| 操作 | 交互 |
|---|---|
| 安装向导执行、Excel 全量同步、启用强制模式、切回观察模式、开启/关闭无 NAT 溯源、恢复默认、重签 CA、恢复备份、更新二进制、保存设置并重启 | 点按钮 → 红色确认弹窗 → 点确认 |
| 重启服务、停用账号、删除账号 / 资产组 / 授权 / 客户端互访 | 点按钮 → 确认弹窗 → 点确认（原先删除类操作没有确认，已补上） |
| 保存账号、添加资产 / 授权 / 互访、改管理密码、在编辑弹窗里改账号密码 | 直接点按钮提交（无二次确认，都是可逆或明确的数据录入） |

顺手把几处「本来要手输」也改成按钮：

- 添加 / 编辑账号的**固定 IP**：点【自动分配】/【换一个】，直接填入下一个可用地址（新增 `GET /api/users/next-ip`）
- **初始密码 / 账号密码**：点【生成随机密码】自动填入；密码以明文存台账，可在【编辑】里查看/复制/修改
- **临时授权到期时间**：点【2 小时 / 1 天 / 7 天 / 30 天 / 清除】快捷填入，不用手敲 `YYYY-MM-DD HH:MM`
- 浏览器原生的 `confirm()` 弹窗全部换成统一的自定义弹窗，移动端显示一致

仍然需要手打字的只有**数据本身**：管理账户名、管理密码、账号名、备注、网段/端口/域名、可选的 SHA256 清单、搜索关键词、文件选择。这些是内容不是确认，无法用按钮代替。

### 账号

「认证账号」页：添加 / 编辑（**账号名**、姓名、部门、固定 IP、**密码**、**有效期**、备注）/ 停用 / 删除 / 导入导出 Excel（只有 Excel，不再有 CSV 两套入口）。**列表一次显示全部账号，不分页**；支持按账号、姓名、部门、IP 搜索。密码**不进列表显示列**，只在【编辑】弹窗里查看（默认星号遮盖，点【显示】可见）；独立的【改密】按钮已删除，改密码统一在【编辑】里做。导入导出**只针对全部账号**，不做单用户选择：导出是当前整张台账，导入是整张表覆盖同步。

**在线用户页只提供「停用账号」，不再有「踢下线」**。原因很实际：踢下线只断当前这一条连接，OpenVPN GUI / Connect 客户端默认会自动重连，几秒后同一个账号又出现在列表里——等于没处理。

点【停用账号】会做三件事：立即断开当前连接、移除它的固定 IP（ccd）、账号状态改为停用。客户端再怎么重连都会认证失败，进不来。要恢复，到「认证账号」页把状态改回启用即可。台账页的【停用】按钮走的是同一条链路。

- **账号名可以直接用中文**（如 `张三`），也支持工号/拼音；不允许空格与 `/`，1~32 位。中文账号的 `ccd/张三` 文件名跟着走，实测 OpenVPN 2.6.23 + TCP 下正常认证并拿到固定 IP（老客户端对中文名的兼容性看它自己的输入框，拿不准就用拼音）
- 固定 IP 只能落在可用段（默认 `10.8.0.2~.254`，开启动态池后池子里的地址会自动排除），重复即整批拒绝
- **账号名可以改**（编辑弹窗第一栏）：工号写错、入职改名都能纠正。改名会把 `users` 主键、`ccd/<用户名>`、用户组成员、资产授权、客户端互访授权一起搬到新名，**不用重新配授权**；旧名的 ccd 会被删掉，改名后旧账号立刻不能拨入。

**Excel 能改什么：** 姓名、部门、固定 IP、密码、有效期、状态、备注都能通过「Excel 全量同步」批量改。但表里的「用户名」是身份本身——把某行的用户名改掉，等于删掉旧号、新建一个号，已配的授权不会跟过去。只想给某人换个账号名，用列表里的【编辑】，别在 Excel 里改。

- **密码以明文保存在台账里**（`users.password_plain`），同时仍存 PBKDF2 哈希用于认证：列表不再显示密码列，【编辑】弹窗里密码框默认以 `*` 遮盖，点【显示】可见，可【生成随机密码】/【复制】，改完保存即重置。输入宽度固定，显示/隐藏切换不会让右侧按钮移动；保存后不再弹二次密码页，也不会丢
- **账号有效期**：添加 / 编辑里填 `YYYY-MM-DD`，当天仍可用，次日零点起自动停用并断开在线会话；留空表示长期有效。认证脚本也会独立检查，即使没人打开后台，到期账号同样拨不进来。老库升级时该项为空，不影响现有账号
- 姓名 / 部门是纯台账字段（不参与认证），Excel 列顺序：用户名 / 姓名 / 部门 / 密码 / VPN固定IP / 状态 / 有效期 / 备注；密码列必填
- 改 IP 会**全量重建 ccd + iptables 链 + 断开该连接**：防止后来的人分到旧 IP、继承上一个人的服务器权限
- 停用账号不生成 ccd，`ccd-exclusive` 直接拒绝；同账号只允许一处在线（不开 `duplicate-cn`）

> **安全提醒（1.3.8 起）：** 为满足「密码可查可改」，台账数据库、Excel 导出、TSV 自动备份里都会出现**明文 VPN 账号密码**；请限制 `data/` 目录与备份文件的读取权限（备份已设 600），导出文件用完及时删除。
> **老版本升级**：1.3.7 及以前只存 PBKDF2 哈希，明文无法反推，升级到 1.3.8 时会给这些老账号**换发一张新密码**，并写到 `data/backups/明文密码迁移-<时间>.txt`（600）——升级后请尽快通知本人。新装 / 新建账号不受影响。

### Excel 全量同步（唯一表格格式）

导出 → 编辑 → 预览变更清单 → 点【确认全量同步】按钮提交。每次自动备份到 `data/backups/users-<时间>.tsv`，误删可直接把该备份当台账重新导入回滚。预览令牌 5 分钟过期。

**密码列必须逐行填写明文，留空整批拒绝**（预览阶段就直接报错，不会落盘）。不再支持「留空保留原密码」「空密码自动生成」和「空密码也重新生成」这些历史上容易误操作的规则：

| 表里这一行 | 结果 |
|---|---|
| 老账号 + 密码列**有值** | 按表中明文覆盖，哈希同步更新 |
| 新账号 + 密码列**有值** | 直接建号并用表中明文密码 |
| 任意行 + 密码列**留空** | 整批拒绝，不写库 |

导出的 Excel 已经带上当前全部明文密码，直接改完再导回来即可。有效期列写 `YYYY-MM-DD`，留空为长期有效；格式错误同样整批拒绝。每次导入仍会自动备份台账到 `data/backups/users-<时间>.tsv`，那份 TSV 也含明文密码，权限 600，别随意外发。

### 自助改密（用户自己动手，不用找管理员）

用户拨上 VPN 后打开 `http://<服务器>:8800/self`：**系统按来源 IP 直接认出他是谁**（来源地址就是他台账里的固定 IP），页面上只有三个框——当前密码、新密码、重复新密码，**没有任何要填 IP 的地方**。

- 没拨 VPN 打不开也改不了：页面会明确提示「请先连上 VPN」。这是故意的——IP 由服务端从连接来源取，前端无法指定别人的账号，也就没法拿它试别人的密码

- 失败不区分「IP 不存在 / 账号停用 / 密码错」，防止被拿来枚举台账
- 来源 IP 连续错 5 次锁 10 分钟
- 改完只影响下次拨号，`company.ovpn` 不用重下
- 页面模板在 `ovpn/self_page.html`（原 `app.py` 内联的 `SELF_PAGE` 已提取，内容保持一致），Web 热更新同样能覆盖到它

### 操作日志与操作者（审计直接记管理员账户）

后台登录用**独立管理账户 + 密码**（`管理密码与安全 → 管理账户`，默认 `admin`，3~32 位字母数字下划线短横线）。账户名与密码分开管理：

- **新装默认 `admin` / `admin`**，首次登录必须改密：改密页不提供旧密码输入（会话已验证过默认口令），只收「新密码 + 重复新密码」；未改完之前，后端拦住除改密 / 会话 / 退出以外的所有接口。
- **改账户名不用验证密码**——忘了密码时也能改（首登强改阶段除外）。
- **改密码要填两遍**（新密码 + 重复新密码），两次不一致直接拒绝，避免手滑输错把自己锁在外面。改完后 `data/admin_password.txt` 只留说明、不再留明文口令。
- **忘了凭据只能走服务器脚本**：Web 界面不再提供【一键重置 / 重新生成账户与密码】入口；在服务器上用 root 执行 **`bash /opt/openvpn-web/reset-admin.sh`**（等价于 `python3 manage.py ovpn_cli reset-admin`，会自动写好 `data/admin_password.txt` 并重启服务），登录页也只提示这条命令。
- 命令行救场：`python3 manage.py ovpn_cli reset-admin` 直接打印新凭据。

老库升级时自动补 `admin` 账户名与强改标记默认值 `0`，原有密码继续可用、不会被迫改密。

审计日志里的「操作者」**就是管理员登录账户名**，不再单独配「操作者名称」——谁干的直接看账户，顶栏也显示「账户名 · 你的来源 IP」。改账户名后新会话立即用新名字记审计。

「日志审计 → Web 操作审计」每条都带 **操作者 IP**（数据库 `audit.ip`，老库升级时自动加列）。三条链路各有来源：

| 日志 | 操作者 | IP 从哪来 |
|---|---|---|
| 后台里的操作（改配置、加账号、改授权……） | 管理员登录账户名 | 请求来源地址（只信任本机反代的 `X-Forwarded-For`） |
| 命令行 / 定时任务（`manage.py ovpn_cli allow`、worker 动作） | `root` 执行的记为空，账号变更记 `web` | 无（显示 `—`），这类操作本来就没有 HTTP 来源 |
| VPN 拨号认证 | 账号名 | 客户端来源公网 IP（`untrusted_ip`） |

### 资产与授权（ACL）

三层结构，解决「人一多、IP 一多就没法看」：

1. **资产组**：一个组里放多条目标。可以一次贴 3 个 IP（一行一条，`目标 [协议] [端口]`），也可以放整个网段。单个 IP 自动补 `/32`。已有条目可点【编辑】直接改目标 / 协议 / 端口，不用删掉重建。
2. **用户组**：手动输入账号名组成一组（回车确认，支持中文账号和【批量】粘贴），成员再多也不用在一长串勾选框里翻。
3. **授权**：「账号 或 用户组」→「资产组」。一次把一组 IP 授给一组人，不用一条条点。

- 同一个「目标 + 协议 + 端口」**只允许存在一条**：重复添加会被拒绝，并告诉你在哪个组里已经有了。端口顺序无关（`80,22` 与 `22,80` 是同一组）。
- 授权可叠加（在组里 + 单独授权，取并集）；临时授权填 `YYYY-MM-DD HH:MM`，到期自动失效。
- 所有列表都有搜索（按页各自适用）+ 分页 + 表头吸顶，几百条也不会把页面撑爆；每条授权会显示「展开成多少条规则」。

硬限制落在服务端防火墙，**立即生效、不用重连、客户端改不了**。ccd 里 push 路由只能当体验优化，客户端自己 `ip route add` 就绕过了，不能当安全边界。DNS(53)/NTP(123) 自动放例外（只放具体 IP）；**未授权的客户端互访默认被挡死**。越权尝试（含观察模式）限流记日志，「日志审计 → 越权尝试」单独一页。

### 网络模式（无 NAT 溯源开关）

「服务端设置」页的「网络模式」卡片，或概览顶部的一键按钮：

- **NAT（默认）**：自建 `OVPNWEB_NAT` 链对 VPN 网段做 SNAT（本机内网 IP 固定时）或 MASQUERADE。开箱可用，无需回程路由；内网日志源 IP = VPN 服务器
- **无 NAT 溯源**：清空 NAT 链并删掉针对 VPN 网段的裸 MASQUERADE/SNAT，内网服务器看到账号固定 IP。开启需点按钮二次确认，并**先配好网关回程静态路由**

切换只重建防火墙规则：不重启 OpenVPN、不断开在线连接、配置指纹不变、`company.ovpn` 不用重新分发。随时可切回。自检页会显示当前模式、IPv4 转发状态、NAT 残留检查。

### 客户端互访（P2P）

「资产与授权」页的「客户端互访」卡片：选源账号 → 目标账号 → 协议/端口 → 添加，即可让两个 VPN 客户端互访，支持到期时间。

**为什么不开 `client-to-client`**：那个指令让流量在 OpenVPN 进程内部直接转发，不经过内核 `FORWARD` 链——ACL 管不到、越权日志没有、源 IP 也不再走我们的规则，整套管控和溯源被绕过。所以这里让客户端间流量照旧经过服务端内核 FORWARD，按「账号固定 IP → 账号固定 IP」在 `OVPN_ACL` 里放行：

- 回包由 `ESTABLISHED,RELATED` 放通（FORWARD 最前那条），不用给反方向再配一条
- `OVPNWEB_NAT` 链第一条是 `-o tun0 -j RETURN`，tun0 → tun0 的包**不做 NAT**，对端看到的仍是对方的固定 VPN IP
- 客户端本身无需任何改动，不受 NAT / 无 NAT 溯源模式影响，也不需要回程路由
- 同样受观察/强制模式、越权日志、到期失效、改 IP 全量重建的约束

### 服务端设置

改端口/协议/网段/加密/MTU/推送路由 → 事务式保存 → 全量重建 → 重启 → 失败回滚。**没有「内网网段」这个输入了**：只有一个「附加推送路由」列表，旧库里的内网网段会在升级时自动并进来。页面会显示**配置指纹**：

- 指纹变化（动了 CA / tls-crypt / 端口 / 协议 / 加密套件 / 实际推送路由集合）→ 红字提示「必须全员替换 `company.ovpn`」
- 指纹按**最终下发给客户端的路由集合**算，所以把同一批路由换个写法不会误报「要重发」；**VPN 网段那条自动路由不计入**（它本来就在 `ifconfig` 里）
- 指纹不变（只换二进制）→ 绿字「无需重新分发」

#### 压缩 / 加密套件（最容易误解的两个）

| 参数 | 到底是什么 | 怎么填 |
|---|---|---|
| **压缩** | 只在「链路确实窄」时才有意义：上行带宽小（4G、ADSL、跨境专线）**并且**流量可压缩（文本、日志、代码、SQL）。视频 / 图片 / zip / HTTPS 本身已压缩，再压只是白烧 CPU。另外 GCM 模式下压缩有 VORACLE 这类明文恢复风险 | 默认**关闭**。确实窄再开 LZ4；改了必须重新分发 `company.ovpn`（客户端要同步开） |
| **数据加密套件** | `data-ciphers` 是**协商顺序表**，不是「同时开几个」。服务端与客户端按顺序比一遍，挑中第一个双方都支持的算法，整条连接只用那一个。写多个是为了兼容不同客户端（x86 服务器有 AES-NI，手机 ARM 上 CHACHA20 更快） | 默认 `AES-256-GCM:CHACHA20-POLY1305` 两个足够。只有要接很老的客户端才勾 `AES-256-CBC`（弱，仅救急） |

**没有「最大客户端」这个设置了**：并发上限天然由「一人一个固定 IP + `ccd-exclusive`」决定——台账里有多少启用账号，就最多能同时进来多少个；不做台账的连接连 ccd 都找不到，直接被拒。再叠一个 `max-clients` 数字只会变成莫名其妙的 `TLS Error: max-clients reached`（升级时这个参数会从 `server.conf` 里删掉）。

**动态地址池（`ifconfig-pool`）默认不写。** 它本来是给「没在台账里的临时连接」兜底用的 DHCP 式分配，本系统的固定 IP 全由 `ccd/ifconfig-push` 下发，用不上。真要接临时用户，在「服务端设置 → 高级选项 → 动态地址池」里勾上并手动填起止地址即可，界面会实时算出「这个池子占掉多少地址、还剩多少固定 IP 可分配」。`/24` 网段关掉池子时能放 253 个固定 IP（`.2~.254`）。


两个高危按钮都做了**点按钮二次确认**（红色确认框）：重新初始化 CA、恢复默认，无需输入任何确认词。

---

## 8. 版本与更新（手动，不自动）

「版本与更新」页只显示**当前服务端版本号与 Web 版本**，不再显示「安全线达标/偏低」「分支 EOL」这类判断——版本本身如实呈现，不做「安全/不安全」的结论（旧版本可能后来暴露问题，静态显示安全只会自相矛盾）。升级只有一条路径：**上传官方源码包，本机自动编译**。

传 `openvpn-2.6.x.tar.gz`（GitHub / 官方 releases 的源码发布包），本机编译并替换服务端二进制，**全程不联网**：

- 依赖库复用 `vendor/static/prefix.tar.gz` 里预编译好的 `libssl.a / libcrypto.a / liblzo2.a / liblz4.a / libcap-ng.a`，只编 openvpn 本体，实测 14 秒出结果
- 流程：**校验 → 打备份点 → 编译 → 原子替换 → 重启 → 自检 → 失败自动回滚**
- 校验：`ldd` 只允许 glibc、版本安全线、分支闸门（默认 2.6.x → 2.6.y，跨大版本需勾选）
- 编译环境（gcc / make / pkg-config）在**安装尾声和每次服务启动时自动检查一遍**，结果连同检查时间记进数据库，版本页直接展示；补装工具后点【重新检测】即可刷新，不用重启。缺工具时用中性文案说明——**不影响当前 VPN 服务和客户端**，只是不能在本机从源码编译新版本（版本页因此不会让人误以为出了故障）。
- 本机编译产物只保证不低于本机 glibc；要发给更老的机器（如 CentOS 7），在构建机用 `build/build-static-openvpn.sh` 编好，直接替换安装目录的 `bin/openvpn` 后重启服务（`POST /api/update/binary` 也能走这条，界面上不暴露）

Web 程序自身不提供「上传更新包」入口（日常更新就是覆盖文件 / 重跑 `install.sh`）。后端 `/api/update/web` 接口仍保留，仅作为「改错代码把页面搞崩」时的远程救场通道：进程还活着就能 POST 一个 zip 换回 Django 项目文件 / `core.py` / `index.html`（**不允许替换 root 执行的 `worker.py`**），`data/` 与 `bin/` 不受影响。

### 备份与恢复（回滚 · 下载 · 迁移，同一套）

快照和全备份已合并成**一种包**：`data/full-backups/openvpn-web-full-*.tar.gz`。改配置、升级二进制、导入台账、跑安装向导之前，系统会自动打一份（来源标记「自动」）；也可以在「版本与更新 → 备份」点【立即备份】手工打一份（来源「手动」）。**两者格式完全一样，都能在本机一键恢复，也都能下载搬到另一台机器**；恢复前会自动再打一份保护备份，出事还能退回来。

同一页还有独立的「认证账号备份」：每次账号变更（新增/修改/删除/Excel 全量同步）前自动把当时整张台账存成一份 **Excel，列与手工导出完全一致（含明文密码）**，可以单独下载——下载后直接用「认证账号 → 导入」做全量同步，就等于从本地文件恢复；也可以点【恢复】在机器上退回那一份的状态（**这份备份里没有的账号会被删除**，恢复前同样会先备份当前台账）。

一份包就是当时的完整业务状态，一个 tar.gz 分两块，各管各的：

| 块 | 内容 | 能做什么 |
|---|---|---|
| `openvpn/` | `ca.crt` / `ca.key` / `server.crt` / `server.key` / `ta.key` / `server.conf` / `company.ovpn` / `ccd/` / `openvpn.bin` | **不依赖本 Web 系统**。拷到任何 OpenVPN 服务器上，改一下配置里的绝对路径，`openvpn --config server.conf` 就能跑；客户端用 `company.ovpn` 连。包内 `openvpn.bin` 用于升级失败时连二进制一起回滚 |
| `web/` | `openvpn-web.db`（账号、授权、白名单、审计、配置）、`web.secret`（会话签名）、`admin_password.txt`、`meta.json` | 装好 Web 后一键还原，管理密码、白名单、账号台账、授权全都不用重配 |

恢复时**不还原**连接日志、认证日志正文和在线连接（会重启服务），审计与任务进度保留，否则恢复后审计会缺一段。数据库走 SQLite 在线备份（不是复制 `.db` 文件），避免和后台任务抢锁。恢复之后客户端不用重发 ovpn。

包内有 `manifest.json`，每个文件带 SHA-256；还有一份中文《恢复说明.txt》。包校验不过就整包拒绝，不会写一半。

四种恢复方式，按场景选：

1. **同一台 Web、恢复本机备份**：备份列表里点对应包那一行的【恢复】，会重建规则并重启服务。
2. **从本地文件恢复（迁移包）**：「恢复」卡片里选本地 `.tar.gz` → 【检查本地备份包】→ 看摘要 → 点【确认恢复】。
3. **新机器迁移（推荐）**：先跑 `install.sh` 装好同版本 Web，把包 scp 上去，然后
   ```bash
   python3 manage.py ovpn_cli restore-full /path/to/openvpn-web-full-xxx.tar.gz
   systemctl restart openvpn-web
   ```
   恢复程序按目标机的目录重新生成 `server.conf` / `company.ovpn`，所以换目录也不用手改；在线用户可以继续用原来的 ovpn（CA 与 ta.key 一起恢复了）。
4. **完全不用 Web**：只取 `openvpn/` 那一块，文件放到目标机的 OpenVPN 数据目录，`ccd/` 覆盖，手改配置里的证书路径后启动 `openvpn --config server.conf`。

> 老版本 `data/snapshots/` 下已经存在的目录快照仍能恢复，只是新版不再生成这种目录。

**安全提醒**：包里有 CA 私钥和 tls-crypt 静态密钥，拿到就等于拿到了整个 VPN 的身份。请加密保存，别放公网网盘。服务器只保留最近 10 份，下载到本地的自己管好。

实测记录（测试机 10.255.114.16）：生成包 16 KB → 下载 → 造两个脏账号 → 分别用命令行和 Web 界面恢复 → 两个脏账号都撤回、`ccd` 与指纹一致、Web/VPN 服务正常。

### 服务端升级后，客户端要不要更新？

不用。ovpn 里装的是连接凭据（CA、tls-crypt、remote、proto、auth-user-pass），不是版本号。2.5 起数据通道加密改成 `data-ciphers` 协商，客户端上报 `IV_CIPHERS`、服务端挑双方都支持的算法，新老版本互通。真正需要重新发 ovpn 的只有三类：改连接参数（地址/端口/协议/MTU）、换 CA / ta.key / 服务端证书主题、改加密套件到某些客户端不支持的算法——而且仍然是**全员一份**。

> 拓扑（`topology subnet`）不在此列：服务端每次连接都会用 `push "topology subnet"` 下发，老 ovpn 即使没写这一行也会被纠正，不用重新分发。

唯一要升级客户端「软件」的情况是它本身太老：**OpenVPN 2.3 及更早**不会协商 `data-ciphers`、也不支持 `tls-crypt`，连不上 2.6 服务端。定个底线：客户端 ≥ 2.4，推荐 2.6 / OpenVPN Connect 3.x。这是一次性要求，跟以后打多少次补丁无关。

---

## 9. 安全边界与纪律

- 纯 HTTP 内网页，**默认只放行本机网卡所在网段 + 127.0.0.1**（升级时自动从「所有私网」收紧）；VPN 拨号用户只放行自助改密页 `/self`，进不了后台。名单可在「管理密码与安全 → 管理端访问白名单」里改，保存时会校验「你当前 IP 还在名单里」，防止把自己关在门外；配错了 SSH 上去 `python3 manage.py ovpn_cli allow 10.20.30.0/24`（`allow-reset` 恢复本机网段）。管理账户 + 密码（PBKDF2）、签名 Cookie（HttpOnly / 8h）、CSRF、失败 5 次锁来源 IP 10 分钟、全操作审计（含操作者 IP）。**别直接暴露公网**，远程管理请走 HTTPS 反代或堡垒机
- `X-Forwarded-For` 只在「本机反代」时才采信（`remote_addr` 是 127.0.0.1），否则任何人加个头就能伪装成白名单地址

- 认证脚本任何异常一律 `exit 1`（fail-closed）；密码走 `/dev/shm` 临时文件，不进环境变量/命令行；同一「账号+来源IP」10 分钟失败 5 次锁定
- 溯源到的是**账号**不是设备：`company.ovpn` 全员共用，账号密码不要转借；一人一账号，别共享。**只有开启「无 NAT 溯源」后内网服务器日志才能看到账号固定 IP**；默认 NAT 模式下内网日志源 IP 是 VPN 服务器，只能靠认证日志查来源公网 IP
- 固定 IP 只能管理员改，不放权给客户端
- 别把 `company.ovpn` 之外的个人凭据塞进客户端配置；服务器配置里不要加 `redirect-gateway` / `duplicate-cn` / `client-to-client`。**别手工加 `client-to-client`**：客户端互访走内核 FORWARD 才能被 ACL 与日志覆盖
- NAT 由自建 `OVPNWEB_NAT` 链接管；别再执行 `firewall-cmd --add-masquerade` 或另加 NAT 规则，否则开启溯源后会有残留导致链路失效
- 上传的二进制会被执行 `--version` 读版本号，**只上传可信来源的静态二进制**

### 9.1 上线前安全清单（v1.3.16 加固）

| 项 | 现状 |
|---|---|
| 安全响应头 | 全站下发 `Content-Security-Policy`（禁外链脚本/对象/嵌套）、`X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`Permissions-Policy` |
| 错误回显 | 500 只回「服务端处理失败，错误已记录」，异常栈只进服务器日志与审计；404/405 保持原状态码 |
| 未登录信息暴露 | `/api/session` 未登录不再返回管理账户名；白名单外来源一律 403 |
| 凭据重置 | Web 端「已登录即可重置管理凭据」的 `/api/admin-reset` 已删除，只保留 `reset-admin.sh` / `--reset-admin` |
| Excel 注入 | 导出的 xlsx 里以 `=` `+` `-` `@` 开头的内容强制按文本落盘，避免打开表格时执行公式 |
| 自助改密页 | 姓名/部门/账号名一律 `textContent` 写入，不再拼 `innerHTML`（台账数据经 Excel 导入，不能被当成 HTML） |
| CSRF / 会话 | 写接口全部校验 CSRF；登录成功 `session.clear()` 防会话固定；Cookie HttpOnly + SameSite=Lax + 8 小时过期 |
| 路径与命令 | 备份包 tar 成员只允许 `openvpn/`、`web/` 前缀；worker 只接受固定 action 白名单 + `safe_path`；Web 侧所有 `subprocess` 不走 shell |
| 文件权限 | `openvpn-web.db` + `-wal/-shm`、`web.secret`、`admin_password.txt` = 600；`data/backups` = 700；`data/` = 750（启动时自动纠正，安装脚本同步设置） |
| HTTPS 反代 | 走 HTTPS 时设 `OVPN_WEB_HTTPS=1` 启动，会话 Cookie 带 `Secure`，同时下发 HSTS |

**已知边界（架构决定，不是漏洞）**
- **Web 管理员 ≈ root**：管理员能给服务器下 iptables、重启服务、上传源码本机编译（编译以 root 执行）。所以后台不要给不信任的人，管理端务必只在内网 / 堡垒机后。
- **明文密码存库**：台账按你的要求保存明文（方便查看、导出 Excel、下发），数据库文件权限必须保持 600，`data/` 目录不要给别人读。
- **默认是 HTTP**：密码与台账在内网明文传输。要跨公网管理，请套 Nginx/Caddy HTTPS 反代（并设 `OVPN_WEB_HTTPS=1`），或只走堡垒机。
- 账号密码长度：管理密码与自助改密强制 ≥8 位；管理员建账号时目前不强制（历史台账常有短密码），建议上线后统一按 ≥8 位维护。

### `ta.key` / tls-crypt 是干什么的（为什么 iKuai 上没有）

`data/ta.key` 就是那段 `-----BEGIN OpenVPN Static key V1-----` 的静态密钥，它在 `server.conf` 里以 `tls-crypt ta.key` 引用、在 `company.ovpn` 里内联在 `<tls-crypt>` 中。它**不加密业务数据**（那是 `data-ciphers` 的事），只作用于 TLS 控制通道：

| 作用 | 没有它会怎样 |
|---|---|
| 给整个握手过程（含证书）再套一层对称加密 | 握手包是明文结构，中间人能看到证书、也能识别出「这是 OpenVPN」 |
| 用静态密钥做 HMAC 认证，密钥不对的包直接丢弃 | 任何人都能往端口上灌包，服务器要陪它走完 TLS 握手，容易被探测/拖资源 |
| 防中间人伪造控制包 | 攻击者可尝试注入/篡改控制通道报文 |

一句话：**它是「先把门藏起来，再验身份」** —— 端口扫描看不出这是 VPN，无效流量在第一层就被丢掉。iKuai 的 OpenVPN 界面通常只给 `tls-auth`（只签名不加密）或干脆不生成，所以你在那边没见过这个文件；不配也能连，只是把端口暴露成「谁都能敲门的 OpenVPN」。

全员共用同一份 `ta.key`：**换掉它 = 所有人必须换 `company.ovpn`**（配置指纹会变，页面会红字提示）。它不参与数据加密，所以平时不用动；但泄露后应连同 CA 一起重签。

---

## 10. 排障清单

| 现象 | 查什么 |
|---|---|
| 连上隧道但 ping 不通内网 | **仅无 NAT 溯源模式**：网关回程静态路由没配（第 2 节）；或切回 NAT 模式先恢复 |
| **ping 通但 HTTPS 超时 / SSH 秒断（10053）** | **仅无 NAT 溯源模式 + 爱快网关**：缺 NAT 过滤规则，回程 TCP 被 SNAT 成网关 IP。按第 2.1 节配「回程流量跳过 NAT」规则 |
| 内网日志源 IP 全是 VPN 服务器 | 当前是默认 NAT 模式（正常）；想看账号固定 IP 就开启「无 NAT 溯源」。若已开启仍如此 → 有 NAT 残留，点「重建规则」，自检页会报 |
| 全员连不上 | 服务没起 / 端口被占 / `ccd-exclusive` 下账号没有 ccd / firewalld reload 冲了 FORWARD（timer 5 分钟内恢复） |
| 某账号连不上 | 状态是否停用、密码、是否被失败锁定、`ccd/<用户名>` 是否存在 |
| Windows 报 `local and remote VPN endpoints must exist within the same 255.255.255.252 subnet` | 客户端把 ccd 下发的 `10.8.0.x 255.255.255.0` 按点对点（net30）解析了，即拓扑没对上。服务端 `server.conf` 要有 `topology subnet` **和** `push "topology subnet"`；客户端 ovpn 也要有 `topology subnet`。缺 push 时 Linux 客户端能连、Windows 直接 fatal，所以别只拿 Linux 验 |
| 改了 IP 权限不对 | 看是否触发了全量重建；自检页对比「启用账号数 / ccd 文件数」 |
| ACL 没生效 | 是否还在观察模式；授权是否过期；目标是否被 LB/NAT 改了源 IP |
| 客户端之间互访不通 | 是否加了互访授权（默认全拒）、是否还在观察模式、双方账号是否启用；自检页看 IPv4 转发是否开启 |
| 时间串不起来 | 所有机器的 NTP |
| Web 打不开 | 先看是不是**白名单拦的**：页面会直接告诉你「来源 IP x.x.x.x 不在管理端白名单内」。SSH 上去 `python3 manage.py ovpn_cli allow <你的网段>`，或 `allow-reset` 恢复成「127.0.0.1 + 本机网卡网段」；再看 `journalctl -u openvpn-web -n 50`、`systemctl status openvpn-web` |
| 特权动作失败 | `journalctl` + 任务日志 `data/tasks/<id>.log`；确认 sudoers 里的解释器路径与 `core.PY` 一致 |
| 上传二进制/源码提示「需要安装码」 | 这是设计如此：这三个动作会把外部代码装成 root 服务，必须有人在机器上按一下。`sudo /opt/openvpn-web/install-code.sh` 拿一个（8 位、15 分钟、只能用一次、填错 5 次作废） |
| 「升级」页说本机不能编译 | 缺 `gcc/make/pkconfig`。装一次：`yum install -y gcc make perl pkgconfig`（Debian/Ubuntu 用 `apt-get install -y gcc make perl pkg-config`），或在安装目录跑 `./install.sh --with-build-tools`。CentOS 7 已 EOL，官方源失效时把 `CentOS-Base.repo` 的 `baseurl` 指到 `mirrors.aliyun.com/centos-vault/7.9.2009/` |

---

## 11. 验证情况与限制

- `python3 src/selftest.py`（或装机后在安装目录 `python3 selftest.py`）：**424 项全通过（实测 Windows 424、装机目录 426）**——配置渲染、双网络模式（默认 NAT / 无 NAT 溯源）与切换不改指纹、自建 NAT 链、IPv4 转发前提、证书签发、账号校验（含中文账号 + 中文 ccd + 改名时授权/用户组跟着搬、**明文密码入库与改密同步**）、动态地址池开关与地址冲突、**安装码闸门（一码一次 / 过期作废 / 错 5 次作废 / 脚本生成的码能直接过）**、认证脚本（放行/拒绝/锁定/fail-closed/**有效期到期拒绝**）、Excel 全量同步（**导出带明文密码、密码列留空整批拒绝、有效期落库**）、**账号有效期到期自动停用并清 ccd**、ACL 默认拒绝与改 IP 不残留、客户端互访授权与过期过滤、跨大版本闸门、安装向导长任务、全部 API（含首页禁用缓存、网络模式切换、P2P 增删、下一个可用 IP、容量、白名单保存与「排除自己」拒绝、独立管理账户登录与免密重置、改密码双次确认、**默认 admin/admin 首登强改：未改密拦接口 / 改密免填旧密码 / 改完默认口令失效 / 凭据文件不留明文**、审计带 IP、按钮式保存/恢复默认/重签 CA）、资产条目编辑（改 IP/协议/端口、重复拒绝）、二进制更新、备份打点与回滚、备份生成/下载/上传/按包名恢复/删除与安全校验
- 前端在 jsdom 里逐页真跑（`tests/smoke.js`），另有 `tests/shots.js` 用无头 Edge 截图肉眼核对版式
- **真实环境实测（2026-11-18，Anolis 8.10 / OpenVPN 2.6.23 / TCP）**：建中文账号 `张三` → 服务端生成 `ccd/张三` → 客户端用中文账号认证通过 → `PUSH_REPLY ... ifconfig 10.8.0.2` 拿到台账固定 IP；白名单按来源判定：隧道内 IP 访问 `/api/state` = 403、`/self` = 200，白名单内网段可进后台，公网 IP 全 403

- 真实 Django HTTP 服务启动验证：首页、会话、登录、CSRF、state、向导自检、账号、版本、导出均 200；测试机 10.255.114.15 上 `install.sh` 完整跑通，装完自检 426 项全过，OpenVPN 与 Web 双进程在跑
- 前端 JS 通过 `node --check`
- 所有 `.py` 通过 `py_compile`，所有 `.sh` 通过 `bash -n`

**限制（如实说明）**：

1. 沙盒内以 MOCK 模式验证业务逻辑，**`iptables` / `systemd` / 真实 OpenVPN 进程未实际执行**，这部分由目标机上的 `install.sh` + 安装向导完成，向导自带环境自检与失败回滚
2. 静态 openvpn 二进制与离线 Python 运行时**未在本环境实际编译/下载**（需要联网构建机），由 `build/build-offline.sh` / `build/Dockerfile.build` 复现；也可在目标机就地编译，或在「升级」页上传二进制（要安装码）
3. 是功能与操作逻辑对齐爱快风格，**不是像素级复刻**，未使用其任何界面素材
4. 单机部署，不做高可用、集群、SSO/MFA、多管理员 RBAC、自动 HTTPS
5. 目标机限定 **x86_64 + glibc ≥ 2.17 + systemd + 可用的 `/dev/net/tun`**：Alpine（musl）、ARM、无 init 系统的容器暂不支持，预检会明确拦住并说明原因；不做无 systemd 的 cron 降级，也不出「依赖系统 python3」的薄包——交付包自带全部运行时，就是为了不因环境差异报错

---

## 12. 版本信息（截至 2026-11-23）

- Web **2.0.0**：**正式版标签**。功能与 1.3.30 一致，仅将 Web 版本标签提升为 2.0.0（`core.WEB_VERSION` 与 `index.html PAGE_VERSION` 同步），标识 Web 框架已完成从 Flask 到 Django 5.2 standalone 的迁移。
- Web **1.3.30**：**Web 框架整体迁到 Django 裸机底座，并同步底座 v1.1.0 的运行时/离线 wheel 能力**。Web 由 Flask 换成 **Django 5.2 standalone**（Waitress + SQLite WAL + WhiteNoise），**不用 Docker**；`core.py` / `fullbackup.py` 与 `index.html` 前端零改动，60+ REST 路径与 `X-CSRF-Token` 契约不变。管理登录改为 **Django Auth + django-axes**（失败 5 次锁 10 分钟、锁定返回 JSON 403），底座 `core` app 改名 `accounts` 以避开业务 `core.py`；启动入口改为 `serve.py`（自动 migrate / 建管理员 / 生成 `data/web.secret` / 收集 HOSTS），救场命令改为 `manage.py ovpn_cli {allow|allow-add|allow-reset|reset-admin|restore-full}`。同步底座 v1.1.0：解释器探测同时兼容 `runtime/python/`、`python/`（交付包）、`runtime/`（源码开发）与旧 `django/python3.12/`；新增 `./start.sh wheel:download` 与 `standalone:install` 的 `--no-index` 离线安装（pip 缺失走 `ensurepip`），`STANDALONE=True` 时 `standalone:*` 不再误进容器。selftest 经兼容层用 Django `Client` 跑原 424 项，全通过；新增 `.gitignore` 对 `.env` / `logs/` / `staticfiles/` / `wheelhouse/` 的忽略。
- Web **1.3.29**：**「拷过去跑一下就好」补齐环境侧**。三处改动：① **强制只用随包便携版 Python**——删掉 `install.sh` 里「系统 python3 有 flask 就用系统的」兜底分支，包内运行时不可用就直接报「交付包不完整」并退出；目标机 Python 版本/依赖千差万别（CentOS 7 只有 3.6、最小化系统压根没装），不碰它才有确定性。② 新增 `precheck.py`（纯标准库，随包分发），在动 `/etc`、运行账号、systemd **之前**一次报齐：CPU 架构、glibc 版本、TUN 设备、init 系统、随包 Python 可执行性、Web 依赖/离线 wheel、**直接试跑包内 openvpn ELF**（glibc/架构不匹配当场发现，不等装完报 `GLIBC_2.xx not found`）、8800/1194 端口、磁盘、**系统时钟**、防火墙/SELinux。硬错误整批中止，警告只提示；时钟只做「RTC 与系统时间差 8 小时」「没开 NTP」这类诊断，**绝不自动改系统时间**（离线机器没有可信基准）。无 systemd 的系统会明确拦住并说明原因（本版不做 cron 降级）。③ 装完自动跑包内 `selftest.py` 并把结果打在「安装完成」里，交付即验收；它自己把数据目录指到临时目录并开 MOCK，不碰生产 `data/`。重装场景端口占用只警告（后面会 restart），首装才硬检查。新增 14 条回归断言（含「安装包不再引用系统 python3」「预检必须在写 sudoers 之前」「precheck.collect() 在 Windows 上也能安全执行」）。
- Web **1.3.28**：**堵住两条「改配置 / 恢复备份就能拿到 root」的注入路径**（安全审查发现）。① `tls_min` 是 `validate_cfg` 的盲区——绕过前端直接打 `/api/settings`，一个带换行的字符串被原样写进以 root 运行的 `server.conf`（`tls-version-min` 行），即可注入 `up`/`plugin` 等任意指令，`settings_save` 随即重启 OpenVPN = root 命令执行；现加白名单（只认 1.2 / 1.3），渲染前再 fail-closed 拦一次。② 全备份恢复会整库覆盖 meta 并重新渲染配置，而 manifest 哈希只证明「包自洽」不证明「包没被改过」——改库重算哈希就能借「恢复」把脏值送进 root 执行的 `rules.sh`（`dns`/`ntp` 未加引号直接拼进 iptables 行）；现在恢复前先复核包内配置，不合法**整包拒绝**（在任何生产文件被覆盖之前）。同一次加固把渲染面全部收口：`rules.sh` 里网段取规范化 IPv4Network、账号 IP/资产网段/端口/协议渲染时再校验（脏值宁可让 apply-rules 失败也不生成脚本）、ccd 生成前校验账号固定 IP；带副作用的 `GET /api/logs?kind=deny` 与 `GET /api/selfcheck` 纳入 CSRF（防跨站 `<img>` 白嫖触发 root worker）。新增 13 条回归断言（含「篡改 meta + 重算哈希 → 恢复必须拒绝」）。
- Web **1.3.27**：**堵住一个能绕过安装码的提权洞**。1.3.23 给「上传即 root」加了安装码闸门，但闸门判断写成了 `if act in GATED and not MOCK`，而 `install.sh` 的 sudoers 又用 `env_keep` 保留了 `OVPN_WEB_MOCK`——任何能调面板 API 的人都能在自己进程里设这个变量、经 sudo 传进 worker，跳过安装码把任意 ELF 装成 root 服务（测试机实测确认：无码时 `replace-binary` 被拦，带 `MOCK=1` 则直接进动作）。现在闸门改看**专用自检开关 `OVPN_WEB_SELFTEST`**（只有直接跑 worker.py 的自检进程才有；sudoers 加 `env_reset` 且只保留 `OVPN_WEB_ROOT`，该开关经 sudo 必被清空），`OVPN_WEB_MOCK` 退回到只管「是否碰真实系统」的职责，并加了 3 条回归断言钉死。顺带修：界面上「24 小时有效」文案与实际 15 分钟/2 小时不符；`install.sh` 探活只靠 curl（最小化系统没有 curl 会误报 Web 没起来，改成优先用包内 python 的 urllib，curl/wget 兜底）。
- Web **1.3.26**：**Windows 上也能打包**。之前只能在 Linux 构建机跑 `build-offline.sh`——它要用包内的 Linux `python` 执行 `pip install`，Windows 上根本跑不了。新增 `build/build-offline.py`：只做「分层 → 平铺」的搬运，不执行任何包内程序，Windows 的 Git Bash 里一条 `python build/build-offline.py` 就出包。代价是运行时的 `site-packages` 是干净的，所以 `install.sh` 第 2 步改成「缺依赖就从 `vendor/wheels` 离线补」（先 `pip --no-index --find-links`，pip 不可用就直接把 wheel 当 zip 解开）。同时把三个 Windows 坑堵在打包环节：软链存根在 tar 里写成真符号链接（实测 745 个）、权限按路径规则写死（仓库里 `bin/openvpn` 记的是 644）、`os.symlink` 失败时回退存根。实测：Windows 打出 134.6MB 包，Linux 上解包 `python3 -V` 正常、依赖补装成功、包内自检 394 项全过。
- Web **1.3.25**：**编译工具不再是拦路虎**。上一版让「缺二进制就地编译」成立，但最小化系统常见地没装 gcc（这台测试机就是：make 有、gcc/perl/pkg-config 全无），真到要编的时候还是卡住。现在 `prepare-openvpn.sh` 在缺工具时会先按本机包管理器（yum / dnf / apt-get）自动补装，补不上才报错，并给出对应发行版的准确包名（CentOS 7 的 pkg-config 包叫 `pkgconfig`，容易踩）。`install.sh` 新增 `--with-build-tools`：装完顺手把工具链备好，以后在「升级」页上传源码就能直接编；不加这个参数则完全不碰包管理器（生产机不该为了用不到的能力多装 100MB）。离线机器上自动补装会失败，但只影响「本机编译」这一条路，不影响安装本身。
- Web **1.3.24**：**装一次，openvpn 和 web 都得在跑**。原来 `install.sh` 在缺 `bin/openvpn` 时直接 `exit 1`——Web 还没起来，人只能对着终端干瞪眼，文档里「`--skip-openvpn` 之后去向导第 2 步上传」这条路其实走不通。现在第 7 步先过 `prepare-openvpn.sh`：包内有二进制就用；没有就用 `vendor/src` 的源码**当场离线编译**（`VENDOR_ONLY=1`，有预编译静态库时只编 openvpn 本体）；实在没有编译环境就明确报错，并说清三条补救路径。二进制齐了才调 `setup.py`；它失败也照样把 Web 起起来（人能登录看日志），最后以 `exit 3` 结束——**不拿非 0 退出码糊弄自动化**。打包脚本同步把 `vendor/src`（18MB 源码）一起带进包，否则「就地编译」是句空话。
- Web **1.3.23**：给「上传即 root」加一道**人闸**。原来三个动作会把外部代码装进系统——替换 openvpn 二进制、本机编译源码、更新 Web 自身——装上去下一次启动就是 root 身份，而 Web 侧的 `check_binary` 只认 `--version` 输出，编个版本号就能过，等于**能登录面板的人口令一泄露就等于 root**。现在这三类动作执行前，worker 要验一个**一次性安装码**：只能在本机 `sudo /opt/openvpn-web/install-code.sh` 生成（8 位、默认 15 分钟、一码一次、填错 5 次作废、令牌 600 root:root 存在 `/etc/openvpn-web/`），Web 进程既生成不了也读不到。装完系统会把首装用的安装码（2 小时）直接印在「安装完成」里，向导第 2 步照填即可。配套：`bin/` 整体改为 `root:root 755`，Web 用户不再能写「下次会以 root 执行」的程序，向导上传二进制也改走 worker（顺带修掉直接 `copy2` 覆盖运行中二进制的 `ETXTBSY`、且旧二进制没快照的问题）。**恢复备份不再覆盖 `bin/openvpn`**（否则「上传一个包再点恢复」就能绕开闸门，只有命令行 `app.py --restore-full` 还保留整包恢复）；升级失败的回滚改由 worker 的 `rollback-binary` 完成——只认 `replace-binary` 当时钉下的 sha256、30 分钟内有效，Web 往 `data/snapshots` 里塞别的 ELF 也顶不上来，所以这个动作不需要安装码也开不出新口子
- Web **1.3.22**：多线程下的**一致性收口**。换 waitress 后请求真并发了，原来靠「同时只有一个请求」挡住的几处立刻暴露：①`iptables` 流水线是「先 FLUSH 再逐条 ADD」，两条交叉会出现「A 刚清空、B 已加完」——ACL 链空着等于全员放行，得等 5 分钟定时重放才恢复；②审计上下文是全局 dict，下一个请求会把它覆盖，A 的操作记成 B 干的；③两个请求同时建号会抢同一个空闲地址，一个必然撞 UNIQUE；④同一秒内两次备份包同名互相覆盖；⑤升级/恢复在进行时，另一个请求还能继续改配置。对策：`core.SYS_LOCK` 把「写配置 + 重建 ccd + 下发规则」串成一段，`worker.py` 加 `flock` 跨进程互斥，生成的 `rules.sh` 自己也 `flock` 并把 iptables 全部改成 `-w 5`；审计上下文改成 thread-local；建号 / 台账导入 / 全量重建 / 备份恢复都在锁内；备份包同名自动加 `-2/-3`；长任务运行期间写接口返回 409 并提示「等它完成」
- Web **1.3.21**：HTTP 服务器换成 **waitress**（单进程 8 线程，纯 Python、离线 wheel 随包安装）——以前是 Werkzeug 开发服务器且 `threaded=False`，一个慢请求（探测公网 IP、读大日志、导出 Excel）会把所有人的页面一起卡住；现在并发请求各占一个线程，SQLite 仍是单写入者，靠 WAL + `busy_timeout` 排队。为此补了三处并发安全：登录失败计数表加锁、公网 IP 探测「只让一个人真去打外网，其余等结果」、长任务 tid 改为拿到写锁后再绑定（否则后来启动的任务会把正在跑的任务 id 顶掉，结果写进别人的任务日志）。`OVPN_WEB_THREADS` 调线程数，`OVPN_WEB_SERVER=werkzeug` 一键退回；版本页显示当前实际服务器
- Web **1.3.20**：长期运行不膨胀——服务器日志改「从文件末尾倒读」（`openvpn.log` 是 `log-append` 无轮转，以前每次开日志页都整文件读入，跑久了会卡）；`openvpn.log` 超过 20MB 启动时转存一份 `.1`；`data/tasks` 里每个任务一对文件，自动清 7 天前的（每小时最多扫一次）；台账自动备份只留最近 200 份（文件与记录一起清，且同一秒内多次变更不再互相覆盖文件名；台账备份与备份点列表各自取最近 100 条，互不挤占）；公网 IP 探测结果缓存 10 分钟（含「探不到」），手动识别按钮强制重探，超时 2.5s→1.5s，没外网时打开向导/设置页不再干等；OpenVPN 版本号按二进制 mtime 缓存，首页加载不再每次 fork 一次二进制
- Web **1.3.19**：撤掉「改 VPN 网段自动迁移账号固定 IP」——网段变更只改服务端，账号地址恢复由管理员手工改（页面会红字列出还停在旧网段的账号，恢复备份后同样提示），不再由系统批量改写台账与 ccd；顺带修掉「只改网段却提示必须全员重发 ovpn」的误报（VPN 网段不进配置指纹），动态池越界与导入旧网段台账时的报错也给出手工修改的方向
- Web **1.3.17**：数据文件权限收紧——`openvpn-web.db` 与 `-wal/-shm`、`web.secret`、`admin_password.txt` 一律 600，`data/backups` 700，`data/` 750（库里存的是明文密码，同机普通用户不能读）；安装脚本同步设置
- Web **1.3.16**：自动备份改成「和导出一模一样的 Excel（含密码）」，下载后可直接用「认证账号 → 导入」恢复，机器上的【恢复】保留；上线前安全加固（安全响应头 + CSP、500 不再回异常栈、删除 `/api/admin-reset`、未登录不下发管理账户名、Excel 公式注入防护、自助页 XSS 修复、可开 HTTPS 模式）
- Web **1.3.15**：「对外地址」留空时自动识别本机公网 IP 填入（可手动改）；本机监听端口与对外映射端口并到同一行；恢复卡片只做本地备份包恢复，机器上的备份点改回列表行内【恢复】；台账备份（账号）移到备份恢复页，并新增下载与恢复
- Web **1.3.14**：区分「本机监听端口」与「对外映射端口」，内网端口映射场景下客户端配置自动使用公网 IP/对外端口；移除 Web 界面「重新生成账户与密码」入口（改用服务器脚本）；台账备份展示移到日志审计；安全页排版调整；备份与恢复拆成两张卡片
- Web **1.3.13**：用户组输入方式与资产组统一——新建/编辑成员都改为「一行一个用户名」的多行文本框，去掉标签芯片和单独的批量弹窗；内容不同但操作规则一致
- Web **1.3.12**：资产条目（新建组 / 管理条目 / 编辑条目）统一为「目标 [协议] [端口]」一套规则；`any` 带端口按 TCP+UDP 生成规则，`icmp` 端口自动为空；用户组批量粘贴改为一行一个账号、不再误拆中文账号，并修正回程路由/白名单批量提示
- Web **1.3.11**：新增页面版本自校验——浏览器缓存了旧界面时自动检测并强制刷新（启动 + 前台每 60 秒比对，只刷一次防死循环），不用再手动 `Ctrl+F5`
- Web **1.3.10**：编译环境检查前置到安装尾声与服务启动时，结果连同时间落库；版本页展示检查时间与中性文案、新增【重新检测】按钮
- Web **1.3.9**：密码框默认以 `*` 遮盖，【显示 / 隐藏】切换只改可见性、右侧按钮位置固定；账号列表移除【显示密码列】开关，密码只在【编辑】里查看
- Web **1.3.8**：账号密码改为明文可查可改（列表不显示密码列；独立【改密】按钮并入【编辑】）；Excel 导出带明文密码、导入密码列必填留空即拒绝；新增账号有效期，到期自动停用并断开
- Web **1.3.7**：登录页不再预填 / 提示默认 `admin` 账户与口令，空账户不再兜底成 admin；CSRF token 失效时前端自动刷新会话并重试一次，不再弹「请刷新页面」
- Web **1.3.6**：去掉账号创建 / 改密保存后的一次性密码弹窗；生成密码改为在输入框明文显示并可直接复制，留空时首点确定先生成、复制后再点确定才保存
- Web **1.3.5**：新装默认账户 `admin` / 默认密码 `admin`，首次登录强制改密；改密前后端只放行改密、会话与退出，改完凭据文件不再留明文
- OpenVPN **2.6.23**（2026-09-23，修 7 个安全问题，含 CVE-2026-84732 未认证远程 DoS、CVE-2026-84471 double-free），2.6 安全支持到 2028-08-31
- OpenVPN **2.7.9**（2026-11-05，当前 Full Stable）
- 2026 年多个 CVE 影响 2.6.0~2.6.20 / 2.6.22，所以自动编译的分支闸门默认锁在 2.6.23 及以上（升级时的准入规则，不在版本页做「安全/不安全」的静态展示）
