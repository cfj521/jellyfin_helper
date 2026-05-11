"""
配额管理：/downloads (NVMe, 限 400G) 容量监控 + 主动清理。

两套清理：
  ① 配额清理（quota_check_and_cleanup）—— 容量逼近阈值时硬清，**不看分享率**。
     按"代价从小到大"排序：已转移 + 已达 ratio 优先清；不够再升级到牺牲档（未达 ratio 的私有种）。
  ② 常规软清（regular_cleanup）—— 定时跑，按分享率/做种天数清。

trash_dir 清理：保留 N 天后删除。
"""
from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from common.qbittorrent_client import QBittorrentClient
from web.backend.config import settings
from web.backend.database import SessionLocal, DownloadDispatchMap
from tools.dispatch.phases import (
    PHASE_ALL_JOBS_DONE, PHASE_CLEANED, ACTIVE_PHASES, DISPATCHED_PHASES,
    STATUS_SUCCEEDED,
)

logger = logging.getLogger(__name__)


def _get_qb() -> QBittorrentClient:
    return QBittorrentClient(
        host=settings.qbittorrent_host,
        username=settings.qbittorrent_username,
        password=settings.qbittorrent_password,
    )


def _save_path_under_downloads(save_path: Optional[str]) -> bool:
    """
    判断 qB 报的 save_path 是否落在配置的下载根目录（settings.qbittorrent_download_path）下。
    用于守卫 delete_files=True：只允许在 downloads 区域内删文件。

    save_path 可能：
      - 跟下载根完全相等：'/downloads'
      - 是子目录：'/downloads/movies/foo'
      - 完全不同的路径（用户在 Jackett RSS 规则里改 save_path 到 /library/...）

    比对前都规范化：去尾部斜杠 + 转小写（Windows 不区分大小写）+ 统一分隔符。
    任一为空/无效 → 视为 False（保守，禁止删文件）。
    """
    if not save_path:
        return False
    root = (settings.qbittorrent_download_path or '').strip()
    if not root:
        return False

    def _norm(p: str) -> str:
        # Windows path separators 统一 → 转 posix；去尾斜杠；小写
        p = p.replace('\\', '/').rstrip('/')
        return p.lower()

    sp = _norm(save_path)
    rt = _norm(root)
    # 完全相等 OR sp 是 rt 的真子目录（防止 /downloads_other 被误判为 /downloads/...）
    return sp == rt or sp.startswith(rt + '/')


def _safe_delete(client: QBittorrentClient, torrent_hash: str, save_path: Optional[str]) -> tuple[bool, bool]:
    """
    统一的删除入口：检查 save_path 是否在 downloads 区域内，
      在 → delete_files=True
      不在 → delete_files=False（仅移除 qB 任务，保留 /library 等外部路径的文件）

    返回 (success, deleted_files)。deleted_files 是实际是否带文件删（用于 freed_bytes 统计）。
    """
    safe_for_files = _save_path_under_downloads(save_path)
    if not safe_for_files:
        logger.warning(
            f"清理 {torrent_hash[:16]}.. save_path={save_path!r} 不在配置的下载根 "
            f"{settings.qbittorrent_download_path!r} 下，降级为仅移除 qB 任务（保留文件）"
        )
    ok = client.delete(torrent_hash, delete_files=safe_for_files)
    return ok, safe_for_files


def _resolve_quota_limit() -> Optional[int]:
    """
    动态拿 limit：shutil.disk_usage(download_path).total。
    路径未配 / stat 失败 → 返回 None（上游据此禁用配额监视和清理）。
    """
    download_path = (settings.qbittorrent_download_path or '').strip()
    if not download_path:
        return None
    try:
        return int(shutil.disk_usage(download_path).total)
    except FileNotFoundError:
        logger.warning(f"配额: 后端 stat 不到 {download_path!r}，禁用配额监视/清理")
    except Exception as e:
        logger.warning(f"配额: shutil.disk_usage({download_path!r}) 失败: {e}，禁用配额监视/清理")
    return None


