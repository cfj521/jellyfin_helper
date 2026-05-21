"""
任务进度 pub/sub：把后台 worker 线程（sync）写入的进度，桥接到 SSE 端点（async）。

为什么需要它：
  - 后台 runner（poster_fix / actor_fix / auto_identify 等）都是 sync def，通过
    BackgroundTasks 跑在 FastAPI threadpool 里。
  - SSE 端点是 async def，跑在 event loop 里。
  - 跨线程通信 → 用 thread-safe 的 queue.Queue。
    publish() 由 worker 调用（非阻塞 put_nowait + 满则丢最旧）；
    subscribe() 由 SSE 调用拿到一个独立的 queue；
    SSE 在 event loop 里用 run_in_executor 包一下 q.get(timeout=...) 不阻塞 loop。

通道命名约定：
  task:{id}   — 单任务详情页订阅（每次 update_task_progress / complete_task 推一份完整 snapshot）
  tasks:any   — 任务列表页订阅（任意 task 状态变化时推一个轻量 ping）

队列满时的策略：丢最旧，保最新。
理由：任务进度是"快照流"，旧的没看不算丢失（最新的快照里有更全的进度）；
这跟"事件流"不一样（事件流丢一条就是丢一个动作）。
"""
import logging
import threading
from queue import Queue, Full, Empty
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 每个订阅者的队列上限。32 足够吸收"前端还没读完 → 后端又推了几条"的瞬时积压。
_QUEUE_MAXSIZE = 32

# 终止哨兵：往任务通道里 put None 表示"此任务已终结，可以关 SSE 了"。
TERMINAL_SENTINEL = None


class TaskPubSub:
    """轻量进程内 pub/sub。无外部依赖（不需要 Redis）。"""

    def __init__(self):
        self._lock = threading.Lock()
        # channel -> 订阅者队列列表
        self._channels: Dict[str, List[Queue]] = {}

    def subscribe(self, channel: str) -> Queue:
        """返回一个独立队列，调用方负责消费 + 在结束时调用 unsubscribe。"""
        q: Queue = Queue(maxsize=_QUEUE_MAXSIZE)
        with self._lock:
            self._channels.setdefault(channel, []).append(q)
        return q

    def unsubscribe(self, channel: str, q: Queue) -> None:
        """从订阅者列表移除（队列本身不需要 close，GC 即可）。"""
        with self._lock:
            subs = self._channels.get(channel)
            if not subs:
                return
            try:
                subs.remove(q)
            except ValueError:
                pass
            if not subs:
                self._channels.pop(channel, None)

    def publish(self, channel: str, data: Any) -> None:
        """非阻塞推送。队列满时丢最旧，保最新（progress 是快照流，最新即包含了之前的内容）。"""
        with self._lock:
            # 复制订阅者列表，避免推送过程中持锁太久
            subs = list(self._channels.get(channel, []))
        for q in subs:
            try:
                q.put_nowait(data)
            except Full:
                # 丢最旧，再 put
                try:
                    q.get_nowait()
                except Empty:
                    pass
                try:
                    q.put_nowait(data)
                except Full:
                    # 极小概率：刚 get 完又被别人 put 满了。放弃这条。
                    pass

    def subscriber_count(self, channel: str) -> int:
        """主要给监控/debug 用。"""
        with self._lock:
            return len(self._channels.get(channel, []))


_instance: Optional[TaskPubSub] = None


def get_pubsub() -> TaskPubSub:
    """全局单例。"""
    global _instance
    if _instance is None:
        _instance = TaskPubSub()
    return _instance
