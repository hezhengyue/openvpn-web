# OpenVPN Web 一体化管理系统

一个目录、一个 systemd 服务、单进程。把「装 OpenVPN 服务端、账号密码、固定 IP、按人限制能访问哪些服务器、可开关的无 NAT 溯源、客户端互访 P2P、Excel 台账、版本与更新」全部收进浏览器，机房断网也能装。

> 交付定稿：x86_64 离线静态二进制、Flask 自带 Web server（不上 gunicorn / Nginx / 数据库服务）、SQLite、账号密码认证、全员一个 `company.ovpn`、ACL 默认拒绝 + 观察模式起步、**网络模式默认 NAT（无 NAT 溯源默认关闭、可一键开启）**、客户端互访走内核 FORWARD、手动上传更新。

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

---

## 3. 目录与文件

### 3.1 仓库（开发机）

源码、构建、测试分开，根目录只留分类目录和一份 README：

```
openvpn-web/
├── src/                      运行时代码（打包后会被平铺到安装目录根）
│   ├── app.py                Flask 主程序（路由 / 登录 / CSRF / 长任务调度）
│   ├── core.py               核心逻辑（DB / 配置渲染 / ACL / 证书 / Excel / 指纹 / 快照）
│   ├── worker.py             特权执行器，sudo 白名单调用，动作白名单
│   ├── setup.py              一键配置器（签 CA / 写配置 / 下发规则 / 起 openvpn / 建账号）
│   ├── auth-verify.py        OpenVPN 认证脚本（fail-closed、失败锁定）
│   ├── index.html            单文件前端，原生 HTML/CSS/JS，不依赖 CDN
│   ├── install.sh            一键安装：落文件 + 权限 + sudoers + systemd + 调 setup.py + 起 Web
│   └── selftest.py           全链路自检（MOCK 模式，167 项）
├── bin/                      预编译静态二进制
│   ├── openvpn               OpenVPN 2.6.23，第三方库全静态，只依赖 glibc（最低 GLIBC_2.17）
│   └── openssl               静态 openssl 3.0.16 命令行（签证书用，最小化系统没有 openssl 也能装）
├── runtime/                  离线 Python 3.12（glibc 2.17 基线，含 Flask + openpyxl）
├── vendor/                   三方件快照（离线构建用）
│   ├── src/                  源码包（openssl / lzo / lz4 / libcap-ng / openvpn）
│   ├── static/prefix.tar.gz  预编译静态库，目标机「上传源码自编译」靠它 14 秒出结果
│   ├── wheels/               flask / openpyxl 及其依赖的 cp312 wheel
│   ├── SHA256SUMS            逐包校验值
│   └── fetch.sh              有网机器上跑一次补齐 vendor/
├── build/                    构建与打包（不进安装包）
│   ├── build-offline.sh      生成完整离线目录 → dist/
│   ├── build-static-openvpn.sh  静态编译 openvpn（也随包发行，供目标机自编译）
│   ├── Dockerfile.build      glibc 2.17 可复现构建环境
│   └── fixtar.py             修包内可执行位（Windows 打包必然丢 exec 位）
├── tests/                    前端测试
│   ├── smoke.js              jsdom 逐页切换抓运行时错误
│   ├── shots.js              无头 Edge 逐页截图（改版式时用肉眼核对）
│   └── package.json          jsdom 依赖
├── dist/                     产物：openvpn-web-x86_64.tar.gz
└── README.md
```

**为什么源码分层、包内平铺**：`install.sh` 里写死了 `$DEST/app.py` / `$DEST/worker.py`，`worker.py` 又按 `ROOT/build-static-openvpn.sh` 找编译脚本，所以安装目录必须平铺。分层只存在于仓库里，`build/build-offline.sh` 负责把 `src/*` 拍平到包根。

### 3.2 装机后（/opt/openvpn-web）

```
/opt/openvpn-web/
├── python/           离线解释器 + Flask + openpyxl（glibc 2.17 即可）
├── bin/openvpn       静态 OpenVPN（第三方库全静态）
├── bin/openssl       静态 openssl 命令行
├── app.py  core.py  worker.py  setup.py  auth-verify.py  index.html
├── install.sh        一键安装
├── build-static-openvpn.sh  + vendor/static/   「上传源码自动编译」用
├── README.md
└── data/             SQLite / 证书 / ccd / ovpn / 日志 / 快照 / 备份
```

