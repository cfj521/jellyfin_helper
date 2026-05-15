"""
media_metadata 表的后台维护任务：
  - cleanup_lru()         —— LRU 清理：删 last_seen_at 超 lru_keep_days 的行
  - refresh stale worker  —— 后台异步 refresh stale 行（保留 last_seen_at 不动）
  - daily_loop()          —— 每天跑一次 cleanup_lru（由 main.py spawn 后台 daemon 线程）

策略详见 docs/2026-05-15-media-metadata-store.md §4.3
"""
import logging
import threading
import time
from datetime import datetime, timedelta
from queue import Empty, Queue
from typing import Optional, Tuple

from sqlalchemy import text

from web.backend.config import settings
from web.backend.database import MediaMetadata, engine
from web.backend.services import metadata_store

logger = logging.getLogger(__name__)


# ============================================================
# LRU 清理
# ============================================================

def cleanup_lru() -> int:
    """
    LRU 清理：删除 last_seen_at < (now - lru_keep_days) 的行。

    返回删除的行数。lru_keep_days=0 时跳过（永不清理）。
    """
    days = int(settings.metadata_lru_keep_days)
    if days <= 0:
        logger.info("metadata LRU: lru_keep_days=0，跳过清理")
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)
    try:
        with engine.begin() as conn:
            res = conn.execute(
                text("DELETE FROM media_metadata WHERE last_seen_at < :cutoff"),
                {"cutoff": cutoff},
            )
            deleted = res.rowcount or 0
        logger.info(
            f"metadata LRU: 删除 {deleted} 行（cutoff < {cutoff.isoformat()}）"
        )
        return deleted
    except Exception:
        logger.exception("metadata LRU 清理异常")
        return 0


# ============================================================
# 后台 stale refresh worker
# ============================================================
# 用户读到 stale 行时入队，worker 单线程异步从上游重抓 + upsert。
# 不阻塞用户响应；上游失败时不更新行（updated_at 保持原 stale 状态，下次 cache miss 再试）。

