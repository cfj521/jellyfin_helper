"""
Jellyfin 库变更轮询器（JellyfinPoller）。

工作原理：每 settings.adult_poll_interval_min 分钟触发 watcher.poll_libraries(...)。
       自身只负责"按周期把活派出去"，不感知 watcher / scanner 的内部状态。

启停由 settings 控制（adult_enabled / adult_auto_scrape / jellyfin_api_key / adult_library_ids
四者全真才启动），config 热重载会通过 notify_settings_changed() 立即唤醒主循环重判。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class JellyfinPoller:
    """周期触发器。模块级单例 `client = JellyfinPoller()`。"""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._wakeup_event: Optional[asyncio.Event] = None  # settings 变化时唤醒
        self._active = False  # poll loop 是否在工作状态（启停条件满足）
        self._last_event_at: Optional[float] = None  # 最近一次"实际派出活"的时刻
        self._last_poll_at: Optional[float] = None   # 最近一次 poll tick
        self._last_error: Optional[str] = None
        self._poll_count = 0
        self._trigger_count = 0  # 实际触发 watcher 的次数

    # ============================================================
    # 公开接口
    # ============================================================

    def status(self) -> dict:
        from backend.config import settings
        return {
            "running": (
                self._task is not None and not self._task.done() if self._task else False
            ),
            "active": self._active,           # 启停条件满足 + poll loop 在工作
            "enabled_now": self._should_be_enabled(),
            "auto_scrape": settings.adult_auto_scrape,
            "library_count": len(settings.adult_library_ids or []),
            "last_event_at": self._last_event_at,
            "last_poll_at": self._last_poll_at,
            "last_error": self._last_error,
            "poll_count": self._poll_count,
            "trigger_count": self._trigger_count,
            "poll_interval_min": settings.adult_poll_interval_min,
        }

    def start(self, loop: asyncio.AbstractEventLoop):
        """在应用启动时调用一次，传入 asyncio loop。"""
        from backend.config import settings
        if self._task and not self._task.done():
            return
        self._loop = loop
        self._stop_event = asyncio.Event()
        self._wakeup_event = asyncio.Event()
        self._task = loop.create_task(self._main_loop(), name="jellyfin-poller")
        logger.info(
            f"JellyfinPoller: 后台 task 已启动（间隔 {settings.adult_poll_interval_min} 分钟）"
        )

    def stop(self):
        """关闭"""
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
        from backend.config import settings
        return bool(
            settings.adult_enabled
            and settings.adult_auto_scrape
            and settings.jellyfin_api_key
            and settings.adult_library_ids
        )

    async def _main_loop(self):
        """主循环：每 settings.adult_poll_interval_min 分钟触发一次 watcher.poll_libraries"""
        from backend.config import settings
        logger.info("JellyfinPoller: 主循环启动")
        try:
            while not self._stop_event.is_set():
                if not self._should_be_enabled():
                    # 不该跑：标记 active=False，等 settings 唤醒或 stop
                    if self._active:
                        logger.info("JellyfinPoller: 启停条件不满足，进入空转")
                    self._active = False
                    await self._wait_for_wakeup_or_stop(timeout=300)
                    continue

                self._active = True
                self._poll_count += 1
                self._last_poll_at = time.time()

                # 派活到线程，避免 watcher 内部的同步 DB / HTTP 阻塞 event loop
                try:
                    results = await asyncio.to_thread(self._do_poll)
                    if results:
                        self._last_event_at = time.time()
                        self._trigger_count += len(results)
                        # 实际有变更的库才打 INFO（watcher 内部也会打详情）
                        changed_libs = [
                            lid for lid, s in results.items()
                            if (s.get('new', 0) + s.get('updated', 0) + s.get('moved', 0)) > 0
                        ]
                        if changed_libs:
                            logger.info(
                                f"JellyfinPoller: poll #{self._poll_count} 实际处理 "
                                f"{len(results)} 库，其中 {len(changed_libs)} 库有新变更"
                            )
                    self._last_error = None
                except Exception as e:
                    self._last_error = f"{type(e).__name__}: {e}"
                    logger.warning(f"JellyfinPoller: poll 异常 {e}")

                # 等下一个 poll tick（或 settings 唤醒 / stop）
                # 每次循环都重新读 settings，支持热重载改间隔立即生效
                await self._wait_for_wakeup_or_stop(
                    timeout=settings.adult_poll_interval_min * 60
                )
        finally:
            self._active = False
            logger.info("JellyfinPoller: 主循环退出")

    def _do_poll(self):
        """同步：调 watcher.poll_libraries 跑增量。

        不再触发 scanner 的全库 rglob —— scanner 只给"用户手动扫描"和"新库初始化"用。
        watcher 通过 Jellyfin /Items?MinDateLastSaved 拿增量，开销 ~= 新增 item 数。
        """
        from backend.config import settings
        from backend.services.adult_watcher import watcher

        lib_ids = list(settings.adult_library_ids or [])
        if not lib_ids:
            return {}
        return watcher.poll_libraries(lib_ids)

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
client = JellyfinPoller()
