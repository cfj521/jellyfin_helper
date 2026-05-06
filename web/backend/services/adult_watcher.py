"""
番号库扫描服务

只在被触发时工作，不再有周期定时器。触发来源：
  1. Jellyfin WebSocket LibraryChanged 事件（services.jellyfin_ws）
  2. 用户配置变更后（_reload_settings → restart_for_new_libraries）
  3. 用户手动点"立即扫描"

核心能力：
  - trigger_libraries(library_ids)  异步扫描指定库（已有任务在跑/冷却中会被跳过）
  - file_mtime 增量缓存：跳过已扫且 mtime 未变的文件
  - 库级冷却（5 分钟）：去抖
  - 任务流式：每识别一个文件就 commit + append 到 task.result.scanned_files
"""
from __future__ import annotations

import json as _json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.wmv', '.mov', '.flv', '.webm', '.m4v', '.ts'}

# 同一个库 X 秒内不重复扫描（去抖）
LIBRARY_COOLDOWN_SEC = 5 * 60


class AdultWatcher:
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
        from web.backend.config import settings
        try:
            from web.backend.services.jellyfin_ws import client as ws_client
            ws_status = ws_client.status()
        except Exception:
            ws_status = {"connected": False, "error": "ws client 未加载"}
        return {
            "enabled": settings.adult_enabled,
            "auto_scrape": settings.adult_auto_scrape,
            "websocket": ws_status,
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
        from web.backend.config import settings

        scheduled: Dict[str, int] = {}
        now = time.time()

        do_scrape = settings.adult_auto_scrape if force_scrape is None else force_scrape

        for lib_id in library_ids:
            with self._lock:
                if lib_id in self._active_tasks:
                    logger.info(f"watcher: 库 {lib_id} 已有扫描任务，跳过")
                    continue
                if not bypass_cooldown:
                    last = self._last_scan_per_lib.get(lib_id, 0)
                    if now - last < LIBRARY_COOLDOWN_SEC:
                        logger.info(f"watcher: 库 {lib_id} 冷却中（剩余 "
                                    f"{int(LIBRARY_COOLDOWN_SEC - (now - last))}s），跳过")
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
        from web.backend.config import settings
        new_ids = set(settings.adult_library_ids or [])
        added = new_ids - self._known_library_ids
        self._known_library_ids = new_ids

        if added:
            logger.info(f"watcher: 检测到新增成人库 {added}，立即扫描")
            self.trigger_libraries(list(added), bypass_cooldown=True)

    def init_known_libraries(self):
        """启动时调用一次，记录初始 library_ids（避免启动后被误判为"新增"）。"""
        from web.backend.config import settings
        self._known_library_ids = set(settings.adult_library_ids or [])

    # ====================================================================
    # 内部：单库扫描（流式 task）
    # ====================================================================

    def _create_scan_task(self, library_id: str) -> Optional[int]:
        try:
            from web.backend.database import SessionLocal
            from web.backend.api.tasks import create_task
            db = SessionLocal()
            try:
                task = create_task(db, "adult_scan", f"扫描库 {library_id[:8]}…")
                task.result = _json.dumps({
                    "library_id": library_id,
                    "scanned": 0,
                    "new": 0,
                    "skipped": 0,
                    "unrecognized": 0,
                    "scraped": 0,
                    "failed": 0,
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
        from web.backend.database import SessionLocal, AdultItem
        from web.backend.api.tasks import update_task_progress, complete_task
        from web.backend.config import settings
        from tools.adult_manager.code_extractor import extract_code

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
                "scanned": 0,
                "new": 0,
                "skipped": 0,
                "unrecognized": 0,
                "scraped": 0,
                "failed": 0,
                "scanned_files": [],
            }
            new_codes_for_scrape: List[str] = []

            for idx, f in enumerate(all_videos):
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    mtime = 0.0
                stats["scanned"] += 1

                existing = db.query(AdultItem).filter(AdultItem.file_path == str(f)).first()
                if existing and existing.file_mtime is not None and abs(existing.file_mtime - mtime) < 1.0:
                    stats["skipped"] += 1
                    self._append_log(db, task_id, stats, {
                        "name": f.name, "status": "skipped", "code": existing.code,
                    })
                    self._tick_progress(db, task_id, idx, total, stats)
                    continue

                code = extract_code(f.name)
                if not code:
                    stats["unrecognized"] += 1
                    self._append_log(db, task_id, stats, {
                        "name": f.name, "status": "unrecognized",
                    })
                    self._tick_progress(db, task_id, idx, total, stats)
                    continue

                if existing:
                    # mtime 变了：重新刮削
                    existing.file_mtime = mtime
                    if not existing.code:
                        existing.code = code
                    db.commit()
                    self._append_log(db, task_id, stats, {
                        "name": f.name, "status": "updated", "code": existing.code,
                    })
                    new_codes_for_scrape.append(existing.code or code)
                else:
                    code_existing = db.query(AdultItem).filter(AdultItem.code == code).first()
                    if code_existing:
                        # 视频换位置
                        code_existing.file_path = str(f)
                        code_existing.file_mtime = mtime
                        db.commit()
                        self._append_log(db, task_id, stats, {
                            "name": f.name, "status": "moved", "code": code,
                        })
                    else:
                        new_item = AdultItem(code=code, file_path=str(f), file_mtime=mtime)
                        db.add(new_item)
                        db.commit()
                        stats["new"] += 1
                        new_codes_for_scrape.append(code)
                        self._append_log(db, task_id, stats, {
                            "name": f.name, "status": "new", "code": code,
                        })

                self._tick_progress(db, task_id, idx, total, stats)

            update_task_progress(db, task_id, 90, f"识别完成，新增 {stats['new']}")

            # 刮削
            if do_scrape and new_codes_for_scrape:
                update_task_progress(db, task_id, 92, f"刮削 {len(new_codes_for_scrape)} 条…")
                stats["scraped"] = self._scrape_codes(new_codes_for_scrape, db, task_id, stats)

            # 通知 Jellyfin
            if stats["new"] > 0 and settings.jellyfin_api_key:
                try:
                    from common.jellyfin_client import JellyfinClient
                    JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key).refresh_library(library_id)
                except Exception as e:
                    logger.warning(f"通知 Jellyfin 失败: {e}")

            self._last_run_summary = {k: v for k, v in stats.items() if k != "scanned_files"}
            self._last_scan_per_lib[library_id] = time.time()
            complete_task(db, task_id, stats)
            logger.info(f"watcher: 库 {library_id} 扫描完成 new={stats['new']} scraped={stats['scraped']}")

        except Exception as e:
            logger.exception(f"watcher: 库 {library_id} 扫描异常")
            try:
                complete_task(db, task_id, {"error": str(e)}, success=False)
            except Exception:
                pass
        finally:
            db.close()
            with self._lock:
                self._active_tasks.pop(library_id, None)

    def _get_library_paths(self, library_id: str) -> List[str]:
        from web.backend.config import settings
        from web.backend.path_translator import translate_path_with_settings
        if not settings.jellyfin_api_key:
            return []
        try:
            from common.jellyfin_client import JellyfinClient
            client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
            for lib in client.get_libraries_normalized():
                if lib['id'] == library_id:
                    # Jellyfin 视角的 /library/videos → 翻译成本机可访问的路径
                    return [translate_path_with_settings(loc) or loc for loc in lib['locations']]
        except Exception as e:
            logger.warning(f"获取库路径失败 {library_id}: {e}")
        return []

    def _append_log(self, db, task_id: int, stats: dict, entry: dict):
        from web.backend.database import Task
        entry["t"] = int(time.time())
        stats["scanned_files"].append(entry)
        if len(stats["scanned_files"]) > 500:
            stats["scanned_files"] = stats["scanned_files"][-500:]
        try:
            task = db.query(Task).filter(Task.id == task_id).first()
            if task:
                task.result = _json.dumps(stats, ensure_ascii=False)
                db.commit()
        except Exception:
            db.rollback()

    def _tick_progress(self, db, task_id: int, idx: int, total: int, stats: dict):
        if idx % 10 == 0 and total > 0:
            pct = 15 + int(75 * (idx + 1) / total)
            from web.backend.api.tasks import update_task_progress
            update_task_progress(db, task_id, pct, f"识别中 {idx+1}/{total}（新增 {stats['new']}）")

    def _scrape_codes(self, codes: List[str], db, task_id: int, stats: dict) -> int:
        from web.backend.config import settings
        from web.backend.database import AdultItem
        from tools.adult_manager.scrapers.manager import ScraperManager
        from tools.adult_manager.nfo_writer import write_nfo as do_write_nfo
        import requests

        manager = ScraperManager(
            delay=settings.adult_scraper_delay,
            proxy=settings.adult_proxy or None,
            sources=settings.adult_sources,
        )
        if not manager.scrapers:
            return 0

        ok = 0
        for code in codes:
            item = db.query(AdultItem).filter(AdultItem.code == code).first()
            if not item:
                continue
            try:
                result = manager.scrape(code)
                if not result:
                    self._append_log(db, task_id, stats, {
                        "name": Path(item.file_path).name if item.file_path else code,
                        "status": "scrape_not_found", "code": code,
                    })
                    continue
                d = result.to_dict()
                item.title = d.get('title')
                item.release_date = d.get('release_date')
                item.studio = d.get('studio')
                item.director = d.get('director')
                item.actors = _json.dumps(d.get('actors') or [], ensure_ascii=False)
                item.tags = _json.dumps(d.get('tags') or [], ensure_ascii=False)
                item.cover_url = d.get('cover_url')
                item.rating = d.get('rating')
                item.source = d.get('source')

                if item.cover_url and item.file_path:
                    try:
                        r = requests.get(item.cover_url, timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
                        r.raise_for_status()
                        cover_path = Path(item.file_path).with_name(Path(item.file_path).stem + '-poster.jpg')
                        cover_path.write_bytes(r.content)
                        item.poster_path = str(cover_path)
                    except Exception as e:
                        logger.warning(f"封面下载失败 {code}: {e}")

                if item.file_path:
                    try:
                        nfo_path = do_write_nfo(Path(item.file_path), d)
                        item.nfo_path = str(nfo_path)
                    except Exception as e:
                        logger.warning(f"NFO 写入失败 {code}: {e}")

                db.commit()
                ok += 1
                self._append_log(db, task_id, stats, {
                    "name": Path(item.file_path).name if item.file_path else code,
                    "status": "scraped", "code": code, "title": item.title,
                })
            except Exception as e:
                logger.warning(f"刮削异常 {code}: {e}")
                stats["failed"] += 1
                self._append_log(db, task_id, stats, {
                    "name": code, "status": "scrape_failed", "code": code, "error": str(e)[:100],
                })
        return ok


# 全局实例
watcher = AdultWatcher()
