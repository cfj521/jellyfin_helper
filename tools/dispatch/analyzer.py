"""分析阶段：扫 dispatch_map(phase=analyzing) 行 → 识别 + 算路径 → 转 dispatch_queued。

高置信自动入流水线（phase=dispatch_queued），低置信留在 phase=analyzing
+ status=needs_review 等用户审核。

跟 adopt.py 区别：adopt 是主动扫 qB 孤儿，analyzer 是 push 主动写的 analyzing 占位行。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Dict, Optional

from tools.dispatch.phases import (
    PHASE_ANALYZING, PHASE_DISPATCH_QUEUED,
    STATUS_RUNNING, STATUS_NEEDS_REVIEW,
)

logger = logging.getLogger(__name__)

# 复用 adopt 的阈值 —— 决策语义一致：高置信自动入流水线
CONFIDENCE_AUTO_THRESHOLD = 0.85

# 触发 event：API push 写完 dispatch_map 行后调 trigger.set() 立刻唤醒 analyzer
# scheduler 启动的独立线程在 trigger.wait() 上阻塞，被 set 或超时后跑一轮
trigger = threading.Event()

# 已经为 metaDL 阶段打过 INFO 的 hash 集合 —— 避免每 5s 刷一条同样的"等待 metadata"
# 一旦该 hash 走出 metaDL（拿到文件列表 / qB 不在了），从 set 中移除
_metadl_announced: set = set()

# 「全在等 metadata」轮次的节流时间戳；用于每轮空走时偶尔打一行 INFO 心跳
_last_waiting_log_ts: float = 0.0
_WAITING_LOG_INTERVAL = 60.0  # 秒


def run_analyzer_loop(stop_event):
    """独立线程：等 trigger 或超时 → 跑一轮 analyze_pending_rows。

    timeout 策略：5s 短轮询。空行时一次 SELECT 几 ms，开销可忽略；
    有行还在 metaDL 时 5s 内重试一次（拿到 metadata 就立即处理）。
    """
    logger.info("analyzer loop 启动（事件驱动 + 5s 兜底）")
    import time as _t
    last_heartbeat = _t.time()
    HEARTBEAT_SECONDS = 600

    while not stop_event.is_set():
        trigger.wait(timeout=5)
        trigger.clear()
        if stop_event.is_set():
            return
        try:
            analyze_pending_rows()
        except Exception as e:
            logger.warning(f"analyze 异常: {e}", exc_info=True)

        if _t.time() - last_heartbeat > HEARTBEAT_SECONDS:
            logger.info("analyzer heartbeat: alive, idle")
            last_heartbeat = _t.time()


def analyze_pending_rows() -> Dict:
    """
    扫一遍 dispatch_map(phase=analyzing, status=running) → 拉 qB 文件 → identify → 决策。
    needs_review 的行不动（等用户审核），running 的才是要主动推进的。
    返回 stats {scanned, processed_auto, processed_review, still_waiting, errors}
    """
    from backend.config import settings
    from backend.database import SessionLocal, DownloadDispatchMap

    if not settings.qbittorrent_configured:
        return _empty_stats()

    # 拿候选行：仅 phase=analyzing + status=running（needs_review 不动，等审核）
    with SessionLocal() as db:
        rows = (
            db.query(DownloadDispatchMap)
            .filter(DownloadDispatchMap.phase == PHASE_ANALYZING)
            .filter(DownloadDispatchMap.phase_status == STATUS_RUNNING)
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
        logger.warning(f"analyze: qB 不通: {e}")
        return _empty_stats()

    qb_by_hash = {(t.get('hash') or '').lower(): t for t in all_torrents}

    stats = _empty_stats()
    stats['scanned'] = len(rows)

    for row in rows:
        try:
            outcome = _analyze_one(qb, row, qb_by_hash.get(row.torrent_hash))
            stats[outcome] += 1
        except Exception as e:
            logger.warning(f"analyze: {row.torrent_hash[:16]}.. 失败: {e}", exc_info=True)
            stats['errors'] += 1

    if stats['processed_auto'] or stats['processed_review']:
        logger.info(
            f"analyze 完成：auto={stats['processed_auto']} review={stats['processed_review']} "
            f"waiting={stats['still_waiting']} err={stats['errors']}"
        )
    elif stats['still_waiting']:
        # 整轮都没东西出结果但还有种子在等 metadata → 节流打一次 INFO 心跳，
        # 让用户看到 analyzer 在跟踪，而不是"卡死了"。
        global _last_waiting_log_ts
        import time as _t
        now = _t.time()
        if now - _last_waiting_log_ts >= _WAITING_LOG_INTERVAL:
            logger.info(
                f"analyze 巡检：仍在等 metadata 的种子 {stats['still_waiting']} 个"
                f"（scanned={stats['scanned']}）"
            )
            _last_waiting_log_ts = now
    return stats


def _empty_stats() -> Dict:
    return {
        'scanned': 0,
        'processed_auto': 0,
        'processed_review': 0,
        'still_waiting': 0,
        'errors': 0,
    }


def _analyze_one(qb, row, qb_t: Optional[Dict]) -> str:
    """单条 analyze 行处理。返回 stats key 名字。

    复用 /api/dispatch/preview 的链：identify → resolve_target → check_duplicates → 决策。
    完整重复强制 needs_review，部分重复 qB skip 已有文件。
    """
    from tools.dispatch.identify import identify_media
    from backend.database import SessionLocal, DownloadDispatchMap

    h = row.torrent_hash

    # qB 里没了 → 清掉 dispatch_map 行
    if not qb_t:
        _metadl_announced.discard(h)
        with SessionLocal() as db:
            db_row = db.query(DownloadDispatchMap).filter_by(torrent_hash=h).first()
            if db_row:
                db.delete(db_row)
                db.commit()
        logger.info(f"analyze: {h[:16]}.. qB 不在了，清理 dispatch_map 行")
        return 'errors'

    # 还没拿到 metadata → 下次再说
    state = qb_t.get('state') or ''
    if state in ('metaDL', 'allocating'):
        # 首次见到这条种子在 metaDL 时打一条 INFO，后续静默走 debug，避免每 5s 刷屏。
        # 集合包 metadata 很大可能等数十秒到几分钟，没有这条日志的话整段是黑屏。
        if h not in _metadl_announced:
            _metadl_announced.add(h)
            name = (qb_t.get('name') or '').strip() or '(未知名)'
            logger.info(
                f"analyze: {h[:16]}.. 等待 metadata（state={state}）: {name[:80]}"
            )
        else:
            logger.debug(f"analyze: {h[:16]}.. 还在 {state}，下次再说")
        return 'still_waiting'

    # 走到这里说明 metadata 已就绪 —— 清掉首次通告标记
    _metadl_announced.discard(h)

    # 拉文件列表
    files = []
    try:
        raw_files = qb.get_files(h) or []
        files = [
            {'index': i, 'name': f.get('name'), 'size': f.get('size')}
            for i, f in enumerate(raw_files)
        ]
    except Exception as e:
        logger.debug(f"analyze: 拉 {h[:16]}.. 文件列表失败: {e}")

    if not files:
        return 'still_waiting'

    # ① identify
    user_hint = None
    if row.media_type and row.media_type not in ('unknown', None):
        user_hint = {'media_type': row.media_type}

    name = qb_t.get('name') or ''
    identified = identify_media(name, files=files, user_hint=user_hint)
    confidence = float(identified.get('confidence') or 0.0)
    media_type = identified.get('media_type') or 'unknown'
    source = identified.get('source') or 'unknown'

    # ② resolve target
    from tools.dispatch.adopt import _resolve_target_safe, _build_review_reason
    target_info = _resolve_target_safe(identified)
    library_id = target_info.get('library_id') or ''
    target_path = target_info.get('target_path') or ''
    move_mode = target_info.get('move_mode') or settings_default_move_mode()

    # ③ 重复检测
    duplicates = {'is_duplicate': False, 'type': 'none', 'existing': [], 'new': [], 'skip_file_indexes': []}
    try:
        from backend.api.dispatch import _check_duplicates
        duplicates = _check_duplicates(identified, files)
    except Exception as e:
        logger.warning(f"analyze: _check_duplicates 失败: {e}")

    dup_type = duplicates.get('type', 'none')
    skip_indexes = duplicates.get('skip_file_indexes') or []

    # 部分重复 → qB skip 已有文件，省带宽和磁盘
    if dup_type == 'partial' and skip_indexes:
        try:
            qb.set_file_priority(h, skip_indexes, 0)
            logger.info(
                f"analyze: {h[:16]}.. 部分重复，已 skip {len(skip_indexes)} 个文件 "
                f"(已有 {len(duplicates.get('existing') or [])} 集，新增 {len(duplicates.get('new') or [])} 集)"
            )
        except Exception as e:
            logger.warning(f"analyze: set_file_priority 失败: {e}")

    # ④ 决策：完整重复（含 adult 番号唯一命中）强制走 needs_review
    full_duplicate = dup_type in ('movie', 'tv', 'adult')

    auto_ok = (
        not full_duplicate
        and confidence >= CONFIDENCE_AUTO_THRESHOLD
        and library_id
        and target_path
        and media_type != 'unknown'
    )

    status_msg = _build_status_message(
        identified, target_info, duplicates, auto_ok, confidence, source
    )

    # 落库
    with SessionLocal() as db:
        db_row = db.query(DownloadDispatchMap).filter_by(torrent_hash=h).first()
        if not db_row:
            return 'errors'

        db_row.media_type = media_type
        db_row.tmdb_id = str(identified.get('tmdb_id') or '') or None
        db_row.imdb_id = str(identified.get('imdb_id') or '') or None
        db_row.series_tmdb_id = str(identified.get('series_tmdb_id') or '') or None
        db_row.series_name = identified.get('series_name')
        db_row.title = identified.get('title')
        db_row.year = identified.get('year')
        db_row.target_library_id = library_id or None
        db_row.target_root = target_info.get('library_root') or None
        db_row.target_path = target_path or None
        db_row.move_mode = move_mode

        # 顺手补 qb_added_on（push 流程占位行还没存）
        if db_row.qb_added_on is None and qb_t.get('added_on'):
            try:
                db_row.qb_added_on = datetime.utcfromtimestamp(int(qb_t['added_on']))
            except (TypeError, ValueError):
                pass

        # 重复检测结果存进 phase_timings JSONB，review modal 读这里
        timings = dict(db_row.phase_timings or {})
        timings['duplicates'] = {
            'type': dup_type,
            'is_duplicate': duplicates.get('is_duplicate', False),
            'existing': duplicates.get('existing') or [],
            'new': duplicates.get('new') or [],
            'skip_file_indexes': skip_indexes,
        }
        db_row.phase_timings = timings

        # 高置信：phase analyzing → dispatch_queued running，等 downloader-watcher 接管
        # 低置信：phase 留在 analyzing，status 改成 needs_review，等用户审核
        if auto_ok:
            db_row.phase = PHASE_DISPATCH_QUEUED
            db_row.phase_status = STATUS_RUNNING
        else:
            db_row.phase = PHASE_ANALYZING
            db_row.phase_status = STATUS_NEEDS_REVIEW
        db_row.status_message = status_msg
        db.commit()

    # 高置信无重复才 resume；其它保持暂停等审核
    if auto_ok:
        try:
            qb.resume(h)
        except Exception as e:
            logger.warning(f"analyze: resume {h[:16]}.. 失败: {e}")
        # 唤醒 downloader-watcher 立刻看 qB 进度（不等 20s 兜底）
        try:
            from tools.dispatch.downloader_watcher import trigger as dl_trigger
            dl_trigger.set()
        except Exception:
            pass

    label = '自动入流水线' if auto_ok else '挂到待审核'
    logger.info(
        f"analyze: {name[:60]} → {label} (media_type={media_type}, "
        f"confidence={confidence:.2f}, source={source}, dup={dup_type})"
    )
    return 'processed_auto' if auto_ok else 'processed_review'


def _build_status_message(identified, target_info, duplicates, auto_ok, confidence, source):
    """组装一句话 status_message：自动通过 / 待审核都给个总结。"""
    from tools.dispatch.adopt import _build_review_reason

    if auto_ok:
        msg = f"自动分析通过：source={source} confidence={confidence:.2f}"
        if duplicates.get('type') == 'partial':
            existing_n = len(duplicates.get('existing') or [])
            new_n = len(duplicates.get('new') or [])
            msg += f"；部分重复：已 skip {existing_n} 集，下载 {new_n} 新集"
        return msg

    # needs_review 场景
    dup_type = duplicates.get('type')
    if dup_type == 'movie':
        names = ', '.join(e['name'] for e in duplicates.get('existing', [])[:2])
        return f"已在媒体库存在该电影（{names}），是否覆盖/升级画质？"
    if dup_type == 'tv':
        existing = duplicates.get('existing') or []
        return f"该剧集所有集都已存在媒体库（{len(existing)} 集），无需重下"
    if dup_type == 'adult':
        ex = duplicates.get('existing', [])
        code = ex[0].get('code') if ex else ''
        return f"番号 {code} 已在成人库存在，是否覆盖/升级画质？"

    return _build_review_reason(identified, target_info)


def settings_default_move_mode() -> str:
    """按需取 default_move_mode，避免顶部 import settings 引入循环。"""
    from backend.config import settings
    return settings.dispatch.default_move_mode or 'copy'
