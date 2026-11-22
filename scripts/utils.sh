#!/usr/bin/env bash
# scripts/utils.sh

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}ℹ️ $1${NC}"; }
log_warn()    { echo -e "${YELLOW}⚠️ $1${NC}"; }
log_error()   { echo -e "${RED}❌ $1${NC}"; }
log_success() { echo -e "${GREEN}✅ $1${NC}"; }

is_true() {
    case "${1,,}" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

clean_pycache() {
    local base_dir="${1:-.}"
    find "$base_dir" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    log_success "Pycache 清理完成"
}

show_help() {
cat << EOF

Django 双模式统一底座
  STANDALONE=False  Docker 全家桶（Nginx + Postgres + Redis + Celery）
  STANDALONE=True   裸机便携（便携 Python + SQLite WAL + WhiteNoise + Waitress）

Docker 模式:
  up/start             启动核心服务（db redis web nginx）
  async:up             额外启动 celery + celery-beat
  init/setup           初始化：迁移 + collectstatic + 管理员
  update               重建镜像并初始化
  down/stop            停止全部容器
  restart / rebuild    重启 / 重建
  logs [service]       查看日志

开发模式（Docker 基础设施 + 本地 .venv）:
  dev / dev:up / dev:down / dev:logs / dev:sync

裸机便携模式:
  standalone:install   用便携 Python 安装 requirements-standalone.txt
  standalone:init      安装依赖 + 迁移 + collectstatic + 管理员
  standalone:up        Waitress 启动（默认 0.0.0.0:8800）
  standalone:dev       Django runserver 便于调试
  standalone:check     执行 Django system check

Django 常用:
  migrate / makemigrations / shell / superuser

清理:
  clean                删除 Docker 容器与数据卷（危险）
  clean:pycache        清理 __pycache__

EOF
}
