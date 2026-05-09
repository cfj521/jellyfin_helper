"""
Jellyfin WebSocket 客户端

通过 Jellyfin 自带的 WebSocket（无需任何插件）订阅 LibraryChanged 事件，
事件触发时调用 AdultWatcher 增量扫描受影响的库。

用 asyncio + websockets 库实现。在 FastAPI lifespan 启动一个后台 task，
应用关闭时优雅退出。

启停条件：
  - settings.adult_enabled = True
  - settings.adult_auto_scrape = True
  - settings.jellyfin_api_key 非空
  - settings.adult_library_ids 非空
任一条件不满足就空转（保持 task 但不连）；条件变化时立即响应。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional, Set
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 重连间隔（秒），指数退避
_RECONNECT_DELAYS = [5, 10, 30, 60, 120]


class JellyfinWSClient:
    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._wakeup_event: Optional[asyncio.Event] = None  # settings 变化时唤醒
        self._connected = False
        self._last_event_at: Optional[float] = None
        self._last_error: Optional[str] = None
        self._reconnect_count = 0
        self._enabled_in_loop = False

    # ============================================================
    # 公开接口
    # ============================================================

    def status(self) -> dict:
        from web.backend.config import settings
        return {
            "running": self._task is not None and not self._task.done() if self._task else False,
            "connected": self._connected,
            "enabled_now": self._should_be_enabled(),
            "auto_scrape": settings.adult_auto_scrape,
            "library_count": len(settings.adult_library_ids or []),
            "last_event_at": self._last_event_at,
            "last_error": self._last_error,
            "reconnect_count": self._reconnect_count,
        }

    def start(self, loop: asyncio.AbstractEventLoop):
        """在应用启动时调用一次，传入 asyncio loop。"""
        if self._task and not self._task.done():
            return
        self._loop = loop
        self._stop_event = asyncio.Event()
        self._wakeup_event = asyncio.Event()
        self._task = loop.create_task(self._main_loop(), name="jellyfin-ws")
        logger.info("JellyfinWSClient: 后台 task 已启动")

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
        """settings 变化后调用，让主循环立即重新评估连接状态"""
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

    def _build_ws_url(self) -> str:
        from web.backend.config import settings
        host = settings.jellyfin_host.rstrip('/')
        # http:// → ws://, https:// → wss://
        parsed = urlparse(host)
        scheme = 'wss' if parsed.scheme == 'https' else 'ws'
        netloc = parsed.netloc or parsed.path  # 容错：host 写成 'jellyfin:8096'
        return f"{scheme}://{netloc}/socket?api_key={settings.jellyfin_api_key}&deviceId=jellyfin-tools"

    async def _main_loop(self):
        """长期运行的主循环：根据 settings 决定连接/断开 + 重连退避"""
        try:
            import websockets
        except ImportError:
            logger.error("websockets 库未安装，请 pip install websockets")
            return

        while not self._stop_event.is_set():
            if not self._should_be_enabled():
                # 不该连：等待 wakeup 或 stop
                self._connected = False
                logger.debug("JellyfinWSClient: 暂不需要连接，进入空转")
                await self._wait_for_wakeup_or_stop(timeout=300)
                continue

            try:
                await self._run_one_session(websockets)
                # session 正常结束（服务端关闭），重置退避计数
                self._reconnect_count = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                logger.warning(f"JellyfinWSClient: 连接异常 {e}")

            # 等待重连延迟（或 wakeup 立即重连）
            if self._stop_event.is_set():
                break
            delay = _RECONNECT_DELAYS[min(self._reconnect_count, len(_RECONNECT_DELAYS) - 1)]
            self._reconnect_count += 1
            logger.info(f"JellyfinWSClient: {delay}s 后重连（第 {self._reconnect_count} 次）")
            await self._wait_for_wakeup_or_stop(timeout=delay)

        logger.info("JellyfinWSClient: 主循环退出")
        self._connected = False

    async def _wait_for_wakeup_or_stop(self, timeout: float):
        """等到 timeout 秒，期间任何 wakeup 或 stop 都立即返回"""
        try:
            done, _ = await asyncio.wait(
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

    async def _run_one_session(self, websockets):
        """单次连接会话；正常返回意味着对方关闭，异常会被外层捕获"""
        url = self._build_ws_url()
        logger.info(f"JellyfinWSClient: 连接 {url.split('api_key=')[0]}api_key=***")

        async with websockets.connect(
            url,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
            max_size=10 * 1024 * 1024,  # 10MB（库变更事件可能很大）
        ) as ws:
            self._connected = True
            self._last_error = None
            logger.info("JellyfinWSClient: 已连接，订阅 LibraryChanged")

            # 订阅事件
            await ws.send(json.dumps({"MessageType": "LibraryChanged", "Data": "Subscribe"}))
            # SessionsStart 也订阅（备用，看到用户行为）
            # 这里只订阅库变化

            while not self._stop_event.is_set():
                # 定期检查 settings 是否变化（不再 enabled 就主动断开）
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    # 超时正常，借机检查 settings
                    if not self._should_be_enabled():
                        logger.info("JellyfinWSClient: settings 已变化，主动断开")
                        return
                    continue

                # 同步 handler 里有 DB 写、HTTP 调用等阻塞操作，不能在事件循环上执行；
                # 否则一次堵住 → FastAPI 整个 API 不响应（包括 /docs）
                await asyncio.to_thread(self._handle_message, msg)

        self._connected = False

    def _handle_message(self, raw: str):
        """处理一条 WebSocket 消息"""
        try:
            data = json.loads(raw)
        except Exception:
            return

        msg_type = data.get('MessageType')
        if msg_type == 'LibraryChanged':
            self._on_library_changed(data.get('Data') or {})
        elif msg_type == 'ForceKeepAlive':
            # Jellyfin 心跳，忽略
            pass
        # 其他事件先不处理

    def _on_library_changed(self, payload: dict):
        """LibraryChanged 事件处理：找出受影响的库 → 触发扫描"""
        from web.backend.config import settings
        from web.backend.services.adult_watcher import watcher

        self._last_event_at = time.time()

        # CollectionFolders 字段直接告诉我们受影响的"库"（即顶层 VirtualFolder）
        affected_libs: Set[str] = set()
        for fid in payload.get('CollectionFolders') or []:
            affected_libs.add(fid)
        # FoldersAddedTo / FoldersRemovedFrom 也算
        for fid in payload.get('FoldersAddedTo') or []:
            affected_libs.add(fid)
        for fid in payload.get('FoldersRemovedFrom') or []:
            affected_libs.add(fid)

        # 命中我们关心的库
        configured = set(settings.adult_library_ids or [])
        hit = affected_libs & configured

        items_added = len(payload.get('ItemsAdded') or [])
        items_updated = len(payload.get('ItemsUpdated') or [])
        items_removed = len(payload.get('ItemsRemoved') or [])

        if not hit:
            logger.debug(
                f"JellyfinWS: LibraryChanged 未命中成人库（affected={affected_libs}, "
                f"items+{items_added}/~{items_updated}/-{items_removed}）"
            )
            return

        logger.info(
            f"JellyfinWS: LibraryChanged 命中 {hit}（items+{items_added}/~{items_updated}/-{items_removed}），"
            f"触发增量扫描"
        )
        # 触发扫描（不绕过冷却 → 短时间多次推送会被合并）
        try:
            watcher.trigger_libraries(list(hit), bypass_cooldown=False)
        except Exception:
            logger.exception("JellyfinWS: trigger_libraries 失败")


# 全局实例
client = JellyfinWSClient()
