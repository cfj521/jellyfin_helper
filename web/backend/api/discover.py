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

logger = logging.getLogger(__name__)
router = APIRouter()


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
    # 推荐/流行列表不受 tmdb_request_delay 约束（30s 延迟仅用于演员图/海报批量修复）
    client = TMDBClient(settings.tmdb_api_key, delay=0.5, language=settings.tmdb_language)

    items = client.trending(media_type=media_type, time_window=time_window, page=page)
    result = {
        "count": len(items),
        "items": _normalize_tmdb(items),
        "page": page,
        "has_more": len(items) >= 20,   # TMDB 一页 20 条
    }
    _cache_set(cache_key, result)
    return {**result, "cached": False}


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
    client = TMDBClient(settings.tmdb_api_key, delay=0.5, language=settings.tmdb_language)

    if media_type == 'movie':
        items = client.popular_movies(page=page)
    elif media_type == 'tv':
        items = client.popular_tv(page=page)
    else:
        raise HTTPException(status_code=400, detail="media_type 必须是 movie / tv")

    result = {
        "count": len(items),
        "items": _normalize_tmdb(items, default_type=media_type),
        "page": page,
        "has_more": len(items) >= 20,
    }
    _cache_set(cache_key, result)
    return {**result, "cached": False}


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
    client = TMDBClient(settings.tmdb_api_key, delay=0.5, language=settings.tmdb_language)

    items = client.list_items(media_type, category, page=page)
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


