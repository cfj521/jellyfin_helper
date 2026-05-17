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

# 日志落盘：logs/jellyfin-helper.log，rotate 20MB × 10 个 = 最多 200MB 历史
# 这次 !!unorganized 事故就是因为只有 stdout、终端关掉就没证据，必须留盘
_ROOT_DIR = Path(__file__).parent.parent.parent
_LOG_DIR = _ROOT_DIR / 'logs'
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_FILE = _LOG_DIR / 'jellyfin-helper.log'

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


class _SilentPollAccessFilter(logging.Filter):
    """过滤掉 uvicorn.access 中高频轮询端点的日志行（200/304 才过滤；错误状态码保留）。

    uvicorn.access 的 record.args 形如 (client_addr, method, full_path, http_version, status_code)。
    我们用 status_code < 400 + path 前缀匹配判断是否静音。
    路径列表与 diagnostics._SILENT_POLL_PATHS 共用一份，单点维护。
    """
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            from web.backend.diagnostics import _SILENT_POLL_PATHS
            args = record.args
            if not isinstance(args, tuple) or len(args) < 5:
                return True
            full_path = str(args[2] or '')
            # 去掉 query string，按纯 path 前缀匹配
            pure_path = full_path.split('?', 1)[0]
            status_code = int(args[4]) if args[4] is not None else 0
            if status_code < 400 and any(pure_path.startswith(p) for p in _SILENT_POLL_PATHS):
                return False
        except Exception:
            pass
        return True


_uvicorn_access_filter = _SilentPollAccessFilter()


