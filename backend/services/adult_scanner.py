"""
成人内容主动扫描器（Scanner）。

职责：遍历 jellyfin 库目录（rglob）→ mtime 跳过判断由 pipeline 兜底 → 每个文件丢给 pipeline 处理 → 创建 Task 流式上报进度。

触发来源：
  - 用户手动点"立即扫描"（/api/adult/scan）
  - 用户配置变更后新加入库的初始化（restart_for_new_libraries）

跟 adult_watcher 的关系：
  - Scanner 是"全库主动扫"，用户主动触发，进 Task 表
  - Watcher 是"jellyfin 增量被动拉"，polling 触发，不进 Task 表
  - 两者共用 AdultPipeline 做实际的识别/入库/刮削
"""
from __future__ import annotations

import json as _json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

from backend.api._media_exts import VIDEO_EXTS

logger = logging.getLogger(__name__)

# 同一个库 X 秒内不重复扫描（去抖）
LIBRARY_COOLDOWN_SEC = 5 * 60


class AdultScanner:
    """全库主动扫描器。模块级单例（见底部 scanner = AdultScanner()）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._last_run_at: Optional[float] = None
        self._last_run_summary: dict = {}

        # 库级冷却：library_id → 上次扫描时间戳
        self._last_scan_per_lib: Dict[str, float] = {}
        # 当前活跃扫描任务：library_id → task_id
        self._active_tasks: Dict[str, int] = {}
        # 上一次已知的 library_ids 集合（用于检测配置变化）
        self._known_library_ids: Set[str] = set()

    # ====================================================================
    # 公开接口
    # ====================================================================

    def status(self) -> dict:
        from backend.config import settings
        try:
            from backend.services.jellyfin_poller import client as poller_client
            poller_status = poller_client.status()
        except Exception:
            poller_status = {"connected": False, "error": "poller client 未加载"}
        # 增量监听器状态（如果加载失败不影响主响应）
        try:
            from backend.services.adult_watcher import watcher
            inc_status = watcher.status()
        except Exception:
            inc_status = {"error": "watcher 未加载"}
        return {
            "enabled": settings.adult_enabled,
            "auto_scrape": settings.adult_auto_scrape,
            # poller 状态（驱动 watcher 的 jellyfin 轮询）
            "change_monitor": poller_status,
            # 增量监听器（per library last_check_at + 最近 summary）
            "watcher": inc_status,
            # scanner 自己的状态（全库扫描）
            "last_run_at": self._last_run_at,
            "last_run_summary": self._last_run_summary,
            "active_tasks": dict(self._active_tasks),
            "library_cooldowns": {
                lib_id: max(0, int(LIBRARY_COOLDOWN_SEC - (time.time() - ts)))
                for lib_id, ts in self._last_scan_per_lib.items()
            },
        }

    def trigger_libraries(
        self,
        library_ids: List[str],
        bypass_cooldown: bool = False,
        force_scrape: Optional[bool] = None,
    ) -> Dict[str, int]:
        """
        立即异步扫描指定的 Jellyfin 库。

        Args:
            library_ids: 要扫的库
            bypass_cooldown: 绕过冷却（用户手动点"立即扫描"时为 True）
            force_scrape: True=强制刮削；False=不刮削；None=按 settings.adult_auto_scrape

        Returns:
            {library_id: task_id}（被冷却或已在跑的库不会出现）
        """
        from backend.config import settings

        scheduled: Dict[str, int] = {}
        now = time.time()

        do_scrape = settings.adult_auto_scrape if force_scrape is None else force_scrape

        for lib_id in library_ids:
            with self._lock:
                if lib_id in self._active_tasks:
                    logger.info(f"scanner: 库 {lib_id} 已有扫描任务，跳过")
                    continue
                if not bypass_cooldown:
                    last = self._last_scan_per_lib.get(lib_id, 0)
                    if now - last < LIBRARY_COOLDOWN_SEC:
                        logger.info(
                            f"scanner: 库 {lib_id} 冷却中（剩余 "
                            f"{int(LIBRARY_COOLDOWN_SEC - (now - last))}s），跳过"
                        )
                        continue

                task_id = self._create_scan_task(lib_id)
                if task_id is None:
                    continue
                self._active_tasks[lib_id] = task_id
                scheduled[lib_id] = task_id

            t = threading.Thread(
                target=self._run_scan_for_library,
                args=(lib_id, task_id, do_scrape),
                daemon=True,
                name=f"AdultScan-{lib_id[:6]}",
            )
            t.start()

        return scheduled

    def restart_for_new_libraries(self):
        """
        在 settings 重载后调用。比对新旧 library_ids，对新增的库触发扫描。
        删除的库不做任何操作（保留 adult_items 数据）。
        """
        from backend.config import settings
        new_ids = set(settings.adult_library_ids or [])
        added = new_ids - self._known_library_ids
        self._known_library_ids = new_ids

        if added:
            logger.info(f"scanner: 检测到新增成人库 {added}，立即扫描")
            self.trigger_libraries(list(added), bypass_cooldown=True)

    def init_known_libraries(self):
        """启动时调用一次，记录初始 library_ids（避免启动后被误判为"新增"）。"""
        from backend.config import settings
        self._known_library_ids = set(settings.adult_library_ids or [])

    # ====================================================================
    # 内部：扫描任务
    # ====================================================================

    def _create_scan_task(self, library_id: str) -> Optional[int]:
        try:
            from backend.database import SessionLocal
            from backend.api.tasks import create_task
            db = SessionLocal()
            try:
                task = create_task(db, "adult_scan", f"扫描库 {library_id[:8]}…")
                task.result = _json.dumps({
                    "library_id": library_id,
                    "scanned": 0, "new": 0, "updated": 0, "moved": 0,
                    "skipped": 0, "unrecognized": 0, "excluded": 0,
                    "scraped": 0, "failed": 0,
                    "scanned_files": [],
                }, ensure_ascii=False)
                db.commit()
                return task.id
            finally:
                db.close()
        except Exception:
            logger.exception("创建扫描 task 失败")
            return None

    def _run_scan_for_library(self, library_id: str, task_id: int, do_scrape: bool):
        from backend.database import SessionLocal, Task
        from backend.api.tasks import update_task_progress, complete_task
        from backend.shutdown import is_shutting_down
        from backend.services.adult_pipeline import AdultPipeline

        db = SessionLocal()
        try:
            update_task_progress(db, task_id, 5, "解析库路径…")

            paths = self._get_library_paths(library_id)
            if not paths:
                complete_task(db, task_id, {"error": f"无法获取库路径 {library_id}"}, success=False)
                return

            update_task_progress(db, task_id, 10, f"扫描 {len(paths)} 个路径")

            all_videos: List[Path] = []
            for p in paths:
                try:
                    for f in Path(p).rglob('*'):
                        if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                            all_videos.append(f)
                except (PermissionError, OSError) as e:
                    logger.warning(f"扫描路径失败 {p}: {e}")

            total = len(all_videos)
            update_task_progress(db, task_id, 15, f"找到 {total} 个视频文件")

            stats = {
                "library_id": library_id,
                "scanned": 0, "new": 0, "updated": 0, "moved": 0,
                "skipped": 0, "unrecognized": 0, "excluded": 0,
                "scraped": 0, "failed": 0,
                "scanned_files": [],
            }

            # progress callback：每条 result 累加 stats，流式写 task.result
            def on_progress(idx: int, total_: int, file_info: dict, result: dict):
                status = result.get('status', 'failed')
                stats['scanned'] += 1
                if status in stats:
                    stats[status] += 1
                else:
                    stats[status] = 1  # 未预期状态也记一笔
                # 刮削成功额外计数
                if result.get('scraped'):
                    stats['scraped'] += 1

                # 落 task.result（每条 ~200B，10000 条 ~2MB JSONB，PG 完全 hold）
                entry = {
                    "name": file_info.get('name') or result.get('name'),
                    "status": status,
                    "code": result.get('code'),
                    "t": int(time.time()),
                }
                if result.get('error'):
                    entry['error'] = result['error'][:100]
                stats['scanned_files'].append(entry)

                try:
                    t = db.query(Task).filter(Task.id == task_id).first()
                    if t:
                        t.result = _json.dumps(stats, ensure_ascii=False)
                        db.commit()
                except Exception:
                    db.rollback()

                # 进度百分比 tick（每 10 条）
                if idx % 10 == 0 and total_ > 0:
                    pct = 15 + int(75 * (idx + 1) / total_)
                    update_task_progress(
                        db, task_id, pct,
                        f"识别中 {idx + 1}/{total_}（新增 {stats['new']}）",
                    )

            pipe = AdultPipeline(do_scrape=do_scrape, on_progress=on_progress)

            for idx, f in enumerate(all_videos):
                if is_shutting_down():
                    logger.info(
                        f"scanner: 收到 shutdown 信号，扫描提前退出"
                        f"（已扫 {idx}/{total}）"
                    )
                    stats["stopped_by_shutdown"] = True
                    break
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    mtime = 0.0
                pipe.process_one(f, file_mtime=mtime, idx=idx, total=total)

            update_task_progress(db, task_id, 90, "识别完成，刷新 Jellyfin…")

            # 刮削期间已经在 pipeline 内部各自短事务，scanner 这边 db 没拉长
            # flush_jellyfin 之前关 db 是个好习惯（jellyfin 通知是慢 HTTP）
            db.close()
            db = None

            pipe.flush_jellyfin(library_id)

            self._last_run_summary = {k: v for k, v in stats.items() if k != "scanned_files"}
            self._last_run_at = time.time()
            self._last_scan_per_lib[library_id] = time.time()
            with SessionLocal() as d:
                complete_task(d, task_id, stats)
            logger.info(
                f"scanner: 库 {library_id} 扫描完成 "
                f"new={stats['new']} scraped={stats['scraped']}"
            )

        except Exception as e:
            logger.exception(f"scanner: 库 {library_id} 扫描异常")
            try:
                with SessionLocal() as d:
                    complete_task(d, task_id, {"error": str(e)}, success=False)
            except Exception:
                pass
        finally:
            if db is not None:
                db.close()
            with self._lock:
                self._active_tasks.pop(library_id, None)

    def _get_library_paths(self, library_id: str) -> List[str]:
        """返回本机视角路径（已 forward-translate），给磁盘扫描用。"""
        from backend.config import settings
        from backend.path_translator import translate_path_with_settings
        if not settings.jellyfin_api_key:
            return []
        try:
            from common.jellyfin_client import JellyfinClient
            client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
            for lib in client.get_libraries_normalized():
                if lib['id'] == library_id:
                    return [translate_path_with_settings(loc) or loc for loc in lib['locations']]
        except Exception as e:
            logger.warning(f"获取库路径失败 {library_id}: {e}")
        return []

    def _get_library_paths_raw(self, library_id: str) -> List[str]:
        """返回 Jellyfin 视角的 locations（不翻译），给 DB 按 file_path 前缀匹配用。
        DB 约定存 Jellyfin view，前缀匹配也得用 Jellyfin view。

        adult.py 的 _build_library_filter 用到，逻辑上跟扫盘没关，但作为
        库路径查询工具方法保留在 scanner 里以便复用。
        """
        from backend.config import settings
        if not settings.jellyfin_api_key:
            return []
        try:
            from common.jellyfin_client import JellyfinClient
            client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
            for lib in client.get_libraries_normalized():
                if lib['id'] == library_id:
                    return list(lib['locations'])
        except Exception as e:
            logger.warning(f"获取库路径失败 {library_id}: {e}")
        return []


# 模块级单例
scanner = AdultScanner()