进程与权限：

- systemd 只拉 `ovpnweb` 用户的一个 Web 进程：`app.run(threaded=False, use_reloader=False, debug=False)`。单线程 + 全局写锁，任何时刻只有一个执行者在改系统，杜绝规则重建竞态
- 长任务（安装、更新、重启）走独立后台线程 + `task_id` 轮询 + 实时日志；HTTP 请求本身不执行几十秒的系统动作
- Web 低权限，`sudoers` 只放行一个精确命令：`python3 worker.py <task_id>`，命令行不传自由参数，动作与参数从 `data/tasks/<id>.json` 读并做白名单
- `data/`、`bin/` 归 `ovpnweb` 可写；`app.py`/`core.py`/`worker.py`/`index.html` 归 root 只读
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
| Flask / openpyxl | — | `vendor/wheels/*.whl`（`pip --no-index`） | PyPI |
| 静态 OpenVPN | `bin/openvpn` 已存在则直接用 | `vendor/src/*.tar.gz`（带 sha256 校验） | 各上游镜像按序回退，下到后**回填 vendor/src** |

所以日常路径是：**仓库自带 `python/` + `vendor/` 里 18M 源码和 wheel → 构建机连网线都不用插**。

**而且现在连构建都不是必须的**：仓库已带编好的 `bin/openvpn`（2.6.23 静态）和装好依赖的 `python/`，
`./build/build-offline.sh` 检测到 `bin/openvpn` 会直接复用、跳过编译（想重编：`FORCE_BUILD_OPENVPN=1 ./build/build-offline.sh`），
只做「拷文件 + 离线装 wheel + 打包」，几十秒出 `dist/openvpn-web-x86_64.tar.gz`。

```bash
./build/build-offline.sh              # 本地优先；缺什么才去下什么
./build/build-offline.sh --skip-openvpn   # 只打 Web 运行时，openvpn 之后在向导第 2 步上传
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

1. 准备 python 运行时（优先复用仓库自带 `python/`），并用 `vendor/wheels` 离线装 `flask==3.1.3`、`openpyxl==3.1.5`
2. 在 manylinux2014（glibc 2.17 ABI）里编译 **OpenVPN 2.6.23 + OpenSSL 3 + LZO/LZ4/libcap-ng 全部静态链接**（`--disable-dco`、关掉 PAM / down-root / systemd / pkcs11 全部插件，我们只用脚本认证）
3. 打包并校验 `ldd bin/openvpn` 只出现 glibc 一家

**上游链接是会坏的**（2026-11-18 实测：OpenVPN 官方 `swupdate.openvpn.org` 连不上，只能走 GitHub tag 归档，而它不带 `configure`、要先 `autoreconf`）。`vendor/` 就是为这件事存在的——见 `vendor/README.md`。镜像打包后放共享盘，用 `VENDOR=/data/vendor ./build/build-offline.sh` 还能让多个项目共用一份。

**只想先试 Web**：`./build/build-offline.sh --skip-openvpn`，之后在浏览器「安装向导」第 2 步直接上传静态二进制即可，不用碰命令行。

> 说明：交付包里**已经含编好的静态 openvpn、静态 openssl 和装好依赖的离线 python**，
> 所以「构建」只是可选项：想换版本、换 ABI 时才需要跑脚本。

---

## 5. 部署：一条命令装完（机房目标机，root 一次）

包里该带的全带了：离线 python（glibc 2.17）、静态 openvpn、静态 openssl、CA 生成所需的一切。
目标机**不联网、不装包、不编译**。

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
```

`install.sh` 做 8 件事（全程幂等，重跑不会签第二套 CA）：

