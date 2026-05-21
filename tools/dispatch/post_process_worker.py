"""
PostProcessWorker：claim phase=jellyfin_recognize_done 的种子，跑后处理链。

链式段（同函数调用栈一气呵成）：
    subtitle_fetching → audio_track_order_adjusting → all_jobs_done

跟 dispatch-pipeline 一样：
  - 单 worker 串行
  - 每 step 写库标 phase（给 UI 看进度 + 崩溃恢复）
  - status=running 中途崩溃的，启动时 reclaim 重跑（要求 step 幂等）
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional

from backend.database import SessionLocal, DownloadDispatchMap
from tools.dispatch.phases import (
    PHASE_JELLYFIN_RECOGNIZE_DONE,
    PHASE_SUBTITLE_ALIGNING,
    PHASE_SUBTITLE_FETCHING, PHASE_AUDIO_TRACK_ORDER_ADJUSTING, PHASE_ALL_JOBS_DONE,
    STATUS_RUNNING, STATUS_SUCCEEDED, STATUS_FAILED, STATUS_WARNED, STATUS_SKIPPED,
)
from tools.dispatch.pipeline_worker import _set_phase, _record_phase_timing, _row_to_dict

logger = logging.getLogger(__name__)


# 触发 event：jellyfin-watcher 把行推到 jellyfin_recognize_done 后调 trigger.set()
trigger = threading.Event()

# 内部硬编码 wait 超时
WAIT_TIMEOUT = 30


class PostProcessWorker:
    """单 worker 串行：jellyfin_recognize_done → subtitle_fetching → audio_track_order_adjusting → all_jobs_done。"""

    def __init__(self, stop_event: Optional[threading.Event] = None):
        self.stop_event = stop_event or threading.Event()

    def _claim_next(self) -> Optional[Dict]:
        """取下一条要处理的种子。优先级：
          ① 链式段中断恢复（subtitle_aligning / subtitle_fetching / audio_track_order_adjusting + status=running）
          ② 新行（jellyfin_recognize_done + status ∈ {running, succeeded, warned, skipped}）

        warned/skipped 也认领：jellyfin 没识别到不阻断字幕/音轨处理。
        """
        with SessionLocal() as db:
            # 优先：链式段崩溃恢复
            row = (
                db.query(DownloadDispatchMap)
                .filter(DownloadDispatchMap.phase.in_([
                    PHASE_SUBTITLE_ALIGNING,
                    PHASE_SUBTITLE_FETCHING, PHASE_AUDIO_TRACK_ORDER_ADJUSTING,
                ]))
                .filter(DownloadDispatchMap.phase_status == STATUS_RUNNING)
                .order_by(DownloadDispatchMap.dispatched_at)
                .first()
            )
            if row:
                logger.info(f"恢复后处理: {row.torrent_hash[:16]}.. (phase={row.phase})")
                # 重置回 subtitle_aligning 起点重跑（align/subtitle/audio 都设计成幂等）
                row.phase = PHASE_SUBTITLE_ALIGNING
                row.phase_status = STATUS_RUNNING
                row.status_message = '从中断点恢复后处理...'
                db.commit()
                return _row_to_dict(row)

            # 否则取新的 jellyfin_recognize_done（不论 succeeded/warned/skipped 都接）
            row = (
                db.query(DownloadDispatchMap)
                .filter(DownloadDispatchMap.phase == PHASE_JELLYFIN_RECOGNIZE_DONE)
                .order_by(DownloadDispatchMap.dispatched_at)
                .first()
            )
            if not row:
                return None
            row.phase = PHASE_SUBTITLE_ALIGNING
            row.phase_status = STATUS_RUNNING
            row.status_message = '准备字幕对齐...'
            db.commit()
            return _row_to_dict(row)

    def run_forever(self):
        from tools.dispatch.post_process import (
            post_process_subtitle, post_process_audio, post_process_subtitle_align,
        )

        logger.info(f"PostProcessWorker 启动（事件驱动 + {WAIT_TIMEOUT}s 兜底）")
        last_heartbeat = time.time()
        HEARTBEAT_SECONDS = 600

        while not self.stop_event.is_set():
            row = self._claim_next()
            if not row:
                trigger.wait(timeout=WAIT_TIMEOUT)
                trigger.clear()
                if time.time() - last_heartbeat > HEARTBEAT_SECONDS:
                    logger.info("PostProcessWorker heartbeat: alive, idle")
                    last_heartbeat = time.time()
                continue
            last_heartbeat = time.time()

            h = row['torrent_hash']
            files = row.get('dispatched_files') or []

            # 没文件就直接跳到终态
            if not files:
                _record_phase_timing(h, PHASE_SUBTITLE_FETCHING, 0)
                _record_phase_timing(h, PHASE_AUDIO_TRACK_ORDER_ADJUSTING, 0)
                _set_phase(h, PHASE_ALL_JOBS_DONE, STATUS_SKIPPED,
                           message='无 dispatched_files，跳过后处理')
                continue

            try:
                # Step 0: subtitle_aligning —— release 自带的无 lang 标签 sidecar 嗅探内容并改名
                # 必须先于 subtitle_fetching，否则下载器会因 sidecar 没 lang 标签当作"缺 chs"重复下
                t0 = time.time()
                _set_phase(h, PHASE_SUBTITLE_ALIGNING, STATUS_RUNNING, message='字幕文件名对齐中...')
                status, info = post_process_subtitle_align(files)
                _set_phase(h, PHASE_SUBTITLE_ALIGNING, _normalize_status(status),
                           message=str(info)[:200])
                _record_phase_timing(h, PHASE_SUBTITLE_ALIGNING, time.time() - t0)

                # 如果 align 改了名，刷新 row 里的 dispatched_files —— 下载器要按新名扫
                if isinstance(info, dict) and info.get('renamed_details'):
                    rename_map = dict(info['renamed_details'])
                    files = [rename_map.get(f, f) for f in files]
                    # 同步写库，避免别处再读到旧路径
                    with SessionLocal() as db:
                        r = db.query(DownloadDispatchMap).filter_by(torrent_hash=h).first()
                        if r:
                            r.dispatched_files = files
                            db.commit()

                # Step 1: subtitle_fetching
                t0 = time.time()
                _set_phase(h, PHASE_SUBTITLE_FETCHING, STATUS_RUNNING, message='字幕处理中...')
                status, info = post_process_subtitle(files)
                _set_phase(h, PHASE_SUBTITLE_FETCHING, _normalize_status(status),
                           message=str(info)[:200])
                _record_phase_timing(h, PHASE_SUBTITLE_FETCHING, time.time() - t0)

                # Step 2: audio_track_order_adjusting
                t0 = time.time()
                _set_phase(h, PHASE_AUDIO_TRACK_ORDER_ADJUSTING, STATUS_RUNNING,
                           message='音轨调整中...')
                status, info = post_process_audio(files)
                _set_phase(h, PHASE_AUDIO_TRACK_ORDER_ADJUSTING, _normalize_status(status),
                           message=str(info)[:200])
                _record_phase_timing(h, PHASE_AUDIO_TRACK_ORDER_ADJUSTING, time.time() - t0)

                # 终态
                _set_phase(h, PHASE_ALL_JOBS_DONE, STATUS_SUCCEEDED, message='全部处理完成')

            except Exception as e:
                logger.exception(f"post_process 异常 {h[:16]}..")
                # 失败留在当前 phase，sweeper 会捞起来重派
                _set_phase(h, PHASE_SUBTITLE_FETCHING, STATUS_FAILED,
                           message='post_process 内部异常', error=str(e))

        logger.info("PostProcessWorker 退出")


def _normalize_status(legacy_status: str) -> str:
    """post_process 模块返回的 status 字符串映射到我们的标准 status。"""
    if legacy_status in ('ok', 'succeeded', 'success'):
        return STATUS_SUCCEEDED
    if legacy_status == 'warned':
        return STATUS_WARNED
    if legacy_status == 'skipped':
        return STATUS_SKIPPED
    if legacy_status == 'failed':
        return STATUS_FAILED
    # running 不应该出现在 step 结果里，兜底当 succeeded
    return STATUS_SUCCEEDED
