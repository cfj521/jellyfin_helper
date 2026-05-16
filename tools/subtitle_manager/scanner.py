"""
字幕扫描器
扫描视频目录，分析字幕匹配情况

判定"视频是否缺某语言字幕"按以下优先级：
  1. 视频容器内嵌字幕轨命中（ffprobe 探测，主要针对 .mkv）
  2. 同目录下的外挂字幕命中：
     a. 文件名带语言后缀（movie.zh.srt）→ 直接读后缀
     b. 文件名不带语言后缀（movie.srt）→ 读前 N 条 cue 内容判语言
  3. 都没命中 → 缺失
"""
import re
import json
import logging
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set
from dataclasses import dataclass, field
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from .embedded_probe import get_embedded_lang_codes
from .lang_detect import detect_subtitle_language

logger = logging.getLogger(__name__)

# 视频扩展名
VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.wmv', '.flv', '.mov', '.m4v', '.ts', '.rmvb'}
# 字幕扩展名
SUB_EXTS = {'.ass', '.srt', '.sup', '.ssa', '.sub'}
# 文件名里可能出现的语言 token —— 旧字符串保留兼容；新代码内部统一归一化
LANG_CODES = [
    'chs.eng', 'cht.eng', 'zho.eng', 'chi.eng',
    'zh-hans', 'zh-hant', 'zh-cn', 'zh-tw',
    'chs', 'cht', 'chi', 'zho', 'zh',
    'eng', 'en',
    'jpn', 'jp', 'ja',
    'kor', 'ko',
    'default', 'forced',
    '简体', '繁体', '简中', '繁中', '中文', '英文', '日文',
]

# 内置归一化映射：各种文件名 / 配置写法 → 内部标准代码
# 注意：zh-hk 在【字幕】场景里通常表示繁体（HK 书面用繁体），保持归到 cht；
# 在【音轨】场景下 zh-hk 一般指粤语，由 embedded_probe.normalize_lang 单独处理。
_LANG_NORMALIZE_MAP = {
    # 简中
    'chs': 'chs', 'zh-cn': 'chs', 'zh-hans': 'chs', 'zhs': 'chs',
    'sc': 'chs', 'simplified': 'chs', '简体': 'chs', '简中': 'chs',
    # 繁中
    'cht': 'cht', 'zh-tw': 'cht', 'zh-hant': 'cht', 'zh-hk': 'cht',
    'tc': 'cht', 'traditional': 'cht', '繁体': 'cht', '繁中': 'cht', '正体': 'cht',
    # 中文（不区分简繁）→ 默认归为简体
    'chi': 'chs', 'zho': 'chs', 'zh': 'chs', 'chinese': 'chs', '中文': 'chs',
    # 粤语（ISO 639-3: yue）
    'yue': 'yue', 'cantonese': 'yue', 'yueh': 'yue',
    '粤语': 'yue', '粵語': 'yue', '广东话': 'yue', '廣東話': 'yue',
    # 英文
    'eng': 'eng', 'en': 'eng', 'english': 'eng', '英文': 'eng', '英语': 'eng',
    # 日文
    'jpn': 'jpn', 'jp': 'jpn', 'ja': 'jpn', 'japanese': 'jpn', '日文': 'jpn', '日语': 'jpn',
    # 韩文
    'kor': 'kor', 'ko': 'kor', 'korean': 'kor', '韩文': 'kor', '韩语': 'kor',
}


def normalize_lang_code(token: Optional[str]) -> Optional[str]:
    """单个语言 token → 归一化标准代码（chs/cht/yue/eng/jpn/kor），无法识别返回 None。"""
    if not token:
        return None
    return _LANG_NORMALIZE_MAP.get(token.lower())


def normalize_lang_list(tokens: List[str]) -> List[str]:
    """把一组 token 归一化、去重，按原顺序保留。"""
    out = []
    for t in tokens:
        code = normalize_lang_code(t)
        if code and code not in out:
            out.append(code)
    return out


def episodes_match(a, b) -> bool:
    """
    判断两个 extract_episode() 的返回值是否指向同一集。

    规则：
      - 任一为 None：不匹配
      - 集号必须相同
      - 双方都标了季：季也必须相同
      - 一方未标季：仅比集号即可（容忍只写 Ep02 的字幕）
    """
    if a is None or b is None:
        return False
    sa, ea = a
    sb, eb = b
    if ea != eb:
        return False
    if sa is None or sb is None:
        return True
    return sa == sb