| 步骤 | 内容 |
|---|---|
| 1 | 释放程序文件（`tar` 管道，不依赖 rsync；`data/` 不覆盖） |
| 2 | 选解释器：包内 `python/` 优先，其次系统 python3 |
| 3 | 建 `ovpnweb` 用户、目录权限；**修复被 Windows/网盘拍平的 python 软链与可执行位** |
| 4 | sudoers 白名单（只放行 `python3 worker.py *`） |
| 5 | systemd unit（保守语法，兼容 CentOS 7 的 systemd 219） |
| 6 | 初始管理密码（600 权限，只打印一次） |
| 7 | 调 `setup.py`：签 CA + 服务端证书 + tls-crypt → `server.conf` / `ccd` / `company.ovpn` / ACL 脚本 → 开 IPv4 转发 → 下发 NAT+ACL → mask 发行版 unit → 装规则重放 timer → 启动 OpenVPN →（可选）建账号 → 开 firewalld/ufw 端口 |
| 8 | 启动 Web 并自检 `GET /api/session` |

浏览器打开 `http://<服务器IP>:8800`，密码见屏幕输出（也存于 `data/admin_password.txt`，600），
**登录后立刻在「管理密码与安全」改掉**。客户端配置是 `data/company.ovpn`（全员共用，含 CA 与 tls-crypt）。

`setup.py` 也可以单独重跑（改对外地址、换网段、补账号）：`./setup.py --help`。

### 卸载 / 删干净重装

```bash
./install.sh --purge        # 在安装目录里执行（它知道自己装在哪）
```

`--purge` 按这个顺序拆：① 把 `data/` 打包到 `/root/openvpn-web-data-<时间>.tar.gz`（**先备走再删**，失败就中止）→ ② 停并删 `openvpn-web` 与 `openvpn-web-replay.*` 单元 → ③ 结束 openvpn 进程 → ④ **先删引用再删链**，拆掉 `OVPN_ACL` / `OVPNWEB_NAT`（引用规则里带 VPN 网段，网段可能被改过，所以是照 `iptables -S` 实际输出逐条 `-D`，不猜参数）→ ⑤ 撤 firewalld 放行 → ⑥ 删安装目录、sudoers、sysctl 配置、`ovpnweb` 账号 → ⑦ 自查残留（unit / 两条链 / 目录）并报告「干净」。

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
3. **填参数**：对外地址/域名、端口、隧道协议（默认 TCP）、VPN 网段、附加推送路由、DNS/NTP 例外、tun-mtu（默认 1400）、加密套件，以及**网络模式**（默认 NAT 开箱可用 / 无 NAT 溯源需先配回程路由）。推送路由是**标签式列表**，一条一个标签，写 20 条也不会糊成一行。最低 TLS（下拉选 1.2 / 1.3）与压缩收在「高级选项」折叠区里（默认收起）—— 这两项默认值适用于绝大多数部署，摆在外面只会让人纠结

> **VPN 网段会自动下发**：`push "route <VPN 网段>"` 由系统自己加（客户端要访问 VPN 网关自身、要和别的客户端互访都靠它），不需要在「附加推送路由」里再写一遍。写了也不会有害——与它重叠的目标会先做减法拆成不重叠的子网再推，只是排序上会并成一条。这条自动路由不计入配置指纹（它在 `ifconfig` 里本来就有），所以不会因为它要求全员重发 ovpn。
4. **执行**：签 CA + 服务端证书 + tls-crypt → 写 `server.conf` / `ccd` / `company.ovpn` / ACL 脚本 → mask 发行版 unit → 装规则重放 timer → 开启 IPv4 转发 → 下发 NAT 与 ACL 规则 → 启动 OpenVPN → 自检。点按钮并在弹窗确认，实时日志与进度条
5. **交付**：下载 `company.ovpn`；若选了无 NAT 溯源，复制回程路由命令给网管（默认 NAT 模式不需要）

每步状态落库，刷新/断电重跑会跳过已完成步骤，不会签出第二套 CA。任何一步失败都会回滚快照。

**ACL 默认「观察模式」起步**：只记录「谁本该被拦」，不真拦。等授权配齐了，在「资产与授权」页点按钮确认才切到默认拒绝，避免一键装完把所有业务访问都挡掉。

---

## 7. 日常操作

