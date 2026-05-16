"""
诊断 / 性能日志工具。

业务方按需 import 用 timed() 包关键路径；HTTP middleware 和 DB pool 监控由 main.py 在启动时挂上。
日志全部走 logger 'diag'，可以在 logging 配置里单独调级别。
"""
import logging
import threading
import time
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger('diag')

# 通用阈值：超过这个毫秒数升级到 WARNING
DEFAULT_SLOW_MS = 500
HTTP_SLOW_MS = 1000           # 外部 HTTP 调用阈值（Jellyfin / scraper / 等）
POOL_HOLD_SLOW_MS = 500       # DB 连接持有时长阈值

# 高频轮询端点：默认级别下静音常规请求行，仅在 SLOW 时打 WARNING。
# 命中规则：path 前缀匹配（含查询参数前的纯 path）。改 -> 重启后端生效。
_SILENT_POLL_PATHS = (
    '/api/downloadpipeline/downloads',
    '/api/downloadpipeline/transfer-info',
    '/api/dispatch/quota',
    '/api/adult/actresses/build/status',
)


def _is_silent_poll_path(path: str) -> bool:
    return any(path.startswith(p) for p in _SILENT_POLL_PATHS)


@contextmanager
def timed(label: str, slow_ms: int = DEFAULT_SLOW_MS, extra: Optional[dict] = None):
    """
    计时上下文管理器：
        with timed('list_items query'):
            items = db.query(...).all()
    超过 slow_ms 升级到 WARNING；正常 INFO。
    extra 给结构化日志额外字段。
    """
    t0 = time.monotonic()
    err = None
    try:
        yield
    except Exception as e:
        err = e
        raise
    finally:
        elapsed = (time.monotonic() - t0) * 1000
        if err:
            logger.warning(f'[diag] {label} = {elapsed:.0f}ms FAIL ({err.__class__.__name__})')
        elif elapsed > slow_ms:
            logger.warning(f'[diag] SLOW {label} = {elapsed:.0f}ms')
        else:
            logger.info(f'[diag] {label} = {elapsed:.0f}ms')


def log_pool_state(label: str = ''):
    """
    打印当前 SQLAlchemy 连接池状态，方便看是不是池满了。
    用法：在怀疑卡的地方手工调一下；或在中间件按需打。
    """
    try:
        from web.backend.database import engine
        pool = engine.pool
        logger.warning(
            f'[diag] pool {label}: size={pool.size()} '
            f'checked_out={pool.checkedout()} '
            f'overflow={pool.overflow()} '
            f'checked_in={pool.checkedin()}'
        )
    except Exception as e:
        logger.warning(f'[diag] log_pool_state 失败: {e}')


# ========== DB 池占用监控（SQLAlchemy events）==========

# 每个连接 checkout 时记下时间戳（key=id(conn_record)），checkin 时算耗时
_checkout_times = {}
_checkout_lock = threading.Lock()


def install_pool_monitoring():
    """
    在 SQLAlchemy engine 上挂 checkout/checkin event listener：
      - checkout 时记录时间戳
      - checkin 时算"该连接被占用多久"，超过 POOL_HOLD_SLOW_MS 打 WARNING
      - 每次 checkout 也顺手报一下当前池占用，>80% 时 WARNING

    main.py 启动时调一次。
    """
    from sqlalchemy import event
    from web.backend.database import engine

    @event.listens_for(engine, "checkout")
    def _on_checkout(dbapi_conn, conn_record, conn_proxy):
        with _checkout_lock:
            _checkout_times[id(conn_record)] = time.monotonic()
        # 池占用率高时告警
        try:
            pool = engine.pool
            checked_out = pool.checkedout()
            total_capacity = pool.size() + pool._max_overflow if hasattr(pool, '_max_overflow') else pool.size()
            if total_capacity > 0 and checked_out / total_capacity > 0.8:
                logger.warning(
                    f'[diag] pool 占用 {checked_out}/{total_capacity} '
                    f'(overflow={pool.overflow()}) — 池快满了！'
                )
        except Exception:
            pass

    @event.listens_for(engine, "checkin")
    def _on_checkin(dbapi_conn, conn_record):
        key = id(conn_record)
        with _checkout_lock:
            t0 = _checkout_times.pop(key, None)
        if t0 is not None:
            elapsed = (time.monotonic() - t0) * 1000
            if elapsed > POOL_HOLD_SLOW_MS:
                # 谁握了这么久？打个 stack trace 提示，让排查时容易看
                import traceback
                stack = ''.join(traceback.format_stack()[-6:-1])  # checkin 的最近 5 帧
                logger.warning(
                    f'[diag] DB 连接被持有 {elapsed:.0f}ms 才归还\n  调用栈:\n{stack}'
                )

    logger.info('[diag] DB 连接池监控已挂载')


# ========== HTTP 请求耗时中间件 ==========

class TimingMiddleware:
    """
    ASGI 风格中间件：每个 HTTP 请求记录耗时。
    >500ms WARNING；正常 INFO（debug 时可调高 logger 级别看全部）。
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return

        method = scope.get('method', '')
        path = scope.get('path', '')
        t0 = time.monotonic()
        status_code = 0

        async def _send(message):
            nonlocal status_code
            if message.get('type') == 'http.response.start':
                status_code = message.get('status', 0)
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            elapsed = (time.monotonic() - t0) * 1000
            if elapsed > DEFAULT_SLOW_MS:
                logger.warning(
                    f'[diag] HTTP SLOW {method} {path} → {status_code} = {elapsed:.0f}ms'
                )
            elif _is_silent_poll_path(path):
                # 高频轮询端点：正常情况下不打，避免日志被淹没
                pass
            else:
                logger.info(
                    f'[diag] HTTP {method} {path} → {status_code} = {elapsed:.0f}ms'
                )
