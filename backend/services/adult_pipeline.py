"""
成人内容处理流水线（Pipeline）。

输入：一个视频文件路径（由 scanner / watcher 喂进来）。
输出：跳过判定 → 番号识别 → 入库决策 → 元数据刮削 → 待通知 Jellyfin 路径攒入实例缓冲。
不感知"调用方是谁"——scanner / watcher 共用同一份处理逻辑。

进度回传：调用方注入 on_progress(idx, total, file_info, result) 拿到每条结果，
         决定上报 Task 还是只打日志。

Jellyfin 通知：实例内缓冲处理过的 path，调用方批末尾调 flush_jellyfin(library_id)
            一次性通知，避免逐条 HTTP。
"""
from __future__ import annotations

import json as _json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, List

logger = logging.getLogger(__name__)


def _jf_path(p) -> str:
    """本机扫到的文件路径 → Jellyfin view（DB 约定存 Jellyfin view）。
    同机部署 / 没配映射时 reverse 不命中规则会原样返回，无影响。"""
    s = str(p)
    from backend.path_translator import reverse_translate_path_with_settings
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


# 结果状态枚举（调用方按 status 分类统计 / 上报）
STATUS_NEW = 'new'                  # 新识别 + 入库
STATUS_UPDATED = 'updated'          # 已存在，mtime 变了重新处理
STATUS_MOVED = 'moved'              # 同 code 已存在但 file_path 变了
STATUS_SKIPPED = 'skipped'          # mtime 未变，跳过
STATUS_UNRECOGNIZED = 'unrecognized'  # 无法识别番号
STATUS_EXCLUDED = 'excluded'        # 用户主动 excluded，跳过
STATUS_FAILED = 'failed'            # 处理过程异常


