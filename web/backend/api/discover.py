"""
内容推荐 API（仅 TMDB 热门 / 流行 / 列表 / 详情）

历史：之前还含 Jackett 搜索 + qB 下载推送，现已拆出：
  - /search* → web/backend/api/resourcesearch.py
  - /download* / /downloads* / /transfer-info* → web/backend/api/downloadpipeline.py
"""
import sys
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

ROOT_DIR = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from web.backend.database import get_db
from web.backend.config import settings
from web.backend.services import metadata_store

logger = logging.getLogger(__name__)
router = APIRouter()


def _tmdb_scrape_lang() -> str:
    """
    TMDB 上游请求用的语言标签（'en-US' / 'zh-CN' 等）。
    优先 metadata.scrape_language（新配置）；老 tmdb.language 仍生效作为兜底。
      'en' → 'en-US'
      'zh' → 'zh-CN'
      其它 → 直接透传（用户可写完整 BCP47 如 'ja-JP'）
    """
    # 兜底兼容老 config：直接读 settings.tmdb_language（绕开 sed 替换，避免自递归）
    raw = settings.metadata_scrape_language or getattr(settings, 'tmdb_language', None) or 'en'
    raw = raw.strip()
    short_map = {'en': 'en-US', 'zh': 'zh-CN', 'ja': 'ja-JP', 'ko': 'ko-KR'}
    return short_map.get(raw.lower(), raw)


def _display_title_for(row) -> Optional[str]:
    """
    按 metadata_display_language 挑展示 title。
    缺对应语言字段时 fallback 到另一个，再缺则 None。

    豆瓣源无独立配置：display_language='en' 时优先 title（英文部分），
    但豆瓣条目 title 经常没有英文 → fallback 到 title_zh 自然展示中文。
    """
    if row is None:
        return None
    lang = (settings.metadata_display_language or 'en').lower()
    if lang.startswith('zh'):
        return row.title_zh or row.title
    return row.title or row.title_zh


# ---------- TMDB 热门：DB 持久化缓存（之前是 in-memory，重启就丢） ----------

from web.backend.config import settings as _settings
from web.backend.cache_store import (
    get_cached as _kv_get,
    set_cached as _kv_set,
    invalidate as _kv_invalidate,
)

_TMDB_SCOPE = 'tmdb_discover'
_TRAKT_SCOPE = 'trakt_discover'
_ANILIST_SCOPE = 'anilist_discover'
_DOUBAN_SCOPE = 'douban_discover'


def _tmdb_ttl_secs() -> int:
    return max(1, _settings.cache_tmdb_minutes) * 60


def _strip_cache_meta(v):
    """剥掉 _cached / _cached_at / _cache_age_seconds，让旧调用点拿到的对象跟原 in-memory 一致。"""
    if isinstance(v, dict):
        return {k: vv for k, vv in v.items() if not k.startswith('_cached') and k != '_cache_age_seconds'}
    return v


def _cache_get(key: str):
    return _strip_cache_meta(_kv_get(_TMDB_SCOPE, key, ttl_seconds=_tmdb_ttl_secs()))


def _cache_set(key: str, data):
    _kv_set(_TMDB_SCOPE, key, data)


def _cache_clear():
    _kv_invalidate(_TMDB_SCOPE)
    _kv_invalidate(_TRAKT_SCOPE)
    _kv_invalidate(_ANILIST_SCOPE)
    _kv_invalidate(_DOUBAN_SCOPE)


def _stale_fallback(scope: str, cache_key: str, ttl_seconds: int) -> Optional[Dict]:
    """
    Stale-while-revalidate 统一兜底：上游拉空/异常时调一下，有过期缓存就返回。
    返回剥过元字段的 dict + cached=True；没有则 None。
    """
    stale = _kv_get(scope, cache_key, ttl_seconds=ttl_seconds, allow_stale=True)
    if stale is None:
        return None
    logger.info(
        f"stale 兜底 {scope}/{cache_key}（{stale.get('_cache_age_seconds')}s 前）"
    )
    return {**_strip_cache_meta(stale), "cached": True}




@router.get("/trending")
def get_trending(
    media_type: str = "all",
    time_window: str = "week",
    page: int = 1,
    refresh: bool = False,
):
    """TMDB 热门内容（all / movie / tv，day / week）。一页 20 条；refresh=true 跳过缓存"""
    if not settings.tmdb_api_key:
        raise HTTPException(status_code=400, detail="未配置 TMDB API Key")
    if media_type not in ("all", "movie", "tv", "person"):
        raise HTTPException(status_code=400, detail="media_type 必须是 all / movie / tv / person")
    if time_window not in ("day", "week"):
        raise HTTPException(status_code=400, detail="time_window 必须是 day / week")
    page = max(1, min(20, int(page)))

    cache_key = f"trending:{media_type}:{time_window}:p{page}"
    if not refresh:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}

    from common.tmdb_client import TMDBClient
    client = TMDBClient(settings.tmdb_api_key, delay=0.5, language=_tmdb_scrape_lang())
    try:
        items = client.trending(media_type=media_type, time_window=time_window, page=page)
    except Exception as e:
        logger.warning(f"TMDB trending 上游异常: {e}")
        items = []
    if items:
        result = {
            "count": len(items),
            "items": _normalize_tmdb(items),
            "page": page,
            "has_more": len(items) >= 20,
        }
        _cache_set(cache_key, result)
        return {**result, "cached": False}
    # 上游空/异常 → stale 兜底
    fallback = _stale_fallback(_TMDB_SCOPE, cache_key, _tmdb_ttl_secs())
    if fallback is not None:
        return fallback
    return {"count": 0, "items": [], "page": page, "has_more": False, "cached": False}


@router.get("/popular")
def get_popular(media_type: str = "movie", page: int = 1, refresh: bool = False):
    """TMDB 流行电影/剧集。30 分钟内存缓存；refresh=true 强制刷新"""
    if not settings.tmdb_api_key:
        raise HTTPException(status_code=400, detail="未配置 TMDB API Key")

    cache_key = f"popular:{media_type}:{page}"
    if not refresh:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}

    from common.tmdb_client import TMDBClient
    client = TMDBClient(settings.tmdb_api_key, delay=0.5, language=_tmdb_scrape_lang())

    if media_type not in ('movie', 'tv'):
        raise HTTPException(status_code=400, detail="media_type 必须是 movie / tv")
    try:
        items = (client.popular_movies(page=page)
                 if media_type == 'movie'
                 else client.popular_tv(page=page))
    except Exception as e:
        logger.warning(f"TMDB popular 上游异常: {e}")
        items = []
    if items:
        result = {
            "count": len(items),
            "items": _normalize_tmdb(items, default_type=media_type),
            "page": page,
            "has_more": len(items) >= 20,
        }
        _cache_set(cache_key, result)
        return {**result, "cached": False}
    fallback = _stale_fallback(_TMDB_SCOPE, cache_key, _tmdb_ttl_secs())
    if fallback is not None:
        return fallback
    return {"count": 0, "items": [], "page": page, "has_more": False, "cached": False}


@router.get("/list")
def get_category_list(
    media_type: str,
    category: str,
    page: int = 1,
    refresh: bool = False,
):
    """
    TMDB 分类列表（与官网导航一致）。30 分钟内存缓存。

    media_type: movie | tv
    category:
      movie → popular | now_playing | upcoming | top_rated
      tv    → popular | airing_today | on_the_air | top_rated
    """
    if not settings.tmdb_api_key:
        raise HTTPException(status_code=400, detail="未配置 TMDB API Key")
    if media_type not in ('movie', 'tv'):
        raise HTTPException(status_code=400, detail="media_type 必须是 movie / tv")

    valid_cats = {
        'movie': {'popular', 'now_playing', 'upcoming', 'top_rated'},
        'tv': {'popular', 'airing_today', 'on_the_air', 'top_rated'},
    }
    if category not in valid_cats[media_type]:
        raise HTTPException(
            status_code=400,
            detail=f"category 对 {media_type} 必须是 {sorted(valid_cats[media_type])} 之一"
        )

    cache_key = f"list:{media_type}:{category}:{page}"
    if not refresh:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}

    from common.tmdb_client import TMDBClient
    client = TMDBClient(settings.tmdb_api_key, delay=0.5, language=_tmdb_scrape_lang())
    try:
        items = client.list_items(media_type, category, page=page)
    except Exception as e:
        logger.warning(f"TMDB list {media_type}/{category} 上游异常: {e}")
        items = []
    if items:
        result = {
            "count": len(items),
            "items": _normalize_tmdb(items, default_type=media_type),
            "media_type": media_type,
            "category": category,
            "page": page,
            "has_more": len(items) >= 20,
        }
        _cache_set(cache_key, result)
        return {**result, "cached": False}
    fallback = _stale_fallback(_TMDB_SCOPE, cache_key, _tmdb_ttl_secs())
    if fallback is not None:
        return fallback
    return {
        "count": 0, "items": [], "media_type": media_type, "category": category,
        "page": page, "has_more": False, "cached": False,
    }


