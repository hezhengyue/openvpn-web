#!/usr/bin/env bash
# scripts/env.sh

# Docker 默认只起核心四件套；异步任务按需 --profile async 启用
SERVICES_FULL="db redis web nginx"
SERVICES_ASYNC="celery celery-beat"
SERVICES_DEV="db redis"

APPS_LIST="accounts jobs"
PRIORITY_APP="${PRIORITY_APP:-${APPS_LIST%% *}}"

ensure_env() {
    [[ -f "$BASE_DIR/.env" ]] && return

    if [[ -f "$BASE_DIR/.env.example" ]]; then
        cp "$BASE_DIR/.env.example" "$BASE_DIR/.env"
        log_warn "已自动创建 .env（请按实际环境修改）"
    else
        log_error "缺少 .env 文件"
        exit 1
    fi
}

# 裸机模式脚本需要读取 .env 中的 STANDALONE / 端口 / 管理员等变量
load_env() {
    [[ -f "$BASE_DIR/.env" ]] || return 0
    set -a
    # shellcheck disable=SC1090
    source "$BASE_DIR/.env"
    set +a
}

# 探测本机主内网 IP，仅用于裸机临时补 HOSTS，不写回 .env
_primary_lan_ip() {
    local ip=""
    if command -v hostname >/dev/null 2>&1; then
        read -r ip _ <<< "$(hostname -I 2>/dev/null)"
    fi
    echo "$ip"
}

# 通用准备：只负责 .env 存在与载入，不改写模式。
# migrate/shell 等通用 Django 命令必须尊重 .env 里的 STANDALONE。
prepare_env() {
    ensure_env
    load_env
}

# 运行时目录解析：与 settings.py 的 _runtime_dir 保持同一优先级，
# 即 独立 *DIR 变量 > RUNTIME_DIR/<subdir> > 项目根/<subdir>（平铺布局）。
_runtime_path() {
    local var="$1" subdir="$2" explicit
    explicit="${!var:-}"
    if [[ -n "$explicit" ]]; then
        printf '%s' "$explicit"
    elif [[ -n "${RUNTIME_DIR:-}" ]]; then
        printf '%s' "${RUNTIME_DIR%/}/$subdir"
    else
        printf '%s' "$BASE_DIR/$subdir"
    fi
}

# 裸机 init 阶段预建运行时目录。settings.py 的 import 阶段已改为 best-effort
# （失败只告警），真正的目录创建由这里承担；失败也只是告警，不中断初始化。
ensure_runtime_dirs() {
    local d
    for d in \
        "$(_runtime_path DATA_DIR data)" \
        "$(_runtime_path CACHE_DIR data/cache)" \
        "$(_runtime_path STATIC_ROOT staticfiles)" \
        "$(_runtime_path MEDIA_ROOT media)" \
        "$(_runtime_path LOG_DIR logs)"; do
        if ! mkdir -p "$d" 2>/dev/null; then
            log_warn "无法预建运行时目录：$d —— 请确认已存在且服务用户可写"
        fi
    done
}

# Docker 生命周期命令专用：命令行入口优先于 .env，显式强制 STANDALONE=False。
prepare_docker_env() {
    prepare_env
    if is_true "${STANDALONE:-false}"; then
        log_warn ".env 中 STANDALONE=True，但当前是 Docker 命令；本次强制按 Docker 模式运行"
    fi
    export STANDALONE=False
    if ! is_true "${USE_HTTPS:-false}" && ! is_true "${DEBUG:-false}"; then
        log_warn "Docker 模式 USE_HTTPS=false：Cookie 不带 Secure，确认前面没有 nginx 做 TLS 终止"
    fi
}

# 裸机命令同样显式归一化：STANDALONE=True，默认按无 TLS 的 HTTP 处理。
# 若裸机前面自行终止了 HTTPS，用 STANDALONE_USE_HTTPS=true 显式保留 Secure Cookie。
prepare_standalone_env() {
    local created=0
    if [[ ! -f "$BASE_DIR/.env" ]]; then
        cp "$BASE_DIR/.env.example" "$BASE_DIR/.env"
        sed -i 's/^STANDALONE=.*/STANDALONE=True/' "$BASE_DIR/.env"
        sed -i 's/^USE_HTTPS=.*/USE_HTTPS=false/' "$BASE_DIR/.env"
        created=1
        log_warn "已创建 .env（裸机默认：STANDALONE=True, USE_HTTPS=false）"
    fi
    load_env
    export STANDALONE=True

    if is_true "${STANDALONE_USE_HTTPS:-false}"; then
        export USE_HTTPS=True
        log_warn "已启用 STANDALONE_USE_HTTPS：请确认 Waitress 前有 HTTPS 终止，否则 Cookie/CSRF 会失效"
    else
        if is_true "${USE_HTTPS:-false}"; then
            log_warn ".env 的 USE_HTTPS=true 已在本次裸机命令中忽略（需要保留请设 STANDALONE_USE_HTTPS=true）"
        fi
        export USE_HTTPS=False
    fi

    # ALLOWED_HOSTS 不会自动包含本机内网 IP；把探测到的 IP 临时加入，避免 400。
    local detected_ip
    detected_ip="$(_primary_lan_ip)"
    if [[ -n "$detected_ip" ]]; then
        case ",${HOSTS:-}," in
            *",$detected_ip,"*) ;;
            *)
                export HOSTS="${HOSTS:+$HOSTS,}$detected_ip"
                log_info "已将本机 IP $detected_ip 加入本次 HOSTS（未写入 .env）"
                ;;
        esac
    fi

    if [[ "$created" -eq 0 ]]; then
        log_info "裸机 HOSTS=${HOSTS:-localhost,127.0.0.1}；若有其他访问域名/IP 请修改 .env"
    fi
}

ensure_cert() {
    mkdir -p "$BASE_DIR/nginx/ssl"

    [[ -f "$BASE_DIR/nginx/ssl/fullchain.pem" && -f "$BASE_DIR/nginx/ssl/privkey.pem" ]] && return

    log_info "生成 SSL 证书"

    openssl req -x509 -nodes -days 3650 \
        -newkey rsa:2048 \
        -keyout "$BASE_DIR/nginx/ssl/privkey.pem" \
        -out "$BASE_DIR/nginx/ssl/fullchain.pem" \
        -subj "/C=CN/ST=Beijing/L=Beijing/O=Dev/CN=localhost" \
        2>/dev/null

    chmod 600 "$BASE_DIR/nginx/ssl/privkey.pem"

    log_success "SSL 证书生成完成"
}
