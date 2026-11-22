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

prepare_env() {
    ensure_env
    load_env
}

# 裸机命令即使 .env 不存在也能跑：从模板创建后改写两个关键开关，
# 并强制导出 STANDALONE=True（命令行显式选择优先于 .env）。
prepare_standalone_env() {
    if [[ ! -f "$BASE_DIR/.env" ]]; then
        cp "$BASE_DIR/.env.example" "$BASE_DIR/.env"
        sed -i 's/^STANDALONE=.*/STANDALONE=True/' "$BASE_DIR/.env"
        sed -i 's/^USE_HTTPS=.*/USE_HTTPS=false/' "$BASE_DIR/.env"
        log_warn "已创建 .env（裸机默认：STANDALONE=True, USE_HTTPS=false）"
    fi
    load_env
    export STANDALONE=True
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
