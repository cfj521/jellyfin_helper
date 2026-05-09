"""
Sweeper：兜底全局轮询 worker，180s 一轮（用户可调，配置项 dispatch.poll_interval_seconds）。

职责：
  ① status=failed 的行 → 按当前 phase 重派给对应 worker（覆盖策略：写 status=running 让 worker 重跑）
  ② phase ∈ 链式段 + status=running + 30min 无更新（updated_at）→ 视为 zombie，重派
  ③ 顺手 trigger 各 worker（万一谁的 trigger 漏了）

不处理：
  - status=needs_review（等用户）
  - status=succeeded / warned / skipped（已正常推进/移交下一 worker）
  - status=paused / stalled（qB 自身状态，让 qB 处理）
  - phase=cleaned / dismissed / all_jobs_done（终态）
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, List

from web.backend.config import settings
from web.backend.database import SessionLocal, DownloadDispatchMap
from tools.dispatch.phases import (
    PHASE_ANALYZING, PHASE_DISPATCH_QUEUED, PHASE_DOWNLOADING, PHASE_DOWNLOAD_DONE,
    PHASE_COPYING, PHASE_ORGANIZING, PHASE_JELLYFIN_RECOGNIZING, PHASE_JELLYFIN_RECOGNIZE_DONE,
    PHASE_SUBTITLE_FETCHING, PHASE_AUDIO_TRACK_ORDER_ADJUSTING, PHASE_ALL_JOBS_DONE,
    PHASE_CLEANED, PHASE_DISMISSED,
    PIPELINE_CHAIN_PHASES, POSTPROCESS_CHAIN_PHASES,
    STATUS_RUNNING, STATUS_FAILED,
)

logger = logging.getLogger(__name__)


# zombie 阈值：链式段内 status=running 但 updated_at 落后超过这个时长 → 视为卡住
ZOMBIE_THRESHOLD_MINUTES = 30

# 最大重试次数：同一 phase 上 sweeper 恢复 N 次仍失败 → 放弃，避免死循环
MAX_RECOVERY_ATTEMPTS = 3


def run_sweeper_loop(stop_event: threading.Event):
    """独立线程：周期性扫整表，恢复失败 / zombie 行。"""
    interval = max(30, int(settings.dispatch.poll_interval_seconds or 180))
    logger.info(f"sweeper 启动（{interval}s 周期）")
    import time as _t
    last_heartbeat = _t.time()
    HEARTBEAT_SECONDS = 600

    while not stop_event.is_set():
        if stop_event.wait(timeout=interval):
            break
        try:
            sweep_once()
        except Exception as e:
            logger.warning(f"sweeper 异常: {e}", exc_info=True)

        if _t.time() - last_heartbeat > HEARTBEAT_SECONDS:
            logger.info("sweeper heartbeat: alive, idle")
            last_heartbeat = _t.time()
    logger.info("sweeper 退出")


def sweep_once() -> Dict:
    """跑一遍：恢复 failed + zombie。返回 stats。"""
    stats = {
        'failed_recovered': 0,
        'zombies_recovered': 0,
        'triggers_fired': 0,
    }

    zombie_cutoff = datetime.utcnow() - timedelta(minutes=ZOMBIE_THRESHOLD_MINUTES)

    with SessionLocal() as db:
        # ① status=failed 的所有行（除了终态）
        failed_rows = (
            db.query(DownloadDispatchMap)
            .filter(DownloadDispatchMap.phase_status == STATUS_FAILED)
            .filter(~DownloadDispatchMap.phase.in_([
                PHASE_CLEANED, PHASE_DISMISSED, PHASE_ALL_JOBS_DONE,
            ]))
            .all()
        )
        for row in failed_rows:
            # 检查重试次数：phase_timings 里记 sweeper_attempts.<phase> 计数
            timings = dict(row.phase_timings or {})
            attempts_map = dict(timings.get('sweeper_attempts') or {})
            attempts = int(attempts_map.get(row.phase, 0))

            if attempts >= MAX_RECOVERY_ATTEMPTS:
                # 已重试 N 次仍失败 → 放弃恢复，留 status=failed 等人工介入
                logger.warning(
                    f"sweeper: {row.torrent_hash[:16]}.. 在 phase={row.phase} 已重试 "
                    f"{attempts} 次仍失败，放弃自动恢复（需人工处理：检查 path_mappings / qB 路径配置 / 磁盘）"
                )
                continue

            new_phase = _recover_target_phase(row.phase)
            if new_phase is None:
                continue
            old_phase = row.phase
            row.phase = new_phase
            row.phase_status = STATUS_RUNNING
            row.status_message = (
                f'sweeper 重派：{old_phase}/failed → {new_phase}/running '
                f'(尝试 {attempts + 1}/{MAX_RECOVERY_ATTEMPTS})'
            )
            attempts_map[old_phase] = attempts + 1
            timings['sweeper_attempts'] = attempts_map
            row.phase_timings = timings
            stats['failed_recovered'] += 1
            logger.info(
                f"sweeper: {row.torrent_hash[:16]}.. failed 恢复 "
                f"{old_phase} → {new_phase} (尝试 {attempts + 1}/{MAX_RECOVERY_ATTEMPTS})"
            )

        # ② zombie：链式段 status=running 但 updated_at 太老
        chain_phases = list(PIPELINE_CHAIN_PHASES | POSTPROCESS_CHAIN_PHASES)
        zombie_rows = (
            db.query(DownloadDispatchMap)
            .filter(DownloadDispatchMap.phase.in_(chain_phases))
            .filter(DownloadDispatchMap.phase_status == STATUS_RUNNING)
            .filter(DownloadDispatchMap.updated_at < zombie_cutoff)
            .all()
        )
        for row in zombie_rows:
            new_phase = _recover_target_phase(row.phase)
            if new_phase is None:
                continue
            old_phase = row.phase
            row.phase = new_phase
            row.phase_status = STATUS_RUNNING
            row.status_message = (
                f'sweeper zombie 恢复：{old_phase} 卡住 >{ZOMBIE_THRESHOLD_MINUTES}min'
            )
            stats['zombies_recovered'] += 1
            logger.warning(
                f"sweeper: {row.torrent_hash[:16]}.. zombie 恢复 "
                f"{old_phase} 卡 {ZOMBIE_THRESHOLD_MINUTES}min+ → {new_phase}"
            )

        db.commit()

    # ③ 触发各 worker（万一谁的 trigger 漏了，让他们立刻去看）
    if stats['failed_recovered'] or stats['zombies_recovered']:
        _fire_all_triggers()
        stats['triggers_fired'] = 1

    if stats['failed_recovered'] or stats['zombies_recovered']:
        logger.info(
            f"sweeper 完成：failed={stats['failed_recovered']} "
            f"zombies={stats['zombies_recovered']}"
        )
    return stats


def _recover_target_phase(current_phase: str) -> str:
    """
    把"失败/僵尸"的行重置回它该重跑的起点 phase（覆盖策略）。

    映射规则：
      - 链式段（copying/organizing/jellyfin_recognizing）→ copying（从头跑，幂等）
      - 后处理链（subtitle_fetching/audio_track_order_adjusting）→ subtitle_fetching
      - 等待段（downloading）→ 自己重跑（watcher 自然处理）
      - jellyfin_recognizing → 单独：让 jellyfin-watcher 重新查
      - download_done / dispatch_queued / analyzing / jellyfin_recognize_done → 各自原地重跑
      - 终态：返回 None
    """
    # 终态不动
    if current_phase in (PHASE_CLEANED, PHASE_DISMISSED, PHASE_ALL_JOBS_DONE):
        return None

    # 链式段重头跑（pipeline 会从 copying claim 开始幂等执行）
    if current_phase in PIPELINE_CHAIN_PHASES:
        return PHASE_COPYING

    # 后处理链重头跑
    if current_phase in POSTPROCESS_CHAIN_PHASES:
        return PHASE_SUBTITLE_FETCHING

    # 其他原地重置（让对应 worker 重新看）
    return current_phase


def _fire_all_triggers():
    """触发所有 worker 的 trigger.set()，立刻把恢复后的行接走。"""
    targets = [
        ('tools.dispatch.analyzer', 'trigger'),
        ('tools.dispatch.downloader_watcher', 'trigger'),
        ('tools.dispatch.pipeline', 'trigger'),
        ('tools.dispatch.jellyfin_watcher', 'trigger'),
        ('tools.dispatch.post_process_worker', 'trigger'),
    ]
    for module_name, attr in targets:
        try:
            import importlib
            mod = importlib.import_module(module_name)
            ev = getattr(mod, attr, None)
            if ev is not None:
                ev.set()
        except Exception:
            pass
