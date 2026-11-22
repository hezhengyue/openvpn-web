#!/usr/bin/env bash
# scripts/compose.sh —— Docker / 裸机双模式运行时封装

# 检测 Compose：找不到时不再直接退出，裸机模式仍可使用 start.sh
if command -v docker &>/dev/null && docker compose version &>/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
    COMPOSE="docker-compose"
else
    COMPOSE=""
fi

require_compose() {
    if [[ -z "$COMPOSE" ]]; then
        log_error "Docker Compose 未安装；当前操作需要 Docker，或改用 standalone:* 命令"
        exit 1
    fi
}

container_running() {
    [[ -n "$COMPOSE" ]] || return 1
    $COMPOSE ps "$1" 2>/dev/null | grep -q "Up"
}

# 每次动态判断：web 容器在跑才算 Docker 模式
is_docker_mode() {
    container_running web
}

# 解释器优先级：显式 PYTHON_BIN > 便携 Python > .venv > python3
python_bin() {
    if [[ -n "${PYTHON_BIN:-}" ]]; then
        # MANAGE/CELERY 会切到 django/ 子目录，相对路径必须先绝对化
        local _candidate="$PYTHON_BIN"
        local _root="${BASE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
        if [[ "$_candidate" != /* && ! -e "$_candidate" && -e "$_root/$_candidate" ]]; then
            _candidate="$_root/$_candidate"
        fi
        if [[ "$_candidate" == */* && -e "$_candidate" ]]; then
            local _dir _base
            _dir="$(cd "$(dirname "$_candidate")" && pwd)"
            _base="$(basename "$_candidate")"
            echo "$_dir/$_base"
        else
            echo "$_candidate"
        fi
        return
    fi

    local base="${BASE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

    # 便携包里的 bin/python 与 bin/python3 只是无 shebang 的占位文本，
    # 真正可执行文件是 bin/python3.12（ELF），必须直接用它。
    if [[ -x "$base/django/python3.12/bin/python3.12" ]]; then
        echo "$base/django/python3.12/bin/python3.12"
    elif [[ -x "$base/django/.venv/bin/python" ]]; then
        echo "$base/django/.venv/bin/python"
    elif command -v python3 &>/dev/null; then
        echo python3
    else
        echo python
    fi
}

PYTHON() {
    if is_docker_mode; then
        $COMPOSE exec -T web python "$@"
    else
        "$(python_bin)" "$@"
    fi
}

PIP() {
    if is_docker_mode; then
        $COMPOSE exec -T web pip "$@"
    else
        "$(python_bin)" -m pip "$@"
    fi
}

CELERY() {
    if is_docker_mode; then
        $COMPOSE exec -T web celery "$@"
    else
        local base="${BASE_DIR:-$PWD}"
        local py
        py="$(python_bin)"
        (cd "$base/django" && "$py" -m celery "$@")
    fi
}

MANAGE() {
    if is_docker_mode; then
        $COMPOSE exec -T web python manage.py "$@"
    else
        local base="${BASE_DIR:-$PWD}"
        local py
        py="$(python_bin)"
        (cd "$base/django" && "$py" manage.py "$@")
    fi
}

WATCHMEDO() {
    if is_docker_mode; then
        $COMPOSE exec -T web watchmedo "$@"
    else
        watchmedo "$@"
    fi
}

compose_cmd() {
    require_compose

    local mode="$1"
    shift
    local cmd="$1"
    shift
    local services

    case "$mode" in
        full)  services="$SERVICES_FULL" ;;
        dev)   services="$SERVICES_DEV" ;;
        *)     log_error "未知 compose mode: $mode"; exit 1 ;;
    esac

    case "$cmd" in
        down|config|port|exec|run|logs|ps)
            $COMPOSE "$cmd" "$@"
            ;;
        *)
            $COMPOSE "$cmd" "$@" $services
            ;;
    esac
}