def get_disk_usage() -> Dict:
    """
    返回下载盘用量统计 —— 全部按真实磁盘空间（shutil.disk_usage）。
      - used: 盘真实已用（含 jellyfin-tools 种子 + 盘上其他东西）
      - limit: 盘真实容量
      - dispatch_used: qB 种子 size 求和（"我们占了多少"，仅展示）
      - torrents_count: qB 上的种子数（仅展示）

    返回: {
        used: bytes 已用,
        limit: bytes 配额（None 时为 0）,
        ratio: 0.0~1.0+,
        state: 'normal' / 'warn' / 'cleanup' / 'over' / 'disabled' / 'unknown',
        torrents_count: N,
        dispatch_used: bytes (我们的种子总大小),
    }
    """
    cfg = settings.qbittorrent_quota
    limit = _resolve_quota_limit()

    # 路径 stat 不到 → 禁用配额功能（不报错，不强清，UI 灰掉）
    if limit is None:
        return {
            'used': 0, 'limit': 0, 'ratio': 0.0,
            'state': 'disabled', 'torrents_count': 0, 'dispatch_used': 0,
        }

    # used 用 shutil 真实读 —— 反映"盘真实已用"（含其他东西占用）
    download_path = (settings.qbittorrent_download_path or '').strip()
    try:
        du = shutil.disk_usage(download_path)
        used = int(du.used)
    except Exception as e:
        logger.warning(f"shutil.disk_usage(used) 失败: {e}")
        used = 0

    # dispatch_used 单独算，纯展示用（"我们占了多少 / 别人占了多少"对比）
    dispatch_used = 0
    n = 0
    try:
        client = _get_qb()
        client.login()
        for t in client.list_torrents() or []:
            n += 1
            dispatch_used += int(t.get('size') or 0)
    except Exception as e:
        logger.debug(f"qB list 失败（不影响 used 真实读）: {e}")

    ratio = used / limit if limit else 0.0
    if ratio >= 1.0:
        state = 'over'
    elif ratio >= cfg.cleanup_threshold:
        state = 'cleanup'
    elif ratio >= cfg.warn_threshold:
        state = 'warn'
    else:
        state = 'normal'

    return {
        'used': used, 'limit': limit, 'ratio': ratio,
        'state': state, 'torrents_count': n,
        'dispatch_used': dispatch_used,
    }


# ============================================================================
# 配额清理（硬清，不看分享率）
# ============================================================================

def _cost_score(row: DownloadDispatchMap, qb_torrent: Optional[Dict]) -> tuple:
    """
    清理代价排序键（越小越优先清）。综合：
      - 已转移到库 vs 还在主流水线（前者代价小）
      - 已达 target_ratio vs 未达（前者代价小）
      - 大文件 vs 小文件（释放空间快的优先清，权重次于上两者）
      - 距离最早可清时间更早的（cleanup_eligible_at）
    """
    seeding_cfg = settings.qbittorrent_seeding
    target_ratio = seeding_cfg.target_ratio

    # 0 = 已转移；1 = 主流水线在跑（不动）
    # 已完成全流程或已被清过 → 视为零代价档（其它都按 1 处理）
    is_dispatched = 0 if row.phase in DISPATCHED_PHASES else 1

    # 0 = 已达 ratio；1 = 未达
    cur_ratio = float(row.last_seen_ratio or 0)
    ratio_met = 0 if cur_ratio >= target_ratio else 1

    # 大文件优先清（按 size 反向）
    size = (qb_torrent or {}).get('size') or 0
    size_inv = -int(size)

    # 早创建的优先清
    age_seconds = -int((row.created_at or datetime.utcnow()).timestamp())

    return (is_dispatched, ratio_met, size_inv, age_seconds)


def quota_check_and_cleanup() -> Optional[Dict]:
    """
    检查配额，超阈值则强清直到回到 target_after_cleanup。
    返回: 清理 summary 或 None（无需清理）。
    """
    cfg = settings.qbittorrent_quota
    if not cfg.enabled:
        return None

    usage = get_disk_usage()
    # disabled = stat 不到下载盘；normal/warn = 没到清理阈值；unknown = qB 不通
    if usage['state'] in ('normal', 'warn', 'unknown', 'disabled'):
        return None

    target_ratio = cfg.target_after_cleanup
    limit = int(usage.get('limit') or 0)
    target_bytes = int(target_ratio * limit)
    logger.warning(
        f"⚠️ 配额触发清理：当前 {usage['ratio']*100:.1f}% "
        f"({usage['used']/1e9:.1f}/{limit/1e9:.0f}GB)，"
        f"目标降至 {target_ratio*100:.0f}% ({target_bytes/1e9:.0f}GB)"
    )

    try:
        client = _get_qb()
        client.login()
        all_torrents = client.list_torrents() or []
    except Exception as e:
        logger.error(f"配额清理时 qB 不通: {e}")
        return None

    qb_by_hash = {(t.get('hash') or '').lower(): t for t in all_torrents}

    cleaned: List[Dict] = []
    freed_bytes = 0
    target_freed = usage['used'] - target_bytes  # 至少要释放这么多

    with SessionLocal() as db:
        # 候选：已落 dispatch_map 的种子（未落库的不算可清候选 —— 它还没走完流水线）
        rows = db.query(DownloadDispatchMap).all()
        rows.sort(key=lambda r: _cost_score(r, qb_by_hash.get(r.torrent_hash)))

        for row in rows:
            if freed_bytes >= target_freed:
                break
            t = qb_by_hash.get(row.torrent_hash)
            if not t:
                continue
            size = int(t.get('size') or 0)
            if size <= 0:
                continue

            # 跳过仍在主流水线运行的种子（避免清掉正在 copy 的源）
            # 主流水线 / 后处理在跑的 / 等外部事件中的（downloading、jellyfin_recognizing 等）
            # —— 一律不动，避免清掉正在 copy 的源 / 还没识别完的种子
            if row.phase in ACTIVE_PHASES:
                logger.debug(
                    f"配额清理: 跳过 {row.torrent_hash[:16]}.. ({t.get('name')!r}) "
                    f"phase={row.phase} 仍活跃"
                )
                continue

            reason = _classify_clean_reason(row)
            logger.warning(
                f"配额清理: 选中 {row.torrent_hash[:16]}.. ({t.get('name')!r}) "
                f"size={size/1e9:.2f}GB phase={row.phase} ratio={row.last_seen_ratio} "
                f"reason={reason!r}"
            )

            # 真正删除：删 qB 任务 + 删文件（仅当 save_path 落在 downloads 根下）
            try:
                ok, deleted_files = _safe_delete(client, row.torrent_hash, t.get('save_path'))
                if ok:
                    # 没删文件就别记 freed_bytes（实际磁盘没释放）
                    if deleted_files:
                        freed_bytes += size
                    cleaned.append({
                        'hash': row.torrent_hash,
                        'name': t.get('name'),
                        'size_gb': round(size / 1e9, 2),
                        'phase': row.phase,
                        'ratio': float(row.last_seen_ratio or 0),
                        'reason': reason,
                        'deleted_files': deleted_files,
                    })
                    # 标记 dispatch_map.cleaned
                    row.phase = PHASE_CLEANED
                    row.phase_status = STATUS_SUCCEEDED
                    row.cleaned_at = datetime.utcnow()
                else:
                    logger.warning(
                        f"配额清理: qB delete 返回 false {row.torrent_hash[:16]}.."
                    )
            except Exception as e:
                logger.warning(f"配额清理失败 {row.torrent_hash[:16]}..: {e}")

        db.commit()

    logger.warning(
        f"✓ 配额清理完成：{len(cleaned)} 个种子，释放 {freed_bytes/1e9:.1f}GB"
    )
    return {
        'cleaned_count': len(cleaned),
        'freed_bytes': freed_bytes,
        'cleaned': cleaned,
        'before': usage,
    }