def format_episode(ep: Optional[Tuple[Optional[int], int]]) -> Optional[str]:
    """把 (season, episode) 元组格式化成 S01E02 / E02 字符串，None 返回 None。"""
    if ep is None:
        return None
    season, episode = ep
    if season is None:
        return f"E{episode:02d}"
    return f"S{season:02d}E{episode:02d}"


@dataclass
class VideoInfo:
    """视频信息"""
    path: Path
    name: str
    episode: Optional[Tuple[Optional[int], int]] = None  # (season, episode)
    subtitles: List[Dict] = field(default_factory=list)
    embedded_langs: List[str] = field(default_factory=list)  # 内嵌字幕轨语言（归一化代码）
    missing_langs: List[str] = field(default_factory=list)

    def to_dict(self):
        return {
            'path': str(self.path),
            'name': self.name,
            'episode': format_episode(self.episode),
            'subtitles': self.subtitles,
            'embedded_langs': self.embedded_langs,
            'missing_langs': self.missing_langs,
            'has_subtitle': len(self.subtitles) > 0 or len(self.embedded_langs) > 0,
        }


@dataclass
class DirectoryInfo:
    """目录信息"""
    path: Path
    name: str
    media_type: str  # 'movie' or 'tv'
    videos: List[VideoInfo] = field(default_factory=list)
    total_videos: int = 0
    # 完全没任何字幕（既没外挂也没内嵌）的视频数
    videos_with_sub: int = 0
    videos_without_sub: int = 0
    # 缺所需语言（required_langs 任一）的视频数 —— 即使有别的语言字幕也算"缺"
    # 这是用户真正关心的"缺字幕"指标
    videos_without_required: int = 0

    def to_dict(self):
        return {
            'path': str(self.path),
            'name': self.name,
            'media_type': self.media_type,
            'videos': [v.to_dict() for v in self.videos],
            'total_videos': self.total_videos,
            'videos_with_sub': self.videos_with_sub,
            'videos_without_sub': self.videos_without_sub,
            'videos_without_required': self.videos_without_required,
        }


@dataclass
class ScanResult:
    """扫描结果"""
    scan_time: str
    root_path: str
    directories: List[DirectoryInfo] = field(default_factory=list)
    total_videos: int = 0
    total_with_sub: int = 0
    total_without_sub: int = 0
    total_without_required: int = 0  # 缺所需语言的视频数
    total_subtitles: int = 0

    def to_dict(self):
        return {
            'scan_time': self.scan_time,
            'root_path': self.root_path,
            'directories': [d.to_dict() for d in self.directories],
            'total_videos': self.total_videos,
            'total_with_sub': self.total_with_sub,
            'total_without_sub': self.total_without_sub,
            'total_without_required': self.total_without_required,
            'total_subtitles': self.total_subtitles,
        }


