"""
Jellyfin Tools Web API
FastAPI 后端服务
"""
import sys
import logging
import traceback
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)

# 添加项目根目录到路径
ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from web.backend.config import settings
from web.backend.database import init_db
from web.backend.api import subtitle, metadata, media, stats, tasks, config_api, discover, jellyfin, audio, maintenance, ratings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    import asyncio

    # 启动时初始化数据库
    init_db()

    # 处理上次未完成的孤儿任务（必须在 API 路由 import 完成、注册表填充后才调用）
    try:
        from web.backend.task_restart import restart_orphan_tasks
        restart_orphan_tasks()
    except Exception as e:
        logger.exception(f"处理孤儿任务时出错: {e}")

    # 初始化 watcher 已知库列表（防止启动后被误判为"新增"）
    try:
        from web.backend.services.adult_watcher import watcher
        watcher.init_known_libraries()
    except Exception as e:
        logger.warning(f"初始化 AdultWatcher 失败: {e}")

    # 启动 Jellyfin WebSocket 客户端（仅在条件满足时实际连接）
    try:
        from web.backend.services.jellyfin_ws import client as ws_client
        ws_client.start(asyncio.get_event_loop())
    except Exception as e:
        logger.warning(f"启动 JellyfinWSClient 失败: {e}")

    yield

    # 关闭
    try:
        from web.backend.services.jellyfin_ws import client as ws_client
        ws_client.stop()
    except Exception:
        pass


app = FastAPI(
    title="Jellyfin Tools",
    description="Jellyfin 媒体服务器工具集 Web API",
    version="1.0.0",
    lifespan=lifespan
)

# CORS 配置
# allow_origins=["*"] 与 allow_credentials=True 不能共存，需按情况切换
_use_credentials = settings.cors_origins != ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=_use_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(subtitle.router, prefix="/api/subtitle", tags=["字幕管理"])
app.include_router(metadata.router, prefix="/api/metadata", tags=["元数据管理"])
app.include_router(media.router, prefix="/api/media", tags=["媒体库管理"])
app.include_router(stats.router, prefix="/api/stats", tags=["统计分析"])
app.include_router(tasks.router, prefix="/api/tasks", tags=["任务管理"])
app.include_router(config_api.router, prefix="/api", tags=["配置管理"])
app.include_router(discover.router, prefix="/api/discover", tags=["内容推荐与下载"])
app.include_router(jellyfin.router, prefix="/api/jellyfin", tags=["Jellyfin 直通"])
app.include_router(audio.router, prefix="/api/audio", tags=["音轨管理"])
app.include_router(maintenance.router, prefix="/api/maintenance", tags=["媒体库维护"])
app.include_router(ratings.router, prefix="/api/ratings", tags=["评分聚合"])

# 成人内容仅在配置开启时挂载
if settings.adult_enabled:
    from web.backend.api import adult
    app.include_router(adult.router, prefix="/api/adult", tags=["成人内容"])


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """统一异常处理：未捕获异常返回 500 + JSON 错误信息"""
    logger.exception(f"未处理异常 [{request.method} {request.url.path}]")
    return JSONResponse(
        status_code=500,
        content={
            "detail": str(exc),
            "type": exc.__class__.__name__,
        },
    )


@app.get("/api/health")
async def health_check():
    """健康检查"""
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/config")
async def get_config():
    """获取配置信息（脱敏）"""
    return {
        "jellyfin": {
            "host": settings.jellyfin_host,
            "connected": bool(settings.jellyfin_api_key)
        },
        "tmdb": {
            "connected": bool(settings.tmdb_api_key)
        },
        "opensubtitles": {
            "connected": bool(settings.opensubtitles_api_key)
        },
        "jackett": {
            "host": settings.jackett_host,
            "connected": bool(settings.jackett_api_key)
        },
        "qbittorrent": {
            "host": settings.qbittorrent_host,
            "connected": bool(settings.qbittorrent_username)
        }
    }


# ============================================================
# 前端 SPA 静态托管
# 当 web/frontend/dist 存在时（npm run build 产物），FastAPI 直接托管前端。
# 开发模式下 dist 不存在，跳过即可（前端走 vite dev server）。
# ============================================================
class SPAStaticFiles(StaticFiles):
    """对未找到的前端路径返回 index.html，让 Vue Router 接管。
    /api/* 路径不做 fallback，保持原 404 让 API 调用方正确感知错误。

    注意 Windows 下 path 会用反斜杠 (\\)，需要同时兼容。
    """

    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except (HTTPException, StarletteHTTPException) as ex:
            normalized = path.replace('\\', '/')
            if ex.status_code == 404 and not normalized.startswith('api/') and normalized != 'api':
                return await super().get_response("index.html", scope)
            raise


_FRONTEND_DIST = ROOT_DIR / "web" / "frontend" / "dist"
if _FRONTEND_DIST.exists():
    # mount("/") 必须放在所有 /api/* 路由之后，否则会拦截
    app.mount("/", SPAStaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")
    logger.info(f"前端 SPA 已挂载：{_FRONTEND_DIST}")
else:
    logger.info("前端 dist 不存在，跳过 SPA 挂载（开发模式请用 vite dev server）")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "web.backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True
    )
