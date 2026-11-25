# 从源码仓库到可运行交付包（GitHub clone 后必读）

GitHub 仓库**刻意不含**体积大的二进制与构建产物，只保留源码、脚本、文档和校验清单。这样 clone 很快、代码清晰；构建所需的约 46MB 文件在你自己的构建机上按本指南几条命令即可全部生成。

> 本仓库的正式交付包是 `dist/openvpn-web-x86_64.tar.gz`（约 146MB，含便携 Python + 静态 OpenVPN），同样**不进 Git**——它是下面流程的产物。

## 为什么这些文件不在仓库里

| 文件 | 大小 | 不在仓库的原因 | 如何生成 |
| --- | --- | --- | --- |
| `bin/openvpn`、`bin/openssl` | ~14M | 编译产物，跟 glibc 版本绑定 | `build/build-static-openvpn.sh` 编译 |
| `vendor/src/*.tar.gz` | ~19M | 上游源码包，可从镜像拉取 | `vendor/fetch.sh` |
| `vendor/wheels/*.whl` | ~13M | 离线 wheel，可从 PyPI 下载 | `start.sh wheel:download` 或 `vendor/fetch.sh` |
| `vendor/static/prefix.tar.gz` | ~5.9M | 预编译静态库 | `build/build-static-openvpn.sh` 产出 |
| `runtime/`（交付包里叫 `python/`） | ~350M | 便携 Python 运行时 | 见下文「准备便携 Python」 |
| `dist/` | ~146M | 交付包，构建产物 | `build/build-offline.py` 或 `.sh` |

这些路径都已在 `.gitignore` 中，**本地生成后照常可用、不会被提交**。

---

## 三步走：clone → 补齐依赖 → 打包

### 前置条件

- 一台 **x86_64 / glibc ≥ 2.17** 的 Linux 构建机（CentOS 7、Rocky 8、Ubuntu 20.04+ 均可），或 Docker。
- 产物要能在老系统（CentOS 7）跑，就**必须在老 glibc 环境里构建**——脚本会自检 `GLIBC_2.17` 上限，超限直接报错。用仓库提供的 `build/Dockerfile.build` 最省事。
- 本机 Python 仅用于跑 `build/build-offline.py`（Windows/Linux 都行）；打包脚本不执行 Linux 程序，纯搬运。

### 第 1 步：补齐 `vendor/`（源码包 + wheel）

在有网的构建机上跑一次，幂等，缺什么补什么：

```bash
./vendor/fetch.sh
```

它会：
- 复用 `build/build-static-openvpn.sh` 的镜像列表，把 OpenSSL / LZO / LZ4 / libcap-ng / OpenVPN 的源码包下到 `vendor/src/`；
- 用当前 Python 下载目标平台（manylinux2014 / cp312）的 wheel 到 `vendor/wheels/`；
- 重算 `vendor/SHA256SUMS`，构建时逐个校验，防止源码被换或下载不完整。

没有本地 Python 也没关系：`fetch.sh` 会跳过 wheel 下载，构建时缺 wheel 会自动回退到 PyPI。想严格离线，先把 wheel 放进 `vendor/wheels/` 再加 `VENDOR_ONLY=1`。

> 上游链接确实会变。本仓库记录过 `swupdate.openvpn.org` 连不上、只能走 GitHub tag 归档的情况，脚本已配多镜像按序回退，`fetch.sh` 不写死 URL。详见 `vendor/README.md`。

### 第 2 步：准备便携 Python（`runtime/`）

交付包里的 `python/` 就是一个完整的便携 CPython 3.12（Linux x86_64 / glibc 2.17），有两种准备方式：

**方式 A（推荐）：让构建脚本自动下载。** 直接做第 3 步，`build/build-offline.sh` 在 `runtime/` 为空时会从 `python-build-standalone` 拉取 install_only 包并解压（版本/URL 由 `PBS_VER` / `PBS_URL` 控制）。这是最省事的。

**方式 B：手动放置。** 从 [astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone/releases) 下载 `cpython-3.12.x+...-x86_64-unknown-linux-gnu-install_only.tar.gz`，解压到仓库根的 `runtime/`，使其满足：

```
runtime/
├── bin/python3.12     # 真正的解释器（ELF）
├── bin/pip
└── lib/python3.12/
```

从 Windows 拷到 Linux 若丢执行位，补一次：

```bash
chmod +x runtime/bin/python3.12 runtime/bin/pip*
```

`scripts/compose.sh` 会按 `PYTHON_BIN` → `runtime/python/bin/python3.1x` → `python/bin/python3.1x` → `runtime/bin/python3.1x` → `.venv`/系统 `python3` 的顺序探测。细节见 `runtime/README.md`。

### 第 3 步：打包

** Linux 构建机（会离线装一遍 wheel，产物 site-packages 是热的）：**

```bash
./build/build-offline.sh                 # 缺什么补什么；自动编 openvpn
VENDOR_ONLY=1 ./build/build-offline.sh   # 严格离线，缺件直接失败
```

** Windows / 不想执行 Linux 程序（纯搬运，安装时再从 wheel 补依赖）：**

```bash
python build/build-offline.py             # 产出 dist/openvpn-web-x86_64/ + .tar.gz
python build/build-offline.py --tar-only  # 只要 tar.gz
```

两个脚本做的是同一件事（分层源码 → 平铺交付包），差别只在要不要执行包内 Python 真装一遍 wheel。产物统一是 `dist/openvpn-web-x86_64.tar.gz`。

** 只编译 OpenVPN 二进制（不打包）：**

```bash
./build/build-static-openvpn.sh            # -> ./bin/openvpn
STATIC_LIBS=1 ./build/build-static-openvpn.sh   # 用 vendor/static 预编库，只编本体，约 14 秒
```

脚本结尾会自检 `ldd bin/openvpn` 只依赖 glibc 家族，并把 `GLIBC_2.17` 上限写死，防止悄悄产出不兼容的包。

** Docker 一条龙（不想手动备工具链）：**

```bash
docker build -f build/Dockerfile.build -t ovpn-build .
docker run --rm -v "$PWD/dist:/out" ovpn-build cp -r /build/dist/openvpn-web-x86_64 /out/
```

---

## 生成后的可用性自检

打包完成后，用**包内自带解释器**跑一遍自检（系统 python3 不在 PATH）：

```bash
cd dist/openvpn-web-x86_64
./python/bin/python3 tests/selftest.py     # 期望 472 项全过
./python/bin/python3 scripts/contract_check.py . --strict   # 期望 0 FAIL / 0 WARN
```

确认 `version.txt` 版本正确、`bin/openvpn` 与 `python/bin/python3` 都在，即可把 `dist/openvpn-web-x86_64.tar.gz` 拷到目标机。

---

## 部署与使用

把交付包拷到目标机（x86_64 / glibc ≥ 2.17 / systemd / `/dev/net/tun`），解压后：

```bash
sudo ./install.sh
```

包里已带全部依赖，**目标机不联网、不装包、不编译**，跑完 OpenVPN 和 Web（默认 8800 端口）都在运行。完整部署步骤、默认凭据、REST API、备份恢复、常见问题见 `README.md`。

---

## 相关文档

- `README.md` — 产品说明、部署、运维、API、FAQ（主文档）
- `vendor/README.md` — 依赖拉取策略、上游镜像状态、编译踩坑记录
- `runtime/README.md` — 便携 Python 布局与解释器探测顺序
- `CONTRACT.md` — 代码分层契约（Web 进程内零裸 SQL、特权边界登记等）

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
