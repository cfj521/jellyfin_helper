"""
音轨扫描器
通过 ffprobe 读取视频音频流的语言/默认标记，决定是否需要把默认音轨改为期望语言。

为何只支持 .mkv：
  matroska 容器允许通过 mkvpropedit 修改单条轨道的 flag-default 而无需重新封装；
  mp4/mov/avi 等修改默认轨需要 remux，代价大，不在本工具范围内。
"""
import json
import logging
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

from tools.subtitle_manager.embedded_probe import normalize_lang
from tools.subtitle_manager.scanner import normalize_lang_code

logger = logging.getLogger(__name__)

# 仅这些容器格式可以无损改默认音轨
MODIFIABLE_VIDEO_EXTS = {'.mkv'}
# 扫描时仍需识别的视频文件类型（避免错过非 mkv 的文件，让结果里能体现"已跳过"）
ALL_VIDEO_EXTS = {'.mkv', '.mp4', '.m4v', '.mov', '.avi', '.wmv', '.flv', '.ts'}

_FFPROBE_PATH: Optional[str] = None
_FFPROBE_CHECKED = False


def _get_ffprobe() -> Optional[str]:
    """惰性查找 ffprobe；进程级缓存。"""
    global _FFPROBE_PATH, _FFPROBE_CHECKED
    if not _FFPROBE_CHECKED:
        _FFPROBE_PATH = shutil.which("ffprobe")
        _FFPROBE_CHECKED = True
        if _FFPROBE_PATH is None:
            logger.warning("未找到 ffprobe，音轨扫描将无法运行。请安装 ffmpeg。")
    return _FFPROBE_PATH


def probe_audio_streams(video: Path, timeout: float = 10.0) -> List[Dict]:
    """
    返回视频的所有音频流信息：
      [{"index": 1, "audio_index": 0, "language": "eng", "title": "...",
        "lang_code": "eng", "is_default": True, "channels": 6,
        "codec": "ac3"}, ...]

    audio_index：在该视频的音频流列表中的 0-based 序号；mkvpropedit 使用 1-based
                 （audio_index + 1 = mkv 的 a{n} 编号）。
    ffprobe 不可用 / 视频损坏返回 []。
    """
    ffprobe = _get_ffprobe()
    if not ffprobe:
        return []

    if not video.exists():
        return []

    cmd = [
        ffprobe,
        "-v", "error",
        "-select_streams", "a",
        "-show_entries",
        "stream=index,codec_name,channels,disposition:stream_tags=language,title",
        "-of", "json",
        str(video),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding='utf-8', errors='replace',
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"ffprobe 超时: {video}")
        return []
    except OSError as e:
        logger.warning(f"ffprobe 调用失败: {video} - {e}")
        return []

    if proc.returncode != 0:
        logger.debug(f"ffprobe rc={proc.returncode}: {video} - {proc.stderr.strip()[:200]}")
        return []

    try:
        data = json.loads(proc.stdout) if proc.stdout else {}
    except json.JSONDecodeError as e:
        logger.warning(f"ffprobe 输出非 JSON: {video} - {e}")
        return []

    streams = data.get('streams') or []
    out: List[Dict] = []
    for ai, s in enumerate(streams):
        tags = s.get('tags') or {}
        language = tags.get('language')
        title = tags.get('title')
        disposition = s.get('disposition') or {}
        out.append({
            'index': s.get('index'),
            'audio_index': ai,                 # 0-based 在音频流中的序号
            'mkv_track_id': ai + 1,            # mkvpropedit 用的 1-based a{n}
            'language': language,
            'title': title,
            'lang_code': normalize_lang(language, title),
            'is_default': bool(disposition.get('default')),
            'channels': s.get('channels'),
            'codec': s.get('codec_name'),
        })
    return out


def _pick_target_track(tracks: List[Dict], preferred_langs: List[str]) -> Optional[Dict]:
    """
    在多条音轨里挑一条最该被设为默认的：
      - 按 preferred_langs 顺序取第一个命中的语言
      - 同语言下声道数多的优先（例如 5.1 优于 2.0），便于自动选"正版主音轨"
    没有任何命中返回 None。
    """
    for lang in preferred_langs:
        candidates = [t for t in tracks if t.get('lang_code') == lang]
        if not candidates:
            continue
        # 按声道数降序；同声道数保持原顺序
        candidates.sort(key=lambda t: -(t.get('channels') or 0))
        return candidates[0]
    return None


