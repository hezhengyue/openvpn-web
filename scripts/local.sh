#!/usr/bin/env bash
# scripts/local.sh —— 本地开发 + 裸机便携模式

# =============================================================================
# 🧑‍💻 进入本地 Python 环境
# =============================================================================
enter_local_env() {
    if is_docker_mode; then
        return
    fi

    cd "$BASE_DIR" || exit 1

    # 裸机模式：直接用便携 Python，无需 .venv
    if is_true "${STANDALONE:-false}"; then
        return
    fi

    if [[ ! -f ".venv/bin/activate" ]]; then
        log_error "Python 虚拟环境不存在"
        log_warn "请先执行：./start.sh dev（开发模式）或设置 STANDALONE=true 走裸机模式"
        exit 1
    fi

    # shellcheck disable=SC1091
    source .venv/bin/activate
    export PYTHON_BIN="$PWD/.venv/bin/python"
}


# =============================================================================
# 🚀 开发模式：Docker 起 db/redis，本地 .venv 跑 Django + Celery 热重载
# =============================================================================
run_local_dev() {
    prepare_env

    log_info "启动开发基础设施"
    compose_cmd dev up -d
    wait_for_service db 60
    wait_for_service redis 60

    cd "$BASE_DIR" || exit 1

    if [[ ! -d ".venv" ]]; then
        log_info "创建 Python 虚拟环境"
        python3 -m venv .venv
    fi

    # shellcheck disable=SC1091
    source .venv/bin/activate
    export PYTHON_BIN="$PWD/.venv/bin/python"

    log_info "安装 Python 依赖"
    PIP install -q --upgrade pip
    PIP install -q -r requirements.txt
    PIP install -q watchdog

    run_migrations
    ensure_superuser

    echo ""
    log_success "开发环境启动完成"
    echo "🌐 Django:  http://127.0.0.1:8000"
    echo "🔐 Admin:   http://127.0.0.1:8000/admin"
    echo "🧵 Celery:  Auto Reload Enabled"
    echo ""

    trap 'kill 0' EXIT

    MANAGE runserver &

    WATCHMEDO auto-restart \
        --directory=. \
        --pattern="*.py" \
        --recursive \
        --ignore-patterns="*/logs/*;*/media/*;*/staticfiles/*;*/__pycache__/*;*/.venv/*;*/.git/*" \
        -- \
        celery -A config worker -l info --pool=solo &

    WATCHMEDO auto-restart \
        --directory=. \
        --pattern="*.py" \
        --recursive \
        --ignore-patterns="*/logs/*;*/media/*;*/staticfiles/*;*/__pycache__/*;*/.venv/*;*/.git/*" \
        -- \
        celery -A config beat -l info &

    wait
}