@router.get("/detail")
def get_detail(
    media_type: str,
    tmdb_id: int,
    refresh: bool = False,
):
    """
    电影 / 剧集详情，含演员、相似推荐、预告等关联数据。30 分钟缓存。
    显示语言由 settings.tmdb_language 控制。
    """
    if not settings.tmdb_api_key:
        raise HTTPException(status_code=400, detail="未配置 TMDB API Key")
    if media_type not in ('movie', 'tv'):
        raise HTTPException(status_code=400, detail="media_type 必须是 movie / tv")

    lang = settings.tmdb_language or 'zh-CN'
    cache_key = f"detail:{media_type}:{tmdb_id}:{lang}"
    if not refresh:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}

    from common.tmdb_client import TMDBClient
    client = TMDBClient(settings.tmdb_api_key, delay=0.5, language=lang)

    raw = client.get_detail(media_type, tmdb_id, language=lang)
    if not raw:
        raise HTTPException(status_code=404, detail="TMDB 中未找到该条目")

    result = _normalize_detail(raw, media_type)
    _cache_set(cache_key, result)
    return {**result, "cached": False}


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

    # 从 translations 里抽英文标题（用于种子搜索 query）
    english_title = None
    translations = (raw.get('translations') or {}).get('translations') or []
    for t in translations:
        if t.get('iso_639_1') == 'en':
            td = t.get('data') or {}
            english_title = td.get('title') or td.get('name')
            if english_title:
                break

    # 兜底：原始语言就是英文 → original_title 即英文
    if not english_title and raw.get('original_language') == 'en':
        english_title = raw.get('original_title') or raw.get('original_name')

    return {
        "tmdb_id": raw.get('id'),
        "media_type": media_type,
        "title": raw.get('title') or raw.get('name'),
        "original_title": raw.get('original_title') or raw.get('original_name'),
        "original_language": raw.get('original_language'),
        "english_title": english_title,
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
    """把 TMDB 多种返回结构统一成一个简化格式给前端用。"""
    lang = settings.tmdb_language or 'zh-CN'
    movie_gmap = _get_tmdb_genre_map('movie', lang)
    tv_gmap = _get_tmdb_genre_map('tv', lang)

    out = []
    for it in items:
        mt = it.get('media_type') or default_type or 'movie'
        title = it.get('title') or it.get('name')
        date = it.get('release_date') or it.get('first_air_date')
        poster = it.get('poster_path')
        # genre_ids 是 TMDB 列表响应里的 id 数组；按 media_type 选对应映射
        gmap = tv_gmap if mt == 'tv' else movie_gmap
        genre_names = [gmap[g] for g in (it.get('genre_ids') or []) if g in gmap]
        out.append({
            "tmdb_id": it.get('id'),
            "media_type": mt,
            "title": title,
            "original_title": it.get('original_title') or it.get('original_name'),
            "original_language": it.get('original_language'),  # 'en' / 'ja' / 'zh' 等
            "overview": it.get('overview'),
            "release_date": date,
            "vote_average": it.get('vote_average'),
            "popularity": it.get('popularity'),
            "poster_url": f"https://image.tmdb.org/t/p/w342{poster}" if poster else None,
            "genres": genre_names,
        })
    return out


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

    if category == 'trending':
        items = client.trending(media_type, page=page, limit=_TRAKT_PAGE_SIZE)
    elif category == 'anticipated':
        items = client.anticipated(media_type, page=page, limit=_TRAKT_PAGE_SIZE)
    elif category == 'popular':
        items = client.popular(media_type, page=page, limit=_TRAKT_PAGE_SIZE)
    else:  # watched_weekly
        items = client.watched(media_type, period='weekly', page=page, limit=_TRAKT_PAGE_SIZE)

    # 给有 tmdb_id 的项补 TMDB 海报：5 路并发 + 每张 tmdb_id 单独 7 天缓存
    out = [it.to_dict() for it in items]
    if settings.tmdb_api_key and out:
        try:
            from common.tmdb_client import TMDBClient
            from concurrent.futures import ThreadPoolExecutor
            tmdb = TMDBClient(settings.tmdb_api_key, delay=0.0, language=settings.tmdb_language)
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

    if category == 'trending':
        items = client.trending(page=page, limit=_ANILIST_PAGE_SIZE)
    elif category == 'popular':
        items = client.popular(page=page, limit=_ANILIST_PAGE_SIZE)
    elif category == 'top_rated':
        items = client.top_rated(page=page, limit=_ANILIST_PAGE_SIZE)
    else:  # current_season
        from datetime import datetime as _dt
        now = _dt.utcnow()
        # 季：Q1 WINTER, Q2 SPRING, Q3 SUMMER, Q4 FALL
        season_map = {1: 'WINTER', 2: 'SPRING', 3: 'SUMMER', 4: 'FALL'}
        season = season_map[((now.month - 1) // 3) + 1]
        items = client.current_season(season=season, year=now.year,
                                       page=page, limit=_ANILIST_PAGE_SIZE)

    out = [it.to_dict() for it in items]
    result = {
        "count": len(out), "items": out,
        "category": category, "page": page, "limit": _ANILIST_PAGE_SIZE,
        "has_more": len(out) >= _ANILIST_PAGE_SIZE,
    }
    _kv_set(_ANILIST_SCOPE, cache_key, result)
    return {**result, "cached": False}


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
    """全局单消费者：从队列里逐个吃 douban_id，全局共享一个 DoubanClient
    （rate_limit + PoW cookie 都跟着 client 走 → 严格 douban_request_delay 全局限速）。
    线程永不退出（队列空时阻塞 .get）"""
    from common.douban_client import DoubanClient
    client = DoubanClient(
        user_agent=settings.douban_user_agent,
        delay=settings.douban_request_delay,
    )
    logger.info("豆瓣详情预取消费者线程启动")
    while True:
        try:
            did = _douban_prefetch_queue.get()
        except Exception as e:
            logger.warning(f"豆瓣预取队列读取异常：{e}")
            continue
        try:
            # 再读一次缓存：可能用户主动点"简介"/"搜种子"提前填上了
            cached = _kv_get(_DOUBAN_DETAIL_SCOPE, did, ttl_seconds=_DOUBAN_DETAIL_TTL)
            if isinstance(cached, dict) and cached.get('summary'):
                continue
            detail = client.fetch_subject_summary(did)
            if detail and detail.get('summary'):
                _kv_set(_DOUBAN_DETAIL_SCOPE, did, detail)
                logger.debug(f"豆瓣详情预取写入缓存 {did}")
            else:
                logger.warning(f"豆瓣详情预取 {did} 拿到空 detail（反爬或条目失效）")
        except Exception as e:
            logger.warning(f"豆瓣详情预取 {did} 失败: {e}")
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
    # 豆瓣页面拉空（很可能是反爬）就不要写缓存，避免长 TTL 把空结果钉死
    if items:
        _kv_set(_DOUBAN_SCOPE, cache_key, result)
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

    from common.anilist_client import AniListClient
    client = AniListClient(
        base_url=cfg.base_url,
        request_delay=cfg.request_delay,
        timeout=cfg.timeout_seconds,
    )
    detail = client.detail(int(anilist_id))
    if not detail:
        raise HTTPException(status_code=404, detail="AniList 中未找到该条目")
    _kv_set(_ANILIST_DETAIL_SCOPE, cache_key, detail)
    return {**detail, "cached": False}


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

    from common.douban_client import DoubanClient
    client = DoubanClient(
        user_agent=settings.douban_user_agent,
        delay=settings.douban_request_delay,
    )
    detail = client.fetch_subject_summary(str(douban_id))
    if not detail:
        raise HTTPException(status_code=404, detail="豆瓣条目页拉取失败（可能反爬或条目不存在）")

    # 拉到正文才写缓存（避免反爬空结果占位）
    if detail.get('summary'):
        _kv_set(_DOUBAN_DETAIL_SCOPE, cache_key, detail)
    return {**detail, "cached": False}
