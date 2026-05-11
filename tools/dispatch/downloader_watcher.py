"""
downloader-watcher: 20s 轮询 qB，推动 dispatch_queued / downloading 阶段的行。

职责：
  - 看 dispatch_map 中 phase IN (dispatch_queued, downloading) 的所有行
  - 拉 qB 该种子的当前 state + progress
  - progress >= 1.0 且 state ∈ DONE_STATES → 推到 phase=download_done，trigger pipeline
  - 还在下 → phase 写为 downloading，status 按 qB state 映射（running / metadata_pending /
    stalled / paused / failed）
  - qB 里没了 → 清掉 dispatch_map 行（用户在 qB Web 删了种）

设计要点：
  - 完全 stateless：只看当前 qB state，不依赖历史；崩溃后下一轮自动捡回
  - status=failed 不在这里"恢复"，由 sweeper 决定是否 recheck 或人工
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, Optional

from tools.dispatch.phases import (
    PHASE_DISPATCH_QUEUED, PHASE_DOWNLOADING, PHASE_DOWNLOAD_DONE,
    STATUS_RUNNING,
    map_qb_state_to_download_status, is_qb_download_done,
)

logger = logging.getLogger(__name__)


# 触发 event：analyzer / adopt 把行推到 dispatch_queued / downloading 后调 trigger.set()
trigger = threading.Event()

# 内部硬编码 wait 超时（不暴露给用户）
WAIT_TIMEOUT = 20


def run_downloader_watcher_loop(stop_event: threading.Event):
    """独立线程：20s 轮询 qB 看 dispatch_queued / downloading 行的进度。"""
    logger.info(f"downloader-watcher 启动（{WAIT_TIMEOUT}s 兜底）")
    import time as _t
    last_heartbeat = _t.time()
    HEARTBEAT_SECONDS = 600  # 10 分钟心跳

    while not stop_event.is_set():
        trigger.wait(timeout=WAIT_TIMEOUT)
        trigger.clear()
        if stop_event.is_set():
            return
        try:
            scan_once()
        except Exception as e:
            logger.warning(f"downloader-watcher 异常: {e}", exc_info=True)

        # 心跳：空闲也每 10 分钟输出一次"我还活着"，便于排查 worker 是否假死
        if _t.time() - last_heartbeat > HEARTBEAT_SECONDS:
            logger.info("downloader-watcher heartbeat: alive, idle")
            last_heartbeat = _t.time()
    logger.info("downloader-watcher 退出")


def scan_once() -> Dict:
    """扫一遍 → 拉 qB → 按状态推进。返回 stats。"""
    from web.backend.config import settings
    from web.backend.database import SessionLocal, DownloadDispatchMap

    if not settings.qbittorrent_host or not settings.qbittorrent_username:
        return _empty_stats()

    with SessionLocal() as db:
        rows = (
            db.query(DownloadDispatchMap)
            .filter(DownloadDispatchMap.phase.in_([
                PHASE_DISPATCH_QUEUED, PHASE_DOWNLOADING,
            ]))
            .all()
        )
    if not rows:
        return _empty_stats()

    # 一次性拉 qB 列表，按 hash 索引
    try:
        from common.qbittorrent_client import QBittorrentClient
        qb = QBittorrentClient(
            settings.qbittorrent_host,
            settings.qbittorrent_username,
            settings.qbittorrent_password,
        )
        all_torrents = qb.list_torrents() or []
    except Exception as e:
        logger.warning(f"downloader-watcher: qB 不通: {e}")
        return _empty_stats()

    qb_by_hash = {(t.get('hash') or '').lower(): t for t in all_torrents}

    stats = _empty_stats()
    stats['scanned'] = len(rows)
    advanced_to_done = False

    with SessionLocal() as db:
        for r in rows:
            row = db.query(DownloadDispatchMap).filter_by(torrent_hash=r.torrent_hash).first()
            if not row:
                continue
            t = qb_by_hash.get(row.torrent_hash)

            # qB 里没了 → 清行（用户手删）
            if not t:
                logger.info(f"downloader-watcher: {row.torrent_hash[:16]}.. qB 不在了，清行")
                db.delete(row)
                stats['removed'] += 1
                continue

            state = t.get('state') or ''
            progress = float(t.get('progress') or 0.0)

            # 顺手补 qb_added_on（push 流程的 analyzing 占位行此时还没存这个）
            if row.qb_added_on is None and t.get('added_on'):
                try:
                    from datetime import datetime as _dt
                    row.qb_added_on = _dt.utcfromtimestamp(int(t['added_on']))
                except (TypeError, ValueError):
                    pass

            # 已下完 → 推到 download_done
            if is_qb_download_done(state, progress):
                row.phase = PHASE_DOWNLOAD_DONE
                row.phase_status = STATUS_RUNNING
                row.status_message = f'下载完成 (qB {state})，等流水线接管'
                stats['advanced_done'] += 1
                advanced_to_done = True
                continue

            # 还在下 / 出错 → 写 downloading + 映射状态
            new_status = map_qb_state_to_download_status(state)
            new_msg = f'qB {state} · {progress*100:.1f}%'
            if (row.phase != PHASE_DOWNLOADING
                or row.phase_status != new_status
                or row.status_message != new_msg):
                row.phase = PHASE_DOWNLOADING
                row.phase_status = new_status
                row.status_message = new_msg
                stats['updated_downloading'] += 1

        db.commit()

    if stats['advanced_done'] or stats['removed']:
        logger.info(
            f"downloader-watcher: scanned={stats['scanned']} "
            f"→done={stats['advanced_done']} updated={stats['updated_downloading']} "
            f"removed={stats['removed']}"
        )

    # 有种子推到 download_done → 唤醒 pipeline
    if advanced_to_done:
        try:
            from tools.dispatch.pipeline_worker import trigger as pipeline_trigger
            pipeline_trigger.set()
        except Exception:
            pass

    return stats


def _empty_stats() -> Dict:
    return {
        'scanned': 0,
        'advanced_done': 0,
        'updated_downloading': 0,
        'removed': 0,
    }
