"""
成人内容增量监听器（IncrementalWatcher）。

跟 adult_scanner（全库主动扫）的区别：
  - 自己 rglob 库目录拿 fs 当前文件集合，跟 DB 已知 file_path 集合做 diff
  - 不依赖 Jellyfin DateLastSaved（被 jellyfin 全库 refresh 误触的根因）
  - 不创建 Task，纯后台跑；只对 diff 出来的新文件喂 pipeline

触发：jellyfin_poller 每 N 秒调一次 poll_libraries()。
状态：DB 自己就是状态（AdultItem.file_path 集合），无需额外状态表。
  （AdultWatcherState 表保留兼容历史 schema，但本模块不再读写它）

冷启动语义：库第一次 poll → 跟之前任何 poll 一样，全部走"diff = fs - DB"。
  首次部署后会把 fs 上所有"DB 未知"的文件喂给 pipeline 触发刮削。这是预期：
  初始扫描既可以让 scanner 全量做，也可以让 watcher 自然吸收。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

from backend.api._media_exts import VIDEO_EXTS

logger = logging.getLogger(__name__)


class AdultWatcher:
    """模块级单例。polling worker 调用 poll_libraries()。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._last_run_at: Optional[float] = None
        self._last_run_summary: dict = {}
        # 防 reentry：同一库正在处理时跳过本轮
        self._running_libs: Set[str] = set()

    # ====================================================================
    # 公开接口
    # ====================================================================

    def status(self) -> dict:
        """暴露给前端 / API：最近一次 summary + 正在跑的库"""
        return {
            "last_run_at": self._last_run_at,
            "last_run_summary": self._last_run_summary,
            "running_libs": list(self._running_libs),
        }

    def poll_libraries(self, library_ids: List[str]) -> Dict[str, dict]:
        """
        polling worker 调用入口。同步处理每个库（顺序跑，避免并发刮削打外站太狠）。

        Returns:
            {library_id: stats} 实际处理过的库（被 reentry 跳过的不出现）
        """
        from backend.config import settings

        if not settings.jellyfin_api_key:
            return {}

        do_scrape = settings.adult_auto_scrape
        results: Dict[str, dict] = {}

        for lib_id in library_ids:
            with self._lock:
                if lib_id in self._running_libs:
                    logger.debug(f"watcher: 库 {lib_id} 正在处理，跳过本轮")
                    continue
                self._running_libs.add(lib_id)
            try:
                stats = self._poll_one_library(lib_id, do_scrape)
                if stats is not None:
                    results[lib_id] = stats
            except Exception:
                logger.exception(f"watcher: 库 {lib_id} 处理异常")
            finally:
                with self._lock:
                    self._running_libs.discard(lib_id)

        self._last_run_at = time.time()
        if results:
            # 合并所有库的 stats 作为 last_run_summary
            agg = {}
            for s in results.values():
                for k, v in s.items():
                    agg[k] = agg.get(k, 0) + v
            self._last_run_summary = agg
        return results

    # ====================================================================
    # 内部
    # ====================================================================

    def _get_library_local_paths(self, library_id: str) -> List[str]:
        """拿库的本机视角路径列表（已 forward translate）。复用 scanner 的实现。"""
        from backend.services.adult_scanner import scanner
        return scanner._get_library_paths(library_id)

    def _poll_one_library(self, library_id: str, do_scrape: bool) -> Optional[dict]:
        """单库处理：rglob fs → diff DB → 喂 pipeline。返回 stats 或 None。"""
        from backend.database import SessionLocal, AdultItem
        from backend.path_translator import reverse_translate_path_with_settings
        from backend.services.adult_pipeline import AdultPipeline
        from backend.shutdown import is_shutting_down

        # ① 拿库的本机路径
        local_paths = self._get_library_local_paths(library_id)
        if not local_paths:
            logger.debug(f"watcher: 库 {library_id} 无路径，跳过")
            return None

        # ② rglob 本机文件系统拿当前所有视频文件（jellyfin view 形式，跟 DB 对齐）
        fs_files: Dict[str, Path] = {}  # jf_path → local Path
        for p in local_paths:
            try:
                for f in Path(p).rglob('*'):
                    if not f.is_file():
                        continue
                    if f.suffix.lower() not in VIDEO_EXTS:
                        continue
                    jf_view = reverse_translate_path_with_settings(str(f)) or str(f)
                    fs_files[jf_view] = f
            except (PermissionError, OSError) as e:
                logger.warning(f"watcher: 扫描路径失败 {p}: {e}")

        # ③ 查 DB 该库已知 file_path 集合（用 jellyfin view 前缀匹配）
        from backend.services.adult_scanner import scanner
        raw_paths = scanner._get_library_paths_raw(library_id)
        if not raw_paths:
            return None
        known_paths: Set[str] = set()
        with SessionLocal() as db:
            for raw in raw_paths:
                prefix = str(raw).rstrip('/').rstrip('\\')
                # 取该前缀下所有 file_path（仅 file_path 列，避免拉整行）
                rows = (
                    db.query(AdultItem.file_path)
                    .filter(AdultItem.file_path.ilike(f'{prefix}%', escape='|'))
                    .all()
                )
                for (fp,) in rows:
                    if fp:
                        known_paths.add(fp)

        # ④ diff = fs - DB = 新文件
        new_jf_paths = set(fs_files.keys()) - known_paths

        if not new_jf_paths:
            # 完全无新增 —— 静默 debug，不打 INFO 噪音
            logger.debug(
                f"[polling] 库 {library_id} fs={len(fs_files)} "
                f"DB={len(known_paths)} diff=0"
            )
            return {'scanned': 0, 'new': 0, 'updated': 0,
                    'skipped': 0, 'unrecognized': 0, 'excluded': 0,
                    'scraped': 0, 'failed': 0}

        # ⑤ 准备 stats + on_progress + pipeline
        stats = {'scanned': 0, 'new': 0, 'updated': 0,
                 'skipped': 0, 'unrecognized': 0, 'excluded': 0,
                 'scraped': 0, 'failed': 0}

        def on_progress(idx, total, file_info, result):
            stats['scanned'] += 1
            status = result.get('status', 'failed')
            if status in stats:
                stats[status] += 1
            if result.get('scraped'):
                stats['scraped'] += 1

        pipe = AdultPipeline(do_scrape=do_scrape, on_progress=on_progress)

        # ⑥ 处理新文件
        new_local_files = [fs_files[jf] for jf in sorted(new_jf_paths)]
        total = len(new_local_files)
        for idx, local_f in enumerate(new_local_files):
            if is_shutting_down():
                logger.info(
                    f"watcher: 收到 shutdown 信号，提前退出（已处理 {idx}/{total}）"
                )
                break
            try:
                mtime = local_f.stat().st_mtime
            except OSError:
                mtime = 0.0
            pipe.process_one(local_f, file_mtime=mtime, idx=idx, total=total)

        # ⑦ flush Jellyfin（pipeline 内部攒了处理过的 path）
        pipe.flush_jellyfin(library_id)

        # ⑧ 日志总结：找到 diff 必打 INFO（带 [polling] 前缀跟 inotify 区分）
        # 若该库 inotify 已经活跃，多半是 polling 兜底捡到了 inotify 漏掉的（重启窗口/外部写入）
        try:
            from backend.services.adult_inotify import inotify_watcher
            inotify_active = inotify_watcher.is_lib_active(library_id)
        except Exception:
            inotify_active = False
        backup_note = '（兜底补漏 inotify 漏的）' if inotify_active else ''
        logger.info(
            f"[polling] 库 {library_id}{backup_note} fs={len(fs_files)} "
            f"DB={len(known_paths)} diff={total} → new={stats['new']} "
            f"updated={stats['updated']} unrecognized={stats['unrecognized']} "
            f"excluded={stats['excluded']} scraped={stats['scraped']} "
            f"failed={stats['failed']}"
        )
        return stats


# 模块级单例
watcher = AdultWatcher()