@router.get("/detail")
def get_detail(
    media_type: str,
    tmdb_id: int,
    refresh: bool = False,
):
    """
    电影 / 剧集详情，含演员、相似推荐、预告等关联数据。30 分钟缓存。
    显示语言由 _tmdb_scrape_lang() 控制。
    """
    if not settings.tmdb_api_key:
        raise HTTPException(status_code=400, detail="未配置 TMDB API Key")
    if media_type not in ('movie', 'tv'):
        raise HTTPException(status_code=400, detail="media_type 必须是 movie / tv")

    lang = _tmdb_scrape_lang() or 'zh-CN'
    cache_key = f"detail:{media_type}:{tmdb_id}:{lang}"
    if not refresh:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}
        # L3 命中 fresh 直接返回（KV miss 时才查，命中 KV 已经隐含 L3 写过）
        l3_row = metadata_store.get_by_tmdb(int(tmdb_id), media_type)
        if l3_row is not None:
            if metadata_store.is_stale(l3_row):
                # stale：仍返回旧数据 + 后台异步 refresh
                try:
                    from web.backend.services.metadata_maintenance import enqueue_refresh
                    enqueue_refresh('tmdb', str(tmdb_id), hint={'media_type': media_type})
                except Exception:
                    pass
            else:
                # detail 响应里 similar/recommendations 是动态字段，L3 没存，命中后不带这两项
                full = metadata_store.row_to_full_dict(l3_row)
                return {**full, "cached": True, "_from_metadata": True}

    from common.tmdb_client import TMDBClient
    client = TMDBClient(settings.tmdb_api_key, delay=0.5, language=lang)
    try:
        raw = client.get_detail(media_type, tmdb_id, language=lang)
    except Exception as e:
        logger.warning(f"TMDB detail {media_type}/{tmdb_id} 上游异常: {e}")
        raw = None
    if raw:
        result = _normalize_detail(raw, media_type)
        _cache_set(cache_key, result)
        _upsert_tmdb_detail(result)
        return {**result, "cached": False}
    # 上游失败 → stale 兜底
    fallback = _stale_fallback(_TMDB_SCOPE, cache_key, _tmdb_ttl_secs())
    if fallback is not None:
        return fallback
    raise HTTPException(status_code=404, detail="TMDB 中未找到该条目")


def _upsert_tmdb_detail(detail: dict) -> None:
    """TMDB detail 完整字段落 L3。"""
    tmdb_id = detail.get('tmdb_id')
    if not tmdb_id:
        return
    try:
        date = detail.get('release_date') or ''
        year = int(date[:4]) if date and len(date) >= 4 and date[:4].isdigit() else None
        # ext 字段：详情独有的（list 没有的）
        ext = {
            'overview': detail.get('overview'),
            'tagline': detail.get('tagline'),
            'original_language': detail.get('original_language'),
            'english_title': detail.get('english_title'),
            'runtime': detail.get('runtime'),
            'episode_runtime': detail.get('episode_runtime'),
            'status': detail.get('status'),
            'backdrop_url': detail.get('backdrop_url'),
            'homepage': detail.get('homepage'),
            'countries': detail.get('countries') or [],
            'spoken_languages': detail.get('spoken_languages') or [],
            'studios': detail.get('studios') or [],
            'genres': detail.get('genres') or [],
            'directors': detail.get('directors') or [],
            'writers': detail.get('writers') or [],
            'cast': detail.get('cast') or [],
            'videos': detail.get('videos') or [],
            'number_of_seasons': detail.get('number_of_seasons'),
            'number_of_episodes': detail.get('number_of_episodes'),
            'seasons': detail.get('seasons') or [],
            'popularity': detail.get('popularity'),
            'vote_average': detail.get('vote_average'),
            'vote_count': detail.get('vote_count'),
        }
        # 去掉 None 值，避免 ext 里全是 None 噪声
        ext = {k: v for k, v in ext.items() if v is not None and v != []}
        # title / title_zh 拆分（detail 阶段中英两个都能从 translations 抽到）
        # _normalize_detail 已分别填 english_title 和 chinese_title（互不依赖 scrape_language）
        title_en = detail.get('english_title')
        title_zh = detail.get('chinese_title')
        # 兜底：english 抽不出来时用 original_title (原片为英语片时)
        if not title_en and detail.get('original_language') == 'en':
            title_en = detail.get('original_title')
        metadata_store.upsert(
            source='tmdb', source_id=str(tmdb_id),
            public={
                'media_type': detail.get('media_type'),
                'title': title_en,                # 英文（种子搜索锚点）
                'title_zh': title_zh,             # 中文（展示用）
                'original_title': detail.get('original_title'),
                'year': year,
                'release_date': date or None,
                'poster_url': detail.get('poster_url'),
            },
            ext=ext,
            bridge_ids={
                'tmdb_id': int(tmdb_id),
                'imdb_id': detail.get('imdb_id'),
            },
        )
    except Exception:
        logger.exception(f"L3 upsert tmdb detail 失败 tmdb_id={tmdb_id}")


def _normalize_detail(raw: dict, media_type: str) -> dict:
    """把 TMDB 详情大对象简化成前端需要的字段"""
    poster = raw.get('poster_path')
    backdrop = raw.get('backdrop_path')

    # 演员（前 20 位）
    credits = raw.get('credits') or {}
    cast = []
    for p in (credits.get('cast') or [])[:20]:
        profile = p.get('profile_path')
        cast.append({
            "id": p.get('id'),
            "name": p.get('name'),
            "character": p.get('character'),
            "profile_url": f"https://image.tmdb.org/t/p/w185{profile}" if profile else None,
        })

    # 导演 / 编剧（仅电影；剧集用 created_by）
    crew = credits.get('crew') or []
    directors = [c.get('name') for c in crew if c.get('job') == 'Director']
    writers = [c.get('name') for c in crew if c.get('department') == 'Writing'][:5]
    if media_type == 'tv':
        directors = [c.get('name') for c in (raw.get('created_by') or [])]

    # 视频（YouTube 预告片优先）
    videos = []
    for v in (raw.get('videos') or {}).get('results') or []:
        if v.get('site') == 'YouTube':
            videos.append({
                "key": v.get('key'),
                "name": v.get('name'),
                "type": v.get('type'),
                "official": v.get('official'),
                "youtube_url": f"https://www.youtube.com/watch?v={v.get('key')}",
                "thumb_url": f"https://img.youtube.com/vi/{v.get('key')}/hqdefault.jpg",
            })

    # 相似 / 推荐（取前 12）
    def _normalize_list(lst, default_type):
        out = []
        for it in (lst or [])[:12]:
            p = it.get('poster_path')
            out.append({
                "tmdb_id": it.get('id'),
                "media_type": it.get('media_type') or default_type,
                "title": it.get('title') or it.get('name'),
                "release_date": it.get('release_date') or it.get('first_air_date'),
                "vote_average": it.get('vote_average'),
                "poster_url": f"https://image.tmdb.org/t/p/w342{p}" if p else None,
            })
        return out

    similar = _normalize_list((raw.get('similar') or {}).get('results'), media_type)
    recommendations = _normalize_list((raw.get('recommendations') or {}).get('results'), media_type)

    # 国家、语言、制作公司
    countries = [c.get('name') for c in (raw.get('production_countries') or [])]
    spoken_langs = [l.get('english_name') for l in (raw.get('spoken_languages') or [])]
    studios = [s.get('name') for s in (raw.get('production_companies') or [])][:5]

    # 季 / 集（剧集独有）
    seasons = []
    for s in (raw.get('seasons') or []):
        sp = s.get('poster_path')
        seasons.append({
            "season_number": s.get('season_number'),
            "name": s.get('name'),
            "episode_count": s.get('episode_count'),
            "air_date": s.get('air_date'),
            "overview": s.get('overview'),
            "poster_url": f"https://image.tmdb.org/t/p/w185{sp}" if sp else None,
        })

    # 外部 ID（IMDb 链接等）
    ext = raw.get('external_ids') or {}
    imdb_id = ext.get('imdb_id') or raw.get('imdb_id')

    # 从 translations 里抽英文 + 中文标题（不论 scrape_language 当前是什么，
    # 两个标题都尽量拿到，方便实体表两列都填）
    english_title = None
    chinese_title = None
    translations = (raw.get('translations') or {}).get('translations') or []
    for t in translations:
        iso = t.get('iso_639_1')
        td = t.get('data') or {}
        cand = td.get('title') or td.get('name')
        if not cand:
            continue
        if iso == 'en' and english_title is None:
            english_title = cand
        elif iso == 'zh' and chinese_title is None:
            # 中文可能含 zh-CN / zh-TW / zh-HK；按 region 优先 CN
            if t.get('iso_3166_1') == 'CN':
                chinese_title = cand
            elif chinese_title is None:
                chinese_title = cand
    # 兜底：原始语言就是英文 → original_title 即英文
    if not english_title and raw.get('original_language') == 'en':
        english_title = raw.get('original_title') or raw.get('original_name')

    # 上游响应 title 本身按当前请求 language=参数 给的——它要么是英文要么是中文（看 scrape_language）
    upstream_title = raw.get('title') or raw.get('name')
    upstream_lang_is_zh = (_tmdb_scrape_lang() or 'en').lower().startswith('zh')
    # 把"中英两个版本"都尽量填齐：上游响应 + translations 互补
    if upstream_lang_is_zh:
        chinese_title = upstream_title or chinese_title
    else:
        english_title = english_title or upstream_title

    return {
        "tmdb_id": raw.get('id'),
        "media_type": media_type,
        "title": upstream_title,
        "original_title": raw.get('original_title') or raw.get('original_name'),
        "original_language": raw.get('original_language'),
        "english_title": english_title,
        "chinese_title": chinese_title,
        "tagline": raw.get('tagline'),
        "overview": raw.get('overview'),
        "release_date": raw.get('release_date') or raw.get('first_air_date'),
        "runtime": raw.get('runtime'),  # 电影分钟；剧集 None
        "episode_runtime": raw.get('episode_run_time'),  # 剧集每集分钟数
        "status": raw.get('status'),
        "vote_average": raw.get('vote_average'),
        "vote_count": raw.get('vote_count'),
        "popularity": raw.get('popularity'),
        "genres": [g.get('name') for g in (raw.get('genres') or [])],
        "homepage": raw.get('homepage'),
        "poster_url": f"https://image.tmdb.org/t/p/w500{poster}" if poster else None,
        "backdrop_url": f"https://image.tmdb.org/t/p/original{backdrop}" if backdrop else None,
        "imdb_id": imdb_id,
        "imdb_url": f"https://www.imdb.com/title/{imdb_id}" if imdb_id else None,
        "countries": countries,
        "spoken_languages": spoken_langs,
        "studios": studios,
        "directors": directors,
        "writers": writers,
        "cast": cast,
        "videos": videos,
        "similar": similar,
        "recommendations": recommendations,
        # 剧集字段
        "number_of_seasons": raw.get('number_of_seasons'),
        "number_of_episodes": raw.get('number_of_episodes'),
        "seasons": seasons,
        "in_production": raw.get('in_production'),
        "next_episode": raw.get('next_episode_to_air'),
    }


