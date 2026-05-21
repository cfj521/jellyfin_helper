"""
进程级 graceful shutdown 信号。

用途：长任务（批量刮削 / 修复封面 / 库扫描 / 女优库构建 / SSE 流）在循环里
每 iteration 前 check 一次 `is_shutting_down()`，命中就 break + 写完结状态退出。

何时被 set：
  ① **信号 handler 早期触发**（install_signal_handlers 在 lifespan startup 装上）
     —— uvicorn --reload / Ctrl+C 一进来就 set。这条路径关键：lifespan teardown
     要等所有 HTTP 连接关闭才跑（见 ② 注释），SSE 这类长连接没有信号 handler
     提前 set 会死锁在 "Waiting for connections to close"
  ② **FastAPI lifespan teardown** 兜底 —— 信号 handler 没装上（如测试）时也能触发

设计原则：
  - 全局单例 threading.Event（线程安全，跨 BackgroundTask / threadpool / daemon thread 共用）
  - 调用方自己负责检测，不强制中断（避免 ORM session 半截关闭等麻烦）
  - 幂等：重复调 set / 多任务都能感知
"""
import logging
import signal
import threading

logger = logging.getLogger(__name__)

_shutdown_event = threading.Event()
_signal_installed = False


def request_shutdown() -> None:
    """由信号 handler / FastAPI lifespan shutdown 调用。幂等。"""
    if not _shutdown_event.is_set():
        logger.info("Shutdown 信号已发出 —— 长任务应当尽快退出")
        _shutdown_event.set()
        # 顺手让常驻服务也响应（actress builder 有自己的 stop_event；其他线程靠 is_shutting_down 自检）
        try:
            from backend.services.actress_builder import builder
            builder.stop()
        except Exception:
            pass


def is_shutting_down() -> bool:
    return _shutdown_event.is_set()


def install_signal_handlers() -> None:
    """**早期**信号 handler：SIGTERM/SIGINT/SIGBREAK 一进来就 request_shutdown()，再 chain 给 uvicorn。

    为什么需要：uvicorn 优雅关闭流程是
        1. 收 signal → 标记 shutting_down
        2. "Waiting for connections to close" 等所有 HTTP 连接结束
        3. 跑 FastAPI lifespan teardown（这里才能调 request_shutdown）
        4. 退进程
    SSE 这类长连接在 (2) 永远不退 → 死锁。提前到 (1) 用信号 handler 直接 set 事件，
    SSE 生成器轮询 is_shutting_down() 即可主动退出，让 (2) 顺利进入 (3)。

    幂等；chain 到 uvicorn 原 handler，不破坏它自己的 shutdown 流程。
    """
    global _signal_installed
    if _signal_installed:
        return
    _signal_installed = True

    def _make_handler(prior):
        def _on_signal(sig, frame):
            request_shutdown()
            if callable(prior) and prior not in (signal.SIG_DFL, signal.SIG_IGN):
                try:
                    prior(sig, frame)
                except Exception:
                    logger.exception("链回 uvicorn 信号 handler 时异常")
        return _on_signal

    # SIGTERM（reload 杀子进程）/ SIGINT（Ctrl+C）/ SIGBREAK（Windows 上 Ctrl+Break）
    for sig_name in ('SIGTERM', 'SIGINT', 'SIGBREAK'):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            prior = signal.getsignal(sig)
            signal.signal(sig, _make_handler(prior))
        except (ValueError, OSError):
            # 非主线程注册会 ValueError；非阻塞失败
            pass


def reset_for_tests() -> None:
    """仅测试用：清掉信号好让 fixtures 复用。"""
    _shutdown_event.clear()