> **全按钮化**：所有高危 / 不可逆操作一律是「点按钮 → 弹窗说明 → 再点一次确认按钮」，**没有任何需要手动输入确认词的地方**（早期版本要输 `INSTALL` / `yes` / `ENFORCE` / `NO-NAT` / `REINIT` / `DEFAULT` / `RESTORE`，已全部去掉）。防误触靠弹窗与醒目的红色确认按钮，不靠打字；登录 + CSRF + 操作审计仍在。

统一收口后的按钮清单：

| 操作 | 交互 |
|---|---|
| 安装向导执行、Excel 全量同步、启用强制模式、切回观察模式、开启/关闭无 NAT 溯源、恢复默认、重签 CA、恢复快照、更新二进制、保存设置并重启 | 点按钮 → 红色确认弹窗 → 点确认 |
| 重启服务、踢下线、删除账号 / 资产组 / 授权 / 客户端互访 | 点按钮 → 确认弹窗 → 点确认（原先删除类操作没有确认，已补上） |
| 保存账号、添加资产 / 授权 / 互访、改密、改管理密码 | 直接点按钮提交（无二次确认，都是可逆或明确的数据录入） |

顺手把几处「本来要手输」也改成按钮：

- 添加 / 编辑账号的**固定 IP**：点【自动分配】/【换一个】，直接填入下一个可用地址（新增 `GET /api/users/next-ip`）
- **初始密码 / 改密**：点【生成随机密码】自动填入，不用自己想
- **临时授权到期时间**：点【2 小时 / 1 天 / 7 天 / 30 天 / 清除】快捷填入，不用手敲 `YYYY-MM-DD HH:MM`
- 浏览器原生的 `confirm()` 弹窗全部换成统一的自定义弹窗，移动端显示一致

仍然需要手打字的只有**数据本身**：登录密码、管理密码、账号名、备注、网段/端口/域名、可选的 SHA256 清单、搜索关键词、文件选择。这些是内容不是确认，无法用按钮代替。

### 账号

「认证账号」页：添加 / 编辑（**账号名**、姓名、部门、固定 IP、备注）/ 改密 / 停用 / 删除 / 导入导出 Excel（只有 Excel，不再有 CSV 两套入口）。列表支持按账号、姓名、部门、IP 搜索并分页。

**踢下线在「在线用户」页**（台账页不做，免得两处功能重复）。这里有个容易误解的点：

> 踢下线只是断掉当前这一条连接，**客户端会自动重连**（OpenVPN GUI / Connect 默认就是断线重连），几秒后同一个账号又出现在列表里——这不是没踢掉。要让它连不上，把账号改成**停用**（`ccd-exclusive` + 认证拒绝，重连也进不来）。
>
> 页面在踢完 3 秒后会再查一次在线列表：回来了就明说「已自动重连」，没回来才提示「已下线」。

- **账号名可以直接用中文**（如 `张三`），也支持工号/拼音；不允许空格与 `/`，1~32 位。中文账号的 `ccd/张三` 文件名跟着走，实测 OpenVPN 2.6.23 + TCP 下正常认证并拿到固定 IP（老客户端对中文名的兼容性看它自己的输入框，拿不准就用拼音）
- 固定 IP 只能落在可用段（默认 `10.8.0.2~.254`，开启动态池后池子里的地址会自动排除），重复即整批拒绝
- **账号名可以改**（编辑弹窗第一栏）：工号写错、入职改名都能纠正。改名会把 `users` 主键、`ccd/<用户名>`、用户组成员、资产授权、客户端互访授权一起搬到新名，**不用重新配授权**；旧名的 ccd 会被删掉，改名后旧账号立刻不能拨入。

**Excel 能改什么：** 姓名、部门、固定 IP、密码、状态、备注都能通过「Excel 全量同步」批量改。但表里的「用户名」是身份本身——把某行的用户名改掉，等于删掉旧号、新建一个号（新号密码会重生成），已配的授权不会跟过去。只想给某人换个账号名，用列表里的【编辑】，别在 Excel 里改。