@router.post("/cache/clear")
def clear_tmdb_cache():
    """清除推荐/流行列表的内存缓存"""
    logger.warning("/discover/cache/clear: 清除 TMDB/Trakt/AniList/豆瓣 列表缓存（用户主动）")
    _cache_clear()
    return {"ok": True}


_TITLE_EN_SCOPE = 'tmdb_title_en'
_TITLE_EN_TTL = 30 * 24 * 60 * 60  # 30 天


@router.get("/title-en")
def get_english_title(media_type: str, tmdb_id: int):
    """
    单条英文标题查询。前端在用户点"搜种子"时调用，避免列表渲染时为每条 30 项各打一次 TMDB。
    缓存 30 天（标题极少变）；命中缓存毫秒级返回。

    没有 TMDB API Key / 没有英文标题时返回 english_title=None，前端自行兜底。
    """
    if media_type not in ('movie', 'tv'):
        raise HTTPException(status_code=400, detail="media_type 必须是 movie / tv")
    if not tmdb_id:
        raise HTTPException(status_code=400, detail="缺少 tmdb_id")

    cache_key = f"{media_type}:{tmdb_id}"
    cached = _kv_get(_TITLE_EN_SCOPE, cache_key, ttl_seconds=_TITLE_EN_TTL)
    if isinstance(cached, dict) and 'english_title' in cached:
        return {"english_title": cached['english_title'], "cached": True}

    if not settings.tmdb_api_key:
        return {"english_title": None, "cached": False}

    from common.tmdb_client import TMDBClient
    client = TMDBClient(settings.tmdb_api_key, delay=0.0, language='en-US')
    en_title = client.get_english_title(media_type, tmdb_id)

    # 即使是 None 也写缓存，避免反复打 TMDB（缺英文翻译的条目存在）
    _kv_set(_TITLE_EN_SCOPE, cache_key, {"english_title": en_title})
    return {"english_title": en_title, "cached": False}


# IMDb→TMDB 临时失败的短 TTL（5 min）：TMDB 502 多为分钟级故障，30 天 None 会把临时态钉死
_TITLE_EN_FAIL_TTL = 5 * 60

# 同 imdb_id 的并发请求短路：第一个进来的查 TMDB，其它的等结果（threading lock 避开 storm）
import threading as _threading
_imdb_lookup_locks: Dict[str, _threading.Lock] = {}
_imdb_lookup_locks_guard = _threading.Lock()


def _get_imdb_lock(imdb_id: str) -> _threading.Lock:
    with _imdb_lookup_locks_guard:
        lk = _imdb_lookup_locks.get(imdb_id)
        if lk is None:
            lk = _threading.Lock()
            _imdb_lookup_locks[imdb_id] = lk
        return lk


@router.get("/title-en-by-imdb")
def get_english_title_by_imdb(imdb_id: str):
    """
    根据 IMDb ID → TMDB find → 英文标题。豆瓣条目专用：豆瓣页有 IMDb ID 但没 TMDB ID，
    搜种子时直接用豆瓣中文标题命中率极低。

    流程：/find/{imdb_id}?external_source=imdb_id → movie_results/tv_results 取第一条
        → get_english_title(media_type, tmdb_id)
    缓存策略：
      - 成功 / 真"TMDB 没收录"   → 30 天（_TITLE_EN_TTL）
      - 临时失败（TMDB 502 等）  → 5 分钟（_TITLE_EN_FAIL_TTL）
    并发：同 imdb_id 第一个走 TMDB，其它人等结果（防 storm）
    """
    if not imdb_id or not imdb_id.startswith('tt'):
        raise HTTPException(status_code=400, detail="imdb_id 必须以 tt 开头")

    cache_key = f"imdb:{imdb_id}"

    def _strip_internal(d: dict) -> dict:
        """剥掉 cache_store 注入的 _cached* 元字段 + transient_failure 标志 → 给前端干净 payload"""
        return {k: v for k, v in d.items() if not k.startswith('_') and k != 'transient_failure'}

    def _read_cached() -> Optional[Dict]:
        """读缓存：成功记录用 30 天 TTL；带 transient_failure 标志的失败记录用短 TTL（5 min）"""
        cached = _kv_get(_TITLE_EN_SCOPE, cache_key, ttl_seconds=_TITLE_EN_TTL)
        if not isinstance(cached, dict) or 'english_title' not in cached:
            return None
        if cached.get('transient_failure'):
            # cache_store.get_cached 自动注入 _cache_age_seconds（int 秒）
            age = cached.get('_cache_age_seconds') or 0
            if age > _TITLE_EN_FAIL_TTL:
                return None   # 临时失败已过短 TTL → 视为未命中，下面重新查
        return cached

    hit = _read_cached()
    if hit is not None:
        return {**_strip_internal(hit), "cached": True}

    if not settings.tmdb_api_key:
        return {"english_title": None, "tmdb_id": None, "media_type": None, "cached": False}

    # 并发短路：拿到锁的查 TMDB，其它人 block；锁释放后第二个再读一次缓存就直接命中
    lock = _get_imdb_lock(imdb_id)
    with lock:
        hit = _read_cached()
        if hit is not None:
            return {**_strip_internal(hit), "cached": True}

        from common.tmdb_client import TMDBClient
        client = TMDBClient(settings.tmdb_api_key, delay=0.0, language='en-US')
        find_data = client._request(f'/find/{imdb_id}', {'external_source': 'imdb_id'})

        # find_data is None → TMDB 502/503/网络炸（_request 已重试 3 次仍失败）→ 临时态，短缓存
        transient = (find_data is None)

        tmdb_id_resolved = None
        media_type = None
        if find_data:
            # 优先电影，其次剧集（豆瓣 IMDb 链接到 movie 的比例最高）
            for it in (find_data.get('movie_results') or []):
                tmdb_id_resolved = it.get('id')
                media_type = 'movie'
                break
            if not tmdb_id_resolved:
                for it in (find_data.get('tv_results') or []):
                    tmdb_id_resolved = it.get('id')
                    media_type = 'tv'
                    break

        en_title = None
        if tmdb_id_resolved and media_type:
            en_title = client.get_english_title(media_type, tmdb_id_resolved)
            # tmdb_id 拿到了但 translations 返回 None → 粗判仍是 TMDB 临时故障，短缓存
            if en_title is None and not transient:
                transient = True

        payload = {
            "english_title": en_title,
            "tmdb_id": tmdb_id_resolved,
            "media_type": media_type,
        }
        if transient:
            payload['transient_failure'] = True
            logger.info(f"TMDB find/translations 临时失败 imdb={imdb_id}，5 min 内短路")
        _kv_set(_TITLE_EN_SCOPE, cache_key, payload)
        return {**_strip_internal(payload), "cached": False}


