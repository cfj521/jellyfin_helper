"""
字幕候选评分系统（Bazarr 风格）。

不同字幕源的"最佳候选"判定逻辑应当统一 —— 否则 OpenSubtitles 用 download_count
排，assrt 用 vote 排，Shooter 用 hash 命中排，跨源比较时无法说"哪个真更好"。

本模块定义 SubtitleCandidate dataclass + score_candidate() 函数，给每个候选
打一个标量分，调用方挑最高分即可。多源混合时也能比较。

参考 Bazarr 的 score breakdown（https://wiki.bazarr.media/Additional-Configuration/Scores/）：
  hash 匹配             +100
  imdbId / tmdbId 匹配  +60
  release group 一致    +50
  resolution 一致       +30
  source（BluRay/WEB-DL/HDTV）一致  +25
  year 匹配             +20
  episode S/E 编号一致  +15
  language 命中         +10  （未命中通常已被过滤掉，给个保底）
  vote / download_count （normalize 0~10）

最终分 = sum(命中条目)，区间通常 0~250+。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# Release tag 提取（从文件名解析）
_RES_PATTERN = re.compile(r'\b(2160p|1080p|720p|480p|4k|uhd)\b', re.I)
_SOURCE_PATTERN = re.compile(
    r'\b(bluray|blu-ray|web-dl|webdl|webrip|web|bdrip|brrip|hdrip|dvdrip|remux|hdtv)\b',
    re.I,
)
_GROUP_PATTERN = re.compile(r'-([A-Za-z0-9_.]{2,30})$')  # 结尾 -GROUPNAME
_YEAR_PATTERN = re.compile(r'(?<![0-9])(19|20)\d{2}(?![0-9])')
_SEASON_EP_PATTERN = re.compile(r'[Ss](\d{1,2})[\s._-]?[Ee](\d{1,3})')


@dataclass
class VideoInfo:
    """从视频文件名 + 外部元数据中提取的归一化信息，供 scoring 使用。"""
    filename: str = ''            # 视频文件名（含扩展名前的 stem）
    resolution: str = ''          # 1080p / 720p / 4k 等（小写）
    source: str = ''              # bluray / web-dl / hdtv 等（小写）
    release_group: str = ''       # 释放组名（小写）
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    imdb_id: str = ''             # tt 开头
    tmdb_id: str = ''             # 数字字符串
    file_hash: str = ''           # 大小写敏感由 caller 决定
    file_size: int = 0            # bytes
    title_normalized: str = ''    # 归一化后的纯标题（无 release tag / 年份 / 编号）

    @classmethod
    def from_filename(cls, filename: str) -> 'VideoInfo':
        """从纯文件名解析。filename 可包含或不含扩展名都行。"""
        name = filename or ''
        # 去扩展名
        if '.' in name and len(name.rsplit('.', 1)[-1]) <= 5:
            stem = name.rsplit('.', 1)[0]
        else:
            stem = name
        info = cls(filename=stem)

        m = _RES_PATTERN.search(stem)
        if m:
            info.resolution = m.group(1).lower()
        m = _SOURCE_PATTERN.search(stem)
        if m:
            info.source = m.group(1).lower().replace('-', '').replace(' ', '')
            # web-dl / webdl 都归一为 webdl
            if info.source in ('webrip', 'web'):
                info.source = info.source
        m = _GROUP_PATTERN.search(stem)
        if m:
            grp = m.group(1).strip('. _')
            # 排除把分辨率当 group 的误匹配
            if not _RES_PATTERN.fullmatch(grp):
                info.release_group = grp.lower()
        m = _YEAR_PATTERN.search(stem)
        if m:
            try:
                info.year = int(m.group())
            except ValueError:
                pass
        m = _SEASON_EP_PATTERN.search(stem)
        if m:
            try:
                info.season = int(m.group(1))
                info.episode = int(m.group(2))
            except ValueError:
                pass
        return info


@dataclass
class SubtitleCandidate:
    """字幕候选条目。每个 provider 把搜到的字幕包装成这个结构。"""
    source: str                                       # provider 名 (opensubtitles/assrt/shooter)
    raw: Dict[str, Any]                               # 原始 sub meta，下载时复用
    download_fn: Optional[Callable[[], Any]] = None   # 延迟下载（return 视情况）
    language: str = ''                                # 字幕语言代码（chs/eng/...）
    release_name: str = ''                            # 字幕侧的 release 文件名（用来比 release group / resolution）
    hash_match: bool = False                          # 字幕侧声明此候选是文件 hash 匹配（最强信号）
    imdb_id: str = ''
    tmdb_id: str = ''
    season: Optional[int] = None
    episode: Optional[int] = None
    year: Optional[int] = None
    vote: float = 0.0                                 # 0-10 标准化（vote_score / download_count rank 等）
    score: int = 0
    score_breakdown: Dict[str, int] = field(default_factory=dict)


# ============================================================================
# 评分函数
# ============================================================================

# 评分常量（Bazarr 默认值 + 项目调整，单位"分"）
SCORE_HASH = 100
SCORE_TMDB_ID = 60
SCORE_IMDB_ID = 60
SCORE_RELEASE_GROUP = 50
SCORE_RESOLUTION = 30
SCORE_SOURCE = 25
SCORE_YEAR = 20
SCORE_EPISODE = 15

# 语言匹配权重 —— 比 Bazarr 默认值（+1）大幅提高，因为我们不做"语言不命中就硬过滤"，
# 仅靠加分让用户偏好的语言（中文）排在英文之前。否则 release 信息全的英文字幕
# （IMDB+group+res+src+year ≈ 195）会淹没仅有 release 部分信息的中文字幕。
# 按 preferred 顺序梯度递减：preferred[0]=+50 / [1]=+40 / [2]=+30 / 兜底 +10
SCORE_LANG_PRIMARY = 50
SCORE_LANG_STEP = 10
SCORE_LANG_FALLBACK = 10
SCORE_LANG = SCORE_LANG_PRIMARY  # 旧调用兼容（仅当 cand.language 没在 preferred 列表里时用）

SCORE_VOTE_MAX = 10  # vote 0~10 直接计入


def score_candidate(
    cand: SubtitleCandidate,
    video: VideoInfo,
    preferred_langs: Optional[List[str]] = None,
) -> SubtitleCandidate:
    """计算候选评分。结果写入 cand.score / cand.score_breakdown 后返回 cand。"""
    breakdown: Dict[str, int] = {}

    # ---- 1. hash 匹配 ----
    if cand.hash_match:
        breakdown['hash'] = SCORE_HASH

    # ---- 2. ID 匹配 ----
    if cand.tmdb_id and video.tmdb_id and str(cand.tmdb_id) == str(video.tmdb_id):
        breakdown['tmdb_id'] = SCORE_TMDB_ID
    if cand.imdb_id and video.imdb_id and cand.imdb_id.lower() == video.imdb_id.lower():
        breakdown['imdb_id'] = SCORE_IMDB_ID

    # ---- 3. release tag（基于 cand.release_name 解析）----
    if cand.release_name:
        rel = VideoInfo.from_filename(cand.release_name)
        if rel.release_group and video.release_group and rel.release_group == video.release_group:
            breakdown['release_group'] = SCORE_RELEASE_GROUP
        if rel.resolution and video.resolution and rel.resolution == video.resolution:
            breakdown['resolution'] = SCORE_RESOLUTION
        if rel.source and video.source and rel.source == video.source:
            breakdown['source'] = SCORE_SOURCE

    # ---- 4. year ----
    if cand.year and video.year and int(cand.year) == int(video.year):
        breakdown['year'] = SCORE_YEAR

    # ---- 5. episode S/E ----
    if (
        cand.season is not None and video.season is not None
        and cand.episode is not None and video.episode is not None
        and cand.season == video.season and cand.episode == video.episode
    ):
        breakdown['episode'] = SCORE_EPISODE

    # ---- 6. 语言命中 ----
    # 用 lang_match_score 实现"双语包覆盖单语 preferred"的语义：
    #   preferred=['chs.eng','chs','eng']
    #     cand.language='chs.eng' → first_hit=0 → +50
    #     cand.language='chs'     → first_hit=1 → +40
    #     cand.language='eng'     → first_hit=2 → +30
    #     cand.language='cht.eng' → first_hit=2（仅命中 eng）→ +30
    #     cand.language='cht'     → 不沾边 → +0（不加分，让排序自然沉底）
    if preferred_langs and cand.language:
        from common.lang_utils import lang_match_score as _lang_score
        neg_cov, first_hit = _lang_score(cand.language, preferred_langs)
        if neg_cov < 0:  # 至少覆盖了一项 preferred
            score = max(SCORE_LANG_PRIMARY - first_hit * SCORE_LANG_STEP, SCORE_LANG_FALLBACK)
            breakdown['lang'] = score

    # ---- 7. vote ----
    if cand.vote:
        # 0-10 → 直接累计；超出 clamp
        v = max(0.0, min(float(cand.vote), float(SCORE_VOTE_MAX)))
        breakdown['vote'] = int(round(v))

    cand.score = sum(breakdown.values())
    cand.score_breakdown = breakdown
    return cand


def pick_best(
    candidates: List[SubtitleCandidate],
    video: VideoInfo,
    preferred_langs: Optional[List[str]] = None,
) -> Optional[SubtitleCandidate]:
    """对全部候选打分，返回最高分（同分时第一个）。空列表返回 None。"""
    if not candidates:
        return None
    for c in candidates:
        score_candidate(c, video, preferred_langs)
    candidates.sort(key=lambda c: (-c.score, c.source))
    return candidates[0]
