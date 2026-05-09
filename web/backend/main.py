"""
Jellyfin Tools Web API
FastAPI 后端服务
"""
import sys
import logging
import logging.handlers
import traceback
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

_LOG_FORMAT = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'

# 日志落盘：data/logs/backend.log，rotate 20MB × 10 个 = 最多 200MB 历史
# 这次 !!unorganized 事故就是因为只有 stdout、终端关掉就没证据，必须留盘
_ROOT_DIR = Path(__file__).parent.parent.parent
_LOG_DIR = _ROOT_DIR / 'data' / 'logs'
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_FILE = _LOG_DIR / 'backend.log'

_file_handler = logging.handlers.RotatingFileHandler(
    _LOG_FILE,
    maxBytes=20 * 1024 * 1024,  # 20 MB
    backupCount=10,
    encoding='utf-8',
)
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_file_handler.setLevel(logging.INFO)

_console_handler = logging.StreamHandler(sys.stderr)
_console_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_console_handler.setLevel(logging.INFO)

# basicConfig 只在 root 没 handler 时生效；显式配置 root logger 拿到两个 handler
_root = logging.getLogger()
_root.setLevel(logging.INFO)
# 清空可能已经被其它 import（如某些 SDK）装的 handler，避免重复输出
_root.handlers.clear()
_root.addHandler(_file_handler)
_root.addHandler(_console_handler)

logger = logging.getLogger(__name__)
logger.info(f"日志落盘启用: {_LOG_FILE} (rotate 20MB × 10)")


def _patch_uvicorn_loggers():
    """让 uvicorn 自带的 access / default logger 也通过 root 落盘 + 输出到 console。

    uvicorn 默认两个 logger 都自带 StreamHandler 且 propagate=False。
    之前的实现保留了它们自己的 handlers + 打开 propagate → 同一条 access log 被
    输出 3 次（uvicorn 自己 stderr + root file + root console，看起来就是"重复 3 次"）。
    正确做法：**清掉 uvicorn 自己的 handlers**，只通过 root 输出一次。
    """
    for name in ('uvicorn', 'uvicorn.access', 'uvicorn.error'):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True


_patch_uvicorn_loggers()

# 添加项目根目录到路径
ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

# 安装 task 日志捕获 handler：每个 worker 运行期间产生的 WARNING+ 日志
# 会自动写入对应 task.result.warnings，前端任务详情页可见。
# 必须在 sys.path 添加之后、其他后端模块 import 之前调用，确保 handler 装好就生效
from web.backend.task_log_capture import install as _install_task_log_capture
_install_task_log_capture()

from web.backend.config import settings
from web.backend.database import init_db
from web.backend.api import subtitle, metadata, media, stats, tasks, config_api, discover, jellyfin, audio, maintenance, ratings, logs as logs_api, dispatch


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    import asyncio

    # uvicorn 在 main 模块加载后才会注册自己的 handler，这里再 patch 一次
    # 确保 uvicorn.access / uvicorn.error 的输出也带上时间戳
    _patch_uvicorn_loggers()

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

    # 启动下载入库流水线（dispatch.enabled 才挂起）
    # scheduler 内部 spawn 全部 6 个 worker（analyzer / downloader-watcher / dispatch-pipeline
    # / jellyfin-watcher / post-process / sweeper），main.py 只起 scheduler + qb 指标轮询即可
    app.state.dispatch_stop_event = None
    app.state.dispatch_threads = []
    try:
        if settings.dispatch.enabled:
            import threading
            from tools.dispatch.poll import run_poll_loop
            from tools.dispatch.scheduler import run_scheduler_loop

            stop_event = threading.Event()
            app.state.dispatch_stop_event = stop_event

            t_poll = threading.Thread(target=run_poll_loop, args=(stop_event,),
                                      name='qb-poll', daemon=True)
            t_sched = threading.Thread(target=run_scheduler_loop, args=(stop_event,),
                                       name='dispatch-scheduler', daemon=True)
            for t in (t_poll, t_sched):
                t.start()
                app.state.dispatch_threads.append(t)
            logger.info("下载入库流水线已启动: qb-poll + scheduler（6 个 worker 由 scheduler 接管）")
    except Exception as e:
        logger.exception(f"启动 dispatch 流水线失败: {e}")

    yield

    # ---- 关闭 ----
    # 1. 先发出全局 shutdown 信号，让长任务 graceful 退出
    try:
        from web.backend.shutdown import request_shutdown
        request_shutdown()
    except Exception:
        pass

    # 2. 停 dispatch 流水线（让 worker 退循环）
    try:
        if app.state.dispatch_stop_event is not None:
            app.state.dispatch_stop_event.set()
    except Exception:
        pass

    # 3. 停 WebSocket
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

# 诊断中间件 + DB 池监控（性能问题排查用）
# 排查完后想关日志：把 logger 'diag' 调到 WARNING+ 即可（保留卡顿告警，去掉正常请求行）
from web.backend.diagnostics import TimingMiddleware, install_pool_monitoring
app.add_middleware(TimingMiddleware)
install_pool_monitoring()

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
app.include_router(logs_api.router, prefix="/api", tags=["日志查看"])
app.include_router(dispatch.router, prefix="/api/dispatch", tags=["下载入库流水线"])

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
def health_check():
    """健康检查"""
    return {"status": "ok", "version": "1.0.0"}


@app.get("/api/config")
def get_config():
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
