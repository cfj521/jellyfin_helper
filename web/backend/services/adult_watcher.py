"""
番号库扫描服务

只在被触发时工作，不再有周期定时器。触发来源：
  1. services.jellyfin_ws 的轮询 worker 周期性触发（默认 60s 一次，自带库级 5min 冷却）
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

from web.backend.api._media_exts import VIDEO_EXTS

logger = logging.getLogger(__name__)

# 同一个库 X 秒内不重复扫描（去抖）
LIBRARY_COOLDOWN_SEC = 5 * 60


def _jf_path(p) -> str:
    """本机扫到的文件路径 → Jellyfin view（DB 约定存 Jellyfin view）。
    同机部署 / 没配映射时 reverse 不命中规则会原样返回，无影响。"""
    s = str(p)
    from web.backend.path_translator import reverse_translate_path_with_settings
    return reverse_translate_path_with_settings(s) or s


def _detect_local_attachments(video_file: Path):
    """
    探测视频文件同目录下已存在的 poster / nfo，返回 (poster_path, nfo_path) 字符串或 None。
    覆盖常见命名：<stem>-poster.{jpg,jpeg,png} / <stem>.nfo

    返回的路径是 Jellyfin view（DB 约定）；同机部署 / 没配映射时与本机视角相同。
    """
    if not video_file:
        return None, None
    parent = video_file.parent
    stem = video_file.stem

    poster_path = None
    for ext in ('.jpg', '.jpeg', '.png'):
        cand = parent / f'{stem}-poster{ext}'
        if cand.exists():
            poster_path = _jf_path(cand)
            break

    nfo = parent / f'{stem}.nfo'
    nfo_path = _jf_path(nfo) if nfo.exists() else None

    return poster_path, nfo_path


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
            from web.backend.services.jellyfin_ws import client as poller_client
            poller_status = poller_client.status()
        except Exception:
            poller_status = {"connected": False, "error": "poller client 未加载"}
        return {
            "enabled": settings.adult_enabled,
            "auto_scrape": settings.adult_auto_scrape,
            # 字段含义已从 WebSocket 改为 polling worker（Jellyfin 10.11 起 WS 不再可用）；
            # 'websocket' key 保留为 API 兼容别名，新代码请用 'change_monitor'
            "change_monitor": poller_status,
            "websocket": poller_status,
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

            from web.backend.shutdown import is_shutting_down
            for idx, f in enumerate(all_videos):
                if is_shutting_down():
                    logger.info(f"收到 shutdown 信号，库扫描提前退出（已扫 {idx}/{total}）")
                    stats["stopped_by_shutdown"] = True
                    break
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    mtime = 0.0
                stats["scanned"] += 1

                existing = db.query(AdultItem).filter(AdultItem.file_path == _jf_path(f)).first()

                # excluded 项不再自动识别 / 刮削，扫描直接跳
                if existing and existing.excluded:
                    stats["skipped"] += 1
                    self._append_log(db, task_id, stats, {
                        "name": f.name, "status": "excluded", "code": existing.code,
                    })
                    self._tick_progress(db, task_id, idx, total, stats)
                    continue

                if existing and existing.file_mtime is not None and abs(existing.file_mtime - mtime) < 1.0:
                    # mtime 没变：跳过。注意 existing.code 可能为 NULL（之前 unrecognized 入库的）
                    if existing.code:
                        stats["skipped"] += 1
                        status = "skipped"
                    else:
                        stats["unrecognized"] += 1
                        status = "unrecognized"
                    self._append_log(db, task_id, stats, {
                        "name": f.name, "status": status, "code": existing.code,
                    })
                    self._tick_progress(db, task_id, idx, total, stats)
                    continue

                # 父目录名优先：避免文件名里水印 / 网站名造成的误识别
                # 例如 "PFES-103/hhd800.com@PFES-103.mp4" 文件名会被误识为 HHD-800，
                #     而父目录 "PFES-103" 是真正的番号，应优先采纳
                # 父目录识别失败时再退回文件名识别
                code = extract_code(f.parent.name) or extract_code(f.name)
                if not code:
                    # 识别失败也入库（code=NULL），让前端能看到这些文件并人工指 code
                    stats["unrecognized"] += 1
                    if existing:
                        # 旧记录 mtime 变了；如果原本有 code 就保留，没有就还是 NULL
                        existing.file_mtime = mtime
                        db.commit()
                    else:
                        # 无番号文件入库：title=文件名 stem，默认 excluded=True（普通列表隐藏）
                        new_item = AdultItem(
                            code=None,
                            title=f.stem,
                            file_path=_jf_path(f),
                            file_mtime=mtime,
                            excluded=True,
                        )
                        db.add(new_item)
                        db.commit()
                    self._append_log(db, task_id, stats, {
                        "name": f.name, "status": "unrecognized",
                        "code": existing.code if existing else None,
                    })
                    self._tick_progress(db, task_id, idx, total, stats)
                    continue

                # 探测当前 video 同目录下已存在的 poster / nfo
                # 重要：清表 / 文件位置变更后，本地 poster/nfo 已存在的话直接关联到 DB，
                # 后续刮削就不必重复下载海报、生成 NFO（_scrape_codes 会跳过）
                local_poster, local_nfo = _detect_local_attachments(f)

                if existing:
                    # mtime 变了：重新刮削
                    was_unrecognized = not existing.code
                    existing.file_mtime = mtime
                    if local_poster:
                        existing.poster_path = local_poster
                    if local_nfo:
                        existing.nfo_path = local_nfo
                    if was_unrecognized:
                        # 之前 unrecognized，现在能识别 → 升级
                        # 注意：可能撞上其他记录已占用同 code 的情况
                        clash = db.query(AdultItem).filter(
                            AdultItem.code == code, AdultItem.id != existing.id
                        ).first()
                        if clash:
                            # 把当前文件路径并入 clash，删掉本记录
                            clash.file_path = _jf_path(f)
                            clash.file_mtime = mtime
                            if local_poster:
                                clash.poster_path = local_poster
                            if local_nfo:
                                clash.nfo_path = local_nfo
                            db.delete(existing)
                            db.commit()
                            self._append_log(db, task_id, stats, {
                                "name": f.name, "status": "moved", "code": code,
                            })
                            new_codes_for_scrape.append(code)
                            self._tick_progress(db, task_id, idx, total, stats)
                            continue
                        existing.code = code
                        stats["new"] += 1  # 算作新识别成功
                    db.commit()
                    self._append_log(db, task_id, stats, {
                        "name": f.name, "status": "updated", "code": existing.code,
                    })
                    new_codes_for_scrape.append(existing.code or code)
                else:
                    code_existing = db.query(AdultItem).filter(AdultItem.code == code).first()
                    if code_existing:
                        # 视频换位置
                        code_existing.file_path = _jf_path(f)
                        code_existing.file_mtime = mtime
                        if local_poster:
                            code_existing.poster_path = local_poster
                        if local_nfo:
                            code_existing.nfo_path = local_nfo
                        db.commit()
                        self._append_log(db, task_id, stats, {
                            "name": f.name, "status": "moved", "code": code,
                        })
                    else:
                        new_item = AdultItem(
                            code=code, file_path=_jf_path(f), file_mtime=mtime,
                            poster_path=local_poster, nfo_path=local_nfo,
                        )
                        db.add(new_item)
                        db.commit()
                        stats["new"] += 1
                        new_codes_for_scrape.append(code)
                        self._append_log(db, task_id, stats, {
                            "name": f.name, "status": "new", "code": code,
                        })

                self._tick_progress(db, task_id, idx, total, stats)

            update_task_progress(db, task_id, 90, f"识别完成，新增 {stats['new']}")

            # 刮削前先释放扫描阶段持有的 DB 连接：刮削里有 HTTP 慢请求，
            # 旧版本在这里继续持有 db → 整个连接池被锁住，前端 API 全部拿不到连接
            db.close()
            db = None

            if do_scrape and new_codes_for_scrape:
                with SessionLocal() as d:
                    update_task_progress(d, task_id, 92, f"刮削 {len(new_codes_for_scrape)} 条…")
                stats["scraped"] = self._scrape_codes(new_codes_for_scrape, task_id, stats)

            # 通知 Jellyfin（精准 path 通知，整库扫兜底）
            if stats["new"] > 0 and settings.jellyfin_api_key:
                try:
                    from common.jellyfin_client import JellyfinClient
                    jf = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)

                    # 收集本轮处理的 item file_path（jellyfin 视角）
                    paths_for_jf = []
                    if new_codes_for_scrape:
                        from web.backend.path_translator import reverse_translate_path_with_settings
                        with SessionLocal() as d:
                            items_in_db = (
                                d.query(AdultItem)
                                .filter(AdultItem.code.in_(new_codes_for_scrape))
                                .all()
                            )
                            for it in items_in_db:
                                if it.file_path:
                                    paths_for_jf.append(
                                        reverse_translate_path_with_settings(it.file_path) or it.file_path
                                    )

                    if paths_for_jf and jf.notify_media_updated(paths_for_jf, update_type='Created'):
                        pass
                    else:
                        # 兜底：库级扫描
                        jf.refresh_library(library_id)
                except Exception as e:
                    logger.warning(f"通知 Jellyfin 失败: {e}")

            self._last_run_summary = {k: v for k, v in stats.items() if k != "scanned_files"}
            self._last_scan_per_lib[library_id] = time.time()
            with SessionLocal() as d:
                complete_task(d, task_id, stats)
            logger.info(f"watcher: 库 {library_id} 扫描完成 new={stats['new']} scraped={stats['scraped']}")

        except Exception as e:
            logger.exception(f"watcher: 库 {library_id} 扫描异常")
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

    def _get_library_paths_raw(self, library_id: str) -> List[str]:
        """返回 Jellyfin 视角的 locations（不翻译），给 DB 按 file_path 前缀匹配用——
        DB 现在约定存 Jellyfin view，前缀匹配也得用 Jellyfin view。
        """
        from web.backend.config import settings
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

    def _scrape_codes(self, codes: List[str], task_id: int, stats: dict) -> int:
        """
        刮削一组番号。**关键**：每条 code 只在读 / 写两个瞬间持有 DB 连接，
        HTTP 抓取期间不持有，避免连接池被占满拖垮整个 API。
        """
        from web.backend.config import settings
        from web.backend.database import AdultItem, SessionLocal
        from tools.adult_manager.scrapers.manager import ScraperManager
        from tools.adult_manager.nfo_writer import write_nfo as do_write_nfo

        from common.rate_limiter import ADULT_SCRAPER_DELAY
        # 后台 adult watcher worker → batch=True
        manager = ScraperManager(
            delay=ADULT_SCRAPER_DELAY,
            sources=settings.adult_sources,
            batch=True,
        )
        if not manager.scrapers:
            return 0

        from web.backend.shutdown import is_shutting_down
        from datetime import datetime, timedelta
        from web.backend.api.adult import COOLDOWN_AFTER_FAILURES, COOLDOWN_DAYS
        ok = 0
        for code in codes:
            if is_shutting_down():
                logger.info(f"收到 shutdown 信号，watcher 刮削提前退出（已处理 {ok} 条）")
                break
            # ---- 短事务 1：读 item 元信息 ----
            # 跳过条件: 用户主动 excluded 永久跳过；自动 cooldown_until 未到期跳过
            with SessionLocal() as db:
                item = db.query(AdultItem).filter(AdultItem.code == code).first()
                if not item:
                    continue
                if item.excluded:
                    logger.debug(f"watcher 跳过已排除条目 {code}")
                    continue
                if item.cooldown_until and item.cooldown_until > datetime.utcnow():
                    logger.debug(f"watcher 跳过冷却中条目 {code}（到期 {item.cooldown_until}）")
                    continue
                file_path = item.file_path
                item_id = item.id

            try:
                # ---- 慢操作：HTTP 抓取，不持有 DB ----
                result = manager.scrape(code)
                if not result:
                    # 失败 +1；达阈值进 7 天 cooldown
                    with SessionLocal() as db:
                        it = db.query(AdultItem).filter(AdultItem.id == item_id).first()
                        if it:
                            it.scrape_attempts = (it.scrape_attempts or 0) + 1
                            it.last_scrape_at = datetime.utcnow()
                            it.source = 'not_found'
                            if it.scrape_attempts >= COOLDOWN_AFTER_FAILURES:
                                it.cooldown_until = datetime.utcnow() + timedelta(days=COOLDOWN_DAYS)
                                logger.info(
                                    f"watcher: {code} 连续失败 {it.scrape_attempts} 次，"
                                    f"进入 {COOLDOWN_DAYS} 天冷却（到 {it.cooldown_until}）"
                                )
                            db.commit()
                        self._append_log(db, task_id, stats, {
                            "name": Path(file_path).name if file_path else code,
                            "status": "scrape_not_found", "code": code,
                        })
                    continue

                d = result.to_dict()

                new_poster_path: Optional[str] = None
                if d.get('cover_url') and file_path:
                    try:
                        from web.backend.api.adult import _download_cover
                        cover_path = _download_cover(d['cover_url'], Path(file_path))
                        if cover_path:
                            new_poster_path = str(cover_path)
                    except Exception as e:
                        logger.warning(f"封面下载失败 {code}: {e}")

                new_nfo_path: Optional[str] = None
                if file_path:
                    try:
                        nfo_path = do_write_nfo(Path(file_path), d)
                        new_nfo_path = str(nfo_path)
                    except Exception as e:
                        logger.warning(f"NFO 写入失败 {code}: {e}")

                # ---- 短事务 2：回写 ----
                # 资源完整性判定：cover 和 nfo 都写盘成功才算"完整刮削"
                # 任一失败 → source 标 'partial'（保留 title/cover_url 等元数据，
                # 让"修复识别错误"任务下次可识别这是个未完成条目，走轻补救路径）
                assets_complete = bool(new_poster_path) and bool(new_nfo_path)
                # final_source 完整：保留 'merged:javbus,...' 原值
                # final_source partial：写成 'partial:javbus,...' —— 保留原源列表，
                #   下次"修复识别错误"补救后能还原成 'merged:xxx'
                orig_source = d.get('source') or ''
                final_source = orig_source if assets_complete else (
                    orig_source.replace('merged:', 'partial:') if orig_source.startswith('merged:')
                    else 'partial:' + orig_source if orig_source else 'partial'
                )
                with SessionLocal() as db:
                    item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
                    if not item:
                        continue
                    item.title = d.get('title')
                    item.release_date = d.get('release_date')
                    item.studio = d.get('studio')
                    item.director = d.get('director')
                    item.actors = _json.dumps(d.get('actors') or [], ensure_ascii=False)
                    item.tags = _json.dumps(d.get('tags') or [], ensure_ascii=False)
                    item.cover_url = d.get('cover_url')
                    item.rating = d.get('rating')
                    item.source = final_source
                    if new_poster_path:
                        item.poster_path = new_poster_path
                    if new_nfo_path:
                        item.nfo_path = new_nfo_path
                    # 成功 → reset 计数器，清除自动 cooldown（用户主动 excluded 不动）
                    item.scrape_attempts = 0
                    item.cooldown_until = None
                    item.last_scrape_at = datetime.utcnow()
                    db.commit()
                    title = item.title

                # 解析 sources：result.source 形如 'merged:javbus,javdb' 或单源 'javbus'
                raw_source = d.get('source') or ''
                sources_list = (
                    raw_source[len('merged:'):].split(',')
                    if raw_source.startswith('merged:')
                    else ([raw_source] if raw_source else [])
                )
                sources_list = [s.strip() for s in sources_list if s.strip()]

                ok += 1
                with SessionLocal() as db:
                    self._append_log(db, task_id, stats, {
                        "name": Path(file_path).name if file_path else code,
                        "status": "scraped", "code": code, "title": title,
                        "sources": sources_list,
                    })
            except Exception as e:
                logger.warning(f"刮削异常 {code}: {e}")
                stats["failed"] += 1
                with SessionLocal() as db:
                    self._append_log(db, task_id, stats, {
                        "name": code, "status": "scrape_failed", "code": code, "error": str(e)[:100],
                    })
        return ok


# 全局实例
watcher = AdultWatcher()
