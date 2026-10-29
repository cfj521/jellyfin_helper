"""把 qB 原始种子名（release name）从下载流水线带到媒体库。

Jellyfin 只认识文件路径，不认识种子名；所以入库后必须在自己的 DB 里
按文件路径保留一份。这里集中处理路径归一化和批量查询，避免各 API 各写一套。
"""
from __future__ import annotations

import logging
from typing import Dict, Iterable, Optional

from sqlalchemy import cast, or_
from sqlalchemy.dialects.postgresql import JSONB

logger = logging.getLogger(__name__)


def normalize_path(path: Optional[str]) -> str:
    """统一成 / 分隔、无尾斜杠，便于跨 Windows/Jellyfin view 匹配。"""
    if not path:
        return ''
    return str(path).replace('\\', '/').rstrip('/')


def _path_variants(paths: Iterable[str]) -> set:
    out = set()
    try:
        from backend.path_translator import (
            reverse_translate_path_with_settings,
            translate_path_with_settings,
        )
    except Exception:
        reverse_translate_path_with_settings = translate_path_with_settings = None

    for p in paths:
        if not p:
            continue
        raw = str(p)
        variants = {raw}
        if translate_path_with_settings:
            try:
                variants.add(translate_path_with_settings(raw) or raw)
            except Exception:
                pass
        if reverse_translate_path_with_settings:
            try:
                variants.add(reverse_translate_path_with_settings(raw) or raw)
            except Exception:
                pass
        for v in list(variants):
            out.add(v)
            out.add(v.replace('\\', '/'))
            out.add(v.replace('/', '\\'))
    return out


def _first_nonempty(*values: Optional[str]) -> Optional[str]:
    for v in values:
        if v and str(v).strip():
            return str(v).strip()
    return None


def source_names_for_paths(db, paths: Iterable[str]) -> Dict[str, str]:
    """批量返回 {原始路径: release 名}。

    优先级：media_items / adult_items 的持久字段 > dispatch_map 的搬运记录。
    查不到就返回空 dict；任何一张表还没迁移也不能影响媒体库列表。
    """
    paths = [p for p in paths if p]
    if not paths:
        return {}

    result: Dict[str, str] = {}
    raw_paths = _path_variants(paths)

    try:
        from backend.database import AdultItem, DownloadDispatchMap, MediaItem

        # ① 已经写入媒体/成人条目的，优先用条目自己的字段
        for model in (MediaItem, AdultItem):
            rows = (
                db.query(model.file_path, model.source_name)
                .filter(model.file_path.in_(list(raw_paths)))
                .filter(model.source_name.isnot(None))
                .all()
            )
            for file_path, source_name in rows:
                key = normalize_path(file_path)
                if key and source_name:
                    result.setdefault(key, str(source_name))

        # ② 还没被扫描入库的，从 dispatch_map 的 dispatched_files 反查
        conds = [
            cast(DownloadDispatchMap.dispatched_files, JSONB).contains([p])
            for p in raw_paths
        ]
        if conds:
            rows = (
                db.query(
                    DownloadDispatchMap.dispatched_files,
                    DownloadDispatchMap.source_name,
                )
                .filter(DownloadDispatchMap.source_name.isnot(None))
                .filter(or_(*conds))
                .limit(2000)
                .all()
            )
            for files, source_name in rows:
                if not source_name:
                    continue
                for file_path in (files or []):
                    key = normalize_path(file_path)
                    if key:
                        result.setdefault(key, str(source_name))
    except Exception as e:
        # 迁移未执行 / 旧库缺列时只降级为“暂无 release 名”，不能拖垮列表
        logger.warning(f"source_names_for_paths 查询失败: {e}")
        db.rollback()
        return {}

    out: Dict[str, str] = {}
    for p in paths:
        # DB 里存的是 Jellyfin view，前端/流水线给的可能是本机路径；
        # 用同一组路径变体回查，才能把结果映射回调用方传入的 key。
        name = None
        for variant in _path_variants([p]):
            name = result.get(normalize_path(variant))
            if name:
                break
        if name:
            out[p] = name
            out[normalize_path(p)] = name
    return out


def lookup_source_name(db, path: Optional[str]) -> Optional[str]:
    """单路径查询；列表场景请用 source_names_for_paths 避免 N+1。"""
    if not path:
        return None
    found = source_names_for_paths(db, [path])
    return found.get(path) or found.get(normalize_path(path)) or next(
        iter(found.values()), None
    )


def record_source_names_for_paths(
    db, source_name: Optional[str], paths: Iterable[str]
) -> int:
    """复制完成后，把 release 名回填到已经存在的媒体/成人条目。

    扫描通常在这之后才建行；这里只更新已存在的行，扫描路径自己也会补。
    """
    source_name = _first_nonempty(source_name)
    paths = [p for p in paths if p]
    if not source_name or not paths:
        return 0

    updated = 0
    try:
        from backend.database import AdultItem, MediaItem

        raw_paths = _path_variants(paths)
        for model in (MediaItem, AdultItem):
            rows = db.query(model).filter(model.file_path.in_(list(raw_paths))).all()
            for row in rows:
                if not row.source_name:
                    row.source_name = source_name
                    updated += 1
        if updated:
            db.commit()
    except Exception as e:
        logger.warning(f"record_source_names_for_paths 失败: {e}")
        db.rollback()
        return 0
    return updated