- 密码列导出时永远为空，服务器只存 PBKDF2 哈希；新密码写到 600 权限的 `初始密码-<时间>.txt`。**页面上生成/重置的密码会明文显示在输入框并在保存后弹窗再显示一次、可一键复制**，不会出现「生成了却不知道是什么」
- 姓名 / 部门是纯台账字段（不参与认证），Excel 列顺序：用户名 / 姓名 / 部门 / 密码 / VPN固定IP / 状态 / 备注；缺列按空处理，老台账照样能导
- 改 IP 会**全量重建 ccd + iptables 链 + 踢下线**：防止后来的人分到旧 IP、继承上一个人的服务器权限
- 停用账号不生成 ccd，`ccd-exclusive` 直接拒绝；同账号只允许一处在线（不开 `duplicate-cn`）

### Excel 全量同步（唯一表格格式）

导出 → 编辑 → 预览变更清单 → 点【确认全量同步】按钮提交。每次自动备份到 `data/backups/users-<时间>.tsv`，误删可直接把该备份当台账重新导入回滚。预览令牌 5 分钟过期。

**密码列的三条规则**（很重要，避免拿一份没填密码的台账把所有人密码冲掉）：

| 表里这一行 | 结果 |
|---|---|
| 老账号 + 密码列**有值** | 覆盖原密码 |
| 老账号 + 密码列**留空** | **保留原密码不动**（预览里会写「密码保持不变」） |
| 新账号 + 密码列留空 | 生成随机初始密码，导入完弹窗**明文列出**并支持一键复制 / 下载 txt |

如果老账号的密码已经没人知道了（比如当年是别人建的），勾选导入面板里的 **「密码列为空时也重新生成」** 再导一次，就能一次性拿到这批账号的新密码清单。

### 自助改密（用户自己动手，不用找管理员）

用户拨上 VPN 后打开 `http://<服务器>:8800/self`：**系统按来源 IP 直接认出他是谁**（来源地址就是他台账里的固定 IP），页面上只有三个框——当前密码、新密码、重复新密码，**没有任何要填 IP 的地方**。

- 没拨 VPN 打不开也改不了：页面会明确提示「请先连上 VPN」。这是故意的——IP 由服务端从连接来源取，前端无法指定别人的账号，也就没法拿它试别人的密码

- 失败不区分「IP 不存在 / 账号停用 / 密码错」，防止被拿来枚举台账
- 来源 IP 连续错 5 次锁 10 分钟
- 改完只影响下次拨号，`company.ovpn` 不用重下
- 页面模板内联在 `app.py` 的 `SELF_PAGE` 里（不再依赖独立的 `self.html`），这样 Web 热更新也能覆盖到它

### 操作日志与操作者（登录没有用户名，但日志不能只写 admin）

后台登录只有一个管理密码、没有用户名，所以审计日志里的「操作者」取一个可配置名称：**管理密码与安全 → 操作者名称**（默认「管理员」）。改完立刻生效（当前会话一起换），顶栏也会显示「名称 · 你的来源 IP」。

「日志审计 → Web 操作审计」每条都带 **操作者 IP**（数据库 `audit.ip`，老库升级时自动加列）。三条链路各有来源：

| 日志 | 操作者 | IP 从哪来 |
|---|---|---|
| 后台里的操作（改配置、加账号、改授权……） | 配置的操作者名称 | 请求来源地址（只信任本机反代的 `X-Forwarded-For`） |
| 命令行 / 定时任务（`app.py --allow`、worker 动作） | `root` 执行的记为空，账号变更记 `web` | 无（显示 `—`），这类操作本来就没有 HTTP 来源 |
| VPN 拨号认证 | 账号名 | 客户端来源公网 IP（`untrusted_ip`） |

### 资产与授权（ACL）

三层结构，解决「人一多、IP 一多就没法看」：

1. **资产组**：一个组里放多条目标。可以一次贴 3 个 IP（一行一条，`目标 [协议] [端口]`），也可以放整个网段。单个 IP 自动补 `/32`。
2. **用户组**：把 3 个账号勾成一组，加减成员只改这里。
3. **授权**：「账号 或 用户组」→「资产组」。一次把一组 IP 授给一组人，不用一条条点。