# TMDB 风格映射缓存（id → 中文名）。/genre/movie/list 与 /genre/tv/list 各拉一次缓存 30 天。
# TMDB 把 movie 和 tv 分两套 genre id 命名空间，所以按 media_type 各存一份。
_TMDB_GENRE_SCOPE = 'tmdb_genre_map'
_TMDB_GENRE_TTL = 30 * 86400


def _get_tmdb_genre_map(media_type: str, lang: str) -> Dict[int, str]:
    """取 TMDB genre id → name 映射，命中缓存就毫秒级。lang 决定返回中文名 / 英文名。"""
    if media_type not in ('movie', 'tv'):
        return {}
    key = f"{media_type}:{lang}"
    cached = _kv_get(_TMDB_GENRE_SCOPE, key, ttl_seconds=_TMDB_GENRE_TTL)
    if isinstance(cached, dict) and cached:
        # JSON 反序列化后键变 str，转回 int
        try:
            return {int(k): v for k, v in cached.items()}
        except (ValueError, TypeError):
            pass
    if not settings.tmdb_api_key:
        return {}
    try:
        from common.tmdb_client import TMDBClient
        client = TMDBClient(settings.tmdb_api_key, delay=0.0, language=lang)
        data = client._request(f'/genre/{media_type}/list', {'language': lang})
        if not data:
            return {}
        gmap = {int(g['id']): g['name'] for g in (data.get('genres') or []) if g.get('id')}
        _kv_set(_TMDB_GENRE_SCOPE, key, gmap)
        return gmap
    except Exception as e:
        logger.warning(f"TMDB genre 映射拉取失败 {media_type}: {e}")
        return {}


def _normalize_tmdb(items: List[dict], default_type: Optional[str] = None) -> List[dict]:
    """把 TMDB 多种返回结构统一成一个简化格式给前端用。

    L3 读 + 写：先 batch 查实体表
      - fresh 命中 → 用 L3 字段覆盖（detail 写过的字段更全），不再 upsert
      - stale/miss → 走 normalize 上游字段 + upsert L3
    """
    lang = _tmdb_scrape_lang() or 'zh-CN'
    movie_gmap = _get_tmdb_genre_map('movie', lang)
    tv_gmap = _get_tmdb_genre_map('tv', lang)

    # 批量查 L3
    tmdb_ids = [it.get('id') for it in items if it.get('id')]
    l3_keys = [('tmdb', str(tid)) for tid in tmdb_ids]
    l3_rows = metadata_store.get_batch(l3_keys) if l3_keys else {}

    # stale 行批量入队 refresh（不阻塞 list 响应）
    try:
        from web.backend.services.metadata_maintenance import enqueue_refresh as _enq_refresh
        for (src, sid), row in l3_rows.items():
            if metadata_store.is_stale(row):
                _enq_refresh(src, sid, hint={'media_type': row.media_type or 'movie'})
    except Exception:
        pass

    out = []
    for it in items:
        tid = it.get('id')
        l3_row = l3_rows.get(('tmdb', str(tid))) if tid else None
        l3_fresh = l3_row is not None and not metadata_store.is_stale(l3_row)

        if l3_fresh:
            # L3 fresh 命中：用 L3 字段（更稳定的翻译 + 详情写过的更全字段）
            # 前端展示标题：按显示语言配置挑（非豆瓣 → display_language 默认 'en'）
            ext = l3_row.ext or {}
            display_title = _display_title_for(l3_row) or it.get('title') or it.get('name')
            norm = {
                "tmdb_id": tid,
                "media_type": l3_row.media_type or (it.get('media_type') or default_type or 'movie'),
                "title": display_title,                    # 给前端的展示标题（按 display_language）
                "title_en": l3_row.title,                  # 英文 title（种子搜索用，永远英文）
                "original_title": l3_row.original_title or it.get('original_title') or it.get('original_name'),
                "original_language": ext.get('original_language') or it.get('original_language'),
                "overview": ext.get('overview') or it.get('overview'),
                "release_date": l3_row.release_date or it.get('release_date') or it.get('first_air_date'),
                "vote_average": ext.get('vote_average') if ext.get('vote_average') is not None else it.get('vote_average'),
                "popularity": ext.get('popularity') if ext.get('popularity') is not None else it.get('popularity'),
                "poster_url": l3_row.poster_url or (
                    f"https://image.tmdb.org/t/p/w342{it.get('poster_path')}" if it.get('poster_path') else None
                ),
                "genres": ext.get('genres') or [],
            }
        else:
            # L3 miss / stale：走原 normalize + upsert
            mt = it.get('media_type') or default_type or 'movie'
            upstream_title = it.get('title') or it.get('name')
            original_title = it.get('original_title') or it.get('original_name')
            orig_lang = it.get('original_language')
            date = it.get('release_date') or it.get('first_air_date')
            poster = it.get('poster_path')
            gmap = tv_gmap if mt == 'tv' else movie_gmap
            genre_names = [gmap[g] for g in (it.get('genre_ids') or []) if g in gmap]

            # list 阶段只有一种语言（上游响应按 scrape_language）；另一语言留待 detail 补
            scrape_is_zh = (_tmdb_scrape_lang() or 'en').lower().startswith('zh')
            if scrape_is_zh:
                title_zh = upstream_title
                title_en = original_title if orig_lang == 'en' else None
            else:
                title_en = upstream_title
                title_zh = None    # 等 detail 用 translations 补
            # 前端展示标题：按显示语言优先
            disp_is_zh = (settings.metadata_display_language or 'en').lower().startswith('zh')
            display = (title_zh or title_en) if disp_is_zh else (title_en or title_zh)

            norm = {
                "tmdb_id": tid,
                "media_type": mt,
                "title": display or upstream_title,  # 前端展示用
                "title_en": title_en,                # 种子搜索锚点
                "original_title": original_title,
                "original_language": orig_lang,
                "overview": it.get('overview'),
                "release_date": date,
                "vote_average": it.get('vote_average'),
                "popularity": it.get('popularity'),
                "poster_url": f"https://image.tmdb.org/t/p/w342{poster}" if poster else None,
                "genres": genre_names,
            }
            # 把要写 L3 的"raw 两种语言版本"塞进去给 _upsert_tmdb_list_item 用
            norm["_title_en_for_l3"] = title_en
            norm["_title_zh_for_l3"] = title_zh
            _upsert_tmdb_list_item(norm)
        out.append(norm)
    return out


def _upsert_tmdb_list_item(norm: dict) -> None:
    """TMDB list item 落 L3 实体表。失败不影响主流程。

    norm 里 _title_en_for_l3 / _title_zh_for_l3 是 list 阶段能拿到的两种语言。
    detail 走 _upsert_tmdb_detail 时会补全另一种语言。
    """
    tmdb_id = norm.get('tmdb_id')
    if not tmdb_id:
        return
    try:
        date = norm.get('release_date') or ''
        year = None
        if date and len(date) >= 4 and date[:4].isdigit():
            year = int(date[:4])
        ext = {
            'overview': norm.get('overview'),
            'original_language': norm.get('original_language'),
            'genres': norm.get('genres') or [],
            'popularity': norm.get('popularity'),
            'vote_average': norm.get('vote_average'),
        }
        public = {
            'media_type': norm.get('media_type'),
            'title': norm.get('_title_en_for_l3'),      # 英文 title（可能 None）
            'title_zh': norm.get('_title_zh_for_l3'),   # 中文 title（可能 None）
            'original_title': norm.get('original_title'),
            'year': year,
            'release_date': date or None,
            'poster_url': norm.get('poster_url'),
        }
        metadata_store.upsert(
            source='tmdb', source_id=str(tmdb_id),
            public=public,
            ext=ext,
            bridge_ids={'tmdb_id': int(tmdb_id)},
        )
    except Exception:
        logger.exception(f"L3 upsert tmdb list 失败 tmdb_id={tmdb_id}")


# ============================================================================
# Trakt 推荐
# ============================================================================

