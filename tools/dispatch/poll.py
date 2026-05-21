"""
qB 指标轮询：定期更新 dispatch_map 行的 last_seen_ratio / seeded_seconds / upload_bytes
            （供 quota / 软清决策用）。

不再负责"发现新种子落 pending" —— 那是 adopt.py 的职责
（adopt 走完整的识别 + 路径计算 + 决策链路，按置信度入流水线）。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Dict, List

from common.qbittorrent_client import QBittorrentClient
from backend.config import settings
from backend.database import SessionLocal, DownloadDispatchMap

logger = logging.getLogger(__name__)


def _get_qb_client() -> QBittorrentClient:
    return QBittorrentClient(
        host=settings.qbittorrent_host,
        username=settings.qbittorrent_username,
        password=settings.qbittorrent_password,
    )


def poll_once() -> Dict:
    """
    扫一次 qB list_torrents，返回 {found_new, updated, errors}。
    """
    client = _get_qb_client()
    try:
        torrents = client.list_torrents() or []
    except Exception as e:
        logger.error(f"qB list_torrents 异常: {e}")
        return {'found_new': 0, 'updated': 0, 'errors': 1}

    if not torrents:
        return {'found_new': 0, 'updated': 0, 'errors': 0}

    found_new = 0
    updated = 0

    with SessionLocal() as db:
        # 已存在的 hash → 行映射（只查这次出现的 hash 即可，避免全表扫描）
        hashes = [t.get('hash', '').lower() for t in torrents if t.get('hash')]
        existing = {
            row.torrent_hash: row
            for row in db.query(DownloadDispatchMap)
            .filter(DownloadDispatchMap.torrent_hash.in_(hashes))
            .all()
        }

        for t in torrents:
            h = (t.get('hash') or '').lower()
            if not h:
                continue

            progress = t.get('progress') or 0
            ratio = t.get('ratio') or 0
            seeding_time = t.get('seeding_time') or 0
            upload_bytes = t.get('uploaded') or 0

            row = existing.get(h)

            # --- 已存在：更新 quota 决策字段 ---
            if row:
                changed = False
                if abs(float(row.last_seen_ratio or 0) - float(ratio)) > 0.01:
                    row.last_seen_ratio = float(ratio)
                    changed = True
                if int(row.seeded_seconds or 0) < int(seeding_time):
                    row.seeded_seconds = int(seeding_time)
                    changed = True
                if int(row.upload_bytes or 0) != int(upload_bytes):
                    row.upload_bytes = int(upload_bytes)
                    changed = True
                if changed:
                    updated += 1
                continue

            # 不在 dispatch_map 的种子留给 adopt.py 走完整识别流程
            # （这里不再落新行，避免裸 pending 跑空流水线）

        db.commit()

    return {'found_new': found_new, 'updated': updated, 'errors': 0}


def run_poll_loop(stop_event=None):
    """
    长循环：每 N 秒扫一次。stop_event.is_set() 时退出。
    """
    interval = max(5, int(settings.dispatch.poll_interval_seconds))
    logger.info(f"qB 完成轮询启动，间隔 {interval}s")
    while True:
        if stop_event is not None and stop_event.is_set():
            logger.info("轮询线程收到停止信号，退出")
            return
        try:
            stats = poll_once()
            if stats['found_new'] or stats['errors']:
                logger.info(f"poll: {stats}")
        except Exception as e:
            logger.exception(f"poll_once 异常: {e}")
        # 分多个小睡眠以便快速响应 stop_event
        for _ in range(interval):
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(1)
