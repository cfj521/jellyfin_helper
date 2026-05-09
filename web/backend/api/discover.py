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
    # 用户在搜索页选的分类，作为 user_hint 帮助识别（搜索页 form.category）
    user_hint_media_type: Optional[str] = None


# ---------- TMDB 热门（带 30 分钟内存缓存）----------

import time as _time
from web.backend.config import settings as _settings
_TMDB_CACHE: dict = {}  # key=str → (data, expire_at)
# TTL 来自 settings.cache_tmdb_minutes（分钟），改完需重启后端
def _tmdb_ttl_secs() -> int:
    return max(1, _settings.cache_tmdb_minutes) * 60


def _cache_get(key: str):
    item = _TMDB_CACHE.get(key)
    if item and item[1] > _time.time():
        return item[0]
    return None


def _cache_set(key: str, data):
    _TMDB_CACHE[key] = (data, _time.time() + _tmdb_ttl_secs())


def _cache_clear():
    _TMDB_CACHE.clear()




@router.get("/trending")
def get_trending(
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

    result = {"count": len(items), "items": _normalize_tmdb(items, default_type=media_type)}
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
def search_jackett(request: SearchRequest):
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
def push_download(
    request: DownloadRequest,
    db: Session = Depends(get_db),
):
    """推送种子到 qB（stop_condition=MetadataReceived）+ 写 dispatch_map(phase='analyzing')。

    分析由 scheduler 的 analyzer 后台跑：高置信自动入流水线，低置信落 needs_review。
    """
    if not settings.qbittorrent_host or not settings.qbittorrent_username:
        raise HTTPException(status_code=400, detail="未配置 qBittorrent")
    if not request.magnet and not request.torrent_url:
        raise HTTPException(status_code=400, detail="必须提供 magnet 或 torrent_url")

    from common.qbittorrent_client import QBittorrentClient
    from web.backend.database import DownloadDispatchMap

    client = QBittorrentClient(
        settings.qbittorrent_host,
        settings.qbittorrent_username,
        settings.qbittorrent_password,
    )

    # save_path 仅当 request 显式指定才传 —— 否则让 qB 用自己的默认下载路径。
    # 注意：settings.qbittorrent_download_path 是**后端视角**（Windows 上的 X:/），
    # 不能当 save_path 传给 qB（Linux），否则 qB 会把它拼到自己的默认下载路径后
    # 形成 /download/X: 这种乱路径。两套路径概念分开：qB 自己管自己的下载路径，
    # 后端通过 path_mappings 反查文件实际位置。
    save_path = request.save_path  # None → qB 默认
    # 拿 metadata 后自动暂停；analyzer 决策后再 resume
    ok = client.add_torrent(
        magnet=request.magnet,
        torrent_url=request.torrent_url,
        save_path=save_path,
        category=request.category,
        stop_condition='MetadataReceived',
    )
    if not ok:
        raise HTTPException(status_code=500, detail="qBittorrent 推送失败（检查登录或种子链接）")

    # 拿到 hash —— magnet 里直接抠
    info_hash = _extract_hash_from_magnet(request.magnet) or ''
    # .torrent URL 模式没法直接拿 hash，靠 qB 反查（容忍空，analyzer 也兜底）
    if not info_hash and request.torrent_url:
        try:
            for t in client.list_torrents() or []:
                # 名字匹配是兜底，magnet 链接才稳
                if (t.get('name') or '').strip() == request.title.strip():
                    info_hash = (t.get('hash') or '').lower()
                    break
        except Exception:
            pass

    # 写 dispatch_map(phase=analyzing) + 触发 analyzer 立刻处理（不等下一个轮询周期）
    if info_hash:
        from tools.dispatch.phases import PHASE_ANALYZING, PHASE_DISMISSED, STATUS_RUNNING
        existing = db.query(DownloadDispatchMap).filter_by(torrent_hash=info_hash).first()

        if existing and existing.phase == PHASE_DISMISSED:
            # 用户之前拒绝过，现在又主动 push 同 hash → 视为重新申请，重置为 analyzing
            logger.info(f"push: 重置已拒绝的 dispatch_map 行 hash={info_hash[:16]}..")
            existing.phase = PHASE_ANALYZING
            existing.phase_status = STATUS_RUNNING
            existing.media_type = request.user_hint_media_type or 'unknown'
            existing.title = request.title
            existing.status_message = '从已拒绝重新提交，等待分析...'
            existing.cleaned_at = None
            db.commit()
            try:
                from tools.dispatch.analyzer import trigger
                trigger.set()
            except Exception:
                pass
        elif not existing:
            # user_hint 暂存到 media_type，analyzer 读取后生成 user_hint 再 identify
            db.add(DownloadDispatchMap(
                torrent_hash=info_hash,
                phase=PHASE_ANALYZING,
                phase_status=STATUS_RUNNING,
                media_type=request.user_hint_media_type or 'unknown',
                title=request.title,
                status_message='等待分析中...',
                created_at=datetime.utcnow(),
            ))
            db.commit()
            try:
                from tools.dispatch.analyzer import trigger
                trigger.set()
            except Exception:
                pass
        # existing 但不是 dismissed → 已经在流水线里跑（重复 push 同 hash 不动）

    return {
        "ok": True,
        "torrent_hash": info_hash,
        "message": "已推送到分析队列，识别后自动入流水线",
    }


def _extract_hash_from_magnet(magnet: Optional[str]) -> Optional[str]:
    """从 magnet:?xt=urn:btih:<hash> 抠出 info_hash（小写）。"""
    if not magnet:
        return None
    import re
    m = re.search(r'btih:([0-9a-fA-F]{40})', magnet)
    return m.group(1).lower() if m else None


@router.get("/downloads")
def list_downloads(
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

    # ---- 关联 dispatch_map：拿"目标媒体库 / 目标路径 / 流水线 phase"展示给前端 ----
    # 取代之前展示的 qB category / tags（那两个我们只写不读，对用户没有诊断价值）
    from web.backend.database import DownloadDispatchMap
    hashes = [(t.get('hash') or '').lower() for t in qbit_torrents if t.get('hash')]
    dispatch_by_hash: Dict[str, DownloadDispatchMap] = {}
    if hashes:
        rows = (
            db.query(DownloadDispatchMap)
            .filter(DownloadDispatchMap.torrent_hash.in_(hashes))
            .all()
        )
        dispatch_by_hash = {r.torrent_hash: r for r in rows}

    # library_id → name 映射：用于把 target_library_id 翻译成可读名字
    lib_id_to_name: Dict[str, str] = {}
    try:
        from common.jellyfin_client import JellyfinClient
        if settings.jellyfin_api_key:
            jc = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
            for lib in (jc.get_libraries_normalized() or []):
                lib_id_to_name[lib['id']] = lib['name']
    except Exception as e:
        logger.warning(f"加载 Jellyfin 库列表失败: {e}")

    def _build_dispatch_info(t: Dict) -> Optional[Dict]:
        """从 dispatch_map 取目标路径 / 库名 / phase；没记录返回 None。
        包含 needs_review 行人工审核 modal 需要的全部元数据（含重复检测结果）。
        """
        h = (t.get('hash') or '').lower()
        dm = dispatch_by_hash.get(h)
        if not dm:
            return None
        # 从 phase_timings JSONB 里取 duplicates 信息（analyzer 写入的）
        timings = dm.phase_timings or {}
        return {
            "media_type": dm.media_type,
            "title": dm.title,
            "year": dm.year,
            "series_name": dm.series_name,
            "target_library_id": dm.target_library_id,
            "target_library_name": lib_id_to_name.get(dm.target_library_id) if dm.target_library_id else None,
            "target_path": dm.target_path,
            "move_mode": dm.move_mode,
            "phase": dm.phase,
            "phase_status": dm.phase_status,
            "status_message": dm.status_message,
            "duplicates": timings.get('duplicates'),    # {type, existing, new, skip_file_indexes}
        }

    return {
        "qbittorrent": [
            {
                "hash": t.get('hash'),
                "name": t.get('name'),
                "size": t.get('size'),
                # qB 原始 progress 是 0–1 浮点（spec），前端 el-progress 自己 *100 显示。
                # 之前在这里又 *100，跟前端组合起来会变成 *10000 → 永远满格。原始透传即可。
                "progress": t.get('progress') or 0,
                "state": t.get('state'),
                "dlspeed": t.get('dlspeed'),
                "upspeed": t.get('upspeed'),
                "save_path": t.get('save_path'),
                "added_on": t.get('added_on'),
                "ratio": t.get('ratio') or 0,
                "eta": t.get('eta'),
                "seeding_time": t.get('seeding_time'),
                "completion_on": t.get('completion_on'),
                "downloaded": t.get('downloaded'),
                "uploaded": t.get('uploaded'),
                # 我们的转移信息（替代了原来的 category/tags 展示）
                "dispatch": _build_dispatch_info(t),
            }
            for t in qbit_torrents
        ],
    }


@router.post("/downloads/{torrent_hash}/pause")
def pause_download(torrent_hash: str):
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
def resume_download(torrent_hash: str):
    if not settings.qbittorrent_host:
        raise HTTPException(status_code=400, detail="未配置 qBittorrent")
    from common.qbittorrent_client import QBittorrentClient
    client = QBittorrentClient(
        settings.qbittorrent_host,
        settings.qbittorrent_username,
        settings.qbittorrent_password,
    )
    return {"ok": client.resume(torrent_hash)}


@router.delete("/downloads/{torrent_hash}")
def delete_download(torrent_hash: str, delete_files: bool = False):
    if not settings.qbittorrent_host:
        raise HTTPException(status_code=400, detail="未配置 qBittorrent")
    from common.qbittorrent_client import QBittorrentClient
    client = QBittorrentClient(
        settings.qbittorrent_host,
        settings.qbittorrent_username,
        settings.qbittorrent_password,
    )
    return {"ok": client.delete(torrent_hash, delete_files=delete_files)}


# ============================================================================
# 批量操作 / 单条增强 / 全局速度控制（Phase H）
# ============================================================================

class BulkActionRequest(BaseModel):
    hashes: List[str]
    action: str   # pause / resume / delete / force_start / recheck / reannounce
    delete_files: bool = False


def _qb():
    from common.qbittorrent_client import QBittorrentClient
    return QBittorrentClient(
        settings.qbittorrent_host,
        settings.qbittorrent_username,
        settings.qbittorrent_password,
    )


@router.post("/downloads/bulk")
def bulk_action(request: BulkActionRequest):
    if not request.hashes:
        return {"ok": False, "message": "未选中任何种子"}
    client = _qb()
    client.login()
    a = request.action.lower()
    ok = False
    if a == 'pause':
        ok = all(client.pause(h) for h in request.hashes)
    elif a == 'resume':
        ok = all(client.resume(h) for h in request.hashes)
    elif a == 'delete':
        ok = all(client.delete(h, delete_files=request.delete_files) for h in request.hashes)
    elif a == 'force_start':
        ok = client.set_force_start(request.hashes, force=True)
    elif a == 'recheck':
        ok = client.recheck(request.hashes)
    elif a == 'reannounce':
        ok = client.reannounce(request.hashes)
    else:
        raise HTTPException(status_code=400, detail=f'未知 action: {a}')
    return {"ok": ok, "count": len(request.hashes), "action": a}


@router.post("/downloads/{torrent_hash}/recheck")
def recheck(torrent_hash: str):
    return {"ok": _qb().recheck(torrent_hash)}


@router.post("/downloads/{torrent_hash}/reannounce")
def reannounce(torrent_hash: str):
    return {"ok": _qb().reannounce(torrent_hash)}


class ForceStartRequest(BaseModel):
    force: bool = True


@router.post("/downloads/{torrent_hash}/force-start")
def force_start(torrent_hash: str, request: ForceStartRequest):
    return {"ok": _qb().set_force_start(torrent_hash, force=request.force)}


@router.get("/transfer-info")
def transfer_info():
    """全局传输状态（速度 / 限速 / 备用速度模式 / 总下载上传量）。"""
    client = _qb()
    info = client.transfer_info() or {}
    return {
        'dl_info_speed': info.get('dl_info_speed', 0),
        'up_info_speed': info.get('up_info_speed', 0),
        'dl_info_data': info.get('dl_info_data', 0),
        'up_info_data': info.get('up_info_data', 0),
        'connection_status': info.get('connection_status'),
        'global_dl_limit': client.get_global_download_limit(),
        'global_up_limit': client.get_global_upload_limit(),
        'alt_speed_enabled': client.get_alt_speed_limits_enabled(),
    }


class SpeedLimitRequest(BaseModel):
    download_limit: Optional[int] = None  # bytes/s
    upload_limit: Optional[int] = None


@router.post("/transfer-info/speed-limit")
def set_speed_limit(request: SpeedLimitRequest):
    client = _qb()
    results = {}
    if request.download_limit is not None:
        results['download'] = client.set_global_download_limit(request.download_limit)
    if request.upload_limit is not None:
        results['upload'] = client.set_global_upload_limit(request.upload_limit)
    return results


@router.post("/transfer-info/toggle-alt-speed")
def toggle_alt_speed():
    return {"ok": _qb().toggle_alt_speed_limits()}