# =============================================================================
# 📦 裸机便携模式：便携 Python + SQLite WAL + WhiteNoise + Waitress
# =============================================================================
standalone_wheel_dir() {
    local dir="${WHEEL_DIR:-wheelhouse}"
    if [[ "$dir" != /* ]]; then
        dir="$BASE_DIR/$dir"
    fi
    echo "$dir"
}

# 在联网机器上下载裸机依赖 wheel，供离线目标机 --no-index 安装。
# 默认按目标 runtime（Linux x86_64 / CPython 3.12）下载；WHEEL_PLATFORM=native
# 表示按构建机当前平台下载（例如目标就是 Windows 调试环境）。
download_standalone_wheels() {
    local py wheels req pyver impl abi platform
    py="$(host_python_bin)"
    wheels="$(standalone_wheel_dir)"
    # 平铺布局：requirements-standalone.txt 就在项目根
    req="$BASE_DIR/requirements-standalone.txt"
    pyver="${WHEEL_PYTHON:-3.12}"
    impl="${WHEEL_IMPL:-cp}"
    abi="${WHEEL_ABI:-cp${pyver//./}}"
    platform="${WHEEL_PLATFORM:-manylinux2014_x86_64}"
    export PIP_DISABLE_PIP_VERSION_CHECK=1

    [[ "$platform" == "native" ]] && platform=""

    mkdir -p "$wheels"

    log_info "解释器：$py"
    log_info "wheel 目录：$wheels"

    local platform_args=()
    if [[ -n "$platform" ]]; then
        log_info "跨平台下载：Python $pyver / $platform"
        platform_args=(
            --only-binary=:all:
            --platform "$platform"
            --python-version "$pyver"
            --implementation "$impl"
            --abi "$abi"
        )
    else
        log_info "本机平台下载（宿主机 Python）"
        platform_args=(--only-binary=:all:)
    fi

    # 1) 逐条下载。django-simpleui 这类只发 sdist 的包先在本机构建成 wheel，
    #    否则 --only-binary 会直接报 "No matching distribution"。
    local spec sdist_dir sdist
    while IFS= read -r spec || [[ -n "$spec" ]]; do
        spec="${spec%%#*}"
        spec="$(printf '%s' "$spec" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        [[ -z "$spec" ]] && continue

        if "$py" -m pip download -q -d "$wheels" "${platform_args[@]}" "$spec" 2>/dev/null; then
            continue
        fi

        log_warn "没有可用 wheel，尝试从 sdist 构建：$spec"
        sdist_dir="$wheels/.sdist"
        rm -rf "$sdist_dir"
        mkdir -p "$sdist_dir"
        "$py" -m pip download -q --no-deps --no-binary=:all: -d "$sdist_dir" "$spec"
        sdist="$(find "$sdist_dir" -maxdepth 1 -type f | head -n 1)"
        if [[ -z "$sdist" ]]; then
            log_error "sdist 下载失败：$spec"
            exit 1
        fi
        "$py" -m pip wheel -q --no-deps -w "$wheels" "$sdist"
        rm -rf "$sdist_dir"
        log_success "已构建 wheel：$spec"
    done < "$req"

    # 2) 全量再解析一次，补齐依赖闭包（本地已构建的 wheel 通过 --find-links 命中）
    "$py" -m pip download -q -d "$wheels" "${platform_args[@]}" \
        --find-links "$wheels" -r "$req"

    local count
    count=$(find "$wheels" -maxdepth 1 -name '*.whl' | wc -l | tr -d ' ')
    if [[ "$count" -eq 0 ]]; then
        log_error "没有下载到任何 wheel，请检查网络或 requirements-standalone.txt"
        exit 1
    fi
    log_success "wheel 下载完成：$count 个 → $wheels"
}

install_standalone() {
    local py wheels req
    py="$(require_python_bin)"
    wheels="$(standalone_wheel_dir)"
    # 平铺布局：requirements-standalone.txt 就在项目根
    req="$BASE_DIR/requirements-standalone.txt"
    export PIP_DISABLE_PIP_VERSION_CHECK=1

    log_info "使用解释器：$py"

    # 便携 Python 通常自带 pip；万一被裁剪过，用自带的 ensurepip 离线恢复
    if ! "$py" -m pip --version >/dev/null 2>&1; then
        log_warn "pip 不可用，尝试 ensurepip 引导"
        "$py" -m ensurepip --upgrade || {
            log_error "ensurepip 失败；请使用带 pip 的便携 Python 构建"
            exit 1
        }
    fi

    if [[ -d "$wheels" ]] && compgen -G "$wheels/*.whl" >/dev/null 2>&1; then
        log_info "检测到离线 wheel 仓库，使用 --no-index 安装"
        "$py" -m pip install -q --no-index --find-links "$wheels" -r "$req"
    else
        log_info "未发现 wheelhouse，联网安装"
        "$py" -m pip install -q --upgrade pip
        "$py" -m pip install -q -r "$req"
    fi

    log_success "裸机模式依赖安装完成"
}

init_standalone() {
    install_standalone
    log_info "初始化 SQLite / 静态文件 / 管理员"
    MANAGE collectstatic --noinput
    run_migrations
    ensure_superuser
    log_success "裸机模式初始化完成"
}

run_standalone() {
    local py
    py="$(require_python_bin)"

    cd "$BASE_DIR" || exit 1

    local host="${STANDALONE_HOST:-0.0.0.0}"
    local port="${STANDALONE_PORT:-8800}"
    local threads="${STANDALONE_THREADS:-8}"

    log_info "裸机模式启动：http://${host}:${port} （Waitress ${threads} threads）"
    exec "$py" -m waitress \
        --host="$host" \
        --port="$port" \
        --threads="$threads" \
        config.wsgi:application
}

dev_standalone() {
    cd "$BASE_DIR" || exit 1
    local host="${STANDALONE_HOST:-127.0.0.1}"
    local port="${STANDALONE_PORT:-8800}"
    MANAGE runserver "${host}:${port}"
}

check_standalone() {
    MANAGE check
}