class AudioTrackScanner:
    """
    扫描视频音轨，给出"是否需要改默认音轨"的判断。

    结果结构：
        {
          'path': str,
          'modifiable': bool,            # 容器格式是否支持就地改默认轨（mkv=True）
          'skipped': bool,               # 是否被跳过（单轨或不可改）
          'skip_reason': Optional[str],  # 跳过原因
          'tracks': [...],               # 全部音轨
          'current_default_track': int,  # 当前默认轨的 mkv_track_id (1-based)
          'current_default_lang': str,
          'should_change': bool,         # 是否应当改
          'target_track': int,           # 应改成的 mkv_track_id
          'target_lang': str,
        }
    """

    def __init__(
        self,
        preferred_langs: List[str],
        skip_single_track: bool = True,
        concurrency: int = 5,
    ):
        # 归一化用户配置（chs/cht/eng/jpn/kor），过滤无法识别的
        normalized = []
        for lang in (preferred_langs or []):
            code = normalize_lang_code(lang)
            if code and code not in normalized:
                normalized.append(code)
        self.preferred_langs = normalized or ['chs', 'eng']
        self.skip_single_track = skip_single_track
        self.concurrency = max(1, concurrency)

    def analyze_video(self, video: Path) -> Dict:
        """单个视频的扫描+判定。"""
        ext = video.suffix.lower()
        result: Dict = {
            'path': str(video),
            'name': video.name,
            'modifiable': ext in MODIFIABLE_VIDEO_EXTS,
            'skipped': False,
            'skip_reason': None,
            'tracks': [],
            'current_default_track': None,
            'current_default_lang': None,
            'should_change': False,
            'target_track': None,
            'target_lang': None,
        }

        if ext not in ALL_VIDEO_EXTS:
            result['skipped'] = True
            result['skip_reason'] = '非视频文件'
            return result

        if not result['modifiable']:
            # 仍可探测显示信息，但不会建议修改
            result['skipped'] = True
            result['skip_reason'] = f'容器 {ext} 不支持就地改默认音轨'

        tracks = probe_audio_streams(video)
        result['tracks'] = tracks

        if not tracks:
            result['skipped'] = True
            result['skip_reason'] = result['skip_reason'] or '无法读取音轨信息'
            return result

        if self.skip_single_track and len(tracks) <= 1:
            result['skipped'] = True
            result['skip_reason'] = '只有 1 条音轨，无需选择'
            return result

        # 找当前默认轨
        defaults = [t for t in tracks if t.get('is_default')]
        if defaults:
            cur = defaults[0]
        else:
            # mkv 没标 default 的情况：约定用第 1 轨（播放器通常这样选）
            cur = tracks[0]
        result['current_default_track'] = cur['mkv_track_id']
        result['current_default_lang'] = cur.get('lang_code') or cur.get('language')

        # 当前默认已经是期望语言之一 → 不动
        if cur.get('lang_code') in self.preferred_langs:
            return result

        # 否则在其它轨找期望语言
        if not result['modifiable']:
            # 不可改的文件即便能找到目标也不建议修改
            return result

        target = _pick_target_track(tracks, self.preferred_langs)
        if target is None:
            return result
        if target.get('mkv_track_id') == cur.get('mkv_track_id'):
            return result

        result['should_change'] = True
        result['target_track'] = target['mkv_track_id']
        result['target_lang'] = target.get('lang_code')
        return result

    def scan_videos(self, videos: List[Path]) -> List[Dict]:
        """并发扫描一批视频。"""
        if not videos:
            return []

        results: List[Dict] = [None] * len(videos)

        def _worker(idx_video):
            idx, v = idx_video
            try:
                results[idx] = self.analyze_video(v)
            except Exception as e:
                logger.warning(f"音轨扫描失败: {v} - {e}")
                results[idx] = {
                    'path': str(v),
                    'name': v.name,
                    'modifiable': False,
                    'skipped': True,
                    'skip_reason': f'扫描异常: {e}',
                    'tracks': [],
                }

        max_workers = min(self.concurrency, len(videos))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(ex.map(_worker, enumerate(videos)))
        return results

    def scan_directory(self, directory: Path, recursive: bool = True) -> List[Dict]:
        """递归扫描目录下所有视频文件。"""
        videos: List[Path] = []
        try:
            iterator = directory.rglob('*') if recursive else directory.iterdir()
            for f in iterator:
                if f.is_file() and f.suffix.lower() in ALL_VIDEO_EXTS:
                    videos.append(f)
        except (PermissionError, OSError) as e:
            logger.warning(f"读取目录失败: {directory} - {e}")
            return []
        return self.scan_videos(videos)