- 同一个「目标 + 协议 + 端口」**只允许存在一条**：重复添加会被拒绝，并告诉你在哪个组里已经有了。端口顺序无关（`80,22` 与 `22,80` 是同一组）。
- 授权可叠加（在组里 + 单独授权，取并集）；临时授权填 `YYYY-MM-DD HH:MM`，到期自动失效。
- 三个列表都有搜索 + 分页 + 表头吸顶，几百条也不会把页面撑爆；每条授权会显示「展开成多少条规则」。

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

**动态地址池（`ifconfig-pool`）默认不写。** 它本来是给「没在台账里的临时连接」兜底用的 DHCP 式分配，本系统的固定 IP 全由 `ccd/ifconfig-push` 下发，用不上。真要接临时用户，在「服务端设置 → 动态地址池」里勾上并手动填起止地址即可，界面会实时算出「这个池子占掉多少地址、还剩多少固定 IP 可分配」。`/24` 网段关掉池子时能放 253 个固定 IP（`.2~.254`）。


两个高危按钮都做了**点按钮二次确认**（红色确认框）：重新初始化 CA、恢复默认，无需输入任何确认词。

---

## 8. 版本与更新（手动，不自动）

「版本与更新」页显示：当前版本、分支 EOL、安全线、配置指纹、最近更新。升级只有一条路径：**上传官方源码包，本机自动编译**。

传 `openvpn-2.6.x.tar.gz`（GitHub / 官方 releases 的源码发布包），本机编译并替换服务端二进制，**全程不联网**：

- 依赖库复用 `vendor/static/prefix.tar.gz` 里预编译好的 `libssl.a / libcrypto.a / liblzo2.a / liblz4.a / libcap-ng.a`，只编 openvpn 本体，实测 14 秒出结果
- 流程：**校验 → 快照 → 编译 → 原子替换 → 重启 → 自检 → 失败自动回滚**
- 校验：`ldd` 只允许 glibc、版本安全线、分支闸门（默认 2.6.x → 2.6.y，跨大版本需勾选）
- 页面会**提前**显示编译环境是否齐备（gcc / make / pkg-config），不会让你传完包才失败
- 本机编译产物只保证不低于本机 glibc；要发给更老的机器（如 CentOS 7），在构建机用 `build/build-static-openvpn.sh` 编好，直接替换安装目录的 `bin/openvpn` 后重启服务（`POST /api/update/binary` 也能走这条，界面上不暴露）

Web 程序自身不提供「上传更新包」入口（日常更新就是覆盖文件 / 重跑 `install.sh`）。后端 `/api/update/web` 接口仍保留，仅作为「改错代码把页面搞崩」时的远程救场通道：进程还活着就能 POST 一个 zip 换回 `app.py` / `core.py` / `index.html`（**不允许替换 root 执行的 `worker.py`**），`data/` 与 `bin/` 不受影响。

### 快照与回滚（完整备份，不是只备一部分）

改配置、升级二进制、导入台账、跑安装向导之前都会自动打快照，也可以点【立即打一份快照】手工打。**一份快照 = 当时的完整业务状态**：

| 包含 | 不包含（回滚时保留现状，不还原） |
|---|---|
| `server.conf`、`company.ovpn` | 连接日志、认证日志正文 |
| `ca.crt` / `ca.key` / `server.crt` / `server.key` / `ta.key` | 在线连接（回滚会重启服务） |
| 账号、资产组、用户组、授权、审计之外的业务库 | 审计日志与任务进度（保留，否则恢复后审计会缺一段） |
| `ccd/`（每人的固定 IP） | 历史快照本身 |
| 当前 `openvpn` 二进制 | |

所以恢复之后是「回到那一刻」，客户端不用重发 ovpn。数据库走 SQLite 在线备份（不是复制 `.db` 文件），避免和后台任务抢锁。

### 服务端升级后，客户端要不要更新？

不用。ovpn 里装的是连接凭据（CA、tls-crypt、remote、proto、auth-user-pass），不是版本号。2.5 起数据通道加密改成 `data-ciphers` 协商，客户端上报 `IV_CIPHERS`、服务端挑双方都支持的算法，新老版本互通。真正需要重新发 ovpn 的只有三类：改连接参数（地址/端口/协议/MTU）、换 CA / ta.key / 服务端证书主题、改加密套件到某些客户端不支持的算法——而且仍然是**全员一份**。

