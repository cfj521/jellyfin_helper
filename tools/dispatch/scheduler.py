"""后台 worker 编排：spawn 6 个独立线程 + 定时跑 quota/adopt/trash。

6 个独立线程 worker：
  ① analyzer            (事件驱动 + 5s 兜底) — analyzing → dispatch_queued
  ② downloader-watcher  (事件驱动 + 20s 轮询)— dispatch_queued/downloading → download_done
  ③ dispatch-pipeline   (事件驱动 + 30s 兜底)— download_done → copying → organizing → jellyfin_recognizing
  ④ jellyfin-watcher    (事件驱动 + 60s 轮询)— jellyfin_recognizing → jellyfin_recognize_done
  ⑤ post-process-worker (事件驱动 + 30s 兜底)— jellyfin_recognize_done → subtitle_fetching → audio_track_order_adjusting → all_jobs_done
  ⑥ sweeper             (周期 dispatch.poll_interval_seconds，默认 180s)— failed/zombie 恢复

定时任务（这个 scheduler 主循环负责）：
  - 配额硬清 (qbittorrent_quota.check_interval_seconds)
  - 软清 (qbittorrent_seeding.cleanup_interval_seconds)
  - 孤儿认领 (dispatch.adopt_interval_seconds)
  - 垃圾清理 (dispatch.trash_interval_seconds)
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)


def _spawn_thread(target, name: str, args=()):
    """启 daemon 线程，import 失败的话仅 warning 不抛。"""
    try:
        threading.Thread(target=target, args=args, daemon=True, name=name).start()
        logger.info(f"启动线程: {name}")
    except Exception as e:
        logger.warning(f"启动 {name} 失败: {e}")


def _start_pipeline_worker(stop_event):
    """DispatchPipeline 是个 class，要包一层。"""
    from tools.dispatch.pipeline import DispatchPipeline
    pl = DispatchPipeline(stop_event=stop_event)
    pl.run_forever()


def _start_post_process_worker(stop_event):
    from tools.dispatch.post_process_worker import PostProcessWorker
    pp = PostProcessWorker(stop_event=stop_event)
    pp.run_forever()


def run_scheduler_loop(stop_event=None):
    """长循环：spawn 6 个 worker 线程 + 定时跑 quota/adopt/trash。"""
    from web.backend.config import settings
    quota_interval = max(30, int(settings.qbittorrent_quota.check_interval_seconds))
    soft_interval = max(60, int(settings.qbittorrent_seeding.cleanup_interval_seconds))
    adopt_interval = max(60, int(settings.dispatch.adopt_interval_seconds))
    trash_interval = max(300, int(settings.dispatch.trash_interval_seconds))

    if stop_event is None:
        stop_event = threading.Event()

    last_quota_check = 0.0
    last_soft_clean = 0.0
    last_trash_clean = 0.0
    last_adopt_scan = 0.0

    # ========== Spawn 6 个 worker 线程 ==========
    try:
        from tools.dispatch.analyzer import run_analyzer_loop
        _spawn_thread(run_analyzer_loop, 'analyzer', args=(stop_event,))
    except ImportError as e:
        logger.warning(f"analyzer 未启动: {e}")

    try:
        from tools.dispatch.downloader_watcher import run_downloader_watcher_loop
        _spawn_thread(run_downloader_watcher_loop, 'downloader-watcher', args=(stop_event,))
    except ImportError as e:
        logger.warning(f"downloader-watcher 未启动: {e}")

    try:
        _spawn_thread(_start_pipeline_worker, 'dispatch-pipeline', args=(stop_event,))
    except ImportError as e:
        logger.warning(f"dispatch-pipeline 未启动: {e}")

    try:
        from tools.dispatch.jellyfin_watcher import run_jellyfin_watcher_loop
        _spawn_thread(run_jellyfin_watcher_loop, 'jellyfin-watcher', args=(stop_event,))
    except ImportError as e:
        logger.warning(f"jellyfin-watcher 未启动: {e}")

    try:
        _spawn_thread(_start_post_process_worker, 'post-process-worker', args=(stop_event,))
    except ImportError as e:
        logger.warning(f"post-process-worker 未启动: {e}")

    try:
        from tools.dispatch.sweeper import run_sweeper_loop
        _spawn_thread(run_sweeper_loop, 'sweeper', args=(stop_event,))
    except ImportError as e:
        logger.warning(f"sweeper 未启动: {e}")

    # ========== 主循环：定时任务 ==========
    logger.info(
        f"dispatch scheduler 启动："
        f"配额 {quota_interval}s / 软清 {soft_interval}s / "
        f"下载项认领 {adopt_interval}s / 垃圾清理 {trash_interval}s"
    )

    while True:
        if stop_event.is_set():
            return
        now = time.time()

        if now - last_quota_check >= quota_interval:
            last_quota_check = now
            try:
                from tools.dispatch.quota import quota_check_and_cleanup
                quota_check_and_cleanup()
            except ImportError:
                pass
            except Exception as e:
                logger.warning(f"quota_check_and_cleanup 异常: {e}")

        if now - last_soft_clean >= soft_interval:
            last_soft_clean = now
            try:
                from tools.dispatch.quota import regular_cleanup
                regular_cleanup()
            except ImportError:
                pass
            except Exception as e:
                logger.warning(f"regular_cleanup 异常: {e}")

        if now - last_adopt_scan >= adopt_interval:
            last_adopt_scan = now
            try:
                from tools.dispatch.adopt import scan_and_adopt_orphans
                scan_and_adopt_orphans()
            except ImportError:
                pass
            except Exception as e:
                logger.warning(f"下载项认领异常: {e}")

        if now - last_trash_clean >= trash_interval:
            last_trash_clean = now
            try:
                from tools.dispatch.trash_cleaner import clean_trash
                clean_trash()
            except ImportError:
                pass
            except Exception as e:
                logger.warning(f"垃圾清理异常: {e}")

        # 短睡眠便于响应 stop_event
        for _ in range(10):
            if stop_event.is_set():
                return
            time.sleep(1)
