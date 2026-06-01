# syntax=docker/dockerfile:1.7
# ============================================================
# Stage 1: 前端构建（Vue 3 + Vite → dist/）
# ============================================================
FROM node:20-alpine AS frontend-builder

WORKDIR /build

# 先拷 manifest 利用 layer 缓存
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund

# 再拷源码做构建（config.yaml 不存在时 vite 用默认端口 fallback，对静态产物无影响）
COPY frontend/ ./
RUN npm run build

# ============================================================
# Stage 2: 运行时（Python 3.12 + FastAPI + 系统工具）
# ============================================================
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

# 系统依赖：
#   ffmpeg     - 字幕内嵌探测 / 转码
#   mkvtoolnix - MKV 音轨调整（mkvpropedit / mkvmerge）
#   libpq5     - psycopg2-binary 运行时
#   libarchive-tools - bsdtar，支持 RAR5 解压（字幕源用）
#   tini       - PID 1 信号转发，避免僵尸进程
#   curl       - HEALTHCHECK 用
#   tzdata     - 时区
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        mkvtoolnix \
        libpq5 \
        libarchive-tools \
        tini \
        curl \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装 Python 依赖（独立 layer，requirements 不变就命中缓存）
COPY requirements.txt ./
RUN pip install -r requirements.txt

# 后端 / 共享模块 / 业务工具 / 版本号
COPY backend/  ./backend/
COPY common/   ./common/
COPY tools/    ./tools/
COPY src/      ./src/
COPY VERSION   ./VERSION

# 前端构建产物
COPY --from=frontend-builder /build/dist ./frontend/dist

# 入口脚本（等 postgres ready、PUID/PGID 调整）
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8099/api/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "backend/run.py"]
