"""
内容推荐与下载 API
- TMDB 热门 / 流行
- Jackett 搜索
- qBittorrent 下载推送 + 状态
"""
import sys
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

ROOT_DIR = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from web.backend.database import get_db, DownloadTask
from web.backend.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------- 请求模型 ----------

class SearchRequest(BaseModel):
    query: str
    category: str = "all"  # all | movie | tv | anime
    indexers: str = "all"
    limit: int = 50


class DownloadRequest(BaseModel):
    title: str
    magnet: Optional[str] = None
    torrent_url: Optional[str] = None
    save_path: Optional[str] = None
    category: Optional[str] = None
    source: str = "jackett"


# ---------- TMDB 热门（带 30 分钟内存缓存）----------

import time as _time
_TMDB_CACHE: dict = {}  # key=str → (data, expire_at)
_TMDB_CACHE_TTL = 30 * 60  # 30 分钟


def _cache_get(key: str):
    item = _TMDB_CACHE.get(key)
    if item and item[1] > _time.time():
        return item[0]
    return None


def _cache_set(key: str, data):
    _TMDB_CACHE[key] = (data, _time.time() + _TMDB_CACHE_TTL)


def _cache_clear():
    _TMDB_CACHE.clear()




@router.get("/trending")
async def get_trending(
    media_type: str = "all",
    time_window: str = "week",
    refresh: bool = False,
):
    """TMDB 热门内容（all / movie / tv，day / week）。30 分钟内存缓存；refresh=true 强制刷新"""
    if not settings.tmdb_api_key:
        raise HTTPException(status_code=400, detail="未配置 TMDB API Key")
    if media_type not in ("all", "movie", "tv", "person"):
        raise HTTPException(status_code=400, detail="media_type 必须是 all / movie / tv / person")
    if time_window not in ("day", "week"):
        raise HTTPException(status_code=400, detail="time_window 必须是 day / week")

    cache_key = f"trending:{media_type}:{time_window}"
    if not refresh:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}

    from common.tmdb_client import TMDBClient
    # 推荐/流行列表不受 tmdb_request_delay 约束（30s 延迟仅用于演员图/海报批量修复）
    client = TMDBClient(settings.tmdb_api_key, delay=0.5, language=settings.tmdb_language)

    items = client.trending(media_type=media_type, time_window=time_window)
    result = {"count": len(items), "items": _normalize_tmdb(items)}
    _cache_set(cache_key, result)
    return {**result, "cached": False}


@router.get("/popular")
async def get_popular(media_type: str = "movie", page: int = 1, refresh: bool = False):
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

    result = {"count": len(items), "items": _normalize_tmdb(items, default_type=media_type)}
    _cache_set(cache_key, result)
    return {**result, "cached": False}


@router.get("/list")
async def get_category_list(
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
    }
    _cache_set(cache_key, result)
    return {**result, "cached": False}


@router.get("/detail")
async def get_detail(
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
async def clear_tmdb_cache():
    """清除推荐/流行列表的内存缓存"""
    _cache_clear()
    return {"ok": True}


def _normalize_tmdb(items: List[dict], default_type: Optional[str] = None) -> List[dict]:
    """把 TMDB 多种返回结构统一成一个简化格式给前端用。"""
    out = []
    for it in items:
        mt = it.get('media_type') or default_type or 'movie'
        title = it.get('title') or it.get('name')
        date = it.get('release_date') or it.get('first_air_date')
        poster = it.get('poster_path')
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
        })
    return out


# ---------- Jackett 搜索 ----------

@router.post("/search")
async def search_jackett(request: SearchRequest):
    """通过 Jackett 搜索种子"""
    if not settings.jackett_api_key:
        raise HTTPException(status_code=400, detail="未配置 Jackett API Key")

    from common.jackett_client import JackettClient, JACKETT_CATEGORIES
    client = JackettClient(settings.jackett_host, settings.jackett_api_key)

    cats = JACKETT_CATEGORIES.get(request.category, [])
    try:
        results = client.search(
            query=request.query,
            categories=cats or None,
            indexers=request.indexers,
            limit=request.limit,
        )
        return {"count": len(results), "results": results}
    except Exception as e:
        logger.exception("Jackett 搜索失败")
        raise HTTPException(status_code=500, detail=f"搜索失败: {e}")


# ---------- qBittorrent ----------