class SubtitleScanner:
    """字幕扫描器"""

    # 哪些视频容器值得探测内嵌字幕；mkv 最常见，mp4 次之
    EMBEDDED_PROBE_EXTS = {'.mkv', '.mp4', '.m4v', '.mov'}

    def __init__(
        self,
        required_langs: Optional[List[str]] = None,
        probe_embedded: bool = True,
        probe_concurrency: int = 5,
    ):
        # required_langs：缺字幕判定的依据（原子单语言）。配置写 'chs.eng' 这种复合也容错，
        # 拆成两个独立 atomic 语言（语义：要 chs AND eng，缺哪个都缺）
        raw_langs: List[str] = required_langs or ['chs', 'eng']
        normalized: List[str] = []
        for item in raw_langs:
            for token in item.split('.'):
                code = normalize_lang_code(token)
                if code and code not in normalized:
                    normalized.append(code)
        self.required_langs: List[str] = normalized or ['chs', 'eng']

        self.probe_embedded = probe_embedded
        self.probe_concurrency = max(1, probe_concurrency)

    def extract_episode(self, filename: str) -> Optional[Tuple[Optional[int], int]]:
        """
        提取剧集编号，返回 (season, episode)：
          - 同时有 SxxExx：(season_int, episode_int)
          - 只有集号（如 ep02、第02话）：(None, episode_int)
          - 没匹配到：None
        """
        patterns_with_season = [
            r'[Ss](\d{1,2})[Ee](\d{1,3})',
            r'[Ss](\d{1,2})\.?[Ee](\d{1,3})',
            r'(\d{1,2})[xX](\d{1,3})',
        ]
        for pattern in patterns_with_season:
            match = re.search(pattern, filename)
            if match:
                return (int(match.group(1)), int(match.group(2)))

        patterns_episode_only = [
            r'[Ee][Pp]?(\d{1,3})',
            r'第(\d{1,3})[集话]',
        ]
        for pattern in patterns_episode_only:
            match = re.search(pattern, filename)
            if match:
                return (None, int(match.group(1)))
        return None

    def extract_lang_codes_from_name(self, filename: str) -> List[str]:
        """
        从字幕文件名里提取所有可识别的语言代码（归一化）。
        支持：
          - 英文 token：'.chs.srt' / '.chs.eng.srt' / '.zh-CN.srt'
          - 中文关键字：'简体.srt' / '简体&英文.srt' / '简&英.srt' / '繁体.ass'
          - 复合写法：'chs&eng' / 'chs_eng' / 'chs-eng'
        识别不到任何 token 返回 []（调用方应进一步内容检测）。

        实现：复用 common.lang_utils.detect_lang_combo，与 assrt / shooter 等
        多个源共享同一套规则，保证识别一致。
        """
        from common.lang_utils import detect_lang_combo
        combo = detect_lang_combo(filename)
        if not combo:
            return []
        # combo 可能是 'chs' 或 'chs.eng' 等；split 成单语 token list
        return [code for code in combo.split('.') if code]

    def get_files(self, directory: Path) -> Tuple[List[Path], List[Path]]:
        """
        获取目录下的视频和字幕文件。

        必须 is_file() 校验：有些用户把目录命名成 `Movie.2024.mkv` 这种带视频后缀的形式
        （Jellyfin 的 movie folder name 推荐风格），不加 is_file 过滤会把目录当成视频文件，
        导致真正的 mkv 不会被扫到。
        """
        videos = []
        subtitles = []
        try:
            for f in directory.iterdir():
                if not f.is_file():
                    continue
                ext = f.suffix.lower()
                if ext in VIDEO_EXTS:
                    videos.append(f)
                elif ext in SUB_EXTS:
                    subtitles.append(f)
        except (PermissionError, OSError):
            pass
        return sorted(videos), sorted(subtitles)

    def _resolve_subtitle_langs(self, sub_path: Path) -> List[str]:
        """
        给一个字幕文件，确定它包含哪些语言（归一化代码列表）：
          1) 文件名能提取到 → 用文件名后缀
          2) 文件名提取不到（裸名 movie.srt）→ 读前 5 条 cue 内容识别
          3) 仍识别不到 → 返回 []
        """
        from_name = self.extract_lang_codes_from_name(sub_path.name)
        if from_name:
            return from_name
        detected = detect_subtitle_language(sub_path)
        return [detected] if detected else []

    def match_subtitles(self, video: Path, subtitles: List[Path]) -> List[Dict]:
        """
        匹配视频对应的字幕。

        匹配策略（沿用旧版）：
          - 文件名包含或被包含
          - 剧集号匹配（兼容 Ep02 / 第02话）
        额外为每条字幕解析它包含的语言（归一化代码列表，可能为空）。
        """
        matched = []
        video_stem = video.stem.lower()
        video_ep = self.extract_episode(video.name)

        for sub in subtitles:
            sub_stem = sub.stem.lower()
            sub_ep = self.extract_episode(sub.name)

            is_match = False
            if video_stem in sub_stem or sub_stem in video_stem:
                is_match = True
            elif episodes_match(video_ep, sub_ep):
                is_match = True

            if is_match:
                langs = self._resolve_subtitle_langs(sub)
                matched.append({
                    'path': str(sub),
                    'name': sub.name,
                    'lang': langs[0] if langs else None,  # 向后兼容：单语言代码
                    'langs': langs,                        # 新字段：完整语言列表（归一化）
                    'ext': sub.suffix.lower(),
                })

        return matched

    def check_missing_langs(self, video_info: 'VideoInfo') -> List[str]:
        """
        检查 required_langs 中哪些语言在该视频上找不到。
        覆盖来源：内嵌字幕轨 + 外挂字幕的 langs 集合。

        zh 覆盖规则：通用中文 'zh'（未指定简繁的字幕，如 .chinese.srt / 内容嗅探不出简繁的）
        同时算覆盖 chs 和 cht 两个需求，避免让用户既缺简又缺繁。
        """
        from common.lang_utils import expand_zh_coverage
        covered: Set[str] = set(video_info.embedded_langs or [])
        for s in video_info.subtitles:
            for lc in s.get('langs') or []:
                covered.add(lc)
        covered = expand_zh_coverage(covered)

        missing = [lc for lc in self.required_langs if lc not in covered]
        return missing

    def _probe_embedded_for_videos(self, videos: List[Path]) -> Dict[Path, List[str]]:
        """
        并发为一批视频探测内嵌字幕语言。
        只对 EMBEDDED_PROBE_EXTS 中的扩展名探测（mkv/mp4/m4v/mov），其它类型跳过。
        """
        if not self.probe_embedded:
            return {}

        targets = [v for v in videos if v.suffix.lower() in self.EMBEDDED_PROBE_EXTS]
        if not targets:
            return {}

        result: Dict[Path, List[str]] = {}

        def _worker(v: Path):
            try:
                return v, get_embedded_lang_codes(v)
            except Exception as e:
                logger.warning(f"内嵌字幕探测失败: {v} - {e}")
                return v, []

        max_workers = min(self.probe_concurrency, len(targets))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for v, langs in ex.map(_worker, targets):
                result[v] = langs

        return result

    def scan_directory(self, directory: Path, root_path: Optional[Path] = None) -> Optional[DirectoryInfo]:
        """扫描单个目录"""
        videos, subtitles = self.get_files(directory)

        if not videos:
            return None

        media_type = 'movie' if len(videos) == 1 else 'tv'

        # 计算显示名：directory 就是 root 时 relative_to 会返回 '.'，回退到目录基名
        # 触发场景：toolbar 选中具体条目（item_paths 模式）时，root_path == directory
        display_name: str
        if root_path:
            try:
                rel = str(directory.relative_to(root_path))
                display_name = rel if rel != '.' else (directory.name or str(directory))
            except ValueError:
                display_name = directory.name or str(directory)
        else:
            display_name = directory.name or str(directory)

        dir_info = DirectoryInfo(
            path=directory,
            name=display_name,
            media_type=media_type,
        )

        # 一次性并发探测目录内所有视频的内嵌字幕
        embedded_map = self._probe_embedded_for_videos(videos)

        for video in videos:
            matched_subs = self.match_subtitles(video, subtitles)
            video_info = VideoInfo(
                path=video,
                name=video.name,
                episode=self.extract_episode(video.name),
                subtitles=matched_subs,
                embedded_langs=embedded_map.get(video, []),
            )
            video_info.missing_langs = self.check_missing_langs(video_info)
            dir_info.videos.append(video_info)

            dir_info.total_videos += 1
            # "有字幕" = 有外挂字幕命中 或 有内嵌字幕轨（语义不区分语言）
            if matched_subs or video_info.embedded_langs:
                dir_info.videos_with_sub += 1
            else:
                dir_info.videos_without_sub += 1
            # "缺所需语言" = required_langs 里任一语言找不到（即使有别的语言字幕也算）
            # 这是用户真正关心的"缺字幕"指标
            if video_info.missing_langs:
                dir_info.videos_without_required += 1

        return dir_info

    def scan(
        self,
        root_path: Path,
        recursive: bool = True,
        progress_cb=None,
    ) -> ScanResult:
        """
        扫描目录。

        progress_cb（可选）：每发现一个含视频的目录后调用一次，签名 (result, dirs_done)。
            用于让长任务实时推送当前累积的扫描数据到任务管理 DB。
            cb 内异常会被吞掉，不影响扫描本身。
        """
        result = ScanResult(
            scan_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            root_path=str(root_path),
        )
        dirs_done_counter = [0]  # mutable closure container

        def process_dir(directory: Path):
            dir_info = self.scan_directory(directory, root_path)
            if dir_info and dir_info.total_videos > 0:
                result.directories.append(dir_info)
                result.total_videos += dir_info.total_videos
                result.total_with_sub += dir_info.videos_with_sub
                result.total_without_sub += dir_info.videos_without_sub
                result.total_without_required += dir_info.videos_without_required
                for v in dir_info.videos:
                    result.total_subtitles += len(v.subtitles)
                dirs_done_counter[0] += 1
                if progress_cb is not None:
                    try:
                        progress_cb(result, dirs_done_counter[0])
                    except Exception:
                        pass  # 进度回调失败不应影响扫描

            if recursive:
                try:
                    videos, subtitles = self.get_files(directory)
                    all_items = set(directory.iterdir())
                    known_files = set(videos + subtitles)
                    for item in sorted(all_items - known_files):
                        try:
                            process_dir(item)
                        except (NotADirectoryError, PermissionError, OSError):
                            pass
                except (PermissionError, OSError):
                    pass

        process_dir(root_path)
        return result

    def save_json(self, result: ScanResult, output_path: Path):
        """保存为JSON"""
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
