"""孤儿种子认领：qB 里有但 dispatch_map 没有的种子拉进流水线。

场景：Jackett RSS / qB 订阅 / 用户在 qB Web 自己加种 —— 这些没走 /preview。

策略：qB category → user_hint 优先；否则 identify_media；
高置信入流水线（phase 按 qB 进度决定 download_done / downloading），
低置信挂 phase=analyzing status=needs_review。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

from tools.dispatch.phases import (
    PHASE_ANALYZING, PHASE_DOWNLOADING, PHASE_DOWNLOAD_DONE,
    STATUS_RUNNING, STATUS_NEEDS_REVIEW,
    map_qb_state_to_download_status, is_qb_download_done,
)

logger = logging.getLogger(__name__)

# 高置信阈值：≥ 该值自动入流水线，否则挂 needs_review
CONFIDENCE_AUTO_THRESHOLD = 0.85

# 合法 media_type（纪录片不独立成 type，跟 TMDB 设计一致按 movie/tv + genre 99 处理）
_VALID_MEDIA_TYPES = {'movie', 'tv', 'anime', 'adult'}

# qB category 别名表：兼容用户各种写法（Movies / 电影 / TV-Shows / etc）
# 比对前规范化：lowercase + 去首尾空白 + - / _ → 空格 + 折叠多空格
_CATEGORY_ALIASES = {
    # 电影
    'movie': 'movie', 'movies': 'movie', 'film': 'movie',
    '电影': 'movie',
    # 剧集
    'tv': 'tv', 'tvshow': 'tv', 'tvshows': 'tv',
    'tv show': 'tv', 'tv shows': 'tv',
    'series': 'tv', 'show': 'tv',
    '剧集': 'tv', '电视剧': 'tv', '美剧': 'tv', '日剧': 'tv', '韩剧': 'tv', '国剧': 'tv',
    # 动漫
    'anime': 'anime', 'animes': 'anime', 'animation': 'anime',
    '动漫': 'anime', '番剧': 'anime', '动画': 'anime',
    # 纪录片走 movie/tv（按结构区分），不独立成 media_type
    # documentary / 纪录片 → 别名表里不映射 → 走识别链路自动分到 movie 或 tv
    # 成人
    'adult': 'adult', 'xxx': 'adult', 'av': 'adult', 'jav': 'adult',
    'porn': 'adult', '18+': 'adult',
    '成人': 'adult', '成人内容': 'adult',
}


def _normalize_category(cat: str) -> str:
    """把 qB category 字符串规范化，便于匹配 alias 表。"""
    if not cat:
        return ''
    s = cat.strip().lower()
    # - / _ 当空格用，再折叠多空格
    s = s.replace('-', ' ').replace('_', ' ')
    s = ' '.join(s.split())
    return s


def scan_and_adopt_orphans() -> Dict:
    """扫一次 qB → 找孤儿 → 识别 → 写 dispatch_map。

    返回 stats: scanned / orphans / adopted_auto / adopted_review / skipped
    """
    from common.qbittorrent_client import QBittorrentClient
    from backend.config import settings
    from backend.database import SessionLocal, DownloadDispatchMap

    if not settings.qbittorrent_configured:
        logger.debug("adopt: qB 未配置，跳过")
        return {'scanned': 0, 'orphans': 0, 'adopted_auto': 0, 'adopted_review': 0, 'skipped': 0}

    try:
        qb = QBittorrentClient(
            settings.qbittorrent_host,
            settings.qbittorrent_username,
            settings.qbittorrent_password,
        )
        all_torrents = qb.list_torrents() or []
    except Exception as e:
        logger.warning(f"adopt: qB 不通，跳过: {e}")
        return {'scanned': 0, 'orphans': 0, 'adopted_auto': 0, 'adopted_review': 0, 'skipped': 0}

    if not all_torrents:
        return {'scanned': 0, 'orphans': 0, 'adopted_auto': 0, 'adopted_review': 0, 'skipped': 0}

    # 已有 dispatch_map 行的判定：逻辑身份 = (torrent_hash, qb_added_on)
    #   ① phase=dismissed → 重置（用户曾拒绝但 qB 又出现）
    #   ② qB.added_on != dispatch_map.qb_added_on → 同 hash 不同加种时间 = 不同任务，重置
    #   ③ qB.added_on == dispatch_map.qb_added_on → 同一次加种，跳过
    #   ④ 旧行 qb_added_on 为空（迁移前老数据）→ 退回 created_at + 60s 兜底
    from datetime import datetime, timedelta
    from tools.dispatch.phases import PHASE_DISMISSED, PHASE_ANALYZING, STATUS_RUNNING

    LEGACY_READD_THRESHOLD_SECONDS = 60  # 老数据兜底（qb_added_on 字段为空）

    # qB 给的 added_on 是 Unix 秒；建 hash → added_on 索引
    qb_added_on_by_hash = {}
    for t in all_torrents:
        h = (t.get('hash') or '').lower()
        if h and t.get('added_on'):
            try:
                qb_added_on_by_hash[h] = datetime.utcfromtimestamp(int(t['added_on']))
            except (TypeError, ValueError):
                pass

    hashes_qb = [(t.get('hash') or '').lower() for t in all_torrents if t.get('hash')]

    stale_hashes = set()  # 要删旧行重处理的 hash
    with SessionLocal() as db:
        known_rows = (
            db.query(
                DownloadDispatchMap.torrent_hash,
                DownloadDispatchMap.phase,
                DownloadDispatchMap.created_at,
                DownloadDispatchMap.qb_added_on,
            )
            .filter(DownloadDispatchMap.torrent_hash.in_(hashes_qb))
            .all()
        )
        known_set = set()
        for h, phase, created_at, db_qb_added in known_rows:
            qb_added = qb_added_on_by_hash.get(h)

            # ① dismissed → 重置
            if phase == PHASE_DISMISSED:
                stale_hashes.add(h)
                continue

            # ② 双方都有 qb_added_on → 直接对比，差超过 1s 视为重加
            if db_qb_added is not None and qb_added is not None:
                if abs((qb_added - db_qb_added).total_seconds()) > 1:
                    stale_hashes.add(h)
                    continue
                known_set.add(h)
                continue

            # ④ 旧行 qb_added_on 为空 → 用 created_at + 60s 兜底
            if db_qb_added is None and qb_added is not None and created_at is not None:
                if qb_added > created_at + timedelta(seconds=LEGACY_READD_THRESHOLD_SECONDS):
                    stale_hashes.add(h)
                    continue

            # ③ 其余视为同一任务
            known_set.add(h)

        # 删 stale 旧行 → 让它们落进 orphans 重处理
        if stale_hashes:
            db.query(DownloadDispatchMap).filter(
                DownloadDispatchMap.torrent_hash.in_(stale_hashes)
            ).delete(synchronize_session=False)
            db.commit()
            logger.info(
                f"adopt: 检测到 {len(stale_hashes)} 个 hash 是新任务"
                f"（qb_added_on 不一致 / phase=dismissed），删旧行重处理"
            )

    orphans = [t for t in all_torrents if (t.get('hash') or '').lower() not in known_set]

    stats = {
        'scanned': len(all_torrents),
        'orphans': len(orphans),
        'adopted_auto': 0,
        'adopted_review': 0,
        'skipped': 0,
    }

    logger.info(
        f"adopt: scanned={len(all_torrents)} orphans={len(orphans)}"
        f"{'，开始识别' if orphans else '（无孤儿）'}"
    )
    if not orphans:
        return stats

    for t in orphans:
        try:
            outcome = _try_adopt_one(qb, t)
            if outcome == 'auto':
                stats['adopted_auto'] += 1
            elif outcome == 'review':
                stats['adopted_review'] += 1
            else:
                stats['skipped'] += 1
        except Exception as e:
            logger.warning(f"adopt: 处理 {t.get('name')!r} 失败: {e}", exc_info=True)
            stats['skipped'] += 1

    if stats['adopted_auto'] or stats['adopted_review']:
        logger.info(
            f"adopt 完成：auto={stats['adopted_auto']} review={stats['adopted_review']} "
            f"skip={stats['skipped']}"
        )
    return stats


def _try_adopt_one(qb, t: Dict) -> str:
    """单个孤儿认领。返回 'auto' / 'review' / 'skip'。"""
    from tools.dispatch.identify import identify_media

    name = t.get('name') or ''
    h = (t.get('hash') or '').lower()
    state = t.get('state') or ''
    tags = (t.get('tags') or '').split(',')
    tags = [tg.strip() for tg in tags if tg.strip()]

    # 用户正在走 /preview 流程的种子，让它自己走完或 cancel
    if 'dispatch-preview' in tags:
        return 'skip'

    # 已经走完流水线打过 library:<media_type> 标签的，跳过避免重复处理
    # （pipeline._run_one 末尾会打这个 tag，dispatch_map 行被人手删 + qB 还在的场景靠这个防重）
    if any(tg.startswith('library:') for tg in tags):
        logger.debug(f"adopt: {name[:40]} 已带 library:* 标签，跳过")
        return 'skip'

    # 还没拿到 metadata，下次再说
    if state in ('metaDL', 'allocating'):
        logger.debug(f"adopt: {name[:40]} 还在 {state}，下次再说")
        return 'skip'

    # 拉文件列表
    files = []
    try:
        raw_files = qb.get_files(h) or []
        files = [
            {'name': f.get('name'), 'size': f.get('size')}
            for f in raw_files
        ]
    except Exception as e:
        logger.debug(f"adopt: 拉 {name[:40]} 文件列表失败: {e}")

    # qB category → user_hint（规范化后查别名表）
    user_hint = None
    raw_cat = t.get('category') or ''
    cat_norm = _normalize_category(raw_cat)
    mapped = _CATEGORY_ALIASES.get(cat_norm)
    if mapped:
        user_hint = {'media_type': mapped}
        logger.info(f"adopt: {name[:40]} 命中 qB category={raw_cat!r} → {mapped}")

    # 识别 + 算目标
    identified = identify_media(name, files=files, user_hint=user_hint)
    confidence = float(identified.get('confidence') or 0.0)
    media_type = identified.get('media_type') or 'unknown'
    source = identified.get('source') or 'unknown'

    target_info = _resolve_target_safe(identified)
    library_id = target_info.get('library_id') or ''
    target_path = target_info.get('target_path') or ''
    move_mode = target_info.get('move_mode') or 'copy'

    # 决策：高置信 + library 已配 + target_path 算得出 + media_type 已识别 → 自动入流水线
    auto_ok = (
        confidence >= CONFIDENCE_AUTO_THRESHOLD
        and library_id
        and target_path
        and media_type != 'unknown'
    )

    # 决定起始 phase + status：
    #   ① 不自动通过 → analyzing + needs_review（停在分析阶段等审核）
    #   ② 自动通过 + qB 已下完 → download_done + running（让 pipeline 直接 claim）
    #   ③ 自动通过 + qB 还在下 → downloading + 映射 qB state（让 watcher 看着）
    progress = float(t.get('progress') or 0.0)
    if not auto_ok:
        phase = PHASE_ANALYZING
        phase_status = STATUS_NEEDS_REVIEW
        status_msg = _build_review_reason(identified, target_info)
    elif is_qb_download_done(state, progress):
        from tools.dispatch.phases import PHASE_DOWNLOAD_DONE
        phase = PHASE_DOWNLOAD_DONE
        phase_status = STATUS_RUNNING
        status_msg = f"自动认领（已下完）：source={source} confidence={confidence:.2f}"
    else:
        phase = PHASE_DOWNLOADING
        phase_status = map_qb_state_to_download_status(state)
        status_msg = f"自动认领（qB {state}）：source={source} confidence={confidence:.2f}"

    # qB 视角的加种时间（构成"任务身份"的一部分）
    qb_added_on = None
    try:
        if t.get('added_on'):
            qb_added_on = datetime.utcfromtimestamp(int(t['added_on']))
    except (TypeError, ValueError):
        pass

    _write_dispatch_row(
        torrent_hash=h,
        identified=identified,
        target_info=target_info,
        phase=phase,
        phase_status=phase_status,
        status_message=status_msg,
        move_mode=move_mode,
        qb_added_on=qb_added_on,
    )

    label = '自动入流水线' if auto_ok else '挂到待审核'
    # 完整诊断：除了 source/confidence，还带上 title/year/target_path —— 这才是出 bug 时
    # 真正想要看到的内容（之前 "Z:/videos/movie/ ()" 那个 bug 就是因为日志只 log 了 source
    # 但没 log title/year，事后想知道 title 是不是空只能靠猜）
    logger.info(
        f"adopt: {name[:60]} → {label} phase={phase} "
        f"(media_type={media_type}, title={identified.get('title')!r}, "
        f"year={identified.get('year')!r}, target_path={target_info.get('target_path')!r}, "
        f"confidence={confidence:.2f}, source={source})"
    )
    # 自动通过：根据起始 phase 触发对应 worker
    if auto_ok:
        try:
            if phase == PHASE_DOWNLOAD_DONE:
                from tools.dispatch.pipeline_worker import trigger as pipeline_trigger
                pipeline_trigger.set()
            else:
                from tools.dispatch.downloader_watcher import trigger as dl_trigger
                dl_trigger.set()
        except Exception:
            pass
    return 'auto' if auto_ok else 'review'


def _resolve_target_safe(identified: Dict) -> Dict:
    """包一层异常保护：识别结果 → target_library_id + target_path。"""
    try:
        from backend.api.dispatch import _resolve_target
        return _resolve_target(identified)
    except Exception as e:
        logger.warning(f"_resolve_target 失败: {e}")
        return {'library_id': '', 'target_path': '', 'move_mode': 'copy', 'rule': None}


def _build_review_reason(identified: Dict, target_info: Dict) -> str:
    """拼出待审核原因（一句话），给 review modal 顶部展示。"""
    reasons = []
    confidence = float(identified.get('confidence') or 0.0)
    if confidence < CONFIDENCE_AUTO_THRESHOLD:
        reasons.append(f"置信度 {confidence:.2f} < {CONFIDENCE_AUTO_THRESHOLD}")
    media_type = identified.get('media_type')
    if not media_type or media_type == 'unknown':
        reasons.append("media_type 未识别")
    # 按 media_type 检查关键标题字段（_resolve_target 同一套规则）
    if media_type == 'movie':
        # movie 只强制要求 title；year 缺失允许通过，target_path 渲染时会自动收掉空括号
        if not identified.get('title'):
            reasons.append("标题未识别")
    elif media_type == 'tv':
        if not identified.get('series_name'):
            reasons.append("剧名未识别")
    elif media_type == 'anime':
        if not (identified.get('series_name') or identified.get('title')):
            reasons.append("番剧名未识别")
    elif media_type == 'adult':
        if not (identified.get('code') or identified.get('title')):
            reasons.append("番号未识别")
    if not target_info.get('library_id'):
        reasons.append("目标库未配置")
    if not target_info.get('target_path'):
        reasons.append("目标路径未生成")
    return "需人工审核：" + "；".join(reasons) if reasons else "需人工审核"


def _write_dispatch_row(
    torrent_hash: str,
    identified: Dict,
    target_info: Dict,
    phase: str,
    phase_status: str,
    status_message: str,
    move_mode: str = 'copy',
    qb_added_on=None,
):
    """写 dispatch_map 行：phase / phase_status 由 adopt 决策决定。
    qb_added_on：qB 视角的加种时间，构成 (torrent_hash, qb_added_on) 任务身份。
    """
    from backend.database import SessionLocal, DownloadDispatchMap

    with SessionLocal() as db:
        # 防并发重复插入
        existing = db.query(DownloadDispatchMap).filter_by(torrent_hash=torrent_hash).first()
        if existing:
            return

        row = DownloadDispatchMap(
            torrent_hash=torrent_hash,
            phase=phase,
            phase_status=phase_status,
            media_type=identified.get('media_type') or 'unknown',
            tmdb_id=str(identified.get('tmdb_id') or '') or None,
            imdb_id=str(identified.get('imdb_id') or '') or None,
            series_tmdb_id=str(identified.get('series_tmdb_id') or '') or None,
            series_name=identified.get('series_name'),
            title=identified.get('title'),
            year=identified.get('year'),
            target_library_id=target_info.get('library_id') or None,
            target_root=target_info.get('library_root') or None,
            target_path=target_info.get('target_path') or None,
            move_mode=move_mode,
            status_message=status_message,
            created_at=datetime.utcnow(),
            qb_added_on=qb_added_on,
        )
        db.add(row)
        db.commit()
