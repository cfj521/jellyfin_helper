"""
内嵌字幕轨道探测器
通过调用系统 ffprobe 获取视频内的字幕轨道及其语言信息。

设计要点：
- 只读容器头（ffprobe 默认行为），不解码任何画面，单文件耗时 ~10-50ms
- ffprobe 不在 PATH 时优雅降级：返回空列表 + 一次性 warning，不影响整体扫描流程
- 线程并发由调用方控制（ThreadPoolExecutor），本模块只关心单文件探测
"""
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ffprobe 可用性检测：进程级一次性，避免每次 shutil.which
_FFPROBE_PATH: Optional[str] = None
_FFPROBE_CHECKED = False


def _get_ffprobe() -> Optional[str]:
    """返回 ffprobe 可执行路径；不存在返回 None。"""
    global _FFPROBE_PATH, _FFPROBE_CHECKED
    if not _FFPROBE_CHECKED:
        _FFPROBE_PATH = shutil.which("ffprobe")
        _FFPROBE_CHECKED = True
        if _FFPROBE_PATH is None:
            logger.warning(
                "未找到 ffprobe，内嵌字幕检测将被跳过。"
                "请在系统 PATH 中提供 ffmpeg/ffprobe（apt install ffmpeg / brew install ffmpeg）。"
            )
        else:
            logger.info(f"ffprobe 路径: {_FFPROBE_PATH}")
    return _FFPROBE_PATH


# ISO 639-2/B → ISO 639-2/T 等价（ffprobe 两种都可能输出）+ 兼容 BCP-47 短码
# 把它们统一归一化为内部使用的 lang code
_LANG_NORMALIZE = {
    # 中文（不区分简繁，需要看 title）；cmn = Mandarin Chinese (ISO 639-3)
    'chi': 'zh', 'zho': 'zh', 'zh': 'zh', 'cmn': 'zh',
    # 粤语（ISO 639-3: yue）；zh-yue / zh-hk 在音轨场景下都按粤语处理
    'yue': 'yue', 'zh-yue': 'yue', 'zh-hk': 'yue',
    # 英文
    'eng': 'en', 'en': 'en',
    # 日文
    'jpn': 'ja', 'jp': 'ja', 'ja': 'ja',
    # 韩文
    'kor': 'ko', 'ko': 'ko',
}


def _looks_like_cantonese(title: Optional[str]) -> bool:
    """音轨 title 含粤语关键字 → 视为粤语轨。"""
    if not title:
        return False
    t = title.lower()
    return any(k in t for k in ('粤', '粵', '广东话', '廣東話', 'cantonese', 'yue', '港语', '港語'))


def _classify_chinese_variant(title: str) -> str:
    """
    根据轨道 title 判断中文是简体还是繁体。
    title 里常见标识：简体/简中/CHS/Simplified、繁体/繁中/CHT/Traditional
    没标识默认按 chs 返回。
    """
    if not title:
        return 'chs'
    t = title.lower()
    if any(k in t for k in ('繁', 'cht', 'traditional', 'trad', '正體', '正体')):
        return 'cht'
    if any(k in t for k in ('简', '簡', 'chs', 'simplified', 'simp')):
        return 'chs'
    return 'chs'  # 默认简体


def normalize_lang(language: Optional[str], title: Optional[str] = None) -> Optional[str]:
    """
    把 ffprobe 报告的 language + title 归一化到内部 lang code。
    返回值与 preferred_langs / preferred_audio_langs 配置中使用的代码一致：
      - chs / cht：简/繁中
      - yue：粤语
      - eng：英文
      - jpn：日文
      - kor：韩文
    无法识别的返回 None。
    """
    if not language:
        # 无 language 标签：尝试从 title 反推（少见但偶尔发生）
        if title:
            if _looks_like_cantonese(title):
                return 'yue'
            t = title.lower()
            if any(k in t for k in ('chinese', 'chi', '中文', '中')):
                return _classify_chinese_variant(title)
            if 'english' in t or 'eng' in t:
                return 'eng'
            if 'japanese' in t or '日' in t or 'jpn' in t:
                return 'jpn'
            if 'korean' in t or '韩' in t or 'kor' in t:
                return 'kor'
        return None

    base = _LANG_NORMALIZE.get(language.lower())
    if base is None:
        return None
    if base == 'yue':
        return 'yue'
    if base == 'zh':
        # language 标签是 chi/zho/zh/cmn —— 优先看 title 是否标记粤语，否则按简繁判定
        if _looks_like_cantonese(title):
            return 'yue'
        return _classify_chinese_variant(title or '')
    if base == 'en':
        return 'eng'
    if base == 'ja':
        return 'jpn'
    if base == 'ko':
        return 'kor'
    return None


def probe_embedded_subtitles(video: Path, timeout: float = 10.0) -> List[Dict]:
    """
    调用 ffprobe 获取视频的内嵌字幕轨。

    Returns:
        [{"index": 2, "language": "chi", "title": "简体", "lang_code": "chs"}, ...]
        ffprobe 不可用 / 视频无内嵌字幕时返回 []。
    """
    ffprobe = _get_ffprobe()
    if not ffprobe:
        return []

    if not video.exists():
        return []

    cmd = [
        ffprobe,
        "-v", "error",
        "-select_streams", "s",  # 只看字幕流
        "-show_entries", "stream=index:stream_tags=language,title",
        "-of", "json",
        str(video),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding='utf-8',
            errors='replace',
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"ffprobe 超时: {video}")
        return []
    except OSError as e:
        logger.warning(f"ffprobe 调用失败: {video} - {e}")
        return []

    if proc.returncode != 0:
        # mp4 / avi 等可能没字幕流，ffprobe 报错也属正常 → DEBUG 级别即可
        logger.debug(f"ffprobe rc={proc.returncode}: {video} - {proc.stderr.strip()[:200]}")
        return []

    try:
        data = json.loads(proc.stdout) if proc.stdout else {}
    except json.JSONDecodeError as e:
        logger.warning(f"ffprobe 输出非 JSON: {video} - {e}")
        return []

    tracks: List[Dict] = []
    for s in (data.get('streams') or []):
        tags = s.get('tags') or {}
        language = tags.get('language')  # ISO 639-2，常见 chi/zho/eng/jpn/und
        title = tags.get('title')
        lang_code = normalize_lang(language, title)
        tracks.append({
            'index': s.get('index'),
            'language': language,
            'title': title,
            'lang_code': lang_code,  # 归一化后的内部代码，识别不出为 None
        })
    return tracks


def get_embedded_lang_codes(video: Path, timeout: float = 10.0) -> List[str]:
    """便捷方法：直接返回归一化后的语言代码列表（去重去 None）。"""
    tracks = probe_embedded_subtitles(video, timeout=timeout)
    seen = []
    for t in tracks:
        code = t.get('lang_code')
        if code and code not in seen:
            seen.append(code)
    return seen