class _MetadataRefreshWorker:
    """
    队列项格式：(source, source_id, hint_dict)
      hint_dict 可携带媒体类型等 worker 需要的额外信息（特别是 TMDB 需要 media_type）
    """

    def __init__(self):
        self._queue: "Queue[Tuple[str, str, dict]]" = Queue()
        self._enqueued: set = set()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def enqueue(self, source: str, source_id: str, hint: Optional[dict] = None):
        if not source or not source_id:
            return
        source_id = str(source_id)
        key = (source, source_id)
        with self._lock:
            if key in self._enqueued:
                return
            self._enqueued.add(key)
            self._queue.put((source, source_id, hint or {}))
            self._ensure_thread()

    def _ensure_thread(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="metadata-refresh-worker", daemon=True
        )
        self._thread.start()
        logger.info("metadata refresh worker 已启动")

    def _run(self):
        while True:
            try:
                source, source_id, hint = self._queue.get(timeout=30.0)
            except Empty:
                logger.debug("metadata refresh worker 空闲退出")
                return
            try:
                self._process_one(source, source_id, hint)
            except Exception:
                logger.exception(
                    f"metadata refresh 异常 source={source} id={source_id}"
                )
            finally:
                with self._lock:
                    self._enqueued.discard((source, source_id))

    def _process_one(self, source: str, source_id: str, hint: dict):
        """
        从上游重抓 → upsert（updated_at 推进）。
        上游失败 → 不动行（updated_at 保持 stale，下次访问再试）。
        """
        if source == 'tmdb':
            self._refresh_tmdb(source_id, hint)
        elif source == 'anilist':
            self._refresh_anilist(source_id)
        elif source == 'douban':
            self._refresh_douban(source_id)
        else:
            logger.warning(f"metadata refresh: 未知 source={source}")

    # ---- 各 source 的实际 refresh ----

    def _refresh_tmdb(self, source_id: str, hint: dict):
        if not settings.tmdb_api_key:
            return
        media_type = hint.get('media_type') or 'movie'
        try:
            from common.tmdb_client import TMDBClient
            from web.backend.api.discover import _tmdb_scrape_lang
            lang = _tmdb_scrape_lang()
            client = TMDBClient(settings.tmdb_api_key, delay=0.5, language=lang)
            raw = client.get_detail(media_type, int(source_id), language=lang)
            if not raw:
                logger.info(f"metadata refresh tmdb={source_id}: 上游空，保留旧行")
                return
        except Exception:
            logger.exception(f"metadata refresh tmdb={source_id}: 上游异常")
            return
        # 复用 discover 端点的 normalize + upsert（避免重复代码）
        try:
            from web.backend.api.discover import _normalize_detail, _upsert_tmdb_detail
            normalized = _normalize_detail(raw, media_type)
            _upsert_tmdb_detail(normalized)
            logger.info(f"metadata refresh tmdb={source_id} 完成")
        except Exception:
            logger.exception(f"metadata refresh tmdb={source_id}: normalize/upsert 异常")

    def _refresh_anilist(self, source_id: str):
        cfg = settings.anilist
        if not cfg.enabled:
            return
        try:
            from common.anilist_client import AniListClient
            client = AniListClient(
                base_url=cfg.base_url,
                request_delay=cfg.request_delay,
                timeout=cfg.timeout_seconds,
            )
            detail = client.detail(int(source_id))
            if not detail:
                logger.info(f"metadata refresh anilist={source_id}: 上游空")
                return
        except Exception:
            logger.exception(f"metadata refresh anilist={source_id}: 上游异常")
            return
        try:
            from web.backend.api.discover import _upsert_anilist_detail
            _upsert_anilist_detail(detail)
            logger.info(f"metadata refresh anilist={source_id} 完成")
        except Exception:
            logger.exception(f"metadata refresh anilist={source_id}: upsert 异常")

    def _refresh_douban(self, source_id: str):
        # 豆瓣反爬严重，refresh 时机要慎重。这里照样调 fetch_subject_summary。
        cfg = settings.douban_lists
        if not cfg.enabled:
            return
        try:
            from common.douban_client import DoubanClient
            client = DoubanClient(
                user_agent=settings.douban_user_agent,
                delay=settings.douban_request_delay,
            )
            detail = client.fetch_subject_summary(str(source_id))
            if not detail or not detail.get('summary'):
                logger.info(f"metadata refresh douban={source_id}: 上游空/反爬挡")
                return
        except Exception:
            logger.exception(f"metadata refresh douban={source_id}: 上游异常")
            return
        try:
            from web.backend.api.discover import _upsert_douban_detail
            _upsert_douban_detail(detail)
            logger.info(f"metadata refresh douban={source_id} 完成")
        except Exception:
            logger.exception(f"metadata refresh douban={source_id}: upsert 异常")


_refresh_worker = _MetadataRefreshWorker()


def enqueue_refresh(source: str, source_id: str, hint: Optional[dict] = None) -> None:
    """对外暴露的入队函数。读路径命中 stale 时调用。"""
    _refresh_worker.enqueue(source, source_id, hint)


# ============================================================
# 每日 LRU 主循环（main.py spawn 一个 daemon 线程跑）
# ============================================================

def daily_loop(stop_event: threading.Event):
    """
    每 24 小时跑一次 cleanup_lru。响应 stop_event 退出。
    主循环醒来 60s 一次检查 stop_event，便于服务关闭时快速退出。
    """
    DAY_SECONDS = 24 * 3600
    last_run = 0.0
    logger.info(
        f"metadata 维护循环已启动（LRU 每 {DAY_SECONDS}s 跑一次, "
        f"keep_days={settings.metadata_lru_keep_days}）"
    )
    while not stop_event.is_set():
        now = time.time()
        if now - last_run >= DAY_SECONDS:
            last_run = now
            try:
                cleanup_lru()
            except Exception:
                logger.exception("metadata LRU 主循环异常（已捕获，下次再试）")
        # 短睡眠便于响应 stop_event
        for _ in range(60):
            if stop_event.is_set():
                return
            time.sleep(1)
