#!/usr/bin/env bash
# ============================================================
# jellyfin-helper 容器入口
# 1. 等 Postgres ready（最长 60s）
# 2. 确保挂载目录可写
# 3. 把 PG 凭据从环境变量注入到 config.yaml（首次启动场景）
# 4. exec CMD
# ============================================================
set -euo pipefail

log() { printf '[entrypoint] %s\n' "$*"; }

# ---------- 1. 等 Postgres ----------
PG_HOST="${POSTGRES_HOST:-postgres}"
PG_PORT="${POSTGRES_PORT:-5432}"

log "等待 Postgres ${PG_HOST}:${PG_PORT} ..."
for i in $(seq 1 60); do
    if (echo > "/dev/tcp/${PG_HOST}/${PG_PORT}") >/dev/null 2>&1; then
        log "Postgres 已就绪（用时 ${i}s）"
        break
    fi
    if [ "$i" = "60" ]; then
        log "ERROR: Postgres 60s 内未就绪，退出"
        exit 1
    fi
    sleep 1
done

# ---------- 2. 检查必需挂载 ----------
for d in /app/data /app/config.yaml; do
    if [ ! -e "$d" ]; then
        log "ERROR: 必需路径 $d 不存在；compose 应该把它 bind mount 进来"
        exit 1
    fi
done

# 媒体 / 下载目录只警告不退出（首次启动可能还没准备好，但 helper 本身能起来）
for d in /media /downloads; do
    if [ ! -d "$d" ]; then
        log "WARN: $d 未挂载，相关功能（媒体扫描 / 下载入库）会失效"
    fi
done

# ---------- 3. 确保 data 目录可写（PUID/PGID 不一致时给个明确报错） ----------
if [ ! -w /app/data ]; then
    log "ERROR: /app/data 不可写；检查 .env 的 PUID/PGID 是否与宿主 ./data 目录属主一致"
    exit 1
fi

# ---------- 4. 启动 ----------
log "启动 backend：$*"
exec "$@"
