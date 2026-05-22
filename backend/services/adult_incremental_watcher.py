"""
成人内容增量监听器（IncrementalWatcher）。

跟 adult_scanner（全库主动扫）的区别：
  - 它问 Jellyfin "上次以来你看见了哪些新 item"（GET /Items?MinDateLastSaved=...）
  - 不 rglob 文件系统，不创建 Task，纯后台跑
  - 拿到 item Path 后丢给共用的 AdultPipeline 处理

触发：jellyfin_poller（原 jellyfin_ws）的 polling 每 60s 调一次 poll_libraries()。
进度持久化：表 adult_watcher_state(library_id PK, last_check_at)。

冷启动语义：
  库第一次进入 poll → 记 last_check_at=now() 直接返回（不把全库当作"新增"灌进 pipeline）。
  从第二轮开始才真正拉增量。这跟 adult_scanner 互补：库初始化扫描走 scanner（一次性），
  之后日常增量靠这个 watcher。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Jellyfin /Items?MinDateLastSaved 拉增量时往前回溯的安全余量秒数；
# 边界 item 重复返回由 pipeline 跳过策略兜底（mtime 未变）
SAFETY_LOOKBACK_SECONDS = 60


def _iso_z(dt: datetime) -> str:
    """datetime → '2026-05-22T12:00:00.000Z'（Jellyfin MinDateLastSaved 接受的格式）"""
    return dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')


class AdultIncrementalWatcher:
    """模块级单例。polling worker 调用 poll_libraries()。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._last_run_at: Optional[float] = None
        self._last_run_summary: dict = {}
        # 防 reentry：同一库正在处理时跳过本轮
        self._running_libs: Set[str] = set()

    # ====================================================================
    # 公开接口
    # ====================================================================

    def status(self) -> dict:
        """暴露给前端 / API：last_check_at per library, 最近一次 summary"""
        from backend.database import SessionLocal, AdultWatcherState
        per_lib = {}
        try:
            with SessionLocal() as db:
                for s in db.query(AdultWatcherState).all():
                    per_lib[s.library_id] = s.last_check_at.isoformat() if s.last_check_at else None
        except Exception:
            logger.debug("incremental_watcher status 读 DB 失败", exc_info=True)
        return {
            "last_run_at": self._last_run_at,
            "last_run_summary": self._last_run_summary,
            "running_libs": list(self._running_libs),
            "last_check_at_per_library": per_lib,
        }

    def poll_libraries(self, library_ids: List[str]) -> Dict[str, dict]:
        """
        polling worker 调用入口。同步处理每个库（顺序跑，避免并发刮削打 Jellyfin 太狠）。

        Returns:
            {library_id: stats} 实际处理过的库（被 reentry 跳过的不出现）
        """
        from backend.config import settings

        if not settings.jellyfin_api_key:
            return {}

        do_scrape = settings.adult_auto_scrape
        results: Dict[str, dict] = {}

        for lib_id in library_ids:
            with self._lock:
                if lib_id in self._running_libs:
                    logger.debug(f"incremental_watcher: 库 {lib_id} 正在处理，跳过本轮")
                    continue
                self._running_libs.add(lib_id)
            try:
                stats = self._poll_one_library(lib_id, do_scrape)
                if stats is not None:
                    results[lib_id] = stats
            except Exception:
                logger.exception(f"incremental_watcher: 库 {lib_id} 处理异常")
            finally:
                with self._lock:
                    self._running_libs.discard(lib_id)

        self._last_run_at = time.time()
        if results:
            # 合并所有库的 stats 作为 last_run_summary
            agg = {}
            for s in results.values():
                for k, v in s.items():
                    agg[k] = agg.get(k, 0) + v
            self._last_run_summary = agg
        return results

    # ====================================================================
    # 内部
    # ====================================================================

    def _poll_one_library(self, library_id: str, do_scrape: bool) -> Optional[dict]:
        """单库处理。返回 stats 或 None（冷启动/失败时）。"""
        from backend.database import SessionLocal, AdultWatcherState
        from backend.config import settings
        from common.jellyfin_client import JellyfinClient
        from backend.path_translator import translate_path_with_settings
        from backend.services.adult_pipeline import AdultPipeline
        from backend.shutdown import is_shutting_down

        now = datetime.utcnow()

        # ① 读 last_check_at；冷启动场景直接 init 然后退出
        with SessionLocal() as db:
            state = db.query(AdultWatcherState).filter_by(library_id=library_id).first()
            if state is None:
                db.add(AdultWatcherState(library_id=library_id, last_check_at=now))
                db.commit()
                logger.info(
                    f"incremental_watcher: 库 {library_id} 首次接入，记 last_check_at={_iso_z(now)}，"
                    f"本轮跳过（初始扫描应由 scanner 完成）"
                )
                return None
            since = state.last_check_at - timedelta(seconds=SAFETY_LOOKBACK_SECONDS)

        # ② 调 Jellyfin 拉增量
        try:
            jf = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
            items = jf.get_items_since(library_id, _iso_z(since))
        except Exception as e:
            logger.warning(f"incremental_watcher: 库 {library_id} 拉增量失败: {e}")
            return None

        if not items:
            # 无增量也要更新 last_check_at，避免下次拉得越来越远
            with SessionLocal() as db:
                s = db.query(AdultWatcherState).filter_by(library_id=library_id).first()
                if s:
                    s.last_check_at = now
                    db.commit()
            return {'scanned': 0, 'new': 0, 'updated': 0, 'moved': 0,
                    'skipped': 0, 'unrecognized': 0, 'excluded': 0,
                    'scraped': 0, 'failed': 0}

        # ③ 准备 stats 和 pipeline
        stats = {'scanned': 0, 'new': 0, 'updated': 0, 'moved': 0,
                 'skipped': 0, 'unrecognized': 0, 'excluded': 0,
                 'scraped': 0, 'failed': 0, 'no_path': 0, 'not_found': 0}

        def on_progress(idx, total, file_info, result):
            stats['scanned'] += 1
            status = result.get('status', 'failed')
            if status in stats:
                stats[status] += 1
            if result.get('scraped'):
                stats['scraped'] += 1

        pipe = AdultPipeline(do_scrape=do_scrape, on_progress=on_progress)

        # ④ 逐 item 处理
        total = len(items)
        for idx, item in enumerate(items):
            if is_shutting_down():
                logger.info(f"incremental_watcher: 收到 shutdown 信号，提前退出（已处理 {idx}/{total}）")
                break
            jf_path = item.get('Path')
            if not jf_path:
                stats['no_path'] += 1
                continue
            # Jellyfin 视角 → 本机视角
            local_path_str = translate_path_with_settings(jf_path) or jf_path
            local_path = Path(local_path_str)
            if not local_path.exists():
                # Jellyfin 知道但本机看不到（path mapping 没配 / 文件被删）
                logger.debug(
                    f"incremental_watcher: 本机找不到文件 {local_path}（jf path={jf_path}）"
                )
                stats['not_found'] += 1
                continue
            pipe.process_one(local_path, idx=idx, total=total)

        # ⑤ flush Jellyfin（pipeline 内部攒了处理过的 path）
        pipe.flush_jellyfin(library_id)

        # ⑥ 写回 last_check_at
        with SessionLocal() as db:
            s = db.query(AdultWatcherState).filter_by(library_id=library_id).first()
            if s:
                s.last_check_at = now
                db.commit()

        # ⑦ 日志总结：有变更才打 INFO，否则 DEBUG
        changed = stats['new'] + stats['updated'] + stats['moved']
        if changed > 0:
            logger.info(
                f"incremental_watcher: 库 {library_id} 增量 {total} 项 → "
                f"new={stats['new']} updated={stats['updated']} moved={stats['moved']} "
                f"scraped={stats['scraped']} failed={stats['failed']} "
                f"skipped/excluded/unrecognized={stats['skipped']+stats['excluded']+stats['unrecognized']}"
            )
        else:
            logger.debug(
                f"incremental_watcher: 库 {library_id} {total} 项无新变更"
            )
        return stats


# 模块级单例
incremental_watcher = AdultIncrementalWatcher()
