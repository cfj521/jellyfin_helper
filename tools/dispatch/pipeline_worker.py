"""
DispatchPipeline：单 worker 串行处理 dispatch_map(phase=download_done) 的种子。

主流水线链式段（同函数调用栈一气呵成）：
    copying → organizing → jellyfin_recognizing（设完释放 worker，jellyfin-watcher 接管）

后处理（subtitle_fetching / audio_track_order_adjusting）由独立 PostProcessWorker
在 jellyfin-watcher 确认入库（phase=jellyfin_recognize_done）后处理，避免阻塞主线。

---
tools/dispatch/ 目录的命名 taxonomy（避免下次重命名又来争论）：
  *_worker.py     状态机推进者：claim phase 并往后推（本文件 + post_process_worker）
  *_watcher.py    监视外部系统状态变化（downloader_watcher 看 qB；jellyfin_watcher 看 jellyfin scan）
  analyzer.py     一次性分析/识别（analyzing → dispatch_queued）
  sweeper.py      周期扫 failed/zombie 行做恢复
  *_cleaner.py    周期清磁盘（trash_cleaner）
  quota.py        磁盘配额检查 + 软清
  scheduler.py    顶层编排（spawn 上述所有 worker + 跑定时任务）
辅助（非 worker 类）：
  copier.py / organizer.py / identify.py / phases.py / poll.py / post_process.py / adopt.py
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from sqlalchemy import or_

from common.qbittorrent_client import QBittorrentClient
from web.backend.config import settings
from web.backend.database import SessionLocal, DownloadDispatchMap
from tools.dispatch.phases import (
    PHASE_DOWNLOAD_DONE, PHASE_COPYING, PHASE_ORGANIZING, PHASE_JELLYFIN_RECOGNIZING,
    PHASE_SUBTITLE_FETCHING, PHASE_AUDIO_TRACK_ORDER_ADJUSTING, PHASE_ALL_JOBS_DONE,
    PHASE_JELLYFIN_RECOGNIZE_DONE,
    STATUS_RUNNING, STATUS_SUCCEEDED, STATUS_FAILED, STATUS_WARNED, STATUS_SKIPPED,
)

logger = logging.getLogger(__name__)


# 触发 event：上游 worker 把行推到 phase=download_done 后调 trigger.set()
# 主 loop 在 trigger.wait() 上阻塞，被 set 或 poll_interval 超时后查 DB
trigger = threading.Event()


# ============================================================================
# Phase 状态机帮手
# ============================================================================

def _set_phase(row_hash: str, phase: str, phase_status: str = 'running',
               message: Optional[str] = None, error: Optional[str] = None,
               extra: Optional[Dict] = None):
    """更新 dispatch_map 行的 phase + status + message。短事务，不持库锁。
    **每次调用都打日志**：phase 转换是 dispatch 系统的核心事件流，不打日志的话出 bug
    根本不知道种子是怎么走到当前状态的。"""
    with SessionLocal() as db:
        row = db.query(DownloadDispatchMap).filter_by(torrent_hash=row_hash).first()
        if not row:
            logger.warning(f"_set_phase 找不到 row: hash={row_hash[:16]}.. phase={phase}")
            return
        old_phase = row.phase
        old_status = row.phase_status
        row.phase = phase
        row.phase_status = phase_status
        if message is not None:
            row.status_message = message
        if error is not None:
            row.error_log = (row.error_log or '') + f'[{phase}] {error}\n'
        if extra:
            for k, v in extra.items():
                if hasattr(row, k):
                    setattr(row, k, v)
        db.commit()

    # 日志按 status 升级别：failed → ERROR、warned/needs_review → WARNING、其它 → INFO
    log_msg = (
        f"phase {row_hash[:16]}.. {old_phase}/{old_status} → {phase}/{phase_status}"
        + (f" msg={message!r}" if message else '')
        + (f" err={error[:200]!r}" if error else '')
    )
    if phase_status in ('failed',):
        logger.error(log_msg)
    elif phase_status in ('warned', 'needs_review'):
        logger.warning(log_msg)
    else:
        logger.info(log_msg)


def _record_phase_timing(row_hash: str, phase: str, duration_seconds: float):
    """累加 phase 耗时到 phase_timings JSON 字段。"""
    with SessionLocal() as db:
        row = db.query(DownloadDispatchMap).filter_by(torrent_hash=row_hash).first()
        if not row:
            return
        timings = dict(row.phase_timings or {})
        timings[phase] = round(duration_seconds, 1)
        row.phase_timings = timings
        db.commit()


# ============================================================================
# DispatchPipeline 主流水线
# ============================================================================

class DispatchPipeline:
    """单 worker 串行处理。
    claim phase=download_done → copying → organizing → 设 jellyfin_recognizing 后释放 worker。
    jellyfin-watcher 看着 jellyfin_recognizing 等入库完成。
    """

    def __init__(self, stop_event: Optional[threading.Event] = None):
        self.stop_event = stop_event or threading.Event()
        self.qb_client: Optional[QBittorrentClient] = None

    def _get_qb(self) -> QBittorrentClient:
        if self.qb_client is None:
            self.qb_client = QBittorrentClient(
                host=settings.qbittorrent_host,
                username=settings.qbittorrent_username,
                password=settings.qbittorrent_password,
            )
        return self.qb_client

    # ---- 取下一个待处理 ----

    def _claim_next_pending(self) -> Optional[Dict]:
        """
        从 dispatch_map 取下一条要跑的种子。优先级：
          ① phase ∈ {copying, organizing} + status=running
             （重启前在链式段中跑了一半被中断的 → 恢复，幂等重跑）
          ② phase=download_done + status=running（新种子，下载已完成）

        ⚠️ 不要把 jellyfin_recognizing 当成本 worker 该恢复的——那是 jellyfin-watcher 的活。
        pipeline 跑完 copying/organizing 调 _step_jellyfin_send 把 phase 切到
        jellyfin_recognizing/running 是"正常释放给 watcher"，不是崩溃。如果把它纳入恢复，
        pipeline 会无限把自己刚切过去的状态又拉回 copying，形成死循环。

        copy 中断恢复利用 copier.py 的 resume：目标已写部分会续传，size 一致则秒过。
        """
        from sqlalchemy import or_, and_
        with SessionLocal() as db:
            # 优先：链式段进程重启前 status=running 的（崩溃恢复）—— 只看 copying / organizing
            row = (
                db.query(DownloadDispatchMap)
                .filter(DownloadDispatchMap.phase.in_([
                    PHASE_COPYING, PHASE_ORGANIZING,
                ]))
                .filter(DownloadDispatchMap.phase_status == STATUS_RUNNING)
                .order_by(DownloadDispatchMap.created_at)
                .first()
            )
            if row:
                logger.info(f"恢复中断流水线: {row.torrent_hash[:16]}.. (phase={row.phase})")
                # 重置回 copying 起点重跑（copier resume 会跳过已写完的部分）
                row.phase = PHASE_COPYING
                row.phase_status = STATUS_RUNNING
                row.status_message = '从中断点恢复...'
                db.commit()
                return _row_to_dict(row)

            # 否则取一条 download_done 起步
            row = (
                db.query(DownloadDispatchMap)
                .filter(DownloadDispatchMap.phase == PHASE_DOWNLOAD_DONE)
                .filter(DownloadDispatchMap.phase_status == STATUS_RUNNING)
                .order_by(DownloadDispatchMap.created_at)
                .first()
            )
            if not row:
                return None
            row.phase = PHASE_COPYING
            row.phase_status = STATUS_RUNNING
            row.status_message = '准备复制...'
            db.commit()
            return _row_to_dict(row)

    # ---- 主循环 ----

    def run_forever(self):
        # 内部硬编码 wait 超时 30s（不暴露给用户，sweeper 兜底由 sweeper 负责）
        WAIT_TIMEOUT = 30
        logger.info(f"DispatchPipeline 启动（事件驱动 + {WAIT_TIMEOUT}s 兜底）")
        last_heartbeat = time.time()
        HEARTBEAT_SECONDS = 600

        while not self.stop_event.is_set():
            row_dict = self._claim_next_pending()
            if not row_dict:
                trigger.wait(timeout=WAIT_TIMEOUT)
                trigger.clear()
                if time.time() - last_heartbeat > HEARTBEAT_SECONDS:
                    logger.info("DispatchPipeline heartbeat: alive, idle")
                    last_heartbeat = time.time()
                continue
            try:
                self._run_one(row_dict)
            except Exception as e:
                logger.exception(f"pipeline 异常 {row_dict.get('torrent_hash')}")
                # 失败留在 copying（最早进入的链式 phase），sweeper 会重派
                _set_phase(
                    row_dict['torrent_hash'], PHASE_COPYING, STATUS_FAILED,
                    message='pipeline 内部异常', error=str(e),
                )
            last_heartbeat = time.time()  # 处理过种子也算心跳
        logger.info("DispatchPipeline 退出")

    def _run_one(self, row: Dict):
        h = row['torrent_hash']
        logger.info(
            f"开始处理 {h[:16]}.. title={row.get('title')!r} "
            f"media_type={row.get('media_type')!r} target_path={row.get('target_path')!r}"
        )

        # Step 1: copying
        if not self._step_copy(row):
            return  # 阻断：失败已写入 phase

        # Step 2: organizing
        if not self._step_organize(row):
            return

        # Step 3: 触发 jellyfin refresh，phase 切到 jellyfin_recognizing 后释放 worker
        self._step_jellyfin_send(row)

        # 给 qB 种子打标签：library:<media_type> 防重复处理
        try:
            qb = self._get_qb()
            qb.login()
            qb.add_tags(h, [f"library:{row.get('media_type') or 'unknown'}"])
        except Exception as e:
            logger.warning(f"打 qB tag 失败（不阻断）: {e}")

    # ---- Step 实现 ----

    def _step_copy(self, row: Dict) -> bool:
        """
        复制 + 整理一次完成（用 TorrentOrganizer）：
          - 视频按 file_template 重命名落到 target_path
          - 字幕跟随视频
          - junk（sample/nfo/RARBG.txt）入 trash_dir
          - 多视频剧集包按 SxxExx 拆 Season

        如果 dispatch_map 没填 media_type 或 target_path（用户没走 Phase D 加种）
        → 退回简单递归复制，不做精细整理。
        """
        from tools.dispatch.copier import copy_tree_with_progress
        from tools.dispatch.organizer import TorrentOrganizer
        h = row['torrent_hash']
        t0 = time.time()

        # 源路径：从 qB 拿种子 content_path（要走 /info endpoint，不能用 /properties）。
        # /properties 不返回 content_path，会 fallback 到 save_path（=下载根），
        # 翻译后变成盘符根，导致 organizer rglob 整盘扫——历史 bug 就是这么来的。
        try:
            qb = self._get_qb()
            qb.login()
            info = qb.get_torrent_meta(h) or {}
        except Exception as e:
            _set_phase(h, PHASE_COPYING, STATUS_FAILED, error=f'拿不到 qB meta: {e}')
            return False

        src = info.get('content_path')
        if not src:
            _set_phase(
                h, PHASE_COPYING, STATUS_FAILED,
                error=f'qB /info 未返回 content_path（save_path={info.get("save_path")!r}），'
                      f'种子可能还没拿到 metadata',
            )
            return False

        target = row.get('target_path') or row.get('target_root')
        if not target:
            logger.warning(f"{h[:16]}.. 无目标路径，跳过 copy")
            _set_phase(h, PHASE_COPYING, STATUS_SKIPPED, message='无目标路径，跳过 copy')
            return True

        # 保险：target 万一是 jellyfin 视角路径（旧 dispatch_map 行 / 用户手动改过）
        # → 走一次 path_translator 转本地路径
        try:
            from web.backend.path_translator import translate_path_with_settings
            translated_target = translate_path_with_settings(target)
            if translated_target and translated_target != target:
                logger.info(f"target 路径翻译: {target} → {translated_target}")
                target = translated_target
        except Exception as e:
            logger.warning(f"path_translator 失败: {e}")

        # 源路径同样保险（虽然 qB 给的本就是本地路径，但用户可能跨主机部署）
        try:
            from web.backend.path_translator import translate_path_with_settings
            translated_src = translate_path_with_settings(src)
            if translated_src:
                src = translated_src
        except Exception:
            pass

        src_path = Path(src)
        logger.info(f"copy {h[:16]}.. src={src_path} → target={target}")
        if not src_path.exists():
            # 给出清晰的诊断方向：可能 path_mappings 没配 / qB 那边路径异常 / 文件被外部删了
            _set_phase(
                h, PHASE_COPYING, STATUS_FAILED,
                error=(
                    f'源路径不存在: {src_path}（qB 报 save_path={info.get("save_path")!r} '
                    f'content_path={info.get("content_path")!r}）。'
                    f'可能原因：① qB 路径与后端视角不一致，需配置 path_mappings；'
                    f'② qB 下载路径配置错（含特殊字符如 ":"）；'
                    f'③ 文件被外部进程删除'
                ),
            )
            return False

        # 防御：src_path 解析后是盘符 / 系统根目录 → 拒绝（path_mappings 配错时常见 fallback）
        # organizer 自身有同款防御，这里再卡一层让错误信息更早 / 更清楚
        try:
            resolved = src_path.resolve()
            if resolved == Path(resolved.anchor) or str(resolved) in ('/', ''):
                _set_phase(
                    h, PHASE_COPYING, STATUS_FAILED,
                    error=f'src_path 解析为盘符/根目录: {src_path}（resolved={resolved}）。'
                          f'通常是 path_mappings 把种子父目录直接翻译成了盘符。',
                )
                return False
        except Exception:
            pass

        # 进度回调（控频写库）
        last_emit = [0.0]
        def cb(bytes_done, bytes_total, current_file=''):
            now = time.time()
            if now - last_emit[0] < 1.0:
                return
            last_emit[0] = now
            pct = bytes_done * 100 // max(bytes_total, 1)
            short = Path(current_file).name if current_file else ''
            with SessionLocal() as db:
                r = db.query(DownloadDispatchMap).filter_by(torrent_hash=h).first()
                if r:
                    r.copy_bytes_done = int(bytes_done)
                    r.copy_bytes_total = int(bytes_total)
                    r.status_message = f'复制中 {pct}% {short}'[:200]
                    db.commit()

        try:
            media_type = row.get('media_type') or 'unknown'
            metadata = {
                'title': row.get('title') or '',
                'year': row.get('year'),
                'series_name': row.get('series_name') or '',
                'anime_name': row.get('series_name') or row.get('title') or '',
                'code': row.get('title') or '',  # 番号场景 title 就是番号
            }
            # 拿到该 media_type 对应的 rule
            rule_obj = settings.dispatch.rules.get(media_type)
            rule = rule_obj.model_dump() if rule_obj else {}
            trash_dir = Path(settings.dispatch.trash_dir) if settings.dispatch.trash_dir else None

            # 走 organizer：rule 中有 file_template（用户配的或 yaml 默认）即启用精细整理
            # organizer 自身在 file_template 缺失时也有兜底，所以条件主要看 media_type 注册
            if media_type in settings.dispatch.rules:
                # 走 organizer：精细整理（去 junk + 按模板命名 + SxxExx 拆 Season）
                organizer = TorrentOrganizer(trash_dir=trash_dir)
                org_result = organizer.organize(
                    src_root=src_path,
                    target_dir=Path(target),
                    media_type=media_type,
                    metadata=metadata,
                    rule=rule,
                    progress_cb=cb,
                )
                dispatched_files = org_result['dispatched_files']
                bytes_copied = org_result['bytes_copied']
                files_count = len(dispatched_files)
            else:
                # 兜底：简单递归复制
                copy_result = copy_tree_with_progress(
                    src_path, Path(target), progress_cb=cb,
                )
                dispatched_files = [str(p) for p in copy_result['dst_files']]
                bytes_copied = copy_result['bytes_copied']
                files_count = copy_result['files_copied']
        except Exception as e:
            logger.exception(f"copy/organize 失败 {h[:16]}..")
            # 跨种子文件冲突（D7 PROPER/REPACK）单独标 needs_review，提示用户手动决策
            from tools.dispatch.copier import CrossTorrentCollisionError
            if isinstance(e, CrossTorrentCollisionError):
                _set_phase(
                    h, PHASE_COPYING, 'needs_review',
                    message='目标位置已存在另一种子的旧文件 — 请决策是否覆盖（参见 error_log）',
                    error=str(e),
                )
            else:
                _set_phase(h, PHASE_COPYING, STATUS_FAILED, error=str(e))
            return False

        elapsed = time.time() - t0
        speed_mb = (bytes_copied / 1e6 / elapsed) if elapsed > 0 else 0
        logger.info(
            f"copy 完成 {h[:16]}.. files={files_count} "
            f"bytes={bytes_copied/1e9:.2f}GB elapsed={elapsed:.1f}s speed={speed_mb:.1f}MB/s"
        )
        _record_phase_timing(h, PHASE_COPYING, elapsed)
        _set_phase(
            h, PHASE_COPYING, STATUS_SUCCEEDED,
            message=f'已转移 {files_count} 个文件 ({bytes_copied/1e9:.2f}GB)',
            extra={
                'dispatched_files': dispatched_files,
                'copy_bytes_done': bytes_copied,
                'copy_bytes_total': bytes_copied,
            },
        )
        return True

    def _step_organize(self, row: Dict) -> bool:
        """
        _step_copy 已经走 organizer 完成精细整理。本 step 留作幂等占位
        + 未来扩展点（如硬字幕标注 / 重复扫描等）。
        """
        h = row['torrent_hash']
        t0 = time.time()
        _set_phase(h, PHASE_ORGANIZING, STATUS_SUCCEEDED, message='整理已在 copying 阶段完成')
        _record_phase_timing(h, PHASE_ORGANIZING, time.time() - t0)
        return True

    def _step_jellyfin_send(self, row: Dict):
        """
        通知 Jellyfin "这些路径有变动"。phase 切到 jellyfin_recognizing 后释放 worker，
        是否真扫到由 jellyfin-watcher 轮询确认。

        策略：**首次入库语义，双管齐下**：
          ① /Library/Media/Updated 精准路径通知（毫秒触发，但服务端 60s 防抖；且对"全新
             Series 文件夹"等顶层结构不一定能从零建出 Series→Season→Episode 层级）
          ② /Items/{lib_id}/Refresh 整库 scan_changes 兜底（确保新顶层结构能被识别）

        两个一起调，单独失败不阻断，watcher 来确认结果。
        """
        h = row['torrent_hash']
        t0 = time.time()
        lib_id = row.get('target_library_id')
        dispatched_files = row.get('dispatched_files') or []

        if not lib_id and not dispatched_files:
            _set_phase(h, PHASE_JELLYFIN_RECOGNIZE_DONE, STATUS_SKIPPED,
                       message='无 target_library_id 也无 dispatched_files，跳过 jellyfin 识别',
                       extra={'dispatched_at': datetime.utcnow()})
            _record_phase_timing(h, PHASE_JELLYFIN_RECOGNIZING, time.time() - t0)
            try:
                from tools.dispatch.post_process_worker import trigger as pp_trigger
                pp_trigger.set()
            except Exception:
                pass
            return

        notified_via = []  # 累加成功调用的标签
        try:
            from common.jellyfin_client import JellyfinClient
            from web.backend.config import settings as _settings
            jf = JellyfinClient(_settings.jellyfin_host, _settings.jellyfin_api_key)

            # ① 精准通知 —— 让 jellyfin 在 LibraryMonitorDelay 后扫这些文件（已识别 item 的更新）
            if dispatched_files:
                paths_for_jf = []
                try:
                    from web.backend.path_translator import reverse_translate_path_with_settings
                    for f in dispatched_files:
                        paths_for_jf.append(reverse_translate_path_with_settings(f) or f)
                except Exception:
                    paths_for_jf = list(dispatched_files)

                if jf.notify_media_updated(paths_for_jf, update_type='Created'):
                    notified_via.append('media_updated')

            # ② 整库 scan_changes —— 兜底首次入库场景（新 Series 顶层结构生成）
            #    跟 ① 同时调，jellyfin 内部会合并防抖
            if lib_id:
                if jf.refresh_library(lib_id, mode='scan_changes'):
                    notified_via.append('refresh_library')

        except Exception as e:
            logger.warning(f"jellyfin 通知失败（不阻断，watcher 接管）: {e}")

        if notified_via:
            logger.info(
                f"jellyfin 通知 {h[:16]}.. lib_id={lib_id!r} "
                f"paths={len(dispatched_files)} via={notified_via}"
            )
            _set_phase(h, PHASE_JELLYFIN_RECOGNIZING, STATUS_RUNNING,
                       message=f'已通知 jellyfin（{"+".join(notified_via)}），等扫描完成',
                       extra={'dispatched_at': datetime.utcnow()})
        else:
            logger.warning(
                f"jellyfin 通知全失败 {h[:16]}.. lib_id={lib_id!r} "
                f"paths={len(dispatched_files)}（watcher 仍会轮询兜底）"
            )
            _set_phase(h, PHASE_JELLYFIN_RECOGNIZING, STATUS_WARNED,
                       message='jellyfin 通知调用全失败，watcher 仍会轮询',
                       extra={'dispatched_at': datetime.utcnow()})
        _record_phase_timing(h, PHASE_JELLYFIN_RECOGNIZING, time.time() - t0)


# ============================================================================
# helpers
# ============================================================================

def _row_to_dict(row: DownloadDispatchMap) -> Dict:
    """ORM 行 → plain dict（避免 db session 关闭后访问出问题）。"""
    return {
        'torrent_hash': row.torrent_hash,
        'media_type': row.media_type,
        'tmdb_id': row.tmdb_id,
        'imdb_id': row.imdb_id,
        'series_tmdb_id': row.series_tmdb_id,
        'series_name': row.series_name,
        'title': row.title,
        'year': row.year,
        'target_library_id': row.target_library_id,
        'target_root': row.target_root,
        'target_path': row.target_path,
        'move_mode': row.move_mode,
        'dispatched_files': list(row.dispatched_files or []),
        'phase': row.phase,
        'phase_status': row.phase_status,
        'phase_timings': dict(row.phase_timings or {}),
    }