@router.post("/download")
async def push_download(
    request: DownloadRequest,
    db: Session = Depends(get_db),
):
    """推送下载任务到 qBittorrent"""
    if not settings.qbittorrent_host or not settings.qbittorrent_username:
        raise HTTPException(status_code=400, detail="未配置 qBittorrent")
    if not request.magnet and not request.torrent_url:
        raise HTTPException(status_code=400, detail="必须提供 magnet 或 torrent_url")

    from common.qbittorrent_client import QBittorrentClient
    client = QBittorrentClient(
        settings.qbittorrent_host,
        settings.qbittorrent_username,
        settings.qbittorrent_password,
    )

    save_path = request.save_path or settings.qbittorrent_download_path
    ok = client.add_torrent(
        magnet=request.magnet,
        torrent_url=request.torrent_url,
        save_path=save_path,
        category=request.category,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="qBittorrent 推送失败（检查登录或种子链接）")

    # 入库本地下载记录
    record = DownloadTask(
        title=request.title,
        source=request.source,
        magnet_link=request.magnet or request.torrent_url,
        status='pending',
        progress=0.0,
        download_path=save_path,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    return {"id": record.id, "ok": True, "message": "已推送到 qBittorrent"}


@router.get("/downloads")
async def list_downloads(
    filter_status: str = "all",
    refresh: bool = True,
    db: Session = Depends(get_db),
):
    """
    列出下载任务。

    refresh=True 时同步 qBittorrent 实时状态。
    """
    qbit_torrents = []
    if refresh and settings.qbittorrent_host and settings.qbittorrent_username:
        try:
            from common.qbittorrent_client import QBittorrentClient
            client = QBittorrentClient(
                settings.qbittorrent_host,
                settings.qbittorrent_username,
                settings.qbittorrent_password,
            )
            qbit_torrents = client.list_torrents(filter_status if filter_status != 'all' else None)
        except Exception as e:
            logger.warning(f"qBittorrent 状态获取失败: {e}")

    # 从数据库取本地记录
    local = db.query(DownloadTask).order_by(DownloadTask.created_at.desc()).limit(200).all()

    return {
        "qbittorrent": [
            {
                "hash": t.get('hash'),
                "name": t.get('name'),
                "size": t.get('size'),
                "progress": round((t.get('progress') or 0) * 100, 1),
                "state": t.get('state'),
                "dlspeed": t.get('dlspeed'),
                "upspeed": t.get('upspeed'),
                "save_path": t.get('save_path'),
                "added_on": t.get('added_on'),
            }
            for t in qbit_torrents
        ],
        "local": [
            {
                "id": d.id,
                "title": d.title,
                "source": d.source,
                "status": d.status,
                "progress": d.progress,
                "download_path": d.download_path,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in local
        ],
    }


@router.post("/downloads/{torrent_hash}/pause")
async def pause_download(torrent_hash: str):
    if not settings.qbittorrent_host:
        raise HTTPException(status_code=400, detail="未配置 qBittorrent")
    from common.qbittorrent_client import QBittorrentClient
    client = QBittorrentClient(
        settings.qbittorrent_host,
        settings.qbittorrent_username,
        settings.qbittorrent_password,
    )
    return {"ok": client.pause(torrent_hash)}


@router.post("/downloads/{torrent_hash}/resume")
async def resume_download(torrent_hash: str):
    if not settings.qbittorrent_host:
        raise HTTPException(status_code=400, detail="未配置 qBittorrent")
    from common.qbittorrent_client import QBittorrentClient
    client = QBittorrentClient(
        settings.qbittorrent_host,
        settings.qbittorrent_username,
        settings.qbittorrent_password,
    )
    return {"ok": client.resume(torrent_hash)}


@router.post("/sync-completed")
async def sync_completed(db: Session = Depends(get_db)):
    """
    扫描 qBittorrent 中已完成（progress >= 1.0）的种子，
    更新数据库状态，并通知 Jellyfin 重新扫描媒体库。
    适合前端定时调用（如每 30 秒一次）。
    """
    if not settings.qbittorrent_host or not settings.qbittorrent_username:
        return {"updated": 0, "refreshed": False, "message": "qBittorrent 未配置"}

    from common.qbittorrent_client import QBittorrentClient
    client = QBittorrentClient(
        settings.qbittorrent_host,
        settings.qbittorrent_username,
        settings.qbittorrent_password,
    )

    try:
        torrents = client.list_torrents('completed')
    except Exception as e:
        return {"updated": 0, "refreshed": False, "error": str(e)}

    completed_hashes = {t.get('hash') for t in torrents if t.get('hash')}
    completed_titles = {t.get('name') for t in torrents}

    # 把数据库里 status != 'completed' 但实际已完成的更新过来
    updated = 0
    for d in db.query(DownloadTask).filter(DownloadTask.status != 'completed').all():
        # 名字粗匹配（qBit 的 name 可能跟 title 略有差异）
        if d.title in completed_titles or any(d.title in n for n in completed_titles if n):
            d.status = 'completed'
            d.progress = 100.0
            from datetime import datetime
            d.completed_at = datetime.utcnow()
            updated += 1

    db.commit()

    # 有新完成的 → 触发 Jellyfin 刷新
    refreshed = False
    if updated > 0 and settings.jellyfin_api_key:
        try:
            from common.jellyfin_client import JellyfinClient
            JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key).refresh_all_libraries()
            refreshed = True
            logger.info(f"sync-completed: 检测到 {updated} 个新完成下载，已触发 Jellyfin 刷新")
        except Exception as e:
            logger.warning(f"触发 Jellyfin 刷新失败: {e}")

    return {
        "updated": updated,
        "refreshed": refreshed,
        "completed_total": len(completed_hashes),
    }


@router.delete("/downloads/{torrent_hash}")
async def delete_download(torrent_hash: str, delete_files: bool = False):
    if not settings.qbittorrent_host:
        raise HTTPException(status_code=400, detail="未配置 qBittorrent")
    from common.qbittorrent_client import QBittorrentClient
    client = QBittorrentClient(
        settings.qbittorrent_host,
        settings.qbittorrent_username,
        settings.qbittorrent_password,
    )
    return {"ok": client.delete(torrent_hash, delete_files=delete_files)}