def _patch_uvicorn_loggers():
    """让 uvicorn 自带的 access / default logger 也通过 root 落盘 + 输出到 console。

    uvicorn 默认两个 logger 都自带 StreamHandler 且 propagate=False。
    之前的实现保留了它们自己的 handlers + 打开 propagate → 同一条 access log 被
    输出 3 次（uvicorn 自己 stderr + root file + root console，看起来就是"重复 3 次"）。
    正确做法：**清掉 uvicorn 自己的 handlers**，只通过 root 输出一次。

    同时给 uvicorn.access 挂一个高频轮询端点过滤器，把 200/304 噪音吞掉
    （错误状态码仍然保留）。
    """
    for name in ('uvicorn', 'uvicorn.access', 'uvicorn.error'):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True

    access_lg = logging.getLogger('uvicorn.access')
    # 避免重复挂载（lifespan 启动时会再 patch 一次）
    if _uvicorn_access_filter not in access_lg.filters:
        access_lg.addFilter(_uvicorn_access_filter)


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
from web.backend.api import (
    subtitle, metadata, media, stats, tasks, config_api,
    discover, resourcesearch, downloadpipeline,
    medialibraries, audio, maintenance, ratings, logs as logs_api, dispatch,
    img_proxy, auth,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    import asyncio

    # uvicorn 在 main 模块加载后才会注册自己的 handler，这里再 patch 一次
    # 确保 uvicorn.access / uvicorn.error 的输出也带上时间戳
    _patch_uvicorn_loggers()

    # 提前装信号 handler：SIGTERM/SIGINT 进来就 set shutdown event
    # 关键：得在 lifespan teardown 之前，否则 SSE 长连接死等"Waiting for connections to close"
    try:
        from web.backend.shutdown import install_signal_handlers
        install_signal_handlers()
    except Exception as e:
        logger.warning(f"安装早期 shutdown 信号 handler 失败: {e}")

    # 启动时初始化数据库
    init_db()

    # 同步 config.yaml 中的用户到数据库
    from web.backend.api.auth import sync_users_from_config
    sync_users_from_config()

    # 豆瓣全局熔断参数已在 rate_limiter.SOURCE_CONFIGS 中定义，无需运行时配置
    from common.rate_limiter import DOUBAN_BREAKER_MAX_FAILURES, DOUBAN_BREAKER_COOLDOWN
    logger.info(
        f"豆瓣全局熔断: max_failures={DOUBAN_BREAKER_MAX_FAILURES} "
        f"cooldown={DOUBAN_BREAKER_COOLDOWN}s（rate_limiter 常量）"
    )

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

    # 启动 media_metadata 维护循环（每日 LRU 清理）
    # 跟 dispatch 共用 stop_event，关服时一起退
    try:
        import threading
        from web.backend.services.metadata_maintenance import daily_loop as _meta_daily_loop
        meta_stop = app.state.dispatch_stop_event or threading.Event()
        if app.state.dispatch_stop_event is None:
            app.state.dispatch_stop_event = meta_stop
        t_meta = threading.Thread(
            target=_meta_daily_loop, args=(meta_stop,),
            name='metadata-maintenance', daemon=True,
        )
        t_meta.start()
        app.state.dispatch_threads.append(t_meta)
    except Exception as e:
        logger.exception(f"启动 metadata 维护循环失败: {e}")

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

    # 4. 强退兜底（治"Ctrl+C 关不掉"）
    # 大多数长任务调 update_task_progress 时会抓到 shutdown 信号自行退出，
    # 但有些任务卡在没法响应 cancel 的同步调用里（远端 HTTP 长 timeout、ffprobe 等），
    # uvicorn graceful shutdown 会一直等。给 15s 缓冲后强 exit，OS 直接清线程。
    #
    # 注意：os._exit() 不跑 atexit / 不 flush stdio，是极端但有效的"立刻死"。
    # 跨平台一致：Linux/macOS/Windows 都不再等线程清理。
    import threading
    import os as _os
    def _force_exit_watchdog():
        import time
        time.sleep(15)
        logger.error("[shutdown] graceful exit 超时 15s，强制 os._exit(0)")
        _os._exit(0)
    threading.Thread(target=_force_exit_watchdog, daemon=True).start()


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

# 认证中间件（在诊断之后注册 = 执行时在诊断之前，即最外层拦截）
from web.backend.auth_middleware import AuthMiddleware
app.add_middleware(AuthMiddleware)

# 注册路由
app.include_router(auth.router, prefix="/api/auth", tags=["认证"])
app.include_router(subtitle.router, prefix="/api/subtitle", tags=["字幕管理"])
app.include_router(metadata.router, prefix="/api/metadata", tags=["元数据管理"])
app.include_router(media.router, prefix="/api/media", tags=["媒体库管理"])
app.include_router(stats.router, prefix="/api/stats", tags=["统计分析"])
app.include_router(tasks.router, prefix="/api/tasks", tags=["任务管理"])
app.include_router(config_api.router, prefix="/api", tags=["配置管理"])
app.include_router(discover.router, prefix="/api/discover", tags=["热门推荐"])
app.include_router(resourcesearch.router, prefix="/api/resourcesearch", tags=["资源搜索"])
app.include_router(downloadpipeline.router, prefix="/api/downloadpipeline", tags=["下载流水线（torrent 操作）"])
app.include_router(medialibraries.router, prefix="/api/medialibraries", tags=["媒体库（Jellyfin 直通）"])
app.include_router(audio.router, prefix="/api/audio", tags=["音轨管理"])
app.include_router(maintenance.router, prefix="/api/maintenance", tags=["媒体库维护"])
app.include_router(ratings.router, prefix="/api/ratings", tags=["评分聚合"])
app.include_router(logs_api.router, prefix="/api", tags=["日志查看"])
app.include_router(dispatch.router, prefix="/api/dispatch", tags=["下载入库流水线"])
app.include_router(img_proxy.router, prefix="/api", tags=["图片反代"])

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
    import os
    import uvicorn
    # 端口优先级：环境变量 BACKEND_PORT > config.yaml server.backend_port > 8000
    port = int(os.environ.get('BACKEND_PORT') or settings.backend_port or 8000)
    # reload 仅开发用：环境变量 BACKEND_RELOAD=1 时启用；服务模式默认关闭（避免 watcher 占进程 + 假阻塞）
    reload = os.environ.get('BACKEND_RELOAD', '').lower() in ('1', 'true', 'yes')
    logger.info(f"启动 uvicorn host=0.0.0.0 port={port} reload={reload}")
    uvicorn.run(
        "web.backend.main:app",
        host="0.0.0.0",
        port=port,
        reload=reload,
    )