# Trakt 海报通过 tmdb_id 反查；这里按 tmdb_id 单独缓存 7 天，避免同一作品反复打 TMDB
_TRAKT_POSTER_SCOPE = 'trakt_poster'
_TRAKT_POSTER_TTL = 7 * 86400


def _fetch_trakt_poster(tmdb, tmdb_id: int, media_type: str) -> Optional[str]:
    """单条 tmdb_id → poster_url（带 DB 缓存）。命中缓存则零调用。"""
    cache_key = f"{media_type}:{tmdb_id}"
    cached = _kv_get(_TRAKT_POSTER_SCOPE, cache_key, ttl_seconds=_TRAKT_POSTER_TTL)
    if isinstance(cached, dict) and 'url' in cached:
        return cached['url'] or None
    try:
        if media_type == 'tv':
            path = tmdb.get_tv_poster_path(int(tmdb_id))
        else:
            path = tmdb.get_movie_poster_path(int(tmdb_id))
    except Exception:
        return None
    url = f"https://image.tmdb.org/t/p/w342{path}" if path else None
    # 缓存：拉到 url 写实际值；拉到空也写空（避免反复试）
    _kv_set(_TRAKT_POSTER_SCOPE, cache_key, {'url': url or ''})
    return url


# Trakt / AniList 后端固定页大小：跟 viewport 无关，让缓存键只跟 page 走
# 缓存命中率最大化；前端按 wanted 切片显示
_TRAKT_PAGE_SIZE = 30
_ANILIST_PAGE_SIZE = 30


@router.get("/trakt")
def get_trakt(
    media_type: str = "movie",
    category: str = "trending",
    page: int = 1,
    refresh: bool = False,
):
    """
    Trakt 推荐：实时观看活动信号（互补 TMDB 元数据流行度）。

    media_type: movie | tv
    category:   trending（热门） | anticipated（期待） | popular（流行） | watched_weekly（本周观看榜）
    page: 分页（无限滚动用），page size 固定 30。前端 wanted < 30 时自己切片显示。
    """
    cfg = settings.trakt
    if not cfg.enabled or not cfg.client_id:
        raise HTTPException(status_code=400, detail="Trakt 未配置 client_id（设置 → 第三方推荐源）")
    if media_type not in ('movie', 'tv'):
        raise HTTPException(status_code=400, detail="media_type 必须是 movie / tv")
    if category not in ('trending', 'anticipated', 'popular', 'watched_weekly'):
        raise HTTPException(status_code=400, detail=f"非法 category: {category}")
    page = max(1, min(20, int(page)))

    # 缓存 key 只含 page —— 同一页的 30 条数据复用，无论用户 viewport 怎么变
    cache_key = f"{media_type}:{category}:p{page}"
    ttl = max(1, cfg.cache_minutes) * 60
    if not refresh:
        cached = _strip_cache_meta(_kv_get(_TRAKT_SCOPE, cache_key, ttl_seconds=ttl))
        if cached is not None:
            return {**cached, "cached": True}

    from common.trakt_client import TraktClient
    client = TraktClient(
        client_id=cfg.client_id,
        base_url=cfg.base_url,
        request_delay=cfg.request_delay,
        timeout=cfg.timeout_seconds,
    )

    try:
        if category == 'trending':
            items = client.trending(media_type, page=page, limit=_TRAKT_PAGE_SIZE)
        elif category == 'anticipated':
            items = client.anticipated(media_type, page=page, limit=_TRAKT_PAGE_SIZE)
        elif category == 'popular':
            items = client.popular(media_type, page=page, limit=_TRAKT_PAGE_SIZE)
        else:  # watched_weekly
            items = client.watched(media_type, period='weekly', page=page, limit=_TRAKT_PAGE_SIZE)
    except Exception as e:
        logger.warning(f"Trakt {media_type}/{category} 上游异常: {e}")
        items = []
    if not items:
        fallback = _stale_fallback(_TRAKT_SCOPE, cache_key, max(1, cfg.cache_minutes) * 60)
        if fallback is not None:
            return fallback

    # 给有 tmdb_id 的项补 TMDB 海报：5 路并发 + 每张 tmdb_id 单独 7 天缓存
    out = [it.to_dict() for it in items]
    if settings.tmdb_api_key and out:
        try:
            from common.tmdb_client import TMDBClient
            from concurrent.futures import ThreadPoolExecutor
            tmdb = TMDBClient(settings.tmdb_api_key, delay=0.0, language=_tmdb_scrape_lang())
            poster_targets = [(d, d.get('tmdb_id'), d.get('media_type') or media_type)
                              for d in out if d.get('tmdb_id')]
            if poster_targets:
                with ThreadPoolExecutor(max_workers=5) as ex:
                    futures = {
                        ex.submit(_fetch_trakt_poster, tmdb, tid, mt): d
                        for (d, tid, mt) in poster_targets
                    }
                    for fut, d in futures.items():
                        try:
                            url = fut.result(timeout=15)
                            if url:
                                d['poster_url'] = url
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"trakt 拼海报失败: {e}")

    result = {
        "count": len(out), "items": out,
        "media_type": media_type, "category": category,
        "page": page, "limit": _TRAKT_PAGE_SIZE,
        "has_more": len(out) >= _TRAKT_PAGE_SIZE,
    }
    if out:
        _kv_set(_TRAKT_SCOPE, cache_key, result)
    return {**result, "cached": False}


# ============================================================================
# AniList 推荐（anime 专用）
# ============================================================================

