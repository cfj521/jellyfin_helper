"""
字幕重命名器
把目录下的字幕重命名为与视频同名（带语言后缀）：
    movie.mkv  +  sub_chs.srt          → movie.chs.srt
    movie.mkv  +  movie.srt（裸名）    → movie.{detected}.srt
                                          ↑ 用前 5 条 cue 内容识别语言
.sup（蓝光图形字幕）无法读字符；裸名 .sup 没法识别 → 仅在文件名带语言后缀时才会被改名
"""
import logging
import re
from pathlib import Path
from typing import List, Tuple, Optional, Dict

from .scanner import VIDEO_EXTS, SUB_EXTS, LANG_CODES, episodes_match
from .lang_detect import detect_subtitle_language

logger = logging.getLogger(__name__)


class SubtitleRenamer:
    """字幕重命名器"""

    def extract_episode(self, filename: str) -> Optional[Tuple[Optional[int], int]]:
        """
        提取剧集编号，返回 (season, episode) 元组：
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

    def extract_lang_code(self, filename: str) -> Optional[str]:
        """
        从文件名提取语言代码（归一化为内部代码，支持双语 / 中文关键字）。

        返回 dot-separated 复合 code 或 None：
          'subtitle.chs.srt'         → 'chs'
          'subtitle.chs.eng.srt'     → 'chs.eng'
          'subtitle.chs&eng.ass'     → 'chs.eng'
          'subtitle.简体&英文.srt'    → 'chs.eng'
          'subtitle.简英.srt'         → 'chs.eng'
          'subtitle.简体.srt'        → 'chs'
          'movie.srt'                → None（裸名 → 上层走内容识别）

        识别规则与 assrt / shooter / scanner 共用同一套（common.lang_utils）。
        """
        from common.lang_utils import detect_lang_combo
        return detect_lang_combo(filename)

    def resolve_lang(self, subtitle: Path, force_lang: Optional[str] = None) -> Optional[str]:
        """
        按优先级确定字幕的语言后缀：
          1. force_lang（用户强制指定）
          2. 文件名里已有语言 token
          3. 内容检测（读前 5 条 cue 第一行）—— 仅对文本字幕生效
        都失败返回 None（调用方决定保留无后缀还是跳过）。
        """
        if force_lang:
            return force_lang
        from_name = self.extract_lang_code(subtitle.name)
        if from_name:
            return from_name
        detected = detect_subtitle_language(subtitle)
        if detected:
            logger.info(f"内容识别字幕语言: {subtitle.name} → {detected}")
        return detected

    def get_files(self, directory: Path) -> Tuple[List[Path], List[Path]]:
        """
        获取目录下的视频和字幕文件。

        is_file() 校验：有些用户把目录命名为 `Movie.2024.mkv` 形式（带视频扩展名），
        不加 is_file 会把目录误识别为视频文件。
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

    def rename_single(self, video: Path, subtitle: Path, lang: str = None, dry_run: bool = True) -> Optional[Tuple[Path, Path]]:
        """单文件重命名（电影模式）"""
        video_stem = video.stem
        sub_ext = subtitle.suffix.lower()

        final_lang = self.resolve_lang(subtitle, force_lang=lang)

        if final_lang:
            new_name = f"{video_stem}.{final_lang}{sub_ext}"
        else:
            new_name = f"{video_stem}{sub_ext}"

        new_path = subtitle.parent / new_name

        if subtitle == new_path:
            return None

        if not dry_run:
            subtitle.rename(new_path)

        return (subtitle, new_path)

    def rename_by_episode(self, videos: List[Path], subtitles: List[Path], lang: str = None, dry_run: bool = True) -> List[Tuple[Path, Path]]:
        """按剧集编号匹配重命名（电视剧模式）"""
        results = []

        # 预提取所有视频的 episode 元组
        video_episodes = [(v, self.extract_episode(v.name)) for v in videos]
        video_episodes = [(v, ep) for v, ep in video_episodes if ep is not None]

        for sub in subtitles:
            sub_ep = self.extract_episode(sub.name)
            if sub_ep is None:
                continue

            # 用 episodes_match 做兼容匹配，找第一个命中的视频
            video = next((v for v, ep in video_episodes if episodes_match(ep, sub_ep)), None)
            if video is None:
                continue

            video_stem = video.stem
            sub_ext = sub.suffix.lower()

            final_lang = self.resolve_lang(sub, force_lang=lang)

            if final_lang:
                new_name = f"{video_stem}.{final_lang}{sub_ext}"
            else:
                new_name = f"{video_stem}{sub_ext}"

            new_path = sub.parent / new_name

            if sub != new_path:
                if not dry_run:
                    if new_path.exists():
                        continue
                    sub.rename(new_path)
                results.append((sub, new_path))

        return results

    def process_directory(self, directory: Path, lang: str = None, dry_run: bool = True,
                          recursive: bool = True, verbose: bool = False, root_dir: Path = None,
                          on_each=None) -> List[Dict]:
        """
        处理目录，返回所有重命名结果。

        返回值：每个 dict 包含
          - type: "movie" | "tv"
          - directory: str (相对显示路径)
          - old_name / new_name: str
          - old_path / new_path: str (绝对路径)
          - dry_run: bool
          - success: bool (dry_run=True 时也算 success，仅表示规则匹配成功)
          - error: Optional[str]

        on_each(result_dict)：每发现一个 rename 对就回调一次，便于 caller 实时累计 + emit 进度。
            cb 内异常会被吞掉。
        """
        results: List[Dict] = []

        def _record(item: Dict):
            results.append(item)
            if on_each is not None:
                try:
                    on_each(item)
                except Exception:
                    pass

        if root_dir is None:
            root_dir = directory

        videos, subtitles = self.get_files(directory)

        try:
            rel_path = directory.relative_to(root_dir)
            display_path = str(rel_path) if str(rel_path) != '.' else directory.name
        except ValueError:
            display_path = str(directory)

        if not (videos and subtitles):
            if verbose and (videos or subtitles):
                print(f"[跳过] {display_path} (视频:{len(videos)}, 字幕:{len(subtitles)})")
        else:
            if verbose:
                print(f"[扫描] {display_path} (视频:{len(videos)}, 字幕:{len(subtitles)})")

        if videos and subtitles:
            if len(videos) == 1 and len(subtitles) == 1:
                result = self.rename_single(videos[0], subtitles[0], lang, dry_run)
                if result:
                    old, new = result
                    print(f"[电影] {display_path}")
                    print(f"  {old.name}")
                    print(f"  → {new.name}")
                    _record({
                        "type": "movie",
                        "directory": display_path,
                        "old_name": old.name,
                        "new_name": new.name,
                        "old_path": str(old),
                        "new_path": str(new),
                        "dry_run": dry_run,
                        "success": True,
                        "error": None,
                    })
            elif len(videos) > 1:
                pairs = self.rename_by_episode(videos, subtitles, lang, dry_run)
                if pairs:
                    print(f"[电视剧] {display_path} ({len(pairs)} 个字幕)")
                    for old, new in pairs:
                        print(f"  {old.name}")
                        print(f"  → {new.name}")
                        _record({
                            "type": "tv",
                            "directory": display_path,
                            "old_name": old.name,
                            "new_name": new.name,
                            "old_path": str(old),
                            "new_path": str(new),
                            "dry_run": dry_run,
                            "success": True,
                            "error": None,
                        })

        if recursive:
            try:
                all_items = set(directory.iterdir())
                known_files = set(videos + subtitles)
                potential_dirs = sorted(all_items - known_files)

                for item in potential_dirs:
                    try:
                        results.extend(
                            self.process_directory(
                                item, lang, dry_run, recursive, verbose, root_dir,
                                on_each=on_each,
                            )
                        )
                    except (NotADirectoryError, PermissionError, OSError):
                        pass
            except (PermissionError, OSError):
                pass

        return results
