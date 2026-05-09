"""
jellyfin-watcher: 60s 轮询 jellyfin items API，确认 jellyfin_recognizing 的种子是否真已入库。

职责：
  - 看 dispatch_map 中 phase=jellyfin_recognizing 的所有行
  - 用 dispatch_map 已存的 (target_library_id, tmdb_id, title) 查 jellyfin
  - 命中（path 匹配 target_path）→ phase=jellyfin_recognize_done，trigger post-process
  - 没命中 → 留在 jellyfin_recognizing 等下一轮（最长 timeout=10min 后报 warned 但仍推进）

设计要点：
  - 完全 stateless，崩溃后下一轮自动捡起
  - jellyfin 大库扫描可能慢，60s 频率够用
  - 超时（dispatched_at 超过 10min 未识别到）→ 标记 warned 并强制推到 recognize_done，
    后续 post-process 仍能跑（字幕/音轨不依赖 jellyfin item）
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from tools.dispatch.phases import (
    PHASE_JELLYFIN_RECOGNIZING, PHASE_JELLYFIN_RECOGNIZE_DONE,
    STATUS_RUNNING, STATUS_SUCCEEDED, STATUS_WARNED,
)

logger = logging.getLogger(__name__)


# 触发 event：pipeline 把行推到 jellyfin_recognizing 后调 trigger.set()
trigger = threading.Event()

# 内部硬编码 wait 超时（不暴露给用户）
WAIT_TIMEOUT = 60

# 最长等待 jellyfin 识别的时长，超时仍推进（不阻塞后处理）
RECOGNIZE_TIMEOUT_MINUTES = 10

# Jellyfin LibraryMonitor 有 45s 硬编码防抖（源码 Task.Delay(45000)，不可配置，
# 详见 Emby.Server.Implementations/IO/LibraryMonitor.cs）。pipeline 触发 trigger 后
# 立刻查必空，等一个 grace 周期再查更省 API。
POST_TRIGGER_GRACE_SECONDS = 60


def run_jellyfin_watcher_loop(stop_event: threading.Event):
    """独立线程：60s 轮询 jellyfin 确认入库。
    服务端 LibraryMonitor 硬编码 45s 防抖（不可配置），调用 /Library/Media/Updated
    后约 45-60 秒才真扫完，所以收到 trigger 后先等一个 grace 周期再查。
    """
    logger.info(f"jellyfin-watcher 启动（{WAIT_TIMEOUT}s 兜底，{POST_TRIGGER_GRACE_SECONDS}s 触发后等待）")
    import time as _t
    last_heartbeat = _t.time()
    HEARTBEAT_SECONDS = 600

    while not stop_event.is_set():
        woken_by_trigger = trigger.wait(timeout=WAIT_TIMEOUT)
        trigger.clear()
        if stop_event.is_set():
            return

        # 被 trigger 唤醒（不是超时唤醒）→ 先等防抖窗口结束再查，避免肯定空跑
        if woken_by_trigger and POST_TRIGGER_GRACE_SECONDS > 0:
            if stop_event.wait(timeout=POST_TRIGGER_GRACE_SECONDS):
                return

        try:
            scan_once()
        except Exception as e:
            logger.warning(f"jellyfin-watcher 异常: {e}", exc_info=True)

        if _t.time() - last_heartbeat > HEARTBEAT_SECONDS:
            logger.info("jellyfin-watcher heartbeat: alive, idle")
            last_heartbeat = _t.time()
    logger.info("jellyfin-watcher 退出")


def scan_once() -> Dict:
    """扫一遍 jellyfin_recognizing 行，确认入库则推进。返回 stats。"""
    from web.backend.config import settings
    from web.backend.database import SessionLocal, DownloadDispatchMap

    if not settings.jellyfin_host or not settings.jellyfin_api_key:
        return _empty_stats()

    with SessionLocal() as db:
        rows = (
            db.query(DownloadDispatchMap)
            .filter(DownloadDispatchMap.phase == PHASE_JELLYFIN_RECOGNIZING)
            .all()
        )
    if not rows:
        return _empty_stats()

    try:
        from common.jellyfin_client import JellyfinClient
        jf = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
    except Exception as e:
        logger.warning(f"jellyfin-watcher: jellyfin 客户端创建失败: {e}")
        return _empty_stats()

    stats = _empty_stats()
    stats['scanned'] = len(rows)
    advanced = False

    timeout_cutoff = datetime.utcnow() - timedelta(minutes=RECOGNIZE_TIMEOUT_MINUTES)

    with SessionLocal() as db:
        for r in rows:
            row = db.query(DownloadDispatchMap).filter_by(torrent_hash=r.torrent_hash).first()
            if not row or row.phase != PHASE_JELLYFIN_RECOGNIZING:
                continue

            # 已超时 → 强制推进（warned），让后处理能跑（字幕/音轨不依赖 jellyfin item）
            disp_at = row.dispatched_at or row.created_at
            if disp_at and disp_at < timeout_cutoff:
                row.phase = PHASE_JELLYFIN_RECOGNIZE_DONE
                row.phase_status = STATUS_WARNED
                row.status_message = f'jellyfin 识别超时 {RECOGNIZE_TIMEOUT_MINUTES}min，强制推进'
                stats['timed_out'] += 1
                advanced = True
                logger.warning(f"jellyfin-watcher: {row.torrent_hash[:16]}.. 超时强推 → recognize_done")
                continue

            # 查 jellyfin 是否已识别这个 item
            if _is_in_jellyfin(jf, row):
                row.phase = PHASE_JELLYFIN_RECOGNIZE_DONE
                row.phase_status = STATUS_SUCCEEDED
                row.status_message = 'jellyfin 已认到 item，进入后处理'
                stats['advanced'] += 1
                advanced = True
            # 否则留在原 phase，下一轮再查

        db.commit()

    if stats['advanced'] or stats['timed_out']:
        logger.info(
            f"jellyfin-watcher: scanned={stats['scanned']} "
            f"advanced={stats['advanced']} timed_out={stats['timed_out']}"
        )

    if advanced:
        try:
            from tools.dispatch.post_process_worker import trigger as pp_trigger
            pp_trigger.set()
        except Exception:
            pass

    return stats


def _is_in_jellyfin(jf, row) -> bool:
    """
    判断 dispatch_map 行是否已被 jellyfin 识别入库。

    优先级：
      1. 按 tmdb_id 查（最准）
      2. 按 title + year 查 + path 包含 target_path（兜底）
    """
    tmdb_id = row.tmdb_id or row.series_tmdb_id
    title = row.title or row.series_name or ''
    target_path = (row.target_path or '').strip()

    # ① 按 tmdb_id 查
    if tmdb_id:
        items = _search_by_tmdb(jf, tmdb_id)
        if items:
            # 找到任一 → 算认到了（path 验证可选，jellyfin 自己的元数据已经匹配）
            return True

    # ② 按标题查 + path 校验
    if title and target_path:
        items = _search_by_title(jf, title)
        norm_target = _norm_path(target_path)
        for item in items or []:
            item_path = _norm_path(item.get('Path') or '')
            if not item_path:
                continue
            # item 的 path 落在 target_path 之下（或相等）即视为命中
            if item_path == norm_target or item_path.startswith(norm_target + '/'):
                return True

    return False


def _search_by_tmdb(jf, tmdb_id: str) -> Optional[List[Dict]]:
    """按 ProviderIds.Tmdb 查 jellyfin item。"""
    try:
        result = jf._request('GET', '/Items', params={
            'Recursive': 'true',
            'IncludeItemTypes': 'Movie,Series',
            'AnyProviderIdEquals': f'Tmdb.{tmdb_id}',
            'Fields': 'Path,ProviderIds',
            'Limit': 5,
        })
        if result and result.get('Items'):
            return result['Items']
    except Exception as e:
        logger.debug(f"_search_by_tmdb({tmdb_id}) 失败: {e}")
    return None


def _search_by_title(jf, title: str) -> Optional[List[Dict]]:
    """按 searchTerm 查 jellyfin item。"""
    try:
        result = jf._request('GET', '/Items', params={
            'Recursive': 'true',
            'IncludeItemTypes': 'Movie,Series,Episode',
            'searchTerm': title,
            'Fields': 'Path,ProviderIds',
            'Limit': 20,
        })
        if result and result.get('Items'):
            return result['Items']
    except Exception as e:
        logger.debug(f"_search_by_title({title!r}) 失败: {e}")
    return None


def _norm_path(p: str) -> str:
    """统一分隔符 + 转小写 + 去尾斜杠（Windows 不区分大小写，路径比对用）。"""
    if not p:
        return ''
    p = p.replace('\\', '/').rstrip('/')
    return p.lower()


def _empty_stats() -> Dict:
    return {
        'scanned': 0,
        'advanced': 0,
        'timed_out': 0,
    }