@router.get("/anilist")
def get_anilist(
    category: str = "trending",
    page: int = 1,
    refresh: bool = False,
):
    """
    AniList 番剧推荐：trending / popular / top_rated / current_season。
    无 API key（公开 GraphQL）。page size 固定 30；前端按 wanted 切片显示。
    """
    cfg = settings.anilist
    if not cfg.enabled:
        raise HTTPException(status_code=400, detail="AniList 已在配置中关闭")
    if category not in ('trending', 'popular', 'top_rated', 'current_season'):
        raise HTTPException(status_code=400, detail=f"非法 category: {category}")
    page = max(1, min(20, int(page)))

    cache_key = f"{category}:p{page}"
    ttl = max(1, cfg.cache_minutes) * 60
    if not refresh:
        cached = _strip_cache_meta(_kv_get(_ANILIST_SCOPE, cache_key, ttl_seconds=ttl))
        if cached is not None:
            return {**cached, "cached": True}

    from common.anilist_client import AniListClient
    client = AniListClient(
        base_url=cfg.base_url,
        request_delay=cfg.request_delay,
        timeout=cfg.timeout_seconds,
    )

    try:
        if category == 'trending':
            items = client.trending(page=page, limit=_ANILIST_PAGE_SIZE)
        elif category == 'popular':
            items = client.popular(page=page, limit=_ANILIST_PAGE_SIZE)
        elif category == 'top_rated':
            items = client.top_rated(page=page, limit=_ANILIST_PAGE_SIZE)
        else:  # current_season
            from datetime import datetime as _dt
            now = _dt.utcnow()
            season_map = {1: 'WINTER', 2: 'SPRING', 3: 'SUMMER', 4: 'FALL'}
            season = season_map[((now.month - 1) // 3) + 1]
        items = client.current_season(season=season, year=now.year,
                                       page=page, limit=_ANILIST_PAGE_SIZE)
    except Exception as e:
        logger.warning(f"AniList {category} 上游异常: {e}")
        items = []
    if not items:
        fallback = _stale_fallback(_ANILIST_SCOPE, cache_key, ttl)
        if fallback is not None:
            return fallback

    out = [it.to_dict() for it in items]
    # L3 读 + 写：fresh 命中用 L3 字段覆盖；miss/stale 才 upsert
    anilist_ids = [d.get('anilist_id') for d in out if d.get('anilist_id')]
    l3_keys = [('anilist', str(aid)) for aid in anilist_ids]
    l3_rows = metadata_store.get_batch(l3_keys) if l3_keys else {}
    # stale 行批量入队 refresh
    try:
        from web.backend.services.metadata_maintenance import enqueue_refresh as _enq_refresh
        for (src, sid), row in l3_rows.items():
            if metadata_store.is_stale(row):
                _enq_refresh(src, sid)
    except Exception:
        pass
    for d in out:
        aid = d.get('anilist_id')
        row = l3_rows.get(('anilist', str(aid))) if aid else None
        if row is not None and not metadata_store.is_stale(row):
            # 用 L3 字段覆盖（更稳定）
            ext = row.ext or {}
            # row.title 是英文（title_english），title_zh AniList 通常 None
            if row.title: d['title_english'] = d.get('title_english') or row.title
            if row.title_zh: d['title_zh'] = row.title_zh
            # 显示标题（按 display_language；AniList 无中文时 fallback 到英文）
            d['title_display'] = _display_title_for(row)
            if ext.get('title_romaji'): d['title_romaji'] = ext['title_romaji']
            if ext.get('title_native'): d['title_native'] = ext['title_native']
            if row.poster_url: d['cover_image'] = row.poster_url
            if ext.get('description'): d['description'] = ext['description']
            if ext.get('genres'): d['genres'] = ext['genres']
            if ext.get('studios'): d['studios'] = ext['studios']
            if ext.get('average_score') is not None: d['average_score'] = ext['average_score']
            if ext.get('popularity') is not None: d['popularity'] = ext['popularity']
            if ext.get('episodes') is not None: d['episodes'] = ext['episodes']
            if ext.get('duration') is not None: d['duration'] = ext['duration']
            if ext.get('format'): d['format'] = ext['format']
            if ext.get('status'): d['status'] = ext['status']
        else:
            _upsert_anilist_item(d)
    result = {
        "count": len(out), "items": out,
        "category": category, "page": page, "limit": _ANILIST_PAGE_SIZE,
        "has_more": len(out) >= _ANILIST_PAGE_SIZE,
    }
    if out:
        _kv_set(_ANILIST_SCOPE, cache_key, result)
    return {**result, "cached": False}


def _upsert_anilist_item(d: dict) -> None:
    """AniList item dict → L3 upsert。是 list 也是简化 detail（AniListItem 字段相同）。

    title 字段语义：
      DB.title    = title_english (英文)
      DB.title_zh = NULL（AniList 不提供官方中文翻译）
      original_title = title_native (日文)
    """
    anilist_id = d.get('anilist_id')
    if not anilist_id:
        return
    try:
        title_en = d.get('title_english') or d.get('title_romaji')
        original_title = d.get('title_native') or d.get('title_romaji')
        year = d.get('season_year')
        # ext：AniList 独有 + 非公共字段
        ext = {
            'title_romaji': d.get('title_romaji'),
            'title_native': d.get('title_native'),
            'description': d.get('description'),
            'banner_image': d.get('banner_image'),
            'season': d.get('season'),
            'season_year': d.get('season_year'),
            'episodes': d.get('episodes'),
            'duration': d.get('duration'),
            'format': d.get('format'),
            'status': d.get('status'),
            'average_score': d.get('average_score'),
            'popularity': d.get('popularity'),
            'genres': d.get('genres') or [],
            'studios': d.get('studios') or [],
        }
        ext = {k: v for k, v in ext.items() if v is not None and v != []}
        bridge_ids = {'anilist_id': int(anilist_id)}
        if d.get('tmdb_id'):
            # AniList → TMDB 反查（已决：启用跨 source 桥接）
            bridge_ids['tmdb_id'] = int(d['tmdb_id'])
        metadata_store.upsert(
            source='anilist', source_id=str(anilist_id),
            public={
                'media_type': 'anime',
                'title': title_en,             # 英文
                'title_zh': None,              # AniList 无中文（前端 fallback 用 title）
                'original_title': original_title,
                'year': year,
                'poster_url': d.get('cover_image'),
            },
            ext=ext,
            bridge_ids=bridge_ids,
        )
    except Exception:
        logger.exception(f"L3 upsert anilist 失败 anilist_id={anilist_id}")


# ============================================================================
# 豆瓣片单
# ============================================================================

# ---- 豆瓣列表项的 detail 懒填充 + 后台预热 ----
# 列表页（特别是 /coming）只有标题/类型/想看数，无海报无评分。
# 每条 douban_id 进 fetch_subject_summary 可以拿到全套（海报、评分、导演、imdb_id、summary），
# 这些已在 _DOUBAN_DETAIL_SCOPE 里 30 天缓存（用户每点一次"简介"/"搜种子"就顺手填一条）。
# 这里做两件事：
#   1) 列表响应前合并已缓存的 detail（idempotent，重复调用安全）
#   2) 没缓存过的 douban_id 推到全局队列，单 worker 线程串行消费（写入 detail 缓存）
# 下次刷新或几分钟后重开同一列表，海报/评分就齐了。
#
# 用"单消费者 + queue"而不是每次起新线程：
#   多线程并发会让 douban 同时接到 N 倍 RPS（每个 client 独立 rate_limit），
#   触发反爬 → 大批量请求拿到 PoW 挑战页 → 实际成功的没几个。
#   单线程消费保证全局速率严格 = douban_request_delay
import queue as _queue

_douban_prefetch_queue: '_queue.Queue[str]' = _queue.Queue()
_douban_prefetch_seen: set = set()         # 全局已入队/在跑的 douban_id（避免重复入队）
_douban_prefetch_seen_lock = _threading.Lock()
_douban_prefetch_worker_started = False    # 单 worker 线程的启动 flag


def _enrich_items_from_detail_cache(items: List[Dict]) -> List[str]:
    """合并 douban_detail 缓存到 list items；返回还没缓存的 douban_id 列表。
    仅在 item 自身字段缺失时用 detail 的值（保留列表语义优先级）。"""
    uncached: List[str] = []
    for it in items:
        did = str(it.get('douban_id') or '').strip()
        if not did:
            continue
        cached = _kv_get(_DOUBAN_DETAIL_SCOPE, did, ttl_seconds=_DOUBAN_DETAIL_TTL)
        if not isinstance(cached, dict) or not cached.get('summary'):
            uncached.append(did)
            continue
        if not it.get('poster_url') and cached.get('poster_url'):
            it['poster_url'] = cached['poster_url']
        if it.get('rating') is None and cached.get('rating') is not None:
            it['rating'] = cached['rating']
        # /coming 用 votes 存"想看人数"（带 votes_label='想看'）；detail 的 votes 是"评价"
        # 语义不同 → 仅在没有 votes_label 时（即非 /coming）才用 detail 的 votes 兜底
        if not it.get('votes_label') and it.get('votes') is None and cached.get('votes') is not None:
            it['votes'] = cached['votes']
        if not it.get('director') and cached.get('director'):
            it['director'] = cached['director']
        if not it.get('imdb_id') and cached.get('imdb_id'):
            it['imdb_id'] = cached['imdb_id']
    return uncached


def _douban_prefetch_consumer():
    """全局单消费者：保守抓豆瓣 detail，含熔断机制
      - 每条间隔 douban.worker_delay（默认 30s）
      - 连续失败 worker_max_failures 次 → 进 cooldown，停 worker_cooldown_seconds（默认 1h）
      - 冷却期内不消费队列（堆积无所谓，反正是异步预取）
    """
    import time as _time
    from common.douban_client import DoubanClient
    # 用更长 delay 避免反爬：跟前台请求 client 分开，独立 rate_limit
    client = DoubanClient(
        user_agent=settings.douban_user_agent,
        delay=settings.douban_worker_delay,
    )
    max_failures = max(1, int(settings.douban_worker_max_failures))
    cooldown_seconds = max(60, int(settings.douban_worker_cooldown_seconds))
    consecutive_failures = 0
    cooldown_until = 0.0

    logger.info(
        f"豆瓣预取 worker 启动：delay={settings.douban_worker_delay}s "
        f"max_failures={max_failures} cooldown={cooldown_seconds}s"
    )
    while True:
        # 熔断中？分段睡（避免阻塞太久无法响应进程退出）
        now = _time.time()
        if now < cooldown_until:
            wait = cooldown_until - now
            logger.warning(
                f"豆瓣预取 worker 冷却中，剩余 {wait:.0f}s（连续失败 {max_failures} 次后启动）"
            )
            _time.sleep(min(wait, 60))
            continue

        try:
            did = _douban_prefetch_queue.get()
        except Exception as e:
            logger.warning(f"豆瓣预取队列读取异常：{e}")
            continue
        try:
            # 再读一次缓存：可能用户主动点"简介"/"搜种子"提前填上了
            cached = _kv_get(_DOUBAN_DETAIL_SCOPE, did, ttl_seconds=_DOUBAN_DETAIL_TTL)
            if isinstance(cached, dict) and cached.get('summary'):
                # 命中缓存不算失败，不增减计数
                continue
            detail = client.fetch_subject_summary(did)
            if detail and detail.get('summary'):
                # 成功：清零计数
                consecutive_failures = 0
                _kv_set(_DOUBAN_DETAIL_SCOPE, did, detail)
                try:
                    _upsert_douban_detail(detail)
                except Exception:
                    logger.exception(f"L3 upsert douban (prefetch) 失败 {did}")
                if detail.get('imdb_id'):
                    try:
                        from web.backend.api.ratings import enqueue_mdblist_by_imdb
                        enqueue_mdblist_by_imdb(detail['imdb_id'], 'movie')
                    except Exception:
                        logger.exception(f"MDB List 入队（豆瓣 prefetch）失败 {did}")
                logger.debug(f"豆瓣详情预取写入缓存 {did}")
            else:
                # 失败：计数 +1，达阈值进熔断
                consecutive_failures += 1
                logger.warning(
                    f"豆瓣预取 {did} 失败（连续 {consecutive_failures}/{max_failures}）"
                )
                if consecutive_failures >= max_failures:
                    cooldown_until = _time.time() + cooldown_seconds
                    logger.error(
                        f"豆瓣预取 worker 进入冷却：连续 {consecutive_failures} 次失败，"
                        f"暂停 {cooldown_seconds}s"
                    )
                    consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            logger.warning(f"豆瓣详情预取 {did} 异常: {e}（连续 {consecutive_failures}/{max_failures}）")
            if consecutive_failures >= max_failures:
                cooldown_until = _time.time() + cooldown_seconds
                logger.error(
                    f"豆瓣预取 worker 进入冷却（异常路径），暂停 {cooldown_seconds}s"
                )
                consecutive_failures = 0
        finally:
            with _douban_prefetch_seen_lock:
                _douban_prefetch_seen.discard(did)
            _douban_prefetch_queue.task_done()


def _ensure_prefetch_worker():
    """单次启动消费者线程，模块级。线程是 daemon，进程退出时自动收。"""
    global _douban_prefetch_worker_started
    if _douban_prefetch_worker_started:
        return
    with _douban_prefetch_seen_lock:
        if _douban_prefetch_worker_started:
            return
        _douban_prefetch_worker_started = True
        _threading.Thread(
            target=_douban_prefetch_consumer,
            daemon=True,
            name='douban-prefetch-consumer',
        ).start()


def _kick_douban_prefetch(douban_ids: List[str]):
    """把 douban_ids 推到全局预取队列，已在队中的不重复推。"""
    if not douban_ids:
        return
    _ensure_prefetch_worker()
    with _douban_prefetch_seen_lock:
        new_ids = [d for d in douban_ids if d not in _douban_prefetch_seen]
        _douban_prefetch_seen.update(new_ids)
    for d in new_ids:
        _douban_prefetch_queue.put(d)
    if new_ids:
        logger.info(f"豆瓣详情预取入队 {len(new_ids)} 条（队列总深度={_douban_prefetch_queue.qsize()}）")


@router.get("/douban-lists")
def get_douban_lists(
    doulist_id: Optional[str] = None,
    page: int = 1,
    refresh: bool = False,
):
    """
    豆瓣 doulist 精选片单。
    不传 doulist_id → 返回 settings.douban_lists.lists 的元数据列表（前端先选）。
    传 doulist_id → 拉该片单第 page 页。
    """
    cfg = settings.douban_lists
    if not cfg.enabled:
        raise HTTPException(status_code=400, detail="豆瓣片单已在配置中关闭")

    # 不传 ID 时返回片单白名单（让前端做"片单选择 → 拉详情"两步流程）
    if not doulist_id:
        return {"lists": cfg.lists}

    page = max(1, min(20, int(page)))
    page_size = 25  # 豆瓣 doulist 固定一页 25 条
    cache_key = f"{doulist_id}:p{page}"
    ttl = max(1, cfg.cache_days) * 86400      # 默认 3 天，反爬严 + 内容变化慢

    def _finalize(result_obj: Dict, from_cache: bool) -> Dict:
        """统一收尾：合并 detail 缓存 + 起后台预取 + 加 cached 标志"""
        uncached = _enrich_items_from_detail_cache(result_obj.get('items', []))
        if uncached:
            _kick_douban_prefetch(uncached)
        return {**result_obj, "cached": from_cache}

    if not refresh:
        cached = _strip_cache_meta(_kv_get(_DOUBAN_SCOPE, cache_key, ttl_seconds=ttl))
        if cached is not None:
            # 缓存命中也走一遍 enrich —— 同一列表第二次访问时，期间预取成果就反映出来
            return _finalize(cached, from_cache=True)

    from common.douban_client import DoubanClient
    # 复用 douban 段的 user_agent + delay 配置
    client = DoubanClient(
        user_agent=settings.douban_user_agent,
        delay=settings.douban_request_delay,
    )
    start = (page - 1) * page_size
    # 按 doulist_id 形态分派：
    #   纯数字       → 经典 /doulist/<id>/ 片单
    #   chart        → /chart 电影排行榜
    #   nowplaying   → /cinema/nowplaying/ 的 #nowplaying section（~19 条，字段全）
    #   upcoming     → /coming 独立页（~30+ 条，无海报评分 → 靠 enrich + 后台预热补全）
    # 这样老配置完全兼容；新增的 source 在配置里用字面字符串作为"id"
    if doulist_id == 'chart':
        items = client.fetch_chart(start=start, limit=page_size)
    elif doulist_id == 'nowplaying':
        items = client.fetch_nowplaying(start=start, limit=page_size)
    elif doulist_id == 'upcoming':
        # /coming 单页 ~30-50 条全在一张 HTML 上，没有真正的分页（start>0 直接返空）
        # 放大 limit 把整页捞下来，has_more 字段会自然为 false（page_size 阈值）
        items = client.fetch_coming(start=start, limit=100)
    else:
        items = client.fetch_doulist(doulist_id, start=start, limit=page_size)

    # 找到对应配置项的元信息（name / media_type）
    meta = next((d for d in cfg.lists if str(d.get('doulist_id')) == str(doulist_id)), None)
    name = (meta or {}).get('name') or f'doulist {doulist_id}'
    media_type = (meta or {}).get('media_type') or 'movie'

    result = {
        "count": len(items),
        "items": items,
        "doulist_id": doulist_id,
        "name": name,
        "media_type": media_type,
        "page": page,
        "limit": page_size,
        "has_more": len(items) >= page_size,
    }
    # 上游拉到数据 → 正常写缓存
    if items:
        _kv_set(_DOUBAN_SCOPE, cache_key, result)
        return _finalize(result, from_cache=False)

    # 上游拉空（反爬 403 / 维护页 / 真的没数据）→ 降级用过期 KV 兜底
    # 比给用户白屏强；同时不污染 KV（不刷新 cached_at）
    stale = _kv_get(_DOUBAN_SCOPE, cache_key, ttl_seconds=ttl, allow_stale=True)
    if stale is not None:
        logger.info(
            f"豆瓣 doulist {doulist_id} 上游拉空，降级用过期缓存"
            f"（{stale.get('_cache_age_seconds')}s 前，{len(stale.get('items') or [])} 条）"
        )
        # 把 stale meta 字段从 result_obj 剥掉再走 finalize
        stale_clean = _strip_cache_meta(stale)
        return _finalize(stale_clean, from_cache=True)

    return _finalize(result, from_cache=False)


_ANILIST_DETAIL_SCOPE = 'anilist_detail'
_ANILIST_DETAIL_TTL = 7 * 86400  # 7 天：番剧详情（评分/集数）会变，但缓存内重复访问无忧


@router.get("/anilist-detail")
def get_anilist_detail(anilist_id: int, refresh: bool = False):
    """AniList 单条番剧详情。前端 AniListDetail.vue 用。"""
    cfg = settings.anilist
    if not cfg.enabled:
        raise HTTPException(status_code=400, detail="AniList 模块已关闭")
    if not anilist_id:
        raise HTTPException(status_code=400, detail="缺少 anilist_id")

    cache_key = str(anilist_id)
    if not refresh:
        cached = _strip_cache_meta(_kv_get(_ANILIST_DETAIL_SCOPE, cache_key, ttl_seconds=_ANILIST_DETAIL_TTL))
        if cached is not None:
            return {**cached, "cached": True}
        # L3 命中 fresh 直接返回（跳过 AniList GraphQL）
        l3_row = metadata_store.get_by_source('anilist', str(anilist_id))
        if l3_row is not None:
            if metadata_store.is_stale(l3_row):
                try:
                    from web.backend.services.metadata_maintenance import enqueue_refresh
                    enqueue_refresh('anilist', str(anilist_id))
                except Exception:
                    pass
            else:
                full = metadata_store.row_to_full_dict(l3_row)
                return {**full, "cached": True, "_from_metadata": True}

    from common.anilist_client import AniListClient
    client = AniListClient(
        base_url=cfg.base_url,
        request_delay=cfg.request_delay,
        timeout=cfg.timeout_seconds,
    )
    try:
        detail = client.detail(int(anilist_id))
    except Exception as e:
        logger.warning(f"AniList detail {anilist_id} 上游异常: {e}")
        detail = None
    if detail:
        _kv_set(_ANILIST_DETAIL_SCOPE, cache_key, detail)
        _upsert_anilist_detail(detail)
        return {**detail, "cached": False}
    # 上游失败 → stale 兜底
    fallback = _stale_fallback(_ANILIST_DETAIL_SCOPE, cache_key, _ANILIST_DETAIL_TTL)
    if fallback is not None:
        return fallback
    raise HTTPException(status_code=404, detail="AniList 中未找到该条目")


def _upsert_anilist_detail(d: dict) -> None:
    """AniList detail dict → L3 upsert（含 characters/relations）。

    title 字段语义同 _upsert_anilist_item：title = title_english, title_zh = NULL。
    """
    anilist_id = d.get('anilist_id') or d.get('id')
    if not anilist_id:
        return
    try:
        title_en = d.get('title_english') or d.get('title_romaji')
        original_title = d.get('title_native') or d.get('title_romaji')
        year = d.get('season_year')
        ext = {
            'title_romaji': d.get('title_romaji'),
            'title_native': d.get('title_native'),
            'idMal': d.get('idMal') or d.get('id_mal'),
            'description': d.get('description'),
            'banner_image': d.get('banner_image'),
            'season': d.get('season'),
            'season_year': d.get('season_year'),
            'start_date': d.get('start_date'),
            'end_date': d.get('end_date'),
            'episodes': d.get('episodes'),
            'duration': d.get('duration'),
            'format': d.get('format'),
            'status': d.get('status'),
            'source': d.get('source'),
            'country_of_origin': d.get('country_of_origin'),
            'average_score': d.get('average_score'),
            'mean_score': d.get('mean_score'),
            'popularity': d.get('popularity'),
            'favourites': d.get('favourites'),
            'genres': d.get('genres') or [],
            'tags': d.get('tags') or [],
            'studios': d.get('studios') or [],
            'trailer': d.get('trailer'),
            'external_links': d.get('external_links') or [],
            'characters': d.get('characters') or [],
            'relations': d.get('relations') or [],
        }
        ext = {k: v for k, v in ext.items() if v is not None and v != []}
        bridge_ids = {'anilist_id': int(anilist_id)}
        if d.get('tmdb_id'):
            bridge_ids['tmdb_id'] = int(d['tmdb_id'])
        metadata_store.upsert(
            source='anilist', source_id=str(anilist_id),
            public={
                'media_type': 'anime',
                'title': title_en,             # 英文
                'title_zh': None,              # AniList 无中文
                'original_title': original_title,
                'year': year,
                'poster_url': d.get('cover_image'),
            },
            ext=ext,
            bridge_ids=bridge_ids,
        )
    except Exception:
        logger.exception(f"L3 upsert anilist detail 失败 anilist_id={anilist_id}")


_DOUBAN_DETAIL_SCOPE = 'douban_detail'
_DOUBAN_DETAIL_TTL = 30 * 86400  # 30 天：豆瓣条目元信息变化极少


@router.get("/douban-detail")
def get_douban_detail(douban_id: str, refresh: bool = False):
    """
    豆瓣条目页元信息（剧情简介 + 演员 + 国家/类型/语言/IMDb 等）。
    用户在 doulist 列表点"简介"时调用 —— doulist 卡片只能爬到导演/类型/年份，正文要单独爬条目页。
    缓存 30 天；豆瓣反爬严 → 命中缓存几乎是必须的。
    """
    cfg = settings.douban_lists  # 复用 douban 段配置
    if not cfg.enabled:
        raise HTTPException(status_code=400, detail="豆瓣模块已关闭")
    if not douban_id or not str(douban_id).isdigit():
        raise HTTPException(status_code=400, detail="缺少或非法的 douban_id")

    cache_key = str(douban_id)
    if not refresh:
        cached = _strip_cache_meta(_kv_get(_DOUBAN_DETAIL_SCOPE, cache_key, ttl_seconds=_DOUBAN_DETAIL_TTL))
        if cached is not None:
            return {**cached, "cached": True}
        # L3 命中 fresh 直接返回（豆瓣反爬最严，命中价值最高）
        # 仅在 store_douban_full=true 时 L3 行才包含 summary 等正文字段
        l3_row = metadata_store.get_by_source('douban', str(douban_id))
        if l3_row is not None:
            if metadata_store.is_stale(l3_row):
                try:
                    from web.backend.services.metadata_maintenance import enqueue_refresh
                    enqueue_refresh('douban', str(douban_id))
                except Exception:
                    pass
            else:
                ext = l3_row.ext or {}
                # summary 是详情页核心字段；store_douban_full=false 时 L3 不存 summary，
                # 但事实字段（countries/genres）仍可用——也直接返回
                if ext.get('summary') or not settings.metadata_store_douban_full:
                    full = metadata_store.row_to_full_dict(l3_row)
                    return {**full, "cached": True, "_from_metadata": True}

    from common.douban_client import DoubanClient
    client = DoubanClient(
        user_agent=settings.douban_user_agent,
        delay=settings.douban_request_delay,
    )
    detail = client.fetch_subject_summary(str(douban_id))

    # 拉到正文才写缓存（避免反爬空结果占位）
    if detail and detail.get('summary'):
        _kv_set(_DOUBAN_DETAIL_SCOPE, cache_key, detail)
        _upsert_douban_detail(detail)
        return {**detail, "cached": False}

    # 上游失败 / 反爬 → 降级用过期 KV 兜底
    stale = _kv_get(_DOUBAN_DETAIL_SCOPE, cache_key, ttl_seconds=_DOUBAN_DETAIL_TTL, allow_stale=True)
    if stale is not None:
        logger.info(
            f"豆瓣 detail {douban_id} 上游失败，降级过期缓存"
            f"（{stale.get('_cache_age_seconds')}s 前）"
        )
        return {**_strip_cache_meta(stale), "cached": True}

    raise HTTPException(status_code=404, detail="豆瓣条目页拉取失败（可能反爬或条目不存在）")


def _split_douban_title(raw: str) -> tuple:
    """
    把豆瓣条目页 title 拆成 (中文部分, 英文部分)。
    示例：
      "蝙蝠侠：黑暗骑士 The Dark Knight" → ("蝙蝠侠：黑暗骑士", "The Dark Knight")
      "让子弹飞"                        → ("让子弹飞", "")
      "The Matrix"                       → ("", "The Matrix")
    策略：从首个 ASCII 字母位置切；前段视为中文，后段视为英文。
    """
    if not raw:
        return ('', '')
    import re
    m = re.search(r'[A-Za-z]', raw)
    if not m:
        return (raw.strip(), '')
    cut = m.start()
    if cut == 0:
        return ('', raw.strip())
    return (raw[:cut].strip(), raw[cut:].strip())


def _upsert_douban_detail(detail: dict) -> None:
    """
    豆瓣 detail → L3 upsert。按 settings.metadata_store_douban_full 裁剪字段。
    full=true：含 summary/cast/director/poster_url（默认）
    full=false：只存事实（countries/languages/genres/duration）
    """
    douban_id = str(detail.get('douban_id') or '')
    if not douban_id:
        return
    try:
        full_mode = bool(settings.metadata_store_douban_full)
        # ext 公共部分（事实，永远存）
        ext = {
            'countries': detail.get('countries') or [],
            'languages': detail.get('languages') or [],
            'genres': detail.get('genres') or [],
            'duration': detail.get('duration'),
        }
        if full_mode:
            # 全字段模式：加上 summary / director / cast / 评分副本
            ext.update({
                'summary': detail.get('summary'),
                'director': detail.get('director'),
                'cast': detail.get('cast') or [],   # 字符串数组（已简化）
                'rating': detail.get('rating'),     # 副本：SOT 在 media_ratings
                'votes': detail.get('votes'),
            })
        ext = {k: v for k, v in ext.items() if v is not None and v != []}

        # 豆瓣 detail.title 通常是 "<中文> <英文>" 拼接（如 "蝙蝠侠：黑暗骑士 The Dark Knight"）
        # 拆分：从第一个 ASCII 字母位置切开
        raw_title = (detail.get('title') or '').strip()
        title_zh, title_en = _split_douban_title(raw_title)
        public = {
            'media_type': 'movie',   # 豆瓣条目无明确 movie/tv 区分；卡片展示默认 movie
            'title': title_en or None,            # 英文部分（可能空）
            'title_zh': title_zh or raw_title,    # 中文部分；拆不出来时整段当中文
            'year': detail.get('year'),
            'release_date': detail.get('release_date'),
        }
        if full_mode:
            public['poster_url'] = detail.get('poster_url')
        bridge_ids = {}
        if detail.get('imdb_id'):
            bridge_ids['imdb_id'] = detail['imdb_id']
        metadata_store.upsert(
            source='douban', source_id=douban_id,
            public=public, ext=ext, bridge_ids=bridge_ids,
        )
    except Exception:
        logger.exception(f"L3 upsert douban detail 失败 douban_id={douban_id}")
