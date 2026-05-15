"""
TorrentOrganizer：把种子包内的文件结构化复制到目标库目录。

输入：源目录 + 目标根 + 媒体类型 + 元数据
输出：dispatched_files（实际复制到目标位置的文件路径列表）

行为：
- 主视频文件（.mkv/.mp4 等）→ 目标目录主文件
- 字幕文件（.srt/.ass/.sup 等）→ 跟视频同目录（同 stem 的优先关联）
- 垃圾文件（sample/nfo/RARBG.txt/proof）→ 移入 trash_dir（保留 N 天后清）
- 多视频包（剧集季 pack）→ 按 SxxExx 拆到 Season XX/

跨盘只能复制（保种）。整理完成后源文件不动（qB 继续做种）；
后续 QuotaManager 在分享率/天数到达后再清 NVMe 副本。
"""
from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from tools.dispatch.copier import copy_file_with_progress

# duplicate resolver 注入点：organizer 不直接依赖 DB，由调用方传入 callable。
# 签名: (src: Path, dst: Path) -> 'proceed' | 'skip'；抛 DuplicateConflictError 让上层接

logger = logging.getLogger(__name__)


VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm', '.ts', '.m2ts', '.mpg', '.mpeg'}
SUB_EXTS = {'.srt', '.ass', '.ssa', '.sup', '.sub', '.idx', '.vtt'}
JUNK_PATTERNS = [
    re.compile(r'sample', re.I),
    re.compile(r'\.nfo$', re.I),
    re.compile(r'rarbg\.(txt|com\.txt)$', re.I),
    re.compile(r'proof', re.I),
    re.compile(r'screens?', re.I),
    re.compile(r'^.*?\.exe$', re.I),
    re.compile(r'\.(jpg|jpeg|png|gif|webp)$', re.I),  # 海报通常无用，jellyfin 自己刮
]
EPISODE_RE = re.compile(r'[Ss](\d{1,2})[.\-_ ]?[Ee](\d{1,3})')


# 内置文件名模板兜底：用户在 yaml/UI 没配 rule.file_template 时使用
# 正常路径下 rule.file_template 来自 DispatchRule.file_template
_DEFAULT_FILE_TEMPLATES = {
    'movie':       '{title} ({year})',
    'tv':          '({series_name})S{season:02d}E{episode:02d}',
    'anime':       '({anime_name}){episode:03d}',
    'adult':       '{code}({title})',
}


def _is_junk(path: Path) -> bool:
    """sample/nfo/RARBG/proof/截图等丢 trash。"""
    s = str(path).lower()
    name = path.name.lower()
    for pat in JUNK_PATTERNS:
        if pat.search(s) or pat.search(name):
            return True
    return False


def _classify_files(src_root: Path) -> Tuple[List[Path], List[Path], List[Path]]:
    """递归分类：videos / subtitles / junk。"""
    videos, subs, junk = [], [], []
    # 防御：拒绝把盘符根 / Linux 根目录当种子包扫——配错路径映射时可能解析成 X:\ 这种危险输入，
    # 一旦放行就会递归整个盘，撞上 qB 锁定的日志文件等导致 PermissionError 失败循环
    resolved = src_root.resolve()
    if resolved == Path(resolved.anchor) or str(resolved) in ('/', ''):
        raise RuntimeError(
            f"src_root 解析成盘符/系统根目录: {src_root!r} (resolved={resolved!r}); "
            f"请检查 path_mappings 配置或 qB content_path 是否异常"
        )
    if src_root.is_file():
        items = [src_root]
    else:
        items = [p for p in src_root.rglob('*') if p.is_file()]
    for f in items:
        if _is_junk(f):
            junk.append(f)
            continue
        ext = f.suffix.lower()
        if ext in VIDEO_EXTS:
            videos.append(f)
        elif ext in SUB_EXTS:
            subs.append(f)
        # 其他文件忽略（封面/cue 等）
    return videos, subs, junk


def _extract_episode(name: str) -> Optional[Tuple[int, int]]:
    m = EPISODE_RE.search(name)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


def _format_template(template: str, **kwargs) -> str:
    """渲染模板：空占位符智能去括号 + 文件名级别 sanitize。

    委托给 tools.dispatch.template_render —— 跟 dispatch.py 的 location_template 渲染共用同一套规则。
    """
    from tools.dispatch.template_render import render_template, sanitize_path_segment
    rendered = render_template(template, kwargs)
    # file stem 是单段路径名，需要 sanitize（非法字符、保留名、长度等）
    return sanitize_path_segment(rendered)


