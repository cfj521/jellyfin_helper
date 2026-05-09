"""
进程级 graceful shutdown 信号。

用途：长任务（批量刮削 / 修复封面 / 库扫描 / 女优库构建）在循环里
每 iteration 前 check 一次 `is_shutting_down()`，命中就 break + 写完结状态退出。
uvicorn `--reload` / Ctrl+C 时 FastAPI lifespan shutdown 阶段会调 `request_shutdown()`。

设计原则：
  - 全局单例 threading.Event（线程安全，跨 BackgroundTask / threadpool / daemon thread 共用）
  - 调用方自己负责检测，不强制中断（避免 ORM session 半截关闭等麻烦）
  - 幂等：重复调 set / 多任务都能感知
"""
import logging
import threading

logger = logging.getLogger(__name__)

_shutdown_event = threading.Event()


def request_shutdown() -> None:
    """由 FastAPI lifespan shutdown 阶段调用。幂等。"""
    if not _shutdown_event.is_set():
        logger.info("Shutdown 信号已发出 —— 长任务应当尽快退出")
        _shutdown_event.set()
        # 顺手让常驻服务也响应（actress builder 有自己的 stop_event；其他线程靠 is_shutting_down 自检）
        try:
            from web.backend.services.actress_builder import builder
            builder.stop()
        except Exception:
            pass


def is_shutting_down() -> bool:
    return _shutdown_event.is_set()


def reset_for_tests() -> None:
    """仅测试用：清掉信号好让 fixtures 复用。"""
    _shutdown_event.clear()
