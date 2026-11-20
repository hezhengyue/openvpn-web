# vendor/ —— 构建期三方件仓库（本地优先，不走云）

目标：**构建机尽量不联网**。能本地就本地，链接换了也不慌。

```
vendor/
├── src/                 静态 OpenVPN 的源码包（15M，随仓库走）
│   ├── openssl-3.0.16.tar.gz
│   ├── lzo-2.10.tar.gz
│   ├── lz4-1.9.4.tar.gz
│   ├── libcap-ng-0.8.5.tar.gz     OpenVPN 2.6 硬依赖，发行版只给 .so，得自己编 .a
│   └── openvpn-2.6.23.tar.gz      官方发布包（自带 configure）
├── static/prefix.tar.gz 预编译静态库（glibc 2.17 基线）：libssl.a / libcrypto.a /
│                        liblzo2.a / liblz4.a / libcap-ng.a + 头文件 + 静态 openssl 命令
│                        → 目标机上「上传源码自动编译」只编 openvpn 本体，14 秒出结果
├── wheels/              flask / openpyxl / waitress 及其依赖（纯 Python，离线安装）
├── SHA256SUMS           上面所有 tar.gz 的校验值，构建时逐个校验
└── fetch.sh             有网机器上跑一次，把缺的补齐（幂等，不重复下）
```

## 构建时怎么用

```bash
./build/build-offline.sh           # 自动优先用 vendor/ 与仓库自带 runtime/
VENDOR_ONLY=1 ./build/build-offline.sh   # 严格离线：任何缺件直接报错，绝不联网
```

三级来源，逐级回退：

| 依赖 | ① 本地 | ② 仓库快照 | ③ 联网（最后兜底） |
|---|---|---|---|
| Python 运行时 | 仓库根 `runtime/`（已自带，零下载） | `vendor/python/*install_only*.tar.gz` | python-build-standalone |
| Flask / openpyxl / waitress | — | `vendor/wheels/*.whl`（`pip --no-index`） | PyPI |
| 静态 OpenVPN | `bin/openvpn` 已存在则直接用 | `vendor/src/*.tar.gz` | 各上游镜像，下到后**回填 vendor/src** |

## 为什么要有这层

上游链接确实会变、会挂。本次实测（2026-11-18）：

| 上游 | 状态 |
|---|---|
| `swupdate.openvpn.org`（OpenVPN 官方发布主机） | **连不上**（HTTP 56 / 超时） |
| `mirrors.aliyun.com/openvpn/`（官方发布包的镜像） | **可用，已设为首选**（自带 `configure`） |
| `github.com/OpenVPN/openvpn` tag 归档 | 可用，但**没有 `configure`**，需要 `autoreconf` |
| `openssl.org` / `oberhumer.com` / `github.com/lz4` | 可用 |
| `people.redhat.com/sgrubb/libcap-ng/` | 可用（GitHub 归档那条当时 502，正好反过来） |
| `astral-sh/python-build-standalone` 资产 | 资产名带日期，换版本链接就变 |

所以脚本里每个包都配了**多个镜像按序回退**，OpenVPN 用 GitHub 归档时必须先 `autoreconf -fi`
（需要 `autoconf automake libtool`，`Dockerfile.build` 已经装上）。

### 实际编译踩到的坑（都已修进脚本）

1. `PKG_CONFIG="$PFX/bin/pkg-config --static"` —— 那个路径从来不存在的，改用系统的 `pkg-config`
2. LZO 2.10 不装 `lzo2.pc`，openvpn 的 configure 报 “pkg-config is required” → 脚本补一个最小 `.pc`
3. OpenVPN 2.6 默认探测 DCO，缺 `libnl-genl-3` 直接报错 → 加 `--disable-dco`
4. `libcap-ng` 是 2.6 的**硬依赖**，发行版 `-devel` 只给 `.so`，静态链会多一个动态依赖 → 自己编 `.a`
5. 产物带 `libresolv.so.2`（glibc 自带），被应用白名单误判为第三方依赖 → `core.ldd_third_party()` 已扩到 glibc 全家族

## 补件 / 升级版本

```bash
./vendor/fetch.sh                  # 补齐缺失项 + 更新 SHA256SUMS
OPENVPN_VER=2.6.24 ./vendor/fetch.sh && OPENVPN_VER=2.6.24 ./build/build-offline.sh
```

`fetch.sh` 不写死 URL，它复用 `build/build-static-openvpn.sh` 里的镜像列表，避免两处不一致；
下载完自动重算 `SHA256SUMS`，下次构建会逐个校验，文件被换过或下传不完整会直接失败。

## 注意

- `vendor/src` 与 `vendor/static` 会随 `dist/` 一起交付：目标机需要它们来做「上传源码自动编译」。
  `vendor/wheels` 只在构建期用（依赖已经装进 `runtime/` 里了）。
- wheel 是按「仓库自带 runtime 的版本」抓的（当前是 3.12 → `cp312 / manylinux2014`）。
  换了运行时版本要重跑 `./vendor/fetch.sh`，否则 `markupsafe` 这种带 C 扩展的包会装不上。
- `prefix.tar.gz` 是在 CentOS 7 / glibc 2.17 环境里编的；它只影响「在目标机上编 openvpn」这件事，
  静态库与目标机 glibc 无关，不会把 ABI 要求带进去。