> 拓扑（`topology subnet`）不在此列：服务端每次连接都会用 `push "topology subnet"` 下发，老 ovpn 即使没写这一行也会被纠正，不用重新分发。

唯一要升级客户端「软件」的情况是它本身太老：**OpenVPN 2.3 及更早**不会协商 `data-ciphers`、也不支持 `tls-crypt`，连不上 2.6 服务端。定个底线：客户端 ≥ 2.4，推荐 2.6 / OpenVPN Connect 3.x。这是一次性要求，跟以后打多少次补丁无关。

---

## 9. 安全边界与纪律

- 纯 HTTP 内网页，**默认只放行本机网卡所在网段 + 127.0.0.1**（升级时自动从「所有私网」收紧）；VPN 拨号用户只放行自助改密页 `/self`，进不了后台。名单可在「管理密码与安全 → 管理端访问白名单」里改，保存时会校验「你当前 IP 还在名单里」，防止把自己关在门外；配错了 SSH 上去 `python3 app.py --allow 10.20.30.0/24`（`--allow-reset` 恢复本机网段）。登录密码 PBKDF2、签名 Cookie（HttpOnly / 8h）、CSRF、失败 5 次锁来源 IP 10 分钟、全操作审计（含操作者 IP）。**别直接暴露公网**，远程管理请走 HTTPS 反代或堡垒机
- `X-Forwarded-For` 只在「本机反代」时才采信（`remote_addr` 是 127.0.0.1），否则任何人加个头就能伪装成白名单地址

- 认证脚本任何异常一律 `exit 1`（fail-closed）；密码走 `/dev/shm` 临时文件，不进环境变量/命令行；同一「账号+来源IP」10 分钟失败 5 次锁定
- 溯源到的是**账号**不是设备：`company.ovpn` 全员共用，账号密码不要转借；一人一账号，别共享。**只有开启「无 NAT 溯源」后内网服务器日志才能看到账号固定 IP**；默认 NAT 模式下内网日志源 IP 是 VPN 服务器，只能靠认证日志查来源公网 IP
- 固定 IP 只能管理员改，不放权给客户端
- 别把 `company.ovpn` 之外的个人凭据塞进客户端配置；服务器配置里不要加 `redirect-gateway` / `duplicate-cn` / `client-to-client`。**别手工加 `client-to-client`**：客户端互访走内核 FORWARD 才能被 ACL 与日志覆盖
- NAT 由自建 `OVPNWEB_NAT` 链接管；别再执行 `firewall-cmd --add-masquerade` 或另加 NAT 规则，否则开启溯源后会有残留导致链路失效
- 上传的二进制会被执行 `--version` 读版本号，**只上传可信来源的静态二进制**

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
| 内网日志源 IP 全是 VPN 服务器 | 当前是默认 NAT 模式（正常）；想看账号固定 IP 就开启「无 NAT 溯源」。若已开启仍如此 → 有 NAT 残留，点「重建规则」，自检页会报 |
| 全员连不上 | 服务没起 / 端口被占 / `ccd-exclusive` 下账号没有 ccd / firewalld reload 冲了 FORWARD（timer 5 分钟内恢复） |
| 某账号连不上 | 状态是否停用、密码、是否被失败锁定、`ccd/<用户名>` 是否存在 |
| Windows 报 `local and remote VPN endpoints must exist within the same 255.255.255.252 subnet` | 客户端把 ccd 下发的 `10.8.0.x 255.255.255.0` 按点对点（net30）解析了，即拓扑没对上。服务端 `server.conf` 要有 `topology subnet` **和** `push "topology subnet"`；客户端 ovpn 也要有 `topology subnet`。缺 push 时 Linux 客户端能连、Windows 直接 fatal，所以别只拿 Linux 验 |
| 改了 IP 权限不对 | 看是否触发了全量重建；自检页对比「启用账号数 / ccd 文件数」 |
| ACL 没生效 | 是否还在观察模式；授权是否过期；目标是否被 LB/NAT 改了源 IP |
| 客户端之间互访不通 | 是否加了互访授权（默认全拒）、是否还在观察模式、双方账号是否启用；自检页看 IPv4 转发是否开启 |
| 时间串不起来 | 所有机器的 NTP |
| Web 打不开 | 先看是不是**白名单拦的**：页面会直接告诉你「来源 IP x.x.x.x 不在管理端白名单内」。SSH 上去 `python3 app.py --allow <你的网段>`，或 `--allow-reset` 恢复成「127.0.0.1 + 本机网卡网段」；再看 `journalctl -u openvpn-web -n 50`、`systemctl status openvpn-web` |
| 特权动作失败 | `journalctl` + 任务日志 `data/tasks/<id>.log`；确认 sudoers 里的解释器路径与 `core.PY` 一致 |

