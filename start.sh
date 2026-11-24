#!/usr/bin/env bash
# start.sh

set -e

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"

source "$BASE_DIR/scripts/utils.sh"
source "$BASE_DIR/scripts/env.sh"
source "$BASE_DIR/scripts/compose.sh"
source "$BASE_DIR/scripts/service.sh"
source "$BASE_DIR/scripts/django.sh"
source "$BASE_DIR/scripts/local.sh"

# 已有 .env 时先载入，裸机命令需要读取 STANDALONE / 端口等变量
[[ -f "$BASE_DIR/.env" ]] && load_env

case "${1:-help}" in

    # =========================================================
    # Production
    # =========================================================

    up|start)
        prepare_docker_env
        require_compose
        ensure_cert

        log_info "启动生产环境"

        compose_cmd full up -d
        ;;

    init|setup)
        prepare_docker_env
        require_compose
        ensure_cert

        log_info "初始化生产环境"

        compose_cmd full up -d

        wait_for_service web 60

        run_migrations

        MANAGE collectstatic --noinput

        ensure_superuser

        $COMPOSE restart web

        log_success "初始化完成"
        ;;

    update)
        prepare_docker_env
        require_compose
        ensure_cert

        log_info "更新生产环境"

        compose_cmd full build --no-cache web celery celery-beat

        compose_cmd full up -d

        wait_for_service web 60

        run_migrations

        MANAGE collectstatic --noinput

        ensure_superuser

        $COMPOSE restart web

        log_success "更新完成"
        ;;

    down|stop)
        compose_cmd full down --remove-orphans
        ;;

    async:up)
        prepare_docker_env
        require_compose
        $COMPOSE --profile async up -d $SERVICES_ASYNC
        ;;

    async:down)
        require_compose
        $COMPOSE stop $SERVICES_ASYNC
        ;;

    restart)
        compose_cmd full down --remove-orphans
        compose_cmd full up -d
        ;;

    rebuild)
        compose_cmd full up -d --build
        ;;

    logs)
        require_compose
        [[ -n "$2" ]] \
            && $COMPOSE logs -f "$2" \
            || $COMPOSE logs -f $SERVICES_FULL
        ;;

    exec)
        require_compose
        container="${2:-web}"
        shell="${3:-bash}"

        $COMPOSE exec "$container" "$shell"
        ;;

    # =========================================================
    # Local Development
    # =========================================================

    dev)
        run_local_dev
        ;;

    dev:up)
        prepare_docker_env
        compose_cmd dev up -d
        ;;

    dev:down)
        compose_cmd dev down
        ;;

    dev:logs)
        [[ -n "$2" ]] \
            && $COMPOSE logs -f "$2" \
            || $COMPOSE logs -f $SERVICES_DEV
        ;;

    dev:ps)
        compose_cmd dev ps
        ;;

    dev:sync)
        prepare_docker_env
        enter_local_env
        run_migrations
        ;;

    # =========================================================
    # 裸机便携模式（STANDALONE=True，无需 Docker）
    # =========================================================

    standalone:install)
        prepare_standalone_env
        install_standalone
        ;;

    standalone:init)
        prepare_standalone_env
        init_standalone
        ;;

    standalone:up)
        prepare_standalone_env
        run_standalone
        ;;

    standalone:dev)
        prepare_standalone_env
        dev_standalone
        ;;

    standalone:check)
        prepare_standalone_env
        check_standalone
        ;;

    wheel:download)
        # 构建机操作，不强制创建/改写 .env；有则读取其中的 WHEEL_* 覆盖项
        if [[ -f "$BASE_DIR/.env" ]]; then
            load_env
        fi
        download_standalone_wheels
        ;;

    # =========================================================
    # Django Commands
    # =========================================================

    migrate)
        prepare_env
        enter_local_env
        MANAGE migrate
        ;;

    makemigrations)
        prepare_env
        enter_local_env
        MANAGE makemigrations
        ;;

    superuser)
        prepare_env
        enter_local_env
        MANAGE createsuperuser
        ;;

    shell)
        prepare_env
        enter_local_env
        MANAGE shell
        ;;

    # =========================================================
    # Clean
    # =========================================================

    clean)
        compose_cmd full down -v --remove-orphans
        ;;

    clean:pycache)
        # 2.6 起源码在根；逐个 Django app 目录清理（不碰 runtime/vendor）
        for d in config core common audit jobs ovpn; do
            [ -d "./$d" ] && clean_pycache "./$d"
        done
        [ -d "./django" ] && clean_pycache ./django   # 兼容旧布局
        ;;

    help|--help|-h|*)
        show_help
        ;;

esac