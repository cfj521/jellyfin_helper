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
    # 番号 prefix + 编号；编号若是 4 位且落在 19xx/20xx → 当年份不当番号
    # （之前 "Planet Earth III 2023" 里 "III 2023" 被误命中成 III-2023 番号）
    re.compile(r'\b([A-Z]{2,7})[\s\-_]+(?!(?:19|20)\d{2}\b)(\d{3,5})(?:[\s\-_]*[A-Z]{1,3})?\b'),
]

_EPISODE_PATTERNS = [
    re.compile(r'[Ss](\d{1,2})[\s._-]?[Ee](\d{1,3})'),
    # NxNN 风格（如 1x12 = S01E12）必须前后是分隔符或字符串边界，
    # 否则 1920x1080 这种分辨率会误命中
    re.compile(r'(?:^|[\s._\-\[])(\d{1,2})[xX](\d{1,3})(?=[\s._\-\]]|$)'),
]

_YEAR_RE = re.compile(r'\b(19\d{2}|20\d{2})\b')

# 发布站前缀：剥掉名字开头的 "www.XXX.org   -   "/"[Site.com]"/"(www.SiteName.cc)" 等
# 主流公网种子站会在 release name 前加这类水印（UIndex / 1337x / TGx / RARBG / EZTV 等都有变体）
# 不剥的话 extract_movie_info 会把这段当 title 的一部分 → TMDB 反查匹配不到 → 走低置信回退或污染 title
_SITE_PREFIX_PATTERNS = [
    # www.xxx.tld 或 [www.xxx.tld] 或 (xxx.tld) 后接连字符/空格
    re.compile(
        r'^\s*[\[\(]?\s*(?:www\.)?[\w-]+\.(?:com|org|net|info|me|io|to|cc|tv|biz|club|li|lol|app|nz)\s*[\]\)]?\s*[-—–_]+\s*',
        re.IGNORECASE,
    ),
    # 单纯 [Site] 前缀（不带 tld 的字幕组类已被 _ANIME_GROUP_PREFIX 处理；这里只抓常见公网站点字面前缀）
    re.compile(r'^\s*\[\s*(?:RARBG|YTS|EZTV|TGx|1337x|UIndex|RuTracker|FitGirl)\s*[^\]]*\]\s*[-—–_]?\s*', re.IGNORECASE),
]


def _strip_site_prefix(name: str) -> str:
    """剥发布站水印前缀；不动后缀的发布组（-OFT/-GROUP）—— 那是 release 工具识别用的"""
    if not name:
        return name
    for pat in _SITE_PREFIX_PATTERNS:
        name = pat.sub('', name, count=1)
    return name.strip()

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

_AV_VERIFY_SCOPE = 'av_verify'
_AV_VERIFY_TTL_DAYS = 30


