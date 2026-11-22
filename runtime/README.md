# runtime/

便携 Python 运行时目录，**不进 Git**。

本项目的布局与底座模板略有差异，源码开发时把便携 Python（Linux x86_64、CPython 3.12）解压到：

```text
runtime/
├── bin/python3.12      # 真正的解释器（ELF）
├── bin/pip
└── lib/python3.12/
```

打包脚本 `build/build-offline.py` 会把整个 `runtime/` 复制成交付包里的 `python/`
（代码里写死 `$DEST/python/bin/python3`），所以 **交付包中运行时目录叫 `python/`**，
不叫 `runtime/python/`。

`scripts/compose.sh` 的 `python_bin()` 按以下顺序探测解释器：

1. 显式 `PYTHON_BIN`
2. `runtime/python/bin/python3.1x/`（底座模板新约定，兼容）
3. `python/bin/python3.1x/`（本项目交付包）
4. `runtime/bin/python3.1x/`（本项目源码开发）
5. `django/python3.12/bin/python3.12/`（底座旧路径，兼容）
6. 开发 `.venv` / 系统 `python3`

只看文件存在、不依赖执行位：从 Windows 拷贝到 Linux 可能丢失 755，
真正执行前由 `require_python_bin()` 给出明确的 `chmod` 提示。

从 Windows 拷贝到 Linux 后若提示 `Permission denied` / 无法执行，补一次执行位：

```bash
chmod +x runtime/bin/python3.12 runtime/bin/pip*
```

## 离线依赖

本项目随包带 `vendor/wheels/`（完整 wheel 仓库）。`./start.sh standalone:install`
检测到 `WHEEL_DIR` 下有 `*.whl` 时会自动走 `--no-index` 离线安装，pip 缺失时用
`ensurepip` 离线恢复。需要新增/更新 wheel 时：

```bash
./start.sh wheel:download          # 联网构建机上下载（默认 manylinux2014_x86_64 / cp312）
WHEEL_PLATFORM=native ./start.sh wheel:download   # 按构建机当前平台下载
```

可用变量：`WHEEL_DIR`（本项目 `.env.example` 设为 `vendor/wheels`）、
`WHEEL_PLATFORM`、`WHEEL_PYTHON`、`WHEEL_ABI`、`WHEEL_IMPL`。

`django-simpleui` 只发布 sdist，脚本会自动下载并在构建机上编成 `py3-none-any` wheel。
