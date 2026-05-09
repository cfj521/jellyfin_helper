"""
完成转移后的字幕/音轨自动处理。

字幕：调 web.backend.api.subtitle.run_subtitle_auto_fix_inline
音轨：调 web.backend.api.audio.run_default_track_inline

两者都是 spike 1+2 已落地的 inline 函数；本模块只做"调用胶水"+ 失败仅警告语义。
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def post_process_subtitle(
    dispatched_files: List[str],
    preferred_langs: Optional[List[str]] = None,
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> Tuple[str, Dict]:
    """
    字幕处理：扫描 + 下载 + 重命名对齐。失败仅警告，不阻断。

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

    langs = preferred_langs or _settings.preferred_langs

    try:
        result = run_subtitle_auto_fix_inline(
            paths=dispatched_files,
            recursive=False,
            expected_langs=langs,
            dry_run=False,
            do_rename=True,
            refresh_library_ids=[],   # 主 pipeline 已 refresh
            progress_cb=progress_cb,
        )
        if result.get('error'):
            return ('warned', {'error': result['error']})
        if result['download'].get('failed', 0) > 0:
            return ('warned', {
                'failed': result['download']['failed'],
                'success': result['download'].get('success', 0),
            })
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
            return ('skipped', {'reason': result['error']})
        return ('ok', result)
    except Exception as e:
        logger.warning(f"音轨处理失败（不阻断）: {e}")
        return ('warned', {'error': str(e)})
