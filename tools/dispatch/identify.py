"""
媒体识别链：从 torrent name + 文件列表识别 media_type / title / year / SxxExx / IDs。

识别优先级（命中即停）：
  ① 用户主动选（user_hint，热门页传入）
  ② 番号正则（adult）
  ③ SxxExx + 系列名 → jellyfin 模糊匹配
  ④ 电影名 + 年份 → TMDB search
  ⑤ LLM 兜底（spike 实测 qwen-plus 100% 准确）
  ⑥ 都没识别 → unknown，落用户确认

返回结构：
  {
    media_type, tmdb_id?, imdb_id?, title, year?, season?, episode?,
    series_tmdb_id?, series_name?, code?,
    confidence, source,
    reasoning?,        # LLM 解释
  }
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ============================================================================
# 番号正则
# ============================================================================
# 主流番号格式：xxx-NNN（含 FC2-PPV-xxx）。3-7 位数字 + 可能后缀 -C / -CN / -uncensored
# 用 [A-Z]{2,5} 匹配公司前缀；过严容易漏，过宽容易误中
_AV_PATTERNS = [
    re.compile(r'\b(FC2[\s\-_]*PPV)[\s\-_]*(\d{3,8})\b', re.I),
    re.compile(r'\b([A-Z]{2,7})[\s\-_]+(\d{3,5})(?:[\s\-_]*[A-Z]{1,3})?\b'),
]

_EPISODE_PATTERNS = [
    re.compile(r'[Ss](\d{1,2})[\s._-]?[Ee](\d{1,3})'),
    # NxNN 风格（如 1x12 = S01E12）必须前后是分隔符或字符串边界，
    # 否则 1920x1080 这种分辨率会误命中
    re.compile(r'(?:^|[\s._\-\[])(\d{1,2})[xX](\d{1,3})(?=[\s._\-\]]|$)'),
]

_YEAR_RE = re.compile(r'\b(19\d{2}|20\d{2})\b')

# 动漫单集格式：' - 12' / '[01]' / '_12_' 等
_ANIME_EP_PATTERNS = [
    re.compile(r'-\s*(\d{1,3})\s*[\[\(]'),       # ' - 12 ['
    re.compile(r'\[(\d{1,3})\]'),                # '[01]'
    re.compile(r'第\s*(\d{1,3})\s*[集話话]'),     # 第12话
]

# 字幕组前缀（强动漫信号）
_ANIME_GROUP_PREFIX = re.compile(
    r'^\s*\[([^\]]+)\]', re.UNICODE,
)


# ============================================================================
# 启发式识别
# ============================================================================

def match_av_code(torrent_name: str, files: Optional[List[Dict]] = None) -> Optional[str]:
    """
    匹配番号。要求至少有一个主视频文件 ≥ 100MB（防误中）。
    返回标准化 code（'SSIS-555' / 'FC2-PPV-3120842'）或 None。
    """
    candidates = []
    for pat in _AV_PATTERNS:
        for m in pat.finditer(torrent_name):
            if 'fc2' in m.group(1).lower():
                candidates.append(f"FC2-PPV-{m.group(2)}")
            else:
                candidates.append(f"{m.group(1).upper()}-{m.group(2)}")

    if not candidates:
        return None

    # 文件大小校验（番号视频通常 ≥ 500MB；至少 100MB 防 sample 误中）
    if files:
        max_size = max((f.get('size') or 0) for f in files)
        if max_size < 100 * 1024 * 1024:
            return None

    return candidates[0]


def extract_episode_info(torrent_name: str) -> Optional[Dict]:
    """提取 SxxExx 信息。返回 {season, episode, series_name_hint} 或 None。
    series_name_hint 已清理 release 名分隔符（'.' '_' '-' → 空格）+ 去尾巴空白。"""
    for pat in _EPISODE_PATTERNS:
        m = pat.search(torrent_name)
        if m:
            season = int(m.group(1))
            episode = int(m.group(2))
            prefix = torrent_name[:m.start()]
            # release 名风格：. _ → 空格；多空格折叠；去前后空白
            series_name = re.sub(r'[._]+', ' ', prefix)
            series_name = re.sub(r'[\-]+', ' ', series_name)  # 防止 'Game-of-Thrones'
            series_name = re.sub(r'\s+', ' ', series_name).strip(' .-_[]')
            return {
                'season': season,
                'episode': episode,
                'series_name_hint': series_name,
            }
    return None


def extract_anime_episode(torrent_name: str) -> Optional[Dict]:
    """动漫单集格式（' - 12'）。返回 {episode, anime_name_hint} 或 None。"""
    # 先看有没有字幕组前缀（强动漫信号）
    has_group_prefix = bool(_ANIME_GROUP_PREFIX.search(torrent_name))

    for pat in _ANIME_EP_PATTERNS:
        m = pat.search(torrent_name)
        if not m:
            continue
        episode = int(m.group(1))
        # anime_name = group 之后到 episode 之前的部分
        body = _ANIME_GROUP_PREFIX.sub('', torrent_name).strip()
        body_before_ep = body[:m.start() - len(torrent_name) + len(body)]
        # 简单清理
        anime_name = re.sub(r'[._]', ' ', body_before_ep).strip(' .-_[]')
        if anime_name and (has_group_prefix or len(anime_name) > 4):
            return {
                'season': 1,                # 动漫默认 S1，除非显式 SxxExx
                'episode': episode,
                'anime_name_hint': anime_name,
                'has_group_prefix': has_group_prefix,
            }
    return None


def extract_movie_info(torrent_name: str) -> Optional[Dict]:
    """提取电影标题 + 年份。返回 {title, year} 或 None。"""
    m = _YEAR_RE.search(torrent_name)
    if not m:
        return None
    year = int(m.group(1))
    # title = 年份之前的部分（去 release tag 风格分隔符）
    title_raw = torrent_name[:m.start()]
    title = re.sub(r'[._]', ' ', title_raw).strip(' .-_')
    if not title or len(title) < 2:
        return None
    return {'title': title, 'year': year}


# ============================================================================
# TMDB 反查
# ============================================================================

def _tmdb_client():
    """统一 TMDBClient 创建（缺 api_key 返回 None）。"""
    from common.tmdb_client import TMDBClient
    from web.backend.config import settings
    if not settings.tmdb_api_key:
        return None
    return TMDBClient(api_key=settings.tmdb_api_key, language=settings.tmdb_language)


def _tmdb_search_movie(query: str, year: Optional[int] = None) -> Optional[Dict]:
    """
    返回 {tmdb_id, title, year} 或 None。
    注意 client.search_movie 返回的是**单个 dict**（已 results[0]），不是 list。
    """
    try:
        c = _tmdb_client()
        if c is None:
            return None
        first = c.search_movie(query, year=year)
        if not first:
            return None
        return {
            'tmdb_id': str(first.get('id')),
            'title': first.get('title') or first.get('original_title'),
            'year': int((first.get('release_date') or '0000')[:4]) or None,
        }
    except Exception as e:
        logger.warning(f"TMDB search_movie 失败 [{query!r}]: {e}")
        return None


def _tmdb_search_tv(query: str, year: Optional[int] = None) -> Optional[Dict]:
    """
    返回 {tmdb_id, title, is_anime} 或 None。
    is_anime: original_language=ja 或 origin_country 含 JP 视为日漫。
    """
    try:
        c = _tmdb_client()
        if c is None:
            return None
        first = c.search_tv(query, year=year)
        if not first:
            return None
        is_anime = (
            first.get('original_language') == 'ja'
            or 'JP' in (first.get('origin_country') or [])
        )
        return {
            'tmdb_id': str(first.get('id')),
            'title': first.get('name') or first.get('original_name'),
            'is_anime': is_anime,
            'first_air_year': int((first.get('first_air_date') or '0000')[:4]) or None,
        }
    except Exception as e:
        logger.warning(f"TMDB search_tv 失败 [{query!r}]: {e}")
        return None


# ============================================================================
# 主入口
# ============================================================================

def identify_media(
    torrent_name: str,
    files: Optional[List[Dict]] = None,
    user_hint: Optional[Dict] = None,
) -> Dict:
    """
    返回识别结果。source 字段标记是哪条规则命中：
      user_hint / regex_avcode / regex_episode / regex_anime / tmdb_search /
      llm_with_tmdb / llm_only / unknown
    """
    files = files or []

    # ① 用户提示
    if user_hint and user_hint.get('media_type'):
        return {
            **user_hint,
            'source': 'user_hint',
            'confidence': 1.0,
        }

    # ② 番号
    av = match_av_code(torrent_name, files)
    if av:
        return {
            'media_type': 'adult',
            'title': av,
            'code': av,
            'source': 'regex_avcode',
            'confidence': 0.95,
        }

    # ③ SxxExx 剧集
    ep = extract_episode_info(torrent_name)
    if ep:
        # 尝试 TMDB 反查 series
        tv = _tmdb_search_tv(ep['series_name_hint']) if ep['series_name_hint'] else None
        return {
            'media_type': 'anime' if (tv and tv.get('is_anime')) else 'tv',
            'title': (tv or {}).get('title') or ep['series_name_hint'],
            'series_name': (tv or {}).get('title') or ep['series_name_hint'],
            'series_tmdb_id': (tv or {}).get('tmdb_id'),
            'season': ep['season'],
            'episode': ep['episode'],
            'source': 'regex_episode',
            'confidence': 0.9 if tv else 0.7,
        }

    # ④ 动漫单集
    anime = extract_anime_episode(torrent_name)
    if anime and anime.get('has_group_prefix'):
        return {
            'media_type': 'anime',
            'title': anime['anime_name_hint'],
            'series_name': anime['anime_name_hint'],
            'season': anime['season'],
            'episode': anime['episode'],
            'source': 'regex_anime',
            'confidence': 0.75,
        }

    # ⑤ 电影名 + 年份
    movie = extract_movie_info(torrent_name)
    if movie:
        tmdb = _tmdb_search_movie(movie['title'], year=movie['year'])
        if tmdb:
            return {
                'media_type': 'movie',
                'tmdb_id': tmdb['tmdb_id'],
                'title': tmdb['title'],
                'year': tmdb['year'] or movie['year'],
                'source': 'tmdb_search',
                'confidence': 0.85,
            }
        # 即使 TMDB 没命中，仍判定为 movie（中等置信度）
        return {
            'media_type': 'movie',
            'title': movie['title'],
            'year': movie['year'],
            'source': 'regex_movie',
            'confidence': 0.6,
        }

    # ⑥ LLM 兜底
    try:
        from common.llm_client import get_default_client
        from web.backend.config import settings
        if settings.llm.enabled and settings.llm.api_key:
            llm = get_default_client()
            llm_result = llm.classify_torrent(torrent_name, files)
            conf = float(llm_result.get('confidence') or 0)
            if conf >= settings.llm.confidence_threshold:
                hit = dict(llm_result)
                hit['source'] = 'llm_only'
                # 无条件 TMDB 反查：不只是验证，更是为了拿 tmdb_id（后续重复检测 / jellyfin 关联都要用）
                hint = llm_result.get('tmdb_search_hint')
                if hint:
                    if llm_result.get('media_type') == 'movie':
                        verified = _tmdb_search_movie(hint, year=llm_result.get('year'))
                        if verified:
                            hit.update(verified)
                            hit['source'] = 'llm_with_tmdb'
                    elif llm_result.get('media_type') in ('tv', 'anime'):
                        verified = _tmdb_search_tv(hint)
                        if verified:
                            hit['series_tmdb_id'] = verified['tmdb_id']
                            hit['source'] = 'llm_with_tmdb'
                # 标准化字段名
                hit['title'] = (
                    hit.get('title_zh') or hit.get('title_en')
                    or hit.get('title_native') or torrent_name[:80]
                )
                if hit.get('media_type') in ('tv', 'anime') and not hit.get('series_name'):
                    hit['series_name'] = hit['title']
                hit['confidence'] = conf
                return hit
    except Exception as e:
        logger.warning(f"LLM 识别异常: {e}")

    # ⑦ 都没识别
    return {
        'media_type': 'unknown',
        'title': torrent_name[:120],
        'source': 'unknown',
        'confidence': 0.0,
    }