---

## 11. 验证情况与限制

- `python3 src/selftest.py`（或装机后在安装目录 `python3 selftest.py`）：**216 项全通过**（Windows 上少 3 条 POSIX 专属用例，213 项）——配置渲染、双网络模式（默认 NAT / 无 NAT 溯源）与切换不改指纹、自建 NAT 链、IPv4 转发前提、证书签发、账号校验（含中文账号 + 中文 ccd + 改名时授权/用户组跟着搬）、动态地址池开关与地址冲突、认证脚本（放行/拒绝/锁定/fail-closed）、Excel 全量同步、ACL 默认拒绝与改 IP 不残留、客户端互访授权与过期过滤、跨大版本闸门、安装向导长任务、全部 API（含首页禁用缓存、网络模式切换、P2P 增删、下一个可用 IP、容量、白名单保存与「排除自己」拒绝、操作者名称、审计带 IP、按钮式保存/恢复默认/重签 CA）、二进制更新、快照回滚
- 前端在 jsdom 里逐页真跑（`tests/smoke.js`），另有 `tests/shots.js` 用无头 Edge 截图肉眼核对版式
- **真实环境实测（2026-11-18，Anolis 8.10 / OpenVPN 2.6.23 / TCP）**：建中文账号 `张三` → 服务端生成 `ccd/张三` → 客户端用中文账号认证通过 → `PUSH_REPLY ... ifconfig 10.8.0.2` 拿到台账固定 IP；白名单按来源判定：隧道内 IP 访问 `/api/state` = 403、`/self` = 200，白名单内网段可进后台，公网 IP 全 403

- 真实 Flask HTTP 服务启动验证：首页、会话、登录、CSRF、state、向导自检、账号、版本、导出均 200
- 前端 JS 通过 `node --check`
- 所有 `.py` 通过 `py_compile`，所有 `.sh` 通过 `bash -n`

**限制（如实说明）**：

1. 沙盒内以 MOCK 模式验证业务逻辑，**`iptables` / `systemd` / 真实 OpenVPN 进程未实际执行**，这部分由目标机上的 `install.sh` + 安装向导完成，向导自带环境自检与失败回滚
2. 静态 openvpn 二进制与离线 Python 运行时**未在本环境实际编译/下载**（需要联网构建机），由 `build/build-offline.sh` / `build/Dockerfile.build` 复现；也可在 Web 向导里直接上传二进制
3. 是功能与操作逻辑对齐爱快风格，**不是像素级复刻**，未使用其任何界面素材
4. 单机部署，不做高可用、集群、SSO/MFA、多管理员 RBAC、自动 HTTPS

---

## 12. 版本信息（截至 2026-11-17）

- OpenVPN **2.6.23**（2026-09-23，修 7 个安全问题，含 CVE-2026-84732 未认证远程 DoS、CVE-2026-84471 double-free），2.6 安全支持到 2028-08-31
- OpenVPN **2.7.9**（2026-11-05，当前 Full Stable）
- 2026 年多个 CVE 影响 2.6.0~2.6.20 / 2.6.22，所以安全线设在 2.6.23
- 发行版 `2.6.19-0ubuntu0.24.04.5` 这类包看着版本低，靠 backport 补丁同样修了 CVE；本项目是自己带二进制，因此**由你负责手动更新**，版本页会显示 EOL 与「低于安全线」红字