class TorrentOrganizer:
    """整理种子包内文件到目标位置。"""

    def __init__(
        self,
        trash_dir: Optional[Path] = None,
    ):
        self.trash_dir = Path(trash_dir) if trash_dir else None

    def organize(
        self,
        src_root: Path,
        target_dir: Path,
        media_type: str,
        metadata: Dict,
        rule: Dict,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
        duplicate_resolver: Optional[Callable[[Path, Path], str]] = None,
    ) -> Dict:
        """
        把 src_root 整理复制到 target_dir。

        rule: 配置中该 media_type 的 DispatchRule（含 file_template 等）
        metadata: {title, year, series_name, season, episode, code, anime_name, ...}
        duplicate_resolver: 渲染 dst 后调一次，返回 'proceed'/'skip'，
                            抛 DuplicateConflictError → organize 也透传给调用方

        返回: {
            'dispatched_files': [str(absolute target paths)],
            'src_files': [...],
            'junk_count': N,
            'bytes_copied': total,
            'skipped_files': [(src, dst, reason)],   # 因 duplicate policy 跳过的
        }
        """
        src_root = Path(src_root)
        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        videos, subs, junk = _classify_files(src_root)
        logger.info(
            f"organize {src_root.name} → {target_dir} "
            f"(videos={len(videos)}, subs={len(subs)}, junk={len(junk)})"
        )

        # 1. 处理 junk：移到 trash_dir（保留同结构，方便审计 / 误删恢复）
        for j in junk:
            self._move_to_trash(src_root, j)

        # 2. 整理视频 + 字幕
        dispatched = []
        bytes_copied = 0
        bytes_total = sum(v.stat().st_size for v in videos if v.exists())

        # rule.file_template 缺失时用内置默认兜底（按 media_type）
        rule_with_tpl = dict(rule or {})
        if not rule_with_tpl.get('file_template'):
            rule_with_tpl['file_template'] = _DEFAULT_FILE_TEMPLATES.get(media_type, '')

        skipped_files: List[Tuple[str, str, str]] = []

        if media_type in ('tv', 'anime') and len(videos) > 1:
            # 整季 pack：每个 video 按 SxxExx 拆到对应 Season 目录
            for v in videos:
                ep = _extract_episode(v.name)
                if ep:
                    s, e = ep
                    dst = self._compose_episode_path(
                        target_dir, v, rule_with_tpl, metadata, season=s, episode=e,
                    )
                    logger.info(f"episode {v.name} → S{s:02d}E{e:02d} → {dst}")
                else:
                    # 提不出集号 → 直接复制到 target_dir（兜底）
                    dst = target_dir / v.name
                    logger.warning(
                        f"video {v.name} 提不出 SxxExx，落到顶层 {dst}（建议手动重命名后再扫库）"
                    )

                # duplicate-policy 决策点：proceed / skip / raise DuplicateConflictError
                if duplicate_resolver is not None:
                    action = duplicate_resolver(v, dst)
                    if action == 'skip':
                        skipped_files.append((str(v), str(dst), 'duplicate_policy'))
                        logger.info(f"organize: skip {v.name} (duplicate_policy)")
                        continue

                bytes_copied += copy_file_with_progress(
                    v, dst,
                    progress_cb=lambda d, t, base=bytes_copied, name=v.name: (
                        progress_cb(base + d, bytes_total, name) if progress_cb else None
                    ),
                    on_displace=self._displace_to_trash,
                )
                dispatched.append(str(dst))
                # 字幕跟随
                self._dispatch_subs_for_video(v, dst, subs, dispatched)
        else:
            # 单文件场景（电影 / 单集 / 番号）
            for v in videos:
                dst = self._compose_movie_path(target_dir, v, rule_with_tpl, metadata)

                if duplicate_resolver is not None:
                    action = duplicate_resolver(v, dst)
                    if action == 'skip':
                        skipped_files.append((str(v), str(dst), 'duplicate_policy'))
                        logger.info(f"organize: skip {v.name} (duplicate_policy)")
                        continue

                bytes_copied += copy_file_with_progress(
                    v, dst,
                    progress_cb=lambda d, t, base=bytes_copied, name=v.name: (
                        progress_cb(base + d, bytes_total, name) if progress_cb else None
                    ),
                    on_displace=self._displace_to_trash,
                )
                dispatched.append(str(dst))
                self._dispatch_subs_for_video(v, dst, subs, dispatched)

        return {
            'dispatched_files': dispatched,
            'src_files': [str(v) for v in videos],
            'junk_count': len(junk),
            'bytes_copied': bytes_copied,
            'skipped_files': skipped_files,
        }

    # ---- helpers ----

    def _displace_to_trash(self, dst: Path):
        """copy_file_with_progress 的异常分支钩子：把要被覆盖的旧 dst 转入 trash 而不是 unlink。
        路径：<trash_dir>/_displaced/<YYYYMMDD-HHMMSS>/<basename>"""
        if not self.trash_dir:
            logger.warning(f"trash_dir 未配置，displace 退回 unlink {dst}")
            try:
                dst.unlink()
            except Exception as e:
                logger.warning(f"unlink 失败 {dst}: {e}")
            return
        ts = time.strftime('%Y%m%d-%H%M%S')
        sub = self.trash_dir / '_displaced' / ts
        sub.mkdir(parents=True, exist_ok=True)
        target = sub / dst.name
        try:
            shutil.move(str(dst), str(target))
            logger.info(f"displace → trash: {dst} → {target}")
        except Exception as e:
            logger.warning(f"displace 入 trash 失败 {dst} → {target}: {e}（unlink 兜底）")
            try:
                dst.unlink()
            except Exception as e2:
                logger.warning(f"unlink 兜底也失败 {dst}: {e2}")

    def _move_to_trash(self, src_root: Path, junk_file: Path):
        """把 junk_file 移到 trash_dir，保持相对结构便于事后人工恢复。"""
        if not self.trash_dir:
            logger.debug(f"trash_dir 未配置，跳过 junk 处理: {junk_file.name}")
            return
        try:
            rel = junk_file.relative_to(src_root) if src_root.is_dir() else Path(junk_file.name)
        except ValueError:
            rel = Path(junk_file.name)
        # 用源目录名作前缀避免不同 release 的同名 sample.mkv 互盖
        src_name = src_root.name if src_root.is_dir() else src_root.stem
        target = self.trash_dir / src_name / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(junk_file), str(target))
            logger.info(f"junk → trash: {junk_file.name} → {target}")
        except Exception as e:
            logger.warning(f"垃圾文件入 trash 失败 {junk_file}: {e}")

    def _compose_movie_path(
        self,
        target_dir: Path,
        video: Path,
        rule: Dict,
        metadata: Dict,
    ) -> Path:
        """单文件目标路径：<target_dir>/<file_template>.ext"""
        ext = video.suffix
        # file_template 用 metadata 渲染；缺字段时退回视频原文件名
        try:
            stem = _format_template(rule.get('file_template') or video.stem, **metadata)
            if not stem or '{' in stem:
                stem = video.stem
        except Exception:
            stem = video.stem
        return target_dir / f"{stem}{ext}"

    def _compose_episode_path(
        self,
        target_dir: Path,
        video: Path,
        rule: Dict,
        metadata: Dict,
        season: int,
        episode: int,
    ) -> Path:
        """剧集季 pack 内单文件路径：<target_dir>/<file_template>.ext"""
        ext = video.suffix
        kw = dict(metadata, season=season, episode=episode)
        try:
            stem = _format_template(rule.get('file_template') or video.stem, **kw)
            if not stem or '{' in stem:
                stem = video.stem
        except Exception:
            stem = video.stem
        return target_dir / f"{stem}{ext}"

    def _dispatch_subs_for_video(
        self,
        video: Path,
        video_dst: Path,
        all_subs: List[Path],
        dispatched_out: List[str],
    ):
        """跟视频同 stem 的字幕跟着改名到目标位置。"""
        v_stem = video.stem.lower()
        for sub in all_subs:
            sub_stem = sub.stem.lower()
            if v_stem in sub_stem or sub_stem in v_stem or sub_stem.startswith(v_stem):
                # 字幕保留 ext + lang 后缀（比如 .chs.srt）
                # 目标名：<video_dst.stem>.<sub.suffix-chain>
                # 简化处理：保留原字幕在 video.stem 之后的部分
                tail = sub.name[len(video.stem):] if sub.name.lower().startswith(v_stem) else sub.suffix
                new_name = f"{video_dst.stem}{tail}"
                dst = video_dst.parent / new_name
                try:
                    copy_file_with_progress(sub, dst)
                    dispatched_out.append(str(dst))
                except Exception as e:
                    logger.warning(f"字幕复制失败 {sub} → {dst}: {e}")
