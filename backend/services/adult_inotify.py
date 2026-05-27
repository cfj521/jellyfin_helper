"""
成人内容文件系统监听器（watchdog/inotify）。

工作原理：
  - watchdog 库抽象 inotify/FSEvents/ReadDirectoryChangesW，
    对每个成人库目录建递归 watch
  - 文件创建 / 移入 / 修改事件 → debounce 5s 后批量喂 pipeline
  - 启动时 per-library 尝试建 watch：成功就启用 inotify，失败该库回落到 polling

跟 watcher（polling）的互补关系：
  - 主路径：inotify 即时触发（几秒内识别新文件）
  - 兜底：watcher polling 仍每 N 秒跑 rglob+DB diff（默认 1h）
    覆盖：服务停机窗口期的事件 / 外部进程写入 / inotify watch quota 超等场景
  - 两者并存无冲突，pipeline file_path + mtime 跳过策略让重复事件无害

启动日志：清楚说明每个库走的是 inotify 还是只靠 polling，方便诊断。
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from pathlib import Path
from typing import Dict, Optional, Set

from backend.api._media_exts import VIDEO_EXTS

logger = logging.getLogger(__name__)

# debounce：事件落地后等多少秒没新事件再 flush
# cp 100 个文件 → 100 个 modified 事件，5s 静默期之后一次性 flush 处理
DEBOUNCE_SEC = 5.0


class AdultInotifyWatcher:
    """模块级单例。main.py 启动时调 start()，关停调 stop()。"""

    def __init__(self):
        self._observer = None  # watchdog Observer
        self._pending: Dict[str, Set[str]] = defaultdict(set)  # lib_id → {paths}
        self._pending_lock = threading.Lock()
        self._flush_timer: Optional[threading.Timer] = None
        # 已成功 watch 的库（per-lib 状态，前端 status 暴露）
        self._active_libs: Set[str] = set()
        self._mode = 'disabled'  # disabled / unavailable / inotify

    # ====================================================================
    # 公开接口
    # ====================================================================

    def start(self) -> dict:
        """启动 inotify。对每个成人库尝试建 watch。返回 status dict。"""
        from backend.config import settings
        from backend.services.adult_scanner import scanner as _scanner

        if not (settings.adult_enabled and settings.adult_library_ids):
            logger.info("[inotify] 成人功能未启用 / 无配置库，监听跳过")
            self._mode = 'disabled'
            return {'mode': self._mode, 'active_libs': [], 'failed_libs': []}

        try:
            from watchdog.observers import Observer
        except ImportError:
            logger.warning(
                "[inotify] watchdog 库未安装（pip install watchdog），"
                "成人库新文件发现将仅依靠 polling 兜底（默认 1h 一次）"
            )
            self._mode = 'unavailable'
            return {'mode': self._mode, 'reason': 'watchdog_not_installed'}

        self._observer = Observer()

        active: list[str] = []
        failed: list[tuple[str, str]] = []
        for lib_id in settings.adult_library_ids:
            paths = _scanner._get_library_paths(lib_id)
            if not paths:
                logger.warning(f"[inotify] 库 {lib_id} 无本机路径，跳过")
                failed.append((lib_id, 'no_path'))
                continue
            success_any = False
            for p in paths:
                if not Path(p).exists():
                    logger.warning(f"[inotify] 库 {lib_id} 路径 {p} 不存在，跳过")
                    continue
                try:
                    handler = self._make_handler(lib_id)
                    self._observer.schedule(handler, p, recursive=True)
                    logger.info(
                        f"[inotify] ✓ 库 {lib_id} 监听 {p}（递归）—— "
                        f"新文件落盘后约 {int(DEBOUNCE_SEC)}s 内自动识别"
                    )
                    success_any = True
                except Exception as e:
                    logger.warning(
                        f"[inotify] ✗ 库 {lib_id} 路径 {p} 监听建立失败"
                        f"（{type(e).__name__}: {e}）—— 该路径回落 polling"
                    )
            if success_any:
                self._active_libs.add(lib_id)
                active.append(lib_id)
            else:
                failed.append((lib_id, 'all_paths_failed'))

        if not active:
            logger.info(
                "[inotify] 没有任何库成功建立监听 —— 全部走 polling 兜底"
            )
            self._observer = None
            self._mode = 'unavailable'
            return {'mode': self._mode, 'active_libs': [], 'failed_libs': failed}

        self._observer.start()
        self._mode = 'inotify'
        logger.info(
            f"[inotify] 启动成功 → {len(active)}/{len(settings.adult_library_ids)} "
            f"个库走 inotify（其余库 + 所有未走 inotify 的场景由 polling 兜底）"
        )
        return {'mode': self._mode, 'active_libs': active, 'failed_libs': failed}

    def stop(self):
        if self._flush_timer:
            self._flush_timer.cancel()
            self._flush_timer = None
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=10)
            except Exception:
                logger.exception("[inotify] observer 关闭异常")
            self._observer = None
        self._active_libs.clear()
        self._mode = 'disabled'
        logger.info("[inotify] 监听器已停止")

    def is_lib_active(self, lib_id: str) -> bool:
        """该库是否走 inotify（暴露给 watcher 判断是否要打"重复处理"日志）。"""
        return lib_id in self._active_libs

    def status(self) -> dict:
        return {
            'mode': self._mode,
            'running': bool(self._observer and self._observer.is_alive()) if self._observer else False,
            'active_libs': sorted(self._active_libs),
            'debounce_sec': DEBOUNCE_SEC,
        }

    # ====================================================================
    # 内部
    # ====================================================================

    def _make_handler(self, lib_id: str):
        """构造一个 watchdog handler，闭包绑定 lib_id。"""
        from watchdog.events import FileSystemEventHandler
        outer = self

        class _Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    outer._enqueue(lib_id, event.src_path)

            def on_moved(self, event):
                # mv 进来的：dest_path 是新位置；同时旧位置 src_path 已"消失"，按删除处理
                if event.is_directory:
                    return
                outer._handle_delete(lib_id, event.src_path)
                outer._enqueue(lib_id, getattr(event, 'dest_path', event.src_path))

            def on_modified(self, event):
                # Linux inotify 没有 IN_CLOSE_WRITE 抽象，cp 过程会反复触发 modified
                # debounce 解决：写完静默 5s 才 flush
                if not event.is_directory:
                    outer._enqueue(lib_id, event.src_path)

            def on_deleted(self, event):
                # 用户删除文件 → 立刻清 DB 行（不删元数据文件 NFO/poster）
                # 这样同 path 再次出现时（删后重加）pipeline 走 NEW 路径触发重刮
                if not event.is_directory:
                    outer._handle_delete(lib_id, event.src_path)

        return _Handler()

    def _handle_delete(self, lib_id: str, fs_path: str):
        """文件删除事件：从 DB 清掉对应 file_path 行；NFO/poster 文件保留不动。

        删除是即时信号（不走 debounce），每个文件单独处理。
        """
        # 视频扩展名过滤（与 _enqueue 对齐）
        if Path(fs_path).suffix.lower() not in VIDEO_EXTS:
            return
        from backend.database import SessionLocal, AdultItem
        from backend.path_translator import reverse_translate_path_with_settings

        jf_path = reverse_translate_path_with_settings(fs_path) or fs_path
        try:
            with SessionLocal() as db:
                row = (
                    db.query(AdultItem)
                    .filter(AdultItem.file_path == jf_path)
                    .first()
                )
                if row:
                    logger.info(
                        f"[inotify] 库 {lib_id} 文件删除 → 清 DB 行 "
                        f"id={row.id} code={row.code} path={jf_path}"
                    )
                    db.delete(row)
                    db.commit()
                else:
                    logger.debug(
                        f"[inotify] 库 {lib_id} 删除事件但 DB 无对应行: {jf_path}"
                    )
        except Exception:
            logger.exception(f"[inotify] 删除事件处理异常: {jf_path}")

    def _enqueue(self, lib_id: str, path: str):
        # 视频扩展名过滤（其它文件不进 pipeline）
        if Path(path).suffix.lower() not in VIDEO_EXTS:
            return
        with self._pending_lock:
            self._pending[lib_id].add(path)
            # 重置 debounce 定时器（每来一条事件重置一次，静默 5s 才 flush）
            if self._flush_timer:
                self._flush_timer.cancel()
            self._flush_timer = threading.Timer(DEBOUNCE_SEC, self._flush)
            self._flush_timer.daemon = True
            self._flush_timer.start()

    def _flush(self):
        """debounce 到期，把累积事件分库批量喂 pipeline。"""
        with self._pending_lock:
            batch = {lib: set(paths) for lib, paths in self._pending.items()}
            self._pending.clear()
            self._flush_timer = None

        if not batch:
            return

        from backend.config import settings
        from backend.services.adult_pipeline import AdultPipeline
        from backend.shutdown import is_shutting_down

        do_scrape = settings.adult_auto_scrape

        for lib_id, paths in batch.items():
            if is_shutting_down():
                logger.info("[inotify] 收到 shutdown 信号，提前退出")
                return
            try:
                # 文件可能在 debounce 间隔内被删 / 移走，过滤一下
                existing = [Path(p) for p in paths if Path(p).exists()]
                if not existing:
                    continue

                stats = {'new': 0, 'updated': 0, 'skipped': 0,
                         'unrecognized': 0, 'excluded': 0,
                         'scraped': 0, 'failed': 0}

                def on_progress(idx, total, file_info, result):
                    s = result.get('status', 'failed')
                    if s in stats:
                        stats[s] += 1
                    if result.get('scraped'):
                        stats['scraped'] += 1

                pipe = AdultPipeline(do_scrape=do_scrape, on_progress=on_progress)
                for idx, f in enumerate(existing):
                    pipe.process_one(f, idx=idx, total=len(existing))
                pipe.flush_jellyfin(lib_id)

                logger.info(
                    f"[inotify] 库 {lib_id} 触发 {len(paths)} 事件 → "
                    f"实际处理 {len(existing)} 文件，"
                    f"new={stats['new']} updated={stats['updated']} "
                    f"skipped={stats['skipped']} unrecognized={stats['unrecognized']} "
                    f"excluded={stats['excluded']} scraped={stats['scraped']} "
                    f"failed={stats['failed']}"
                )
            except Exception:
                logger.exception(f"[inotify] 库 {lib_id} flush 异常")


# 模块级单例
inotify_watcher = AdultInotifyWatcher()
