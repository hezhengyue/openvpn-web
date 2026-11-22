#!/usr/bin/env bash
# scripts/local.sh —— 本地开发 + 裸机便携模式

# =============================================================================
# 🧑‍💻 进入本地 Python 环境
# =============================================================================
enter_local_env() {
    if is_docker_mode; then
        return
    fi

    cd "$BASE_DIR/django" || exit 1

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

    cd "$BASE_DIR/django" || exit 1

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
install_standalone() {
    log_info "使用解释器：$(python_bin)"
    PIP install -q --upgrade pip
    PIP install -q -r "$BASE_DIR/django/requirements-standalone.txt"
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
    py="$(python_bin)"

    cd "$BASE_DIR/django" || exit 1

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
    cd "$BASE_DIR/django" || exit 1
    local host="${STANDALONE_HOST:-127.0.0.1}"
    local port="${STANDALONE_PORT:-8800}"
    MANAGE runserver "${host}:${port}"
}

check_standalone() {
    MANAGE check
}
