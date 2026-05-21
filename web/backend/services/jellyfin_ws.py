"""
Jellyfin 库变更轮询客户端（原 WebSocket 客户端，Jellyfin 10.11 改造后改 polling）

为什么不用 WebSocket（实测发现的死结）：
  Jellyfin 10.11+ 用 `api_key` 直连 WebSocket 时**不会**收到 `LibraryChanged` 推送 ——
  这类推送只发给「真实 user session」(必须 access_token + UserId)。
  我们持续 30+ 次 LibraryChanged 订阅，0 次收到事件。Jellyfin 端日志反向印证：
      "Sending ForceKeepAlive message to 1 inactive WebSockets."
      "Lost 1 WebSockets"
  我们的 WS 连接稳定但永远是 "inactive"，被排除在 admin broadcast 列表外。
  详见: SessionMessageType enum + SessionManager.SendMessageToAdminSessionsAsync

改 polling 后的行为：
  每 N 秒（默认 60s）触发一次 watcher.trigger_libraries(adult_library_ids)。
  watcher 自带 5min library cooldown，所以大部分轮询会被合并掉 ——
  实际"真正发起扫描"频率 ≈ 每 5min 1 次，跟 WS 推送场景的体感几乎一致。

公开 API 保持跟旧 WS client 完全一致（main.py / config_api.py / adult_watcher.py 不用改）：
  - start(loop)
  - stop()
  - notify_settings_changed()
  - status() —— 字段名沿用旧的，"connected" 现在表示 polling task 是否激活
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


# 轮询间隔（秒）。watcher 自带 5min 冷却，所以轮询频率高也不重复扫；
# 设短一点让新文件发现延迟更小（首次出现 → 最长等 POLL_INTERVAL + 扫描时长）
POLL_INTERVAL_SEC = 60


class JellyfinWSClient:
    """名字保留 WSClient 是为了不改 main.py 启动入口；内部实际是 polling worker。"""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._wakeup_event: Optional[asyncio.Event] = None  # settings 变化时唤醒
        self._connected = False
        self._last_event_at: Optional[float] = None  # 最近一次"实际触发了扫描"的时刻
        self._last_poll_at: Optional[float] = None   # 最近一次 poll tick（即使被冷却跳过也算）
        self._last_error: Optional[str] = None
        self._poll_count = 0
        self._scan_trigger_count = 0

    # ============================================================
    # 公开接口（必须跟旧 WS client 保持兼容）
    # ============================================================

    def status(self) -> dict:
        from web.backend.config import settings
        return {
            "running": self._task is not None and not self._task.done() if self._task else False,
            # 'connected' 字段：原 WS 时代是真 WS 连接状态；polling 模式下表示 "polling task 在跑"
            "connected": self._connected,
            "enabled_now": self._should_be_enabled(),
            "auto_scrape": settings.adult_auto_scrape,
            "library_count": len(settings.adult_library_ids or []),
            "last_event_at": self._last_event_at,
            "last_poll_at": self._last_poll_at,
            "last_error": self._last_error,
            # 'reconnect_count' 字段保留兼容；polling 没有"重连"概念，复用为"轮询次数"
            "reconnect_count": self._poll_count,
            # 实际触发的扫描次数（被 cooldown 跳过的不算）
            "scan_trigger_count": self._scan_trigger_count,
            # 新增：透明告知前端这是 polling 模式
            "mode": "polling",
            "poll_interval_sec": POLL_INTERVAL_SEC,
        }

    def start(self, loop: asyncio.AbstractEventLoop):
        """在应用启动时调用一次，传入 asyncio loop。"""
        if self._task and not self._task.done():
            return
        self._loop = loop
        self._stop_event = asyncio.Event()
        self._wakeup_event = asyncio.Event()
        self._task = loop.create_task(self._main_loop(), name="jellyfin-change-poller")
        logger.info(
            f"JellyfinWSClient: 后台 task 已启动（polling 模式，间隔 {POLL_INTERVAL_SEC}s）"
        )

    def stop(self):
        """关闭客户端"""
        if self._stop_event:
            try:
                self._loop.call_soon_threadsafe(self._stop_event.set)
            except Exception:
                pass
        if self._wakeup_event:
            try:
                self._loop.call_soon_threadsafe(self._wakeup_event.set)
            except Exception:
                pass

    def notify_settings_changed(self):
        """settings 变化后调用，让主循环立即重新评估启停状态 + 立刻 poll 一次"""
        if self._wakeup_event and self._loop:
            try:
                self._loop.call_soon_threadsafe(self._wakeup_event.set)
            except Exception:
                pass

    # ============================================================
    # 内部
    # ============================================================

    def _should_be_enabled(self) -> bool:
        from web.backend.config import settings
        return bool(
            settings.adult_enabled
            and settings.adult_auto_scrape
            and settings.jellyfin_api_key
            and settings.adult_library_ids
        )

    async def _main_loop(self):
        """polling 主循环：每 POLL_INTERVAL_SEC 触发一次 trigger_libraries"""
        logger.info("JellyfinWSClient: polling loop 启动")
        try:
            while not self._stop_event.is_set():
                if not self._should_be_enabled():
                    # 不该跑：标记 connected=False，等 settings 唤醒或 stop
                    if self._connected:
                        logger.info("JellyfinWSClient: 启停条件不满足，进入空转")
                    self._connected = False
                    await self._wait_for_wakeup_or_stop(timeout=300)
                    continue

                self._connected = True
                self._poll_count += 1
                self._last_poll_at = time.time()

                # 真正干活在另一个线程（watcher.trigger_libraries 会启 thread 扫，
                # 但 trigger_libraries 本身的状态机锁是同步的，到 to_thread 防 event loop 阻塞）
                try:
                    scheduled = await asyncio.to_thread(self._do_poll)
                    if scheduled:
                        self._last_event_at = time.time()
                        self._scan_trigger_count += len(scheduled)
                        logger.info(
                            f"JellyfinWS polling: 触发扫描 {len(scheduled)} 个库 "
                            f"{list(scheduled.keys())}（poll #{self._poll_count}）"
                        )
                    self._last_error = None
                except Exception as e:
                    self._last_error = f"{type(e).__name__}: {e}"
                    logger.warning(f"JellyfinWSClient: polling 异常 {e}")

                # 等下一个 poll tick（或 settings 唤醒 / stop）
                await self._wait_for_wakeup_or_stop(timeout=POLL_INTERVAL_SEC)
        finally:
            self._connected = False
            logger.info("JellyfinWSClient: polling loop 退出")

    def _do_poll(self):
        """同步：调 watcher.trigger_libraries —— 已扫过的库会被 cooldown 跳过"""
        from web.backend.config import settings
        from web.backend.services.adult_watcher import watcher

        lib_ids = list(settings.adult_library_ids or [])
        if not lib_ids:
            return {}
        # bypass_cooldown=False：watcher 自家 5min 冷却生效，避免每分钟空转 stat 整库
        return watcher.trigger_libraries(lib_ids, bypass_cooldown=False)

    async def _wait_for_wakeup_or_stop(self, timeout: float):
        """等到 timeout 秒，期间任何 wakeup 或 stop 都立即返回"""
        try:
            await asyncio.wait(
                [
                    asyncio.create_task(self._stop_event.wait()),
                    asyncio.create_task(self._wakeup_event.wait()),
                ],
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            # 清掉 wakeup 标志，准备下次
            if self._wakeup_event.is_set():
                self._wakeup_event.clear()
        except asyncio.CancelledError:
            raise


# 全局实例
client = JellyfinWSClient()
