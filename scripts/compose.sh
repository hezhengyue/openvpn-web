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

# 每次动态判断：STANDALONE 显式开启时永远用本地解释器，
# 否则 web 容器在跑才算 Docker 模式（避免 standalone:* 误进容器执行）。
is_docker_mode() {
    is_true "${STANDALONE:-false}" && return 1
    container_running web
}

# 解释器优先级：显式 PYTHON_BIN > 便携 Python > .venv > python3
python_bin() {
    if [[ -n "${PYTHON_BIN:-}" ]]; then
        # 相对路径必须先绝对化（脚本可能切到项目根之外的子目录）
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
    # 只看 -f，不依赖执行位：从 Windows 拷贝到 Linux 可能丢失 755，
    # 真正执行前由 require_python_bin 给出明确 chmod 提示。
    # 本项目的平铺交付包用 python/；源码开发用 runtime/；兼容底座的
    # runtime/python/ 与旧路径 django/python3.12/。
    local candidate
    for candidate in \
        "$base/runtime/python/bin/python3.12" \
        "$base/runtime/python/bin/python3.13" \
        "$base/runtime/python/bin/python3.11" \
        "$base/python/bin/python3.12" \
        "$base/python/bin/python3.13" \
        "$base/python/bin/python3.11" \
        "$base/runtime/bin/python3.12" \
        "$base/runtime/bin/python3.13" \
        "$base/runtime/bin/python3.11" \
        "$base/django/python3.12/bin/python3.12"; do
        if [[ -f "$candidate" ]]; then
            echo "$candidate"
            return
        fi
    done

    if [[ -f "$base/.venv/bin/python" ]]; then
        echo "$base/.venv/bin/python"
    elif [[ -f "$base/django/.venv/bin/python" ]]; then
        echo "$base/django/.venv/bin/python"
    elif command -v python3 &>/dev/null; then
        echo python3
    else
        echo python
    fi
}

# wheel:download 是“构建机”操作：目标 runtime 可能是另一平台（如 Windows
# 上下载 Linux wheel），因此优先用宿主机 Python，而不是 runtime/python。
host_python_bin() {
    if [[ -n "${PYTHON_BIN:-}" ]]; then
        python_bin
        return
    fi
    if command -v python3 &>/dev/null && python3 -V &>/dev/null; then
        echo python3
        return
    fi
    if command -v python &>/dev/null && python -V &>/dev/null; then
        echo python
        return
    fi
    python_bin
}

# 取解释器并确认真的能执行；失败时提示执行位问题（Windows→Linux 拷贝常见）
require_python_bin() {
    local py
    py="$(python_bin)"
    if ! "$py" -V >/dev/null 2>&1; then
        log_error "解释器无法执行：$py" >&2
        log_warn "若从 Windows 拷贝到 Linux，执行位数可能丢失，请运行：chmod +x \"$py\"" >&2
        exit 1
    fi
    printf '%s\n' "$py"
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
        (cd "$base" && "$py" -m celery "$@")
    fi
}

MANAGE() {
    if is_docker_mode; then
        $COMPOSE exec -T web python manage.py "$@"
    else
        local base="${BASE_DIR:-$PWD}"
        local py
        py="$(python_bin)"
        # 2.6 起源码与交付同构为根布局（manage.py 在 base 根）；兼容 django/ 子目录
        local mgdir="$base"
        [ -f "$base/manage.py" ] || mgdir="$base/django"
        (cd "$mgdir" && "$py" manage.py "$@")
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
