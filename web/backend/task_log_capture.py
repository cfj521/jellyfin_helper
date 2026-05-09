"""
Task 日志捕获

让每个后台 worker 运行期间产生的 WARNING+ 级别日志自动写入 task.result.warnings，
前端任务详情页就能看到刮削站点 403 / SSL EOF 等错误，不必去翻服务器日志。

工作机制：
  - 全局 logging.Handler 监听所有 record
  - 通过 thread-local + ContextVar 双重追踪"当前线程/协程绑定的 task_id"
  - 当 record 产生时，按当前 task_id 关联到对应缓冲区
  - update_task_progress / complete_task 时 attach + drain，无需改 worker
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------- 当前 task 标识：thread-local 主路径 + ContextVar 兜底 async ----------
_thread = threading.local()
try:
    import contextvars
    _ctx_task_id: Optional[contextvars.ContextVar] = contextvars.ContextVar(
        'task_log_current', default=None
    )
except Exception:
    _ctx_task_id = None


def _get_current() -> Optional[int]:
    tid = getattr(_thread, 'task_id', None)
    if tid is not None:
        return tid
    if _ctx_task_id is not None:
        return _ctx_task_id.get()
    return None


def _get_sub_key() -> Optional[str]:
    """当前关联的子项标识（如视频番号），用于前端按子项合并 warnings"""
    return getattr(_thread, 'sub_key', None)


def attach(task_id: int, sub_key: Optional[str] = None) -> None:
    """
    绑定当前线程 / 协程到 task_id（幂等）。

    sub_key（可选）：当前正在处理的子项标识（如视频番号 / 文件名）。
    后续 emit 的 warning 会带上这个 key，前端可据此把同一视频的多条警告合并。
    传 sub_key=None 不修改已有 sub_key（即只更新 task_id）。
    传 sub_key=''（空字符串）显式清除。
    """
    _thread.task_id = task_id
    if sub_key is not None:
        _thread.sub_key = sub_key or None
    if _ctx_task_id is not None:
        try:
            _ctx_task_id.set(task_id)
        except Exception:
            pass


def detach() -> None:
    _thread.task_id = None
    _thread.sub_key = None
    if _ctx_task_id is not None:
        try:
            _ctx_task_id.set(None)
        except Exception:
            pass


@contextmanager
def task_log_scope(task_id: int):
    """显式上下文管理器（线程/异步 worker 都可以用）"""
    prev = getattr(_thread, 'task_id', None)
    attach(task_id)
    try:
        yield
    finally:
        if prev is None:
            detach()
        else:
            attach(prev)


# ---------- 缓冲区 + Handler ----------

_buffers: Dict[int, List[dict]] = {}
_buffer_lock = threading.Lock()
_MAX_PER_TASK = 200  # 单个 task 最多保留多少条，防止爆炸


class _TaskLogHandler(logging.Handler):
    """从 root logger 拦截 WARNING+，关联到当前 task_id 缓冲。"""

    def __init__(self):
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        # 自身递归保护：避免任何 task_log_capture 自己产生的 log 被回收
        if record.name == __name__:
            return
        tid = _get_current()
        if tid is None:
            return
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += '\n' + logging.Formatter().formatException(record.exc_info)
            sub_key = _get_sub_key()
            entry = {
                'ts': int(record.created),
                'level': record.levelname,
                'logger': record.name,
                'msg': msg[:1000],  # 单条上限，防超长
                'sub_key': sub_key,  # 子项标识（如视频番号），前端用于合并展示
            }
            with _buffer_lock:
                buf = _buffers.setdefault(tid, [])
                buf.append(entry)
                if len(buf) > _MAX_PER_TASK:
                    del buf[: len(buf) - _MAX_PER_TASK]
        except Exception:
            pass


_installed = False


def install() -> None:
    """挂到 root logger（main.py 启动时调一次，幂等）"""
    global _installed
    if _installed:
        return
    logging.getLogger().addHandler(_TaskLogHandler())
    _installed = True


# ---------- 给 tasks.py 用的接口 ----------

def peek(task_id: int) -> List[dict]:
    """读取但不清空（运行中部分写入 result 用）"""
    with _buffer_lock:
        return list(_buffers.get(task_id, []))


def drain(task_id: int) -> List[dict]:
    """读取并清空（task 完成时用）"""
    with _buffer_lock:
        return _buffers.pop(task_id, [])


def discard(task_id: int) -> None:
    """无条件丢弃（极少用）"""
    with _buffer_lock:
        _buffers.pop(task_id, None)
