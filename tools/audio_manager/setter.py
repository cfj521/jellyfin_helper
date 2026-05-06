"""
默认音轨修改器：调用 mkvpropedit 修改 mkv 的 flag-default。

mkvpropedit 来自 mkvtoolnix 套件（apt install mkvtoolnix / brew install mkvtoolnix）。
"""
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_MKVPROPEDIT_PATH: Optional[str] = None
_MKVPROPEDIT_CHECKED = False


def get_mkvpropedit() -> Optional[str]:
    """惰性查找 mkvpropedit；进程级缓存。"""
    global _MKVPROPEDIT_PATH, _MKVPROPEDIT_CHECKED
    if not _MKVPROPEDIT_CHECKED:
        _MKVPROPEDIT_PATH = shutil.which("mkvpropedit")
        _MKVPROPEDIT_CHECKED = True
        if _MKVPROPEDIT_PATH is None:
            logger.warning(
                "未找到 mkvpropedit，无法修改默认音轨。"
                "请安装 mkvtoolnix（apt install mkvtoolnix / brew install mkvtoolnix）。"
            )
    return _MKVPROPEDIT_PATH


def set_default_audio_track(
    video: Path,
    target_track_id: int,
    total_tracks: int,
    timeout: float = 60.0,
) -> Dict:
    """
    把目标音轨设为默认，其它音轨清掉默认标记。

    Args:
        video: mkv 文件路径
        target_track_id: 目标音轨的 mkv 1-based track id（即 a{n} 中的 n）
        total_tracks: 该视频总音轨数（用于把其它轨的 flag-default 清掉）

    Returns:
        {
          'success': bool,
          'cmd': str,
          'stdout': str,
          'stderr': str,
          'error': Optional[str],
        }
    """
    mkvpropedit = get_mkvpropedit()
    if not mkvpropedit:
        return {
            'success': False,
            'cmd': '',
            'stdout': '',
            'stderr': '',
            'error': 'mkvpropedit 未安装',
        }

    if not video.exists():
        return {
            'success': False, 'cmd': '', 'stdout': '', 'stderr': '',
            'error': f'文件不存在: {video}',
        }

    if target_track_id < 1 or target_track_id > total_tracks:
        return {
            'success': False, 'cmd': '', 'stdout': '', 'stderr': '',
            'error': f'目标 track id 越界: {target_track_id} (共 {total_tracks} 轨)',
        }

    args: List[str] = [mkvpropedit, str(video)]
    for tid in range(1, total_tracks + 1):
        flag = '1' if tid == target_track_id else '0'
        args.extend(['--edit', f'track:a{tid}', '--set', f'flag-default={flag}'])

    cmd_str = ' '.join(args)
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            encoding='utf-8', errors='replace',
        )
    except subprocess.TimeoutExpired:
        return {
            'success': False, 'cmd': cmd_str, 'stdout': '', 'stderr': '',
            'error': f'mkvpropedit 超时（>{timeout}s）',
        }
    except OSError as e:
        return {
            'success': False, 'cmd': cmd_str, 'stdout': '', 'stderr': '',
            'error': f'mkvpropedit 调用失败: {e}',
        }

    return {
        'success': proc.returncode == 0,
        'cmd': cmd_str,
        'stdout': proc.stdout,
        'stderr': proc.stderr,
        'error': None if proc.returncode == 0 else proc.stderr.strip()[:300] or f'rc={proc.returncode}',
    }