def verify_av_code(code: str) -> bool:
    """跑 ScraperManager（merge=False，第一源命中即返回）验证番号是否真实存在。
    结果存 KvCache 30 天，避免重复爬。

    返回 True = 至少一个源能查到这个番号（很可能是 AV）；False = regex 命中但 scraper 全 miss
    （多半是误命中，得降置信度让用户审核）。
    """
    if not code:
        return False
    code = code.upper().strip()
    # 优先查缓存
    try:
        from backend.cache_store import get_cached, set_cached
        cached = get_cached(_AV_VERIFY_SCOPE, code, ttl_seconds=_AV_VERIFY_TTL_DAYS * 86400)
        if isinstance(cached, dict) and 'verified' in cached:
            return bool(cached['verified'])
    except Exception:
        get_cached = set_cached = None  # type: ignore

    # 实跑 scraper（merge=False：第一源命中即返回，快）
    try:
        from tools.adult_manager.scrapers.manager import ScraperManager
        mgr = ScraperManager()
        result = mgr.scrape(code, merge=False)
        verified = bool(result and result.title)
    except Exception as e:
        logger.warning(f"verify_av_code 失败 {code}: {e}（按未验证处理）")
        return False

    if set_cached is not None:
        try:
            set_cached(_AV_VERIFY_SCOPE, code, {'verified': verified})
        except Exception:
            pass
    logger.info(f"verify_av_code {code} → {'命中' if verified else '未命中（多半是误中）'}")
    return verified


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
    from backend.config import settings
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

    user_hint 是"用户已断言"的字段（典型来自 qB category alias 映射，或前端 AddTorrentDialog 的下拉）。
    它**仅用于覆盖识别链路的结果**，不能短路识别链——历史 bug：只有 media_type 的 hint 早期 return 会
    让 title/year 全 None，adopt 调用方直接写库 → 'Z:/videos/movie/ ()' 这种垃圾路径。
    """
    files = files or []

    # 预处理：剥发布站水印前缀，避免污染后续标题提取（番号 / SxxExx / 年份 regex 都受益）
    # 例：'www.UIndex.org    -    Schindlers List 1993 ...' → 'Schindlers List 1993 ...'
    cleaned_name = _strip_site_prefix(torrent_name) if torrent_name else torrent_name
    if cleaned_name != torrent_name:
        logger.info(f"剥发布站前缀: {torrent_name!r} → {cleaned_name!r}")
    torrent_name = cleaned_name

    # 跑完整识别链路拿到 title/year/series_name 等，再让 user_hint 后置覆盖（如有）
    result = _identify_chain(torrent_name, files)

    # user_hint 后置覆盖：用户断言的字段优先；链路抽出的字段留下来
    if user_hint:
        user_set = {k: v for k, v in user_hint.items() if v is not None}
        if user_set:
            base_source = result.get('source') or 'unknown'
            result = {**result, **user_set}
            result['source'] = f"user_hint+{base_source}" if base_source != 'unknown' else 'user_hint'
            # 用户断言后置信度提升，但不上 1.0（title/year 等仍是启发式抽的）
            result['confidence'] = max(float(result.get('confidence') or 0), 0.85)

    # 出口诊断日志：记录关键字段 + source。出问题时直接捞日志就能定位是哪条链路抽空了什么字段
    _log_identify_result(torrent_name, user_hint, result)
    return result


def _log_identify_result(torrent_name: str, user_hint: Optional[Dict], result: Dict) -> None:
    """统一记录 identify_media 的最终结果。对结果不完整的情况升到 WARNING。"""
    mt = result.get('media_type')
    title = result.get('title')
    year = result.get('year')
    series = result.get('series_name')
    source = result.get('source')
    conf = result.get('confidence')

    # 判定是否"看起来不对"：media_type 已定但缺关键标题字段 → 后续 _resolve_target 会兜底拦截，
    # 但日志里要先看见，免得再出现"没日志可查"的 bug
    suspicious = False
    if mt == 'movie' and not title:
        suspicious = True
    elif mt == 'tv' and not series and not title:
        suspicious = True
    elif mt == 'anime' and not series and not title:
        suspicious = True
    elif mt == 'adult' and not result.get('code') and not title:
        suspicious = True

    log_msg = (
        f"identify_media: name={torrent_name[:80]!r} "
        f"hint={user_hint!r} → "
        f"media_type={mt!r} title={title!r} year={year!r} "
        f"series_name={series!r} source={source!r} conf={conf}"
    )
    if suspicious:
        logger.warning(f"识别结果不完整（关键字段缺失）：{log_msg}")
    else:
        logger.info(log_msg)


# confidence-driven 识别链：
#   - 每步产出 (result, conf) 累计到 best
#   - 任一步 conf >= HIGH 直接返回（短路）
#   - 全跑完仍无人达 HIGH 时返回 best
HIGH_CONFIDENCE = 0.70


def _title_similarity(a: str, b: str) -> float:
    """简单 token Jaccard 相似度。用来挡 TMDB 误匹配
    （'Planet Earth 2006' → 'Final Days of Planet Earth' 这种 token 重合度很低）。
    a 是用户 release 来的标题，b 是 TMDB 返回的标题；都 lowercase + 去标点 + 分词。"""
    def _tokens(s: str) -> set:
        if not s:
            return set()
        # 去标点符号 + lowercase + 拆词
        s = re.sub(r'[^\w\s]', ' ', s.lower())
        return {t for t in s.split() if len(t) >= 2}
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


# release 标签词（清洗时剥掉这些保留标题骨架）
_RELEASE_TAG_TOKENS = re.compile(
    r'\b(?:2160p|1080p|720p|480p|4K|UHD|HDR|HDR10\+?|DV|DolbyVision|Atmos|TrueHD|'
    r'BluRay|Blu-ray|BDRip|BDRemux|REMUX|WEB-?DL|WEBRip|HDTV|DVDRip|'
    r'x264|x265|H\.?264|H\.?265|HEVC|AVC|10bit|8bit|AAC|AC3|DTS(?:-HD|HD)?|MA|5\.1|7\.1|'
    r'PROPER|REPACK|RERIP|EXTENDED|INTERNAL|LIMITED|UNCUT|UNRATED|IMAX|'
    r'TERMiNAL|YTS\.MX|YIFY|RARBG|EZTV|UTR|EVO|FGT|GalaxyRG)\b',
    re.I,
)


def _clean_release_name(name: str) -> str:
    """去 release 标签 + 年份 + 文件扩展，留 title 骨架。供"无年份通用 TMDB 搜"用。
    e.g. 'Planet.Earth II 2160p 4K UHD 10bit HDR BluRay AAC 5' → 'Planet Earth II'
    """
    s = name
    # 去括号/方括号包裹的整段（YTS 标签 / 字幕组 / 站点水印）
    s = re.sub(r'[\[\(][^\[\]\(\)]{1,40}[\]\)]', ' ', s)
    # 先去 AAC/AC3/DTS/TrueHD/Atmos 后面跟的声道数字（5.1 / 7.1 / 5 / 7）—— 先清这个，
    # 否则下一步 _RELEASE_TAG_TOKENS 只清 AAC 等字母词，孤立的数字 5 / 7 留着会污染标题
    s = re.sub(
        r'\b(?:AAC|AC3|DTS(?:-HD|HD)?|MA|TrueHD|Atmos|DDP?|EAC3)[\s.\-]+\d+(?:\.\d+)?\b',
        ' ',
        s,
        flags=re.I,
    )
    # 去 release 标签词
    s = _RELEASE_TAG_TOKENS.sub(' ', s)
    # 去年份
    s = _YEAR_RE.sub(' ', s)
    # 去文件扩展
    s = re.sub(r'\.(mkv|mp4|avi|m2ts|ts|wmv)$', '', s, flags=re.I)
    # . _ - → 空格；多空格折叠
    s = re.sub(r'[._\-]+', ' ', s).strip()
    s = re.sub(r'\s+', ' ', s)
    return s


def _pick_llm_title(hit: Dict, fallback: str) -> str:
    """从 LLM hit 里按用户语言偏好选 title。
    settings.metadata_scrape_language 控制：en* → 英文优先；zh* → 中文优先；其它 → native。"""
    try:
        from backend.config import settings
        lang = (settings.metadata_scrape_language or 'en').lower()
    except Exception:
        lang = 'en'
    zh = hit.get('title_zh')
    en = hit.get('title_en')
    native = hit.get('title_native')
    if lang.startswith('zh'):
        return zh or en or native or fallback
    if lang.startswith('en'):
        return en or native or zh or fallback
    # 其它（日语/韩语等）→ native 优先
    return native or en or zh or fallback


def _run_llm(torrent_name: str, files: List[Dict]) -> Optional[Dict]:
    """跑 LLM + TMDB 反查。失败 / 未启用返回 None。conf 是 LLM 自己给的，不再卡阈值。"""
    try:
        from common.llm_client import get_default_client
        from backend.config import settings
        if not (settings.llm.enabled and settings.llm.api_key):
            return None
        llm = get_default_client()
        llm_result = llm.classify_torrent(torrent_name, files)
    except Exception as e:
        logger.warning(f"LLM 识别异常: {e}")
        return None

    conf = float(llm_result.get('confidence') or 0)
    if conf <= 0:
        return None

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
                hit['title'] = verified['title']    # 用 TMDB 标题覆盖（按 tmdb_language 配置返回）
                hit['source'] = 'llm_with_tmdb'

    # 标准化 title：按用户语言偏好选
    hit['title'] = _pick_llm_title(hit, fallback=torrent_name[:80])
    if hit.get('media_type') in ('tv', 'anime') and not hit.get('series_name'):
        hit['series_name'] = hit['title']
    hit['confidence'] = conf
    return hit


def _try_av(torrent_name: str, files: List[Dict]) -> Optional[Dict]:
    """步 1：番号 regex + scraper 验证。"""
    av = match_av_code(torrent_name, files)
    if not av:
        return None
    verified = verify_av_code(av)
    return {
        'media_type': 'adult',
        'title': av,
        'code': av,
        'source': 'regex_avcode_verified' if verified else 'regex_avcode_unverified',
        'confidence': 0.95 if verified else 0.50,
    }


def _try_anime(torrent_name: str) -> Optional[Dict]:
    """步 2：动漫单集格式（字幕组前缀 + 集号）。"""
    anime = extract_anime_episode(torrent_name)
    if not anime or not anime.get('has_group_prefix'):
        return None
    return {
        'media_type': 'anime',
        'title': anime['anime_name_hint'],
        'series_name': anime['anime_name_hint'],
        'season': anime['season'],
        'episode': anime['episode'],
        'source': 'regex_anime',
        'confidence': 0.75,
    }


def _try_episode_tmdb(torrent_name: str) -> Optional[Dict]:
    """步 3a：SxxExx 命中 + TMDB tv 搜索。"""
    ep = extract_episode_info(torrent_name)
    if not ep:
        return None
    tv = _tmdb_search_tv(ep['series_name_hint']) if ep['series_name_hint'] else None
    return {
        'media_type': 'anime' if (tv and tv.get('is_anime')) else 'tv',
        'title': (tv or {}).get('title') or ep['series_name_hint'],
        'series_name': (tv or {}).get('title') or ep['series_name_hint'],
        'series_tmdb_id': (tv or {}).get('tmdb_id'),
        'season': ep['season'],
        'episode': ep['episode'],
        'source': 'regex_episode',
        'confidence': 0.90 if tv else 0.70,
    }


def _try_movie_tmdb(torrent_name: str) -> Optional[Dict]:
    """步 3b：电影名 + 年份 → TMDB movie 搜索。加 title fuzzy 校验防误匹配。"""
    movie = extract_movie_info(torrent_name)
    if not movie:
        return None
    tmdb = _tmdb_search_movie(movie['title'], year=movie['year'])
    if tmdb:
        sim = _title_similarity(movie['title'], tmdb.get('title') or '')
        if sim >= 0.5:
            return {
                'media_type': 'movie',
                'tmdb_id': tmdb['tmdb_id'],
                'title': tmdb['title'],
                'year': tmdb['year'] or movie['year'],
                'source': 'tmdb_search',
                'confidence': 0.85,
            }
        # 命中但相似度低 —— 可能 TMDB 返回了同年的不同片（'Planet Earth 2006' → 'Final Days...'）
        logger.info(
            f"tmdb_search 弱命中（title fuzzy sim={sim:.2f}）: "
            f"query={movie['title']!r} → tmdb={tmdb.get('title')!r}"
        )
        return {
            'media_type': 'movie',
            'tmdb_id': tmdb['tmdb_id'],
            'title': movie['title'],     # 用 release 标题，不用可疑的 TMDB 结果
            'year': movie['year'],
            'source': 'tmdb_search_weak',
            'confidence': 0.50,
        }
    # TMDB 没命中：regex_movie 中等置信度
    return {
        'media_type': 'movie',
        'title': movie['title'],
        'year': movie['year'],
        'source': 'regex_movie',
        'confidence': 0.60,
    }


def _try_generic_tmdb(torrent_name: str) -> Optional[Dict]:
    """步 3c：清洗 release tag 后通用 tv + movie 搜（不要求年份）。
    解决 'Planet.Earth II 2160p 4K UHD ...' 这种没年份 / 没 SxxExx 的整季 pack。"""
    cleaned = _clean_release_name(torrent_name)
    if len(cleaned) < 3:
        return None
    tv = _tmdb_search_tv(cleaned)
    mv = _tmdb_search_movie(cleaned)

    # 各自算 fuzzy 相似度，挑分高的；都低就回 None
    tv_sim = _title_similarity(cleaned, (tv or {}).get('title') or '') if tv else 0.0
    mv_sim = _title_similarity(cleaned, (mv or {}).get('title') or '') if mv else 0.0

    if not tv and not mv:
        return None
    if tv_sim >= mv_sim and tv_sim >= 0.4:
        return {
            'media_type': 'anime' if tv.get('is_anime') else 'tv',
            'title': tv['title'],
            'series_name': tv['title'],
            'series_tmdb_id': tv['tmdb_id'],
            'source': 'tmdb_generic',
            'confidence': 0.70,
        }
    if mv_sim >= 0.4:
        return {
            'media_type': 'movie',
            'tmdb_id': mv['tmdb_id'],
            'title': mv['title'],
            'year': mv.get('year'),
            'source': 'tmdb_generic',
            'confidence': 0.70,
        }
    return None


def _identify_chain(torrent_name: str, files: List[Dict]) -> Dict:
    """完整识别链路（无 user_hint 影响）。

    confidence-driven：每步产出 result + conf；conf ≥ HIGH_CONFIDENCE 立即返回；
    否则累计 best，跑完所有步骤后返回当前 best（或 unknown）。

    步骤顺序（默认）：① 番号 ② 动漫 ③ TMDB 链（SxxExx → movie+year → generic） ④ LLM
    settings.llm.prefer_first=True 时把 ④ 挪到 ① 前面。

    详细置信度参考 config.yaml.example 里 llm 段的注释。
    """
    best: Optional[Dict] = None
    fallback_default = {
        'media_type': 'unknown',
        'title': torrent_name[:120],
        'source': 'unknown',
        'confidence': 0.0,
    }

    def consider(candidate: Optional[Dict]) -> bool:
        """累计 candidate；返回 True 表示触达 HIGH 阈值，调用方应短路返回 best。"""
        nonlocal best
        if candidate is None:
            return False
        c = float(candidate.get('confidence') or 0)
        if best is None or c > float(best.get('confidence') or 0):
            best = candidate
        return c >= HIGH_CONFIDENCE

    # 读 prefer_first 开关
    prefer_llm_first = False
    try:
        from backend.config import settings
        prefer_llm_first = bool(getattr(settings.llm, 'prefer_first', False))
    except Exception:
        pass

    # 步骤集合（生成器；按需调用，避免无意义的 TMDB / LLM HTTP）
    def llm_step():
        return _run_llm(torrent_name, files)

    def av_step():
        return _try_av(torrent_name, files)

    def anime_step():
        return _try_anime(torrent_name)

    def tmdb_steps():
        # 三个子步骤按相对置信度顺序：SxxExx > movie+year > generic
        for fn in (_try_episode_tmdb, _try_movie_tmdb, _try_generic_tmdb):
            yield fn(torrent_name)

    if prefer_llm_first:
        # LLM 优先模式：LLM → 番号 → 动漫 → TMDB 链
        if consider(llm_step()):
            return best
        if consider(av_step()):
            return best
        if consider(anime_step()):
            return best
        for cand in tmdb_steps():
            if consider(cand):
                return best
    else:
        # 默认：番号 → 动漫 → TMDB 链 → LLM
        if consider(av_step()):
            return best
        if consider(anime_step()):
            return best
        for cand in tmdb_steps():
            if consider(cand):
                return best
        if consider(llm_step()):
            return best

    return best or fallback_default
