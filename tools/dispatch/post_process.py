"""
完成转移后的字幕/音轨自动处理。

字幕：调 web.backend.api.subtitle.run_subtitle_auto_fix_inline
音轨：调 web.backend.api.audio.run_default_track_inline

两者都是 spike 1+2 已落地的 inline 函数；本模块只做"调用胶水"+ 失败仅警告语义。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# 字幕扩展名（跟 organizer 一致）
_SUB_EXTS = ('.srt', '.ass', '.ssa', '.sub', '.vtt', '.sup', '.idx', '.smi')


def post_process_subtitle_align(
    dispatched_files: List[str],
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> Tuple[str, Dict]:
    """字幕文件名对齐：release 自带的 sidecar 字幕没有 lang 标签时，
    用 SubtitleRenamer 嗅探内容主语言，重命名为 ``<video_stem>.<lang>.<ext>``。

    场景：BD release 经常带 .srt 但文件名是 ``Movie.2024.srt``（无 lang token），
    后续 subtitle_fetcher 把它视作 "lang=未知" → 不满足 preferred_langs → 又下一份。
    本步在 fetch 前先嗅探重命名，避免重复下载。

    实现：复用 ``SubtitleRenamer.process_directory``（已有内容嗅探 + 电影/剧集模式拆分）。
    输入 dispatched_files 收齐所在目录，每个目录跑一遍 process_directory。

    返回 (status, info)：status ∈ {'ok','skipped','warned'}；
    info.renamed_details 是 [(old_path, new_path)] 供调用方更新 dispatched_files 路径。
    """
    if not dispatched_files:
        return ('skipped', {'reason': 'no dispatched files'})

    # 收集字幕所在的目录（去重）
    sidecar_dirs = sorted({
        Path(p).parent for p in dispatched_files
        if Path(p).suffix.lower() in _SUB_EXTS and Path(p).exists()
    })
    if not sidecar_dirs:
        return ('skipped', {'reason': 'no sidecar subtitles in dispatched_files'})

    from tools.subtitle_manager.renamer import SubtitleRenamer
    renamer = SubtitleRenamer()
    logger.info(
        f"post_process_subtitle_align: 扫描 {len(sidecar_dirs)} 个目录"
        f"（共 {sum(1 for p in dispatched_files if Path(p).suffix.lower() in _SUB_EXTS)} 个 sidecar）"
    )

    all_results: List[Dict] = []
    for d in sidecar_dirs:
        try:
            results = renamer.process_directory(
                d, dry_run=False, recursive=False, verbose=False,
            )
        except Exception as e:
            logger.warning(f"align process_directory {d} 异常: {e}")
            continue
        all_results.extend(results)

    renamed_pairs = [
        (r['old_path'], r['new_path']) for r in all_results
        if r.get('success') and not r.get('dry_run') and r.get('old_path') != r.get('new_path')
    ]
    failed = [r for r in all_results if r.get('error')]

    info = {
        'directories': len(sidecar_dirs),
        'renamed': len(renamed_pairs),
        'failed': len(failed),
        'renamed_details': renamed_pairs[:20],     # 限长，避免 status_message 爆
    }
    if failed:
        logger.warning(f"post_process_subtitle_align: 部分失败 {len(failed)}")
        info['failed_details'] = [r.get('error') for r in failed[:10]]
        return ('warned', info)
    logger.info(f"post_process_subtitle_align: ok renamed={len(renamed_pairs)}")
    return ('ok', info)


def post_process_subtitle(
    dispatched_files: List[str],
    required_langs: Optional[List[str]] = None,
    downloading_langs: Optional[List[str]] = None,
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> Tuple[str, Dict]:
    """
    字幕处理：扫描 + 下载 + 重命名对齐。失败仅警告，不阻断。

    required_langs：判定"缺字幕"的依据（None → settings.required_langs）。
    downloading_langs：自动下载时的语言优先级（None → settings.downloading_langs）。
    返回 (status, info) 其中 status ∈ {'ok', 'warned', 'skipped'}。
    """
    if not dispatched_files:
        return ('skipped', {'reason': 'no dispatched files'})

    try:
        from web.backend.api.subtitle import run_subtitle_auto_fix_inline
        from web.backend.config import settings as _settings
    except Exception as e:
        logger.warning(f"字幕模块 import 失败（不阻断）: {e}")
        return ('warned', {'error': str(e)})

    req = required_langs or _settings.required_langs
    dl  = downloading_langs or _settings.downloading_langs
    logger.info(
        f"post_process_subtitle: 处理 {len(dispatched_files)} 个文件 "
        f"required={req} downloading={dl}"
    )

    try:
        result = run_subtitle_auto_fix_inline(
            paths=dispatched_files,
            recursive=False,
            required_langs=req,
            downloading_langs=dl,
            dry_run=False,
            do_rename=True,
            refresh_library_ids=[],   # 主 pipeline 已 refresh
            progress_cb=progress_cb,
        )
        if result.get('error'):
            logger.warning(f"post_process_subtitle: error={result['error']}")
            return ('warned', {'error': result['error']})
        dl_stats = result.get('download') or {}
        success = dl_stats.get('success', 0)
        failed = dl_stats.get('failed', 0)
        if failed > 0:
            logger.warning(
                f"post_process_subtitle: 部分下载失败 success={success} failed={failed}"
            )
            return ('warned', {'failed': failed, 'success': success})
        logger.info(f"post_process_subtitle: ok success={success}")
        return ('ok', result)
    except Exception as e:
        logger.warning(f"字幕处理失败（不阻断）: {e}")
        return ('warned', {'error': str(e)})


def post_process_audio(
    dispatched_files: List[str],
    preferred_langs: Optional[List[str]] = None,
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> Tuple[str, Dict]:
    """
    音轨处理：检查 default 音轨是否符合 preferred_audio_langs。
    符合 → 跳过；不符合且能切 → 改 default flag（mkvpropedit）；mkvtoolnix 缺失 → 自动降级为预览。

    返回 (status, info) 其中 status ∈ {'ok', 'warned', 'skipped'}。
    """
    if not dispatched_files:
        return ('skipped', {'reason': 'no dispatched files'})

    try:
        from web.backend.api.audio import run_default_track_inline
        from web.backend.config import settings as _settings
    except Exception as e:
        logger.warning(f"音轨模块 import 失败（不阻断）: {e}")
        return ('warned', {'error': str(e)})

    langs = preferred_langs or _settings.preferred_audio_langs
    logger.info(
        f"post_process_audio: 处理 {len(dispatched_files)} 个文件 langs={langs}"
    )

    try:
        result = run_default_track_inline(
            item_paths=dispatched_files,
            preferred_langs=langs,
            skip_single_track=True,
            apply=True,
            refresh_library_ids=[],
            progress_cb=progress_cb,
        )
        # mkvtoolnix 不可用会返回 error 字段且自动降级为预览
        if result.get('error'):
            logger.info(f"post_process_audio: 跳过 reason={result['error']!r}")
            return ('skipped', {'reason': result['error']})
        applied = result.get('applied') or 0
        logger.info(f"post_process_audio: ok applied={applied}")
        return ('ok', result)
    except Exception as e:
        logger.warning(f"音轨处理失败（不阻断）: {e}")
        return ('warned', {'error': str(e)})