class AdultPipeline:
    """
    成人内容处理流水线。一次扫描批次创建一个实例，跑完调 flush_jellyfin 释放。

    用法：
        pipe = AdultPipeline(do_scrape=True, on_progress=cb)
        for idx, f in enumerate(files):
            pipe.process_one(f, idx=idx, total=len(files))
        pipe.flush_jellyfin(library_id)
    """

    def __init__(
        self,
        *,
        do_scrape: bool,
        on_progress: Optional[Callable[[int, int, dict, dict], None]] = None,
    ):
        self._do_scrape = do_scrape
        self._on_progress = on_progress
        # 处理过的 jellyfin-view 路径，批末尾一次性 notify
        self._processed_paths: List[str] = []
        # ScraperManager 懒初始化（构造耗时，且无 scrape 任务时不需要）
        self._scraper_mgr = None

    # ====================================================================
    # 主入口
    # ====================================================================

    def process_one(
        self,
        file_path: Path,
        *,
        file_mtime: Optional[float] = None,
        idx: int = 0,
        total: int = 0,
    ) -> dict:
        """单文件全流程。异常吞掉返回 status='failed'。

        Args:
            file_path: 本机视角的视频文件路径
            file_mtime: 已知就传，None 时本函数自己 stat
            idx, total: 用于 on_progress 算百分比

        Returns:
            {status, code, item_id, name, error?}
        """
        try:
            return self._process_one_inner(file_path, file_mtime, idx, total)
        except Exception as e:
            logger.exception(f"pipeline: 处理异常 {file_path}")
            result = {
                'status': STATUS_FAILED,
                'code': None,
                'item_id': None,
                'name': file_path.name,
                'error': str(e)[:200],
            }
            self._emit_progress(idx, total, {'name': file_path.name}, result)
            return result

    def flush_jellyfin(self, library_id: str) -> None:
        """批末尾一次性 notify_media_updated；失败回落 refresh_library(library_id)。
        clear 内部缓冲，pipeline 实例可继续被复用（实际上一般不复用，一批一个）。"""
        if not self._processed_paths:
            return
        try:
            from backend.config import settings
            from common.jellyfin_client import JellyfinClient
            from backend.path_translator import reverse_translate_path_with_settings

            if not settings.jellyfin_api_key:
                logger.debug("pipeline: 未配 jellyfin_api_key，跳过通知")
                return

            jf = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
            # DB 存 jellyfin view；notify_media_updated 需要 jellyfin server 视角的绝对路径，
            # 大多数同机部署下两者一致，但若用户配了 path_translator 则 reverse 一下
            paths_for_jf = [
                reverse_translate_path_with_settings(p) or p
                for p in self._processed_paths
            ]
            ok = jf.notify_media_updated(paths_for_jf, update_type='Created')
            if not ok:
                # 精准通知失败 → 兜底库级刷新
                jf.refresh_library(library_id)
        except Exception as e:
            logger.warning(f"pipeline: jellyfin 通知失败: {e}")
        finally:
            self._processed_paths.clear()

    # ====================================================================
    # 内部：核心处理
    # ====================================================================

    def _process_one_inner(
        self, file_path: Path, file_mtime: Optional[float], idx: int, total: int,
    ) -> dict:
        from backend.database import SessionLocal, AdultItem
        from tools.adult_manager.code_extractor import extract_code

        # 取 mtime（已知就用，否则 stat）
        if file_mtime is None:
            try:
                file_mtime = file_path.stat().st_mtime
            except OSError:
                file_mtime = 0.0

        jf_path = _jf_path(file_path)
        name = file_path.name

        # 跳过判定 + 入库决策都在一个事务里
        with SessionLocal() as db:
            existing = (
                db.query(AdultItem)
                .filter(AdultItem.file_path == jf_path)
                .first()
            )

            # ① excluded：用户主动排除，永远跳过
            if existing and existing.excluded:
                result = {
                    'status': STATUS_EXCLUDED, 'code': existing.code,
                    'item_id': existing.id, 'name': name,
                }
                self._emit_progress(idx, total, {'name': name}, result)
                return result

            # ② mtime 未变：跳过
            if (
                existing and existing.file_mtime is not None
                and abs(existing.file_mtime - file_mtime) < 1.0
            ):
                if existing.code:
                    status = STATUS_SKIPPED
                else:
                    # 历史 unrecognized 占位行，依然算 unrecognized 不算 skipped
                    status = STATUS_UNRECOGNIZED
                result = {
                    'status': status, 'code': existing.code,
                    'item_id': existing.id, 'name': name,
                }
                self._emit_progress(idx, total, {'name': name}, result)
                return result

            # ③ 识别番号：父目录名优先（避免文件名水印 / 网站名误识）
            code = extract_code(file_path.parent.name) or extract_code(file_path.name)

            # ④ 本地 attachments 探测（清表 / 文件位置变更场景能避免重复下海报）
            local_poster, local_nfo = _detect_local_attachments(file_path)

            # ⑤ 识别失败：占位入库（excluded=True，普通列表隐藏）
            if not code:
                if existing:
                    existing.file_mtime = file_mtime
                    db.commit()
                    item_id = existing.id
                else:
                    new_item = AdultItem(
                        code=None, title=file_path.stem, file_path=jf_path,
                        file_mtime=file_mtime, excluded=True,
                    )
                    db.add(new_item)
                    db.commit()
                    item_id = new_item.id
                result = {
                    'status': STATUS_UNRECOGNIZED,
                    'code': existing.code if existing else None,
                    'item_id': item_id, 'name': name,
                }
                self._emit_progress(idx, total, {'name': name}, result)
                return result

            # ⑥ 识别成功：入库决策（new / updated / moved 三个分支）
            item_id, status = self._upsert_recognized(
                db, jf_path, file_mtime, code, existing, local_poster, local_nfo,
            )

        # ⑦ 攒待通知的 jellyfin path
        if status in (STATUS_NEW, STATUS_UPDATED, STATUS_MOVED):
            self._processed_paths.append(jf_path)

        # ⑧ 刮削（如果开启）
        scraped = False
        if self._do_scrape and status in (STATUS_NEW, STATUS_UPDATED, STATUS_MOVED):
            scraped = self._scrape_one(code, item_id, file_path)

        result = {
            'status': status, 'code': code, 'item_id': item_id, 'name': name,
            'scraped': scraped,
        }
        self._emit_progress(idx, total, {'name': name}, result)
        return result

    # ====================================================================
    # 内部：入库决策
    # ====================================================================

    def _upsert_recognized(
        self, db, jf_path: str, mtime: float, code: str,
        existing, local_poster, local_nfo,
    ):
        """识别成功后的入库三分支。返回 (item_id, status)。

        - existing 命中 file_path：之前已知文件，mtime 变了 → updated（或从 unrecognized → new）
        - existing 不在但同 code 已存在另一行：视为文件移动 → moved
        - 全新：直接 new
        """
        from backend.database import AdultItem

        # 分支 A：file_path 已存在
        if existing:
            was_unrecognized = not existing.code
            existing.file_mtime = mtime
            if local_poster:
                existing.poster_path = local_poster
            if local_nfo:
                existing.nfo_path = local_nfo

            if was_unrecognized:
                # 历史无 code 占位行升级，但要看同 code 是否已被其他行占用
                clash = (
                    db.query(AdultItem)
                    .filter(AdultItem.code == code, AdultItem.id != existing.id)
                    .first()
                )
                if clash:
                    # 当前文件并入 clash，原占位行删掉
                    clash.file_path = jf_path
                    clash.file_mtime = mtime
                    if local_poster:
                        clash.poster_path = local_poster
                    if local_nfo:
                        clash.nfo_path = local_nfo
                    db.delete(existing)
                    db.commit()
                    return clash.id, STATUS_MOVED
                existing.code = code
                db.commit()
                return existing.id, STATUS_NEW  # 算新识别成功

            db.commit()
            return existing.id, STATUS_UPDATED

        # 分支 B：file_path 不在，但同 code 已存在
        code_existing = db.query(AdultItem).filter(AdultItem.code == code).first()
        if code_existing:
            code_existing.file_path = jf_path
            code_existing.file_mtime = mtime
            if local_poster:
                code_existing.poster_path = local_poster
            if local_nfo:
                code_existing.nfo_path = local_nfo
            db.commit()
            return code_existing.id, STATUS_MOVED

        # 分支 C：全新
        new_item = AdultItem(
            code=code, file_path=jf_path, file_mtime=mtime,
            poster_path=local_poster, nfo_path=local_nfo,
        )
        db.add(new_item)
        db.commit()
        return new_item.id, STATUS_NEW

    # ====================================================================
    # 内部：刮削
    # ====================================================================

    def _get_scraper_mgr(self):
        """懒构造 ScraperManager，整个 pipeline 实例复用。"""
        if self._scraper_mgr is None:
            try:
                from common.rate_limiter import ADULT_SCRAPER_DELAY
                from tools.adult_manager.scrapers.manager import ScraperManager
                from backend.config import settings
                # 后台 worker → batch=True 走 batch 配额
                self._scraper_mgr = ScraperManager(
                    delay=ADULT_SCRAPER_DELAY,
                    sources=settings.adult_sources,
                    batch=True,
                )
            except Exception:
                logger.exception("ScraperManager 初始化失败")
                self._scraper_mgr = False  # 标记初始化失败，避免反复重试
        return self._scraper_mgr or None

    def _scrape_one(self, code: str, item_id: int, file_path: Path) -> bool:
        """
        单条 code 刮削。返回是否成功。

        DB 连接管理：HTTP 抓取期间不持有 DB 连接，避免连接池被占满拖垮 API。
        """
        from backend.database import SessionLocal, AdultItem
        from backend.api.adult import COOLDOWN_AFTER_FAILURES, COOLDOWN_DAYS

        mgr = self._get_scraper_mgr()
        if mgr is None or not mgr.scrapers:
            return False

        # ---- 短事务 1：读 item 元信息 + 跳过判断 ----
        with SessionLocal() as db:
            item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
            if not item:
                return False
            if item.excluded:
                return False
            if item.cooldown_until and item.cooldown_until > datetime.utcnow():
                logger.debug(f"pipeline: {code} 冷却中（到 {item.cooldown_until}），跳过")
                return False
            item_file_path = item.file_path

        # ---- 慢操作：HTTP 抓取，不持 DB ----
        try:
            result = mgr.scrape(code)
        except Exception as e:
            logger.warning(f"pipeline: 刮削异常 {code}: {e}")
            return False

        if not result:
            # 失败计数 +1；达阈值进 7 天冷却
            with SessionLocal() as db:
                it = db.query(AdultItem).filter(AdultItem.id == item_id).first()
                if it:
                    it.scrape_attempts = (it.scrape_attempts or 0) + 1
                    it.last_scrape_at = datetime.utcnow()
                    it.source = 'not_found'
                    if it.scrape_attempts >= COOLDOWN_AFTER_FAILURES:
                        it.cooldown_until = datetime.utcnow() + timedelta(days=COOLDOWN_DAYS)
                        logger.info(
                            f"pipeline: {code} 连续失败 {it.scrape_attempts} 次，"
                            f"进入 {COOLDOWN_DAYS} 天冷却（到 {it.cooldown_until}）"
                        )
                    db.commit()
            return False

        d = result.to_dict()

        # 资源下载
        new_poster_path: Optional[str] = None
        if d.get('cover_url') and item_file_path:
            try:
                from backend.api.adult import _download_cover
                cp = _download_cover(d['cover_url'], Path(item_file_path))
                if cp:
                    new_poster_path = str(cp)
            except Exception as e:
                logger.warning(f"封面下载失败 {code}: {e}")

        new_nfo_path: Optional[str] = None
        if item_file_path:
            try:
                from tools.adult_manager.nfo_writer import write_nfo
                np = write_nfo(Path(item_file_path), d)
                new_nfo_path = str(np)
            except Exception as e:
                logger.warning(f"NFO 写入失败 {code}: {e}")

        # ---- 短事务 2：回写 ----
        # 资源完整性：cover/nfo 任一失败 → source 标 'partial:xxx'
        assets_complete = bool(new_poster_path) and bool(new_nfo_path)
        orig_source = d.get('source') or ''
        final_source = orig_source if assets_complete else (
            orig_source.replace('merged:', 'partial:') if orig_source.startswith('merged:')
            else ('partial:' + orig_source if orig_source else 'partial')
        )
        with SessionLocal() as db:
            it = db.query(AdultItem).filter(AdultItem.id == item_id).first()
            if not it:
                return False
            it.title = d.get('title')
            it.release_date = d.get('release_date')
            it.studio = d.get('studio')
            it.director = d.get('director')
            it.actors = _json.dumps(d.get('actors') or [], ensure_ascii=False)
            it.tags = _json.dumps(d.get('tags') or [], ensure_ascii=False)
            it.cover_url = d.get('cover_url')
            it.rating = d.get('rating')
            it.source = final_source
            if new_poster_path:
                it.poster_path = new_poster_path
            if new_nfo_path:
                it.nfo_path = new_nfo_path
            # 成功 → reset 计数器，清自动 cooldown（用户主动 excluded 不动）
            it.scrape_attempts = 0
            it.cooldown_until = None
            it.last_scrape_at = datetime.utcnow()
            db.commit()
        return True

    # ====================================================================
    # 内部：进度回调
    # ====================================================================

    def _emit_progress(self, idx: int, total: int, file_info: dict, result: dict):
        if self._on_progress is None:
            return
        try:
            self._on_progress(idx, total, file_info, result)
        except Exception:
            logger.exception("pipeline: on_progress callback 异常")