def _classify_clean_reason(row: DownloadDispatchMap) -> str:
    """给清理日志用的原因分类。"""
    target_ratio = settings.qbittorrent_seeding.target_ratio
    cur = float(row.last_seen_ratio or 0)
    if cur >= target_ratio:
        return f'已达分享率 {cur:.2f}'
    if row.phase in DISPATCHED_PHASES:
        return f'已转移但未达分享率 {cur:.2f} (配额牺牲档)'
    return '其他'


# ============================================================================
# 常规软清（按分享率 / 做种天数）
# ============================================================================

def regular_cleanup() -> Optional[Dict]:
    """
    定时跑：清理已达 target_ratio 或 min_seed_days 的种子。
    """
    seeding_cfg = settings.qbittorrent_seeding
    target_ratio = seeding_cfg.target_ratio
    min_days = seeding_cfg.min_seed_days
    min_hours = seeding_cfg.min_seed_hours_per_torrent

    try:
        client = _get_qb()
        client.login()
        all_torrents = client.list_torrents() or []
    except Exception as e:
        logger.warning(f"软清 qB 不通: {e}")
        return None
    qb_by_hash = {(t.get('hash') or '').lower(): t for t in all_torrents}

    cleaned = []
    freed = 0

    with SessionLocal() as db:
        rows = (
            db.query(DownloadDispatchMap)
            .filter(DownloadDispatchMap.phase == PHASE_ALL_JOBS_DONE)
            .all()
        )
        for row in rows:
            t = qb_by_hash.get(row.torrent_hash)
            if not t:
                continue
            seeding_time = int(t.get('seeding_time') or 0)
            ratio = float(t.get('ratio') or 0)
            size = int(t.get('size') or 0)

            # 不到最小做种 24h 不动
            if seeding_time < min_hours * 3600:
                continue
            # 任一条件达成则清
            ratio_ok = ratio >= target_ratio
            days_ok = seeding_time >= min_days * 86400
            if not (ratio_ok or days_ok):
                continue

            logger.info(
                f"软清: 选中 {row.torrent_hash[:16]}.. ({t.get('name')!r}) "
                f"size={size/1e9:.2f}GB ratio={ratio:.2f} "
                f"seed_days={seeding_time/86400:.1f} "
                f"reason={'ratio' if ratio_ok else 'days'}"
            )

            try:
                ok, deleted_files = _safe_delete(client, row.torrent_hash, t.get('save_path'))
                if ok:
                    cleaned.append({
                        'hash': row.torrent_hash, 'name': t.get('name'),
                        'size_gb': round(size / 1e9, 2),
                        'ratio': ratio, 'seed_days': round(seeding_time / 86400, 1),
                        'deleted_files': deleted_files,
                    })
                    if deleted_files:
                        freed += size
                    row.phase = PHASE_CLEANED
                    row.phase_status = STATUS_SUCCEEDED
                    row.cleaned_at = datetime.utcnow()
            except Exception as e:
                logger.warning(f"软清失败 {row.torrent_hash[:16]}..: {e}")
        db.commit()

    if cleaned:
        logger.info(
            f"软清完成：{len(cleaned)} 个种子，释放 {freed/1e9:.1f}GB"
        )
    return {'cleaned_count': len(cleaned), 'freed_bytes': freed, 'cleaned': cleaned}
