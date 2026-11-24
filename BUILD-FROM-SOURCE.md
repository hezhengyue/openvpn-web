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
./python/bin/python3 tests/selftest.py     # 期望 461 项全过
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
