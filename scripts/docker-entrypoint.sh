#!/usr/bin/env bash
# ============================================================
# jellyfin-helper 容器入口
#
# 设计：
#   - 容器默认以 root 启动
#   - 第一次 entry：自动 mkdir -p + chown -R PUID:PGID /app/data /app/logs
#     用 gosu 降权到 PUID:PGID 后 exec 自己（$0 $@）
#   - 第二次 entry（已经是 PUID:PGID）：跳过 chown，等 Postgres，最后 exec CMD
#
# 这样 bind mount 源目录（./data/helper、./logs）即便被 docker daemon
# 以 root 创建，也会被 entrypoint 自动 chown 给 PUID:PGID，用户不需要
# 手动 mkdir + sudo chown。
# ============================================================
set -euo pipefail

log() { printf '[entrypoint] %s\n' "$*"; }

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# ─────────────────────────────────────────────────────
# 阶段 1：root → chown → gosu 切换
# ─────────────────────────────────────────────────────
if [ "$(id -u)" = "0" ]; then
    # 哪些目录需要 chown：用 env 配置，默认 helper 的 /app/data /app/logs
    # bootstrap 容器走 /workspace/data；其它场景可按需扩展
    CHOWN_DIRS="${CHOWN_DIRS:-/app/data /app/logs}"

    log "以 root 启动；chown 目录 [${CHOWN_DIRS}] → ${PUID}:${PGID}"

    # bind mount 源不存在时 docker 会以 root 创建空目录；
    # 这里兜底 mkdir 一次确保目录在容器视角存在
    for d in $CHOWN_DIRS; do
        mkdir -p "$d"
        chown -R "${PUID}:${PGID}" "$d"
    done

    # /media /downloads 是用户的外部目录，不擅自 chown

    log "降权到 ${PUID}:${PGID}，重新 exec 自己"
    exec gosu "${PUID}:${PGID}" "$0" "$@"
fi

# ─────────────────────────────────────────────────────
# 阶段 2：非 root（PUID:PGID）下的正常启动
# ─────────────────────────────────────────────────────

# 等 Postgres（最长 60s）— bootstrap 不依赖 postgres，但等一下无害
PG_HOST="${POSTGRES_HOST:-postgres}"
PG_PORT="${POSTGRES_PORT:-5432}"
if [ "${SKIP_WAIT_POSTGRES:-0}" != "1" ]; then
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
fi

# 检查 config.yaml 已 bind mount（兼容 helper 的 /app/config.yaml
# 和 bootstrap 的 /workspace/config.yaml；都没找到才报错）
if [ ! -e /app/config.yaml ] && [ ! -e /workspace/config.yaml ]; then
    log "ERROR: config.yaml 未挂载；先 cp config.yaml.example config.yaml"
    exit 1
fi

# 媒体 / 下载目录只警告不退出
for d in /media /downloads; do
    if [ ! -d "$d" ] && [ "${SKIP_MEDIA_CHECK:-0}" != "1" ]; then
        log "WARN: $d 未挂载，相关功能（媒体扫描 / 下载入库）会失效"
    fi
done

log "启动：$*"
exec "$@"
