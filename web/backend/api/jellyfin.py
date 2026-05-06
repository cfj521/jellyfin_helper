"""
Jellyfin 直通 API
- 媒体库列表（含路径、类型、ID）
- 单个库的条目
- 触发刷新
- 系统信息
- 库统计聚合（视频数 / 占用 / 缺字幕 / 缺海报）
"""
import sys
import os
import shutil
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session

ROOT_DIR = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from common.jellyfin_client import JellyfinClient
from web.backend.database import get_db, MediaItem, Task
from web.backend.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.wmv', '.mov', '.flv', '.webm', '.m4v', '.ts', '.rmvb'}
SUBTITLE_EXTS = {'.srt', '.ass', '.ssa', '.sub', '.idx', '.vtt', '.sup'}
AUDIO_EXTS = {'.mp3', '.flac', '.wav', '.aac', '.m4a', '.ogg', '.wma', '.opus', '.ape', '.dsf'}
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.heic'}


# ---- 媒体库统计 in-memory 缓存（2 小时 TTL）----
# 单 worker 部署够用；重启会丢，但首次进页面会重算，可接受。
# 多 worker 时各 worker 各自缓存（可能不一致），需要时再升级到 DB / Redis。
from threading import Lock as _CacheLock
from datetime import datetime as _dt, timedelta as _td

_LIB_STATS_CACHE: Dict[str, Dict] = {}  # library_id → {'data': ..., 'cached_at': datetime}
_LIB_STATS_LOCK = _CacheLock()
_LIB_STATS_TTL = _td(hours=2)


def _get_cached_lib_stats(library_id: str) -> Optional[Dict]:
    with _LIB_STATS_LOCK:
        entry = _LIB_STATS_CACHE.get(library_id)
    if not entry:
        return None
    age = _dt.utcnow() - entry['cached_at']
    if age > _LIB_STATS_TTL:
        return None
    # 返回浅拷贝，附加 _cache 字段告知前端这是缓存
    out = dict(entry['data'])
    out['_cached'] = True
    out['_cached_at'] = entry['cached_at'].isoformat()
    out['_cache_age_seconds'] = int(age.total_seconds())
    return out


def _set_cached_lib_stats(library_id: str, data: Dict):
    with _LIB_STATS_LOCK:
        _LIB_STATS_CACHE[library_id] = {'data': data, 'cached_at': _dt.utcnow()}


def _invalidate_lib_stats_cache(library_id: Optional[str] = None):
    """library_id=None 时清空全部缓存（用于"全部强制刷新"）"""
    with _LIB_STATS_LOCK:
        if library_id is None:
            _LIB_STATS_CACHE.clear()
        else:
            _LIB_STATS_CACHE.pop(library_id, None)


def _client() -> JellyfinClient:
    if not settings.jellyfin_api_key:
        raise HTTPException(status_code=400, detail="未配置 Jellyfin API Key")
    return JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)


def _path_exists_locally(p: str) -> bool:
    """
    检查路径在本机是否可访问（同 VM 部署时通常 True；Docker 路径不一致时为 False）。
    会先走 path_mappings 转换，所以即使 Jellyfin 报 Linux 路径而本工具在 Windows 上跑，
    只要配置好映射就能正确判断。
    """
    if not p:
        return False
    try:
        from web.backend.path_translator import translate_path_with_settings
        translated = translate_path_with_settings(p)
        return os.path.exists(translated)
    except (OSError, ValueError):
        return False


# ---------- 库列表 ----------

@router.get("/libraries")
async def list_libraries(check_paths: bool = True):
    """
    列出 Jellyfin 媒体库。

    Args:
        check_paths: 是否检查路径在本机可访问性（默认 True）
    """
    client = _client()
    try:
        libraries = client.get_libraries_normalized()
    except Exception as e:
        logger.exception("获取 Jellyfin 媒体库失败")
        raise HTTPException(status_code=502, detail=f"无法连接 Jellyfin: {e}")

    if check_paths:
        for lib in libraries:
            lib['locations_status'] = [
                {"path": p, "accessible": _path_exists_locally(p)}
                for p in lib['locations']
            ]
            lib['all_accessible'] = all(s['accessible'] for s in lib['locations_status'])

    # 库封面图：用 PrimaryImageItemId（Jellyfin 库自身的代表图）
    # 没有 image tag 时 Jellyfin 会随机选一个 item 的海报作为库封面，但 URL 不带 tag 也能拉
    host = (settings.jellyfin_host or "").rstrip('/')
    api_key = settings.jellyfin_api_key
    for lib in libraries:
        item_id = lib.get('primary_image_item_id') or lib.get('id')
        if host and item_id:
            # 用窄宽度参数：库卡片只是背景，不需要原图
            lib['cover_url'] = (
                f"{host}/Items/{item_id}/Images/Primary"
                f"?maxWidth=400&quality=70&api_key={api_key}"
            )
        else:
            lib['cover_url'] = None

    return {
        "count": len(libraries),
        "libraries": libraries,
    }


@router.get("/libraries/{library_id}/items")
async def get_library_items(
    library_id: str,
    item_type: Optional[str] = None,  # Movie / Series / Episode；不传按 collection_type 自动推断
    start_index: int = 0,
    limit: int = 50,
    search: Optional[str] = None,
):
    """
    列出某个库的条目（分页）。
    返回字段包含海报缩略图 URL、Jellyfin 详情页 URL、演员数/有图演员数。

    search：按名称模糊搜索（透传 Jellyfin 的 SearchTerm 参数，服务端做匹配）。

    item_type 默认推断逻辑（避免 TV 库递归返回 Series+Season+Episode 一锅端）：
      tvshows → Series（顶层只显示剧；季/集靠 tree-table 懒加载）
      movies  → Movie
      mixed   → Movie,Series
      其他    → 不限制（按 Jellyfin 默认）
    """
    client = _client()

    # 推断默认 item_type
    if not item_type:
        try:
            libs = client.get_libraries_normalized()
            target = next((l for l in libs if l['id'] == library_id), None)
            coll = (target or {}).get('collection_type')
            type_map = {
                'tvshows':    'Series',
                'movies':     'Movie',
                'mixed':      'Movie,Series',
                'musicvideos':'MusicVideo',
                'music':      'MusicAlbum',
                'homevideos': 'Video',
                'boxsets':    'BoxSet',
                'books':      'Book',
            }
            item_type = type_map.get(coll)
        except Exception as e:
            logger.warning(f"推断 item_type 失败 lib={library_id}: {e}")
            # 失败兜底：不限制类型（保持旧行为）

    page = client.get_library_items_page(
        library_id,
        start_index=start_index,
        limit=limit,
        item_types=item_type,
        fields=(
            "Path,ProductionYear,ImageTags,ProviderIds,People,"
            "CommunityRating,OfficialRating,RunTimeTicks,ChildCount,Overview,"
            "OriginalTitle"
        ),
        search_term=search,
    )
    items = page['items']
    total = page['total']

    host = (settings.jellyfin_host or "").rstrip('/')

    return {
        "count": len(items),
        "total": total,
        "start_index": start_index,
        "limit": limit,
        "items": [_build_item_dict(i, host) for i in items],
    }


# ============================================================
# Series → Season → Episode 钻取（带 30 分钟内存缓存）
# ============================================================

import threading
import time
from common.jellyfin_client import JellyfinClient as _JfClientType  # alias for typing

_CHILDREN_CACHE_TTL = 60 * 60  # 1 小时


class _TTLCache:
    """简易 TTL 内存缓存：用于剧集/季的子节点查询。"""

    def __init__(self, ttl: int):
        self.ttl = ttl
        self._store: Dict[str, tuple] = {}  # key -> (expires_at, value)
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.time() >= expires_at:
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value):
        with self._lock:
            self._store[key] = (time.time() + self.ttl, value)

    def invalidate(self, key: Optional[str] = None):
        with self._lock:
            if key is None:
                self._store.clear()
            else:
                self._store.pop(key, None)


_seasons_cache = _TTLCache(_CHILDREN_CACHE_TTL)
_episodes_cache = _TTLCache(_CHILDREN_CACHE_TTL)


def _build_item_dict(i: Dict, host: str) -> Dict:
    """
    把 Jellyfin 原始 item dict 转成前端表格需要的扁平 dict。
    Series / Season / Episode 共用此函数，按 type 字段填不同语义。
    """
    from web.backend.api._item_health import compute_health, extract_suggested_title_year

    item_id = i.get('Id')
    item_type = i.get('Type')
    image_tag = (i.get('ImageTags') or {}).get('Primary')
    # Episode 的缩略图是 Type='Primary' 但语义上是"thumb"
    poster_url = (
        f"{host}/Items/{item_id}/Images/Primary?maxHeight=120&tag={image_tag}&quality=80"
        if (host and image_tag) else None
    )
    detail_url = f"{host}/web/index.html#!/details?id={item_id}" if host else None

    # 演员统计：仅 Series/Movie 层级才有意义
    people = i.get('People') or []
    actors = [p for p in people if p.get('Type') == 'Actor']
    actors_total = len(actors)
    actors_with_image = sum(1 for p in actors if p.get('PrimaryImageTag'))

    suggested_title, suggested_year = extract_suggested_title_year(i)

    runtime_ticks = i.get('RunTimeTicks') or 0
    runtime_min = round(runtime_ticks / 600_000_000.0, 1) if runtime_ticks else None

    return {
        "id": item_id,
        "name": i.get('Name'),
        "type": item_type,
        "year": i.get('ProductionYear'),
        "path": i.get('Path'),
        "tmdb_id": (i.get('ProviderIds') or {}).get('Tmdb'),
        "has_image": bool(image_tag),
        "poster_url": poster_url,
        "detail_url": detail_url,
        "edit_url": detail_url,
        "actors_total": actors_total,
        "actors_with_image": actors_with_image,
        "community_rating": i.get('CommunityRating'),
        "official_rating": i.get('OfficialRating'),
        "runtime_min": runtime_min,
        "child_count": i.get('ChildCount'),
        # Episode 专属字段（其它层级为 None）
        "season_name": i.get('SeasonName'),
        "series_name": i.get('SeriesName'),
        "series_id": i.get('SeriesId'),
        "season_number": i.get('ParentIndexNumber'),
        "episode_number": i.get('IndexNumber'),
        "suggested_title": suggested_title,
        "suggested_year": suggested_year,
        "health": compute_health(i),
        # 标记是否可展开（前端 tree-table 用）
        # Series / Season 都永远 true：即使空也允许用户点开看为啥空。
        # 之前依赖 ChildCount，但 /Shows/{id}/Seasons 端点常常不返回 ChildCount → Season 看不到箭头
        # Episode 永远 false
        "has_children": item_type in ('Series', 'Season'),
    }


@router.get("/series/{series_id}/seasons")
async def get_series_seasons(series_id: str, force: bool = False):
    """
    拉某部剧的全部 Season（懒加载触发，30 分钟内存缓存）。

    force=true：旁路缓存，强制从 Jellyfin 重新拉取并刷新缓存。
    """
    cache_key = f"seasons:{series_id}"
    if not force:
        cached = _seasons_cache.get(cache_key)
        if cached is not None:
            return cached

    client = _client()
    try:
        raw_items = client.get_seasons_of_series(series_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"拉取季列表失败: {e}")

    host = (settings.jellyfin_host or "").rstrip('/')
    items = [_build_item_dict(i, host) for i in raw_items]
    payload = {"count": len(items), "items": items}
    _seasons_cache.set(cache_key, payload)
    return payload


@router.get("/seasons/{season_id}/episodes")
async def get_season_episodes(season_id: str, force: bool = False):
    """
    拉某季的全部 Episode（按集号排序，30 分钟内存缓存）。
    """
    cache_key = f"episodes:{season_id}"
    if not force:
        cached = _episodes_cache.get(cache_key)
        if cached is not None:
            return cached

    client = _client()
    try:
        raw_items = client.get_episodes_of_season(season_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"拉取集列表失败: {e}")

    host = (settings.jellyfin_host or "").rstrip('/')
    items = [_build_item_dict(i, host) for i in raw_items]
    payload = {"count": len(items), "items": items}
    _episodes_cache.set(cache_key, payload)
    return payload


@router.post("/cache/clear-children")
async def clear_children_cache():
    """清空 seasons/episodes/aggregates 缓存（强制刷新按钮用）。"""
    _seasons_cache.invalidate()
    _episodes_cache.invalidate()
    _aggregates_cache.invalidate()
    return {"status": "ok"}


# ============================================================
# Series 行聚合摘要：季数 / 集数 / 时长合计 / 字幕覆盖（best-effort）
# 前端列表加载后批量调一次，覆盖到 Series 行上
# ============================================================

_aggregates_cache = _TTLCache(_CHILDREN_CACHE_TTL)


class SeriesAggregatesRequest(BaseModel):
    series_ids: List[str]


def _compute_series_aggregate(series_id: str, client) -> Dict:
    """
    查 /Shows/{id}/Episodes 拿到所有 Episode 的 RunTimeTicks +
    ParentIndexNumber，从中聚合季数/集数/总时长。
    """
    # 直接调原始 endpoint（不限制 SeasonId，拿全剧所有集）
    raw = client._request('GET', f'/Shows/{series_id}/Episodes', params={
        'Fields': 'RunTimeTicks,IndexNumber,ParentIndexNumber',
    }) or {}
    items = raw.get('Items') or []
    season_set = set()
    total_ticks = 0
    episode_count = 0
    for ep in items:
        season_set.add(ep.get('ParentIndexNumber'))
        total_ticks += int(ep.get('RunTimeTicks') or 0)
        episode_count += 1
    total_runtime_min = round(total_ticks / 600_000_000.0, 1) if total_ticks else None
    return {
        'season_count': len(season_set),
        'episode_count': episode_count,
        'total_runtime_min': total_runtime_min,
    }


def _compute_series_subtitle_coverage(series_path: str, db: Session) -> Optional[Dict]:
    """
    Best-effort：从最近一次 subtitle_scan 任务的 result.directories 中聚合
    Series 路径下的字幕覆盖率。

    没有最近的 subtitle_scan 数据 / 路径不在扫描结果中 / 任何错误 → 返回 None
    （前端理解为"未知"，不显示覆盖率 chip）

    返回 {total_videos, with_required, without_required, coverage_pct}
    """
    if not series_path:
        return None
    try:
        from web.backend.database import Task as _Task
        from datetime import datetime as _dt, timedelta as _td
        # 取最近 24 小时内最新的 completed subtitle_scan，避免被远古任务带偏
        cutoff = _dt.utcnow() - _td(hours=24)
        task = (
            db.query(_Task)
            .filter(
                _Task.task_type == 'subtitle_scan',
                _Task.status == 'completed',
                _Task.completed_at >= cutoff,
            )
            .order_by(_Task.completed_at.desc())
            .first()
        )
        if not task or not task.result:
            return None

        result = json.loads(task.result)
        dirs = result.get('directories') or []
        if not dirs:
            return None

        # 路径归一化，路径前缀比对
        norm_series = series_path.replace('\\', '/').rstrip('/').lower()
        if not norm_series:
            return None

        total = 0
        without_req = 0
        for d in dirs:
            d_path = (d.get('path') or '').replace('\\', '/').rstrip('/').lower()
            if not d_path:
                continue
            # 这个目录在 Series 子树里
            if d_path == norm_series or d_path.startswith(norm_series + '/'):
                total += int(d.get('total_videos') or 0)
                without_req += int(d.get('without_required') or 0)

        if total == 0:
            return None

        with_req = max(0, total - without_req)
        coverage_pct = round(with_req * 100 / total)
        return {
            'total_videos': total,
            'with_required': with_req,
            'without_required': without_req,
            'coverage_pct': coverage_pct,
        }
    except Exception as e:
        logger.debug(f"字幕覆盖聚合失败 {series_path}: {e}")
        return None


@router.post("/series/aggregates")
async def get_series_aggregates(
    req: SeriesAggregatesRequest,
    db: Session = Depends(get_db),
):
    """
    批量返回多部剧的聚合摘要：季数 / 集数 / 总时长 / 字幕覆盖（best-effort）。

    前端列表加载完成后调一次，把结果合并回 Series 行。
    单条结果 1 小时缓存（与 seasons/episodes 同步），强制刷新按钮一键清空。
    """
    if not req.series_ids:
        return {"results": {}}

    client = _client()
    results: Dict[str, Dict] = {}

    # 先取每条的 series 信息（拿 Path 用来算字幕覆盖）。Path 可由调用方传也可
    # 这里现取；为简化我们这里用一次轻量 get_item 拿 Path（也走缓存）
    series_path_cache: Dict[str, str] = {}

    for sid in req.series_ids:
        cache_key = f"agg:{sid}"
        cached = _aggregates_cache.get(cache_key)
        if cached is not None:
            results[sid] = cached
            continue

        try:
            agg = _compute_series_aggregate(sid, client)
        except Exception as e:
            logger.warning(f"Series {sid} 聚合失败: {e}")
            agg = {'season_count': None, 'episode_count': None, 'total_runtime_min': None}

        # 字幕覆盖（best-effort）
        try:
            if sid not in series_path_cache:
                series_item = client.get_item(sid, fields='Path')
                series_path_cache[sid] = (series_item or {}).get('Path') or ''
            sp = series_path_cache[sid]
            sub_cov = _compute_series_subtitle_coverage(sp, db)
        except Exception:
            sub_cov = None
        agg['subtitle_coverage'] = sub_cov

        _aggregates_cache.set(cache_key, agg)
        results[sid] = agg

    return {"results": results}


# ---------- 重新识别（刮削元数据）----------

class IdentifySearchRequest(BaseModel):
    """搜索远端 metadata 候选。name / year / tmdb_id 至少给一个。"""
    item_type: str = "Movie"      # Movie / Series / BoxSet ... Folder 自动按 Movie 走
    name: Optional[str] = None
    year: Optional[int] = None
    tmdb_id: Optional[str] = None  # 直接锁定 TMDB ID（最快路径）
    language: Optional[str] = None  # 不传走 settings.tmdb_language


class IdentifyApplyRequest(BaseModel):
    """把某个候选 apply 到现有条目。candidate 是 search 返回的一项整对象。"""
    candidate: Dict[str, Any]
    replace_all_images: bool = True


@router.post("/items/{item_id}/identify-search")
async def identify_search(item_id: str, req: IdentifySearchRequest):
    """
    在 Jellyfin 配置的元数据 provider（TMDB 等）里搜索候选。
    返回候选数组，前端展示供用户选择。
    """
    client = _client()

    provider_ids = None
    if req.tmdb_id:
        provider_ids = {'Tmdb': str(req.tmdb_id)}

    if not (req.name or provider_ids):
        raise HTTPException(
            status_code=400,
            detail="必须提供 name 或 tmdb_id 至少一个",
        )

    # 元数据语言：默认跟 settings.tmdb_language 一致；调用方可显式覆盖
    language = req.language or settings.tmdb_language or 'en-US'
    try:
        results = client.remote_search(
            item_id=item_id,
            item_type=req.item_type or 'Movie',
            name=req.name,
            year=req.year,
            provider_ids=provider_ids,
            language=language,
        )
    except Exception as e:
        logger.exception("Jellyfin remote_search 调用失败")
        raise HTTPException(status_code=502, detail=f"Jellyfin 远端搜索失败: {e}")

    return {
        "count": len(results),
        "candidates": results,
    }


@router.get("/items/{item_id}/sample-evidence")
async def get_sample_evidence(item_id: str):
    """
    取证：判断该条目是否是 sample。
    返回判定（sample / sample-likely / unclear / main-content）+ 证据明细。
    前端在确认对话框里展示给用户。
    """
    client = _client()
    try:
        item = client.get_item(item_id, fields='Path,MediaSources,RunTimeTicks,Name,Type')
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"读取 Jellyfin 条目失败: {e}")

    if not item:
        raise HTTPException(status_code=404, detail=f"条目不存在: {item_id}")

    from web.backend.api._sample_evidence import gather_sample_evidence
    evidence = gather_sample_evidence(item)
    evidence['name'] = item.get('Name')
    evidence['type'] = item.get('Type')
    return evidence


def _normalize_for_compare(p: str) -> str:
    """统一为 forward-slash + 去尾斜杠 + 小写，仅用于路径前缀比对。"""
    if not p:
        return ''
    return p.replace('\\', '/').rstrip('/').lower()


def _assert_safe_to_delete(item_path: str, item_name: str):
    """
    安全校验：拒绝删除任何"库根"或"库根的祖先目录"。

    场景：
      - item_path == 某个库的 location → 删了等于把整库扔了
      - item_path 是某个库 location 的祖先（如 /library 是 /library/videos 的祖先）→ 灾难
    检查通过则正常返回；不通过抛 400。
    """
    if not item_path:
        # 没有 Path 的条目（如纯 metadata 节点）允许删，Jellyfin 不会删文件
        return

    item_norm = _normalize_for_compare(item_path)

    # 归一化后变空，说明原路径只有斜杠 / 反斜杠 —— 系统根，拒绝
    if not item_norm:
        raise HTTPException(
            status_code=400,
            detail=f"拒绝删除：路径是系统根 ({item_path})，绝对不允许",
        )

    # 极端兜底：盘符根（万一路径解析出错）
    DANGEROUS_ROOTS = {
        'c:', 'd:', 'e:', 'f:', 'g:', 'h:', 'i:', 'j:',
        'k:', 'l:', 'm:', 'n:', 'o:', 'p:', 'q:', 'r:', 's:',
        't:', 'u:', 'v:', 'w:', 'x:', 'y:', 'z:',
    }
    if item_norm in DANGEROUS_ROOTS:
        raise HTTPException(
            status_code=400,
            detail=f"拒绝删除：路径是系统根 ({item_path})，绝对不允许",
        )

    # 跟所有库 location 比对
    try:
        client = _client()
        libraries = client.get_libraries_normalized()
    except Exception as e:
        logger.warning(f"安全校验时无法读取库列表: {e}")
        # 读不到库列表，保守拒绝以防误删
        raise HTTPException(
            status_code=502,
            detail=f"无法验证安全性（读取库列表失败: {e}），删除已拒绝",
        )

    for lib in libraries:
        for loc in lib.get('locations') or []:
            loc_norm = _normalize_for_compare(loc)
            if not loc_norm:
                continue
            # 完全相等：item 就是库根
            if item_norm == loc_norm:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"拒绝删除：该路径是媒体库 \"{lib.get('name')}\" 的根目录 ({loc})。"
                        f"删除会把整个库扔掉。如果确实想移除这个库，请去 Jellyfin 后台操作。"
                    ),
                )
            # item 是库 location 的祖先目录（极少见但灾难性）
            if loc_norm.startswith(item_norm + '/'):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"拒绝删除：该路径 ({item_path}) 是媒体库 \"{lib.get('name')}\" "
                        f"根目录 ({loc}) 的祖先目录。这种删除范围超出预期，已拦截。"
                    ),
                )


def _find_library_for_path(client: JellyfinClient, item_path: str) -> Optional[Dict]:
    """
    找到包含 item_path 的库。返回 library dict 或 None。
    """
    if not item_path:
        return None
    p_norm = item_path.replace('\\', '/').rstrip('/').lower()
    try:
        libs = client.get_libraries_normalized()
    except Exception:
        return None
    for lib in libs:
        for loc in lib.get('locations') or []:
            loc_norm = loc.replace('\\', '/').rstrip('/').lower()
            if p_norm == loc_norm or p_norm.startswith(loc_norm + '/'):
                return lib
    return None


@router.delete("/items/{item_id}")
async def delete_item(item_id: str):
    """
    删除 Jellyfin 条目。

    流程：
      1. 调 Jellyfin DELETE API（连同物理文件，前提是 API key 用户有 EnableContentDeletion 权限）
      2. 如 Jellyfin 拒绝（典型场景：Folder 类型的"伪条目"如 Box.Cover/Poster 子目录，
         Jellyfin DB 里有 item 记录但实际无元数据，DELETE 会 500），且路径通过安全校验，
         降级为后端直接物理删除 + 触发 Jellyfin scan_changes 清理孤儿 item

    安全保护：删除前会拒绝任何等于库根 / 库根祖先 / 系统根的 path
    """
    from web.backend.path_translator import translate_path_with_settings

    client = _client()

    # 先查 item 信息做安全校验
    try:
        item = client.get_item(item_id, fields='Path,Name,Type')
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"读取条目失败: {e}")

    if not item:
        raise HTTPException(status_code=404, detail=f"条目不存在: {item_id}")

    item_path = item.get('Path') or ''
    item_name = item.get('Name') or ''

    # 阻断危险删除
    _assert_safe_to_delete(item_path, item_name)

    # ── Phase 1: 先尝试 Jellyfin DELETE ──
    jellyfin_error: Optional[str] = None
    try:
        client.delete_item(item_id)
        return {"success": True, "method": "jellyfin", "item_id": item_id}
    except Exception as e:
        jellyfin_error = str(e)
        logger.warning(
            f"Jellyfin DELETE 失败，尝试物理删除 fallback: {item_id} "
            f"(path={item_path}) - {e}"
        )

    # ── Phase 2: 物理删除 fallback ──
    if not item_path:
        # 没有路径就没法 fallback
        raise HTTPException(
            status_code=502,
            detail=f"Jellyfin 删除失败: {jellyfin_error}（条目无 Path，无法物理删除 fallback）",
        )

    # 路径映射（Linux→Windows 等）
    try:
        local_path_str = translate_path_with_settings(item_path) or item_path
    except Exception:
        local_path_str = item_path

    p = Path(local_path_str)
    if not p.exists():
        raise HTTPException(
            status_code=502,
            detail=(
                f"Jellyfin 删除失败: {jellyfin_error}；"
                f"物理路径不存在，无法 fallback 删除（{local_path_str}）"
            ),
        )

    # 物理删除
    try:
        if p.is_file() or p.is_symlink():
            p.unlink()
            kind = '文件'
        elif p.is_dir():
            shutil.rmtree(p)
            kind = '目录'
        else:
            raise RuntimeError(f"未知路径类型: {local_path_str}")
    except Exception as e:
        logger.exception(f"物理删除失败: {local_path_str}")
        raise HTTPException(
            status_code=500,
            detail=(
                f"Jellyfin 删除失败: {jellyfin_error}；"
                f"物理删除也失败: {e}"
            ),
        )

    # 触发包含该路径的库 scan_changes —— Jellyfin 会清理 DB 里的孤儿 item
    refreshed_lib_id = None
    lib = _find_library_for_path(client, item_path)
    if lib:
        try:
            client.refresh_library(lib['id'], mode='scan_changes')
            refreshed_lib_id = lib['id']
        except Exception as e:
            logger.warning(f"物理删除后刷新库失败: {e}")

    return {
        "success": True,
        "method": "physical_delete",
        "item_id": item_id,
        "deleted_kind": kind,
        "deleted_path": local_path_str,
        "library_refreshed": refreshed_lib_id,
        "warning": (
            f"Jellyfin DELETE 不可用（{jellyfin_error}），已通过物理删除 + 重扫库清理"
        ),
    }


@router.post("/items/{item_id}/identify-apply")
async def identify_apply(item_id: str, req: IdentifyApplyRequest):
    """把选中的候选 apply 到现有条目，Jellyfin 会自动刷新元数据。"""
    if not req.candidate:
        raise HTTPException(status_code=400, detail="缺少 candidate")

    client = _client()
    try:
        client.remote_search_apply(
            item_id=item_id,
            candidate=req.candidate,
            replace_all_images=req.replace_all_images,
        )
    except Exception as e:
        logger.exception("Jellyfin remote_search_apply 调用失败")
        raise HTTPException(status_code=502, detail=f"Jellyfin 应用候选失败: {e}")

    return {"success": True, "item_id": item_id}


@router.get("/libraries/{library_id}/stats")
async def library_stats(
    library_id: str,
    force_refresh: bool = False,
    db: Session = Depends(get_db),
):
    """
    单个库的聚合统计：视频数 / 总大小 / 缺海报 / 健康度 / 总时长。

    缓存：内存级 2 小时 TTL。force_refresh=true 时跳过缓存重算。
    """
    if not force_refresh:
        cached = _get_cached_lib_stats(library_id)
        if cached is not None:
            return cached

    client = _client()
    libs = client.get_libraries_normalized()
    target = next((l for l in libs if l['id'] == library_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="库不存在")

    collection_type = target['collection_type']

    # ---- 1. 文件系统聚合（按库类型只统计相关扩展名） ----
    total_size = 0
    video_count = 0
    sub_count = 0
    audio_count = 0
    image_count = 0
    accessible = True

    # 是否需要扫各类
    scan_video = collection_type in ('movies', 'tvshows', 'musicvideos', 'homevideos', 'mixed')
    scan_audio = collection_type in ('music', 'mixed')
    scan_image = collection_type in ('photos', 'mixed')

    from web.backend.path_translator import translate_path_with_settings

    for loc in target['locations']:
        # path_translator 把 Jellyfin 视角的 /library/videos 翻译成 Z:/videos
        # 之前漏了这一步，rglob 直接用 Jellyfin 路径在 Windows 后端啥都扫不到 → 0 字节
        local_loc = translate_path_with_settings(loc) or loc
        if not Path(local_loc).exists():
            accessible = False
            continue
        try:
            for f in Path(local_loc).rglob('*'):
                if not f.is_file():
                    continue
                ext = f.suffix.lower()
                if scan_video and ext in VIDEO_EXTS:
                    video_count += 1
                    try:
                        total_size += f.stat().st_size
                    except OSError:
                        pass
                elif scan_video and ext in SUBTITLE_EXTS:
                    sub_count += 1
                elif scan_audio and ext in AUDIO_EXTS:
                    audio_count += 1
                    try:
                        total_size += f.stat().st_size
                    except OSError:
                        pass
                elif scan_image and ext in IMAGE_EXTS:
                    image_count += 1
                    try:
                        total_size += f.stat().st_size
                    except OSError:
                        pass
        except (PermissionError, OSError):
            accessible = False

    # ---- 2. Jellyfin 元数据聚合（按库类型选 item_types） ----
    type_map = {
        'movies': 'Movie',
        'tvshows': 'Series',
        'music': 'MusicAlbum,Audio',
        'musicvideos': 'MusicVideo',
        'homevideos': 'Video',
        'photos': 'Photo',
        'boxsets': 'BoxSet',
        'books': 'Book',
        'mixed': 'Movie,Series',
    }
    item_types = type_map.get(collection_type, 'Movie,Series')

    try:
        # compute_health 需要 Path / Name / Type / RunTimeTicks 等字段
        items = client.get_library_items(
            library_id,
            item_types=item_types,
            fields='ImageTags,ProviderIds,Path,RunTimeTicks,ChildCount',
        )
    except Exception:
        items = []

    movies_count = sum(1 for i in items if i.get('Type') == 'Movie')
    series_count = sum(1 for i in items if i.get('Type') == 'Series')
    items_with_poster = sum(1 for i in items if (i.get('ImageTags') or {}).get('Primary'))
    items_with_tmdb = sum(1 for i in items if (i.get('ProviderIds') or {}).get('Tmdb'))

    # 健康度：调用 compute_health 给每个 item 打分，统计有问题的数量
    from web.backend.api._item_health import compute_health
    items_with_issues = 0
    for it in items:
        try:
            h = compute_health(it)
            if h.get('issues'):
                items_with_issues += 1
        except Exception:
            pass
    items_total = len(items)
    items_healthy = items_total - items_with_issues

    # ---- 3. 总时长（RunTimeTicks 累加；剧集库 Series 本身没有时长，需额外拉 Episode）----
    total_runtime_ticks = sum(int(i.get('RunTimeTicks') or 0) for i in items)
    if collection_type == 'tvshows' or (collection_type == 'mixed' and series_count > 0):
        try:
            episodes = client.get_library_items(
                library_id,
                item_types='Episode',
                fields='RunTimeTicks',
            )
            total_runtime_ticks += sum(int(e.get('RunTimeTicks') or 0) for e in episodes)
        except Exception as e:
            logger.warning(f"拉取 Episode 时长失败: {e}")
    # 1 RunTimeTick = 100ns，转秒：÷ 10_000_000
    total_runtime_seconds = total_runtime_ticks // 10_000_000

    result = {
        "library": {
            "id": target['id'],
            "name": target['name'],
            "collection_type": collection_type,
            "locations": target['locations'],
            "accessible": accessible,
        },
        "filesystem": {
            "video_count": video_count,
            "subtitle_count": sub_count,
            "audio_count": audio_count,
            "image_count": image_count,
            "total_size_bytes": total_size,
            "total_size_gb": round(total_size / (1024 ** 3), 2),
        },
        "jellyfin": {
            "movies": movies_count,
            "series": series_count,
            "total_items": items_total,
            "with_poster": items_with_poster,
            "without_poster": items_total - items_with_poster,
            "with_tmdb_id": items_with_tmdb,
            # 健康度：基于 _item_health.compute_health（含未识别、嵌套主文件、name/year 错位等）
            "items_healthy": items_healthy,
            "items_with_issues": items_with_issues,
            "health_ratio": round(items_healthy / items_total, 3) if items_total else 1.0,
            # 总时长（秒）：电影库直接累加 Movie.RunTimeTicks；
            # 剧集库额外拉 Episode 累加（Series 本身的 RunTimeTicks 通常为 0）
            "total_runtime_seconds": total_runtime_seconds,
        },
    }
    _set_cached_lib_stats(library_id, result)
    return result


@router.get("/libraries/{library_id}/subtitle-stats")
async def library_subtitle_stats(
    library_id: str,
    background_tasks: BackgroundTasks,
    max_age_minutes: int = 60,
    force_refresh: bool = False,
    db: Session = Depends(get_db),
):
    """
    懒加载某库的"缺字幕"统计。

    流程：
      1. （非 force_refresh 时）查 DB 里 max_age_minutes 内、状态为 completed 且覆盖
         此 library_id 的 subtitle_scan 任务 → 直接返回该任务的 without_required
      2. 查 DB 里有没有正在运行的 subtitle_scan 任务覆盖此库 → 返回 task_id 给前端轮询
      3. 都没有（或 force_refresh） → 立即启动新任务

    force_refresh=true 时跳过步骤 1，直接走 2/3。

    返回：
      - status: 'ready' | 'running'
      - task_id: 任务 id（前端可以轮询）
      - without_required / total_videos / completed_at（仅 status=ready 时）
    """
    from web.backend.api.subtitle import _resolve_scope, run_subtitle_scan
    from web.backend.api.tasks import create_task

    # 检查库是否存在
    client = _client()
    libs = client.get_libraries_normalized()
    target = next((l for l in libs if l['id'] == library_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="库不存在")

    cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)

    def _task_covers_lib(task: Task) -> bool:
        """看 task.result.library_ids 里是否包含本库 ID"""
        if not task.result:
            return False
        try:
            r = json.loads(task.result)
            if isinstance(r, dict):
                return library_id in (r.get('library_ids') or [])
        except Exception:
            return False
        return False

    # 1. 找最近的 completed 任务（force_refresh 时跳过这一步）
    if not force_refresh:
        recent_completed = (
            db.query(Task)
            .filter(Task.task_type == 'subtitle_scan')
            .filter(Task.status == 'completed')
            .filter(Task.completed_at >= cutoff)
            .order_by(Task.completed_at.desc())
            .all()
        )
        for t in recent_completed:
            if _task_covers_lib(t):
                try:
                    r = json.loads(t.result) if t.result else {}
                except Exception:
                    r = {}
                return {
                    'status': 'ready',
                    'task_id': t.id,
                    'without_required': r.get('without_required', 0),
                    'total_videos': r.get('total_videos', 0),
                    'with_subtitles': r.get('with_subtitles', 0),
                    'completed_at': t.completed_at.isoformat() if t.completed_at else None,
                    'cached_age_minutes': int((datetime.utcnow() - t.completed_at).total_seconds() / 60) if t.completed_at else 0,
                }

    # 2. 找正在运行的任务（pending / running）
    running = (
        db.query(Task)
        .filter(Task.task_type == 'subtitle_scan')
        .filter(Task.status.in_(['pending', 'running']))
        .order_by(Task.created_at.desc())
        .all()
    )
    for t in running:
        # running 中的 result 可能还没写 library_ids（要等任务完成才写完整）
        # 兜底：用 message 含 "库 {name}" 匹配
        if _task_covers_lib(t) or (target.get('name') and target['name'] in (t.message or '')):
            return {
                'status': 'running',
                'task_id': t.id,
                'message': t.message,
                'progress': t.progress,
            }

    # 3. 都没有 → 启动新任务
    if not target.get('locations'):
        raise HTTPException(status_code=400, detail=f"库 {target['name']} 没有配置任何路径")

    paths, _, label, refresh_ids = _resolve_scope(library_id=library_id)
    expected_langs = settings.preferred_langs
    task = create_task(db, "subtitle_scan", f"扫描: {label}（{len(paths)} 个路径）")
    background_tasks.add_task(
        run_subtitle_scan,
        task.id,
        paths,
        True,  # recursive
        expected_langs,
        list(refresh_ids or []),
    )
    return {
        'status': 'running',
        'task_id': task.id,
        'message': '已启动后台扫描',
        'progress': 0,
    }


# ---------- 触发刷新 ----------

@router.post("/libraries/{library_id}/refresh")
async def refresh_library(library_id: str, mode: str = 'scan_changes'):
    """
    触发某媒体库刷新。

    mode:
      - scan_changes      扫描新的和有修改的文件（默认，最快）
      - missing_metadata  搜索缺少的元数据
      - replace_all       覆盖所有元数据
    """
    client = _client()
    ok = client.refresh_library(library_id, mode=mode)
    if not ok:
        raise HTTPException(status_code=500, detail="刷新触发失败，查看后端日志")
    return {"ok": True, "library_id": library_id, "mode": mode}


@router.post("/refresh-all")
async def refresh_all():
    """触发全局媒体库刷新（耗时操作，Jellyfin 内部异步执行）"""
    client = _client()
    ok = client.refresh_all_libraries()
    if not ok:
        raise HTTPException(status_code=500, detail="全局刷新触发失败")
    return {"ok": True}


# ---------- 系统信息 ----------

@router.get("/system")
async def get_system():
    """获取 Jellyfin 系统信息"""
    client = _client()
    info = client.get_system_info()
    if not info:
        raise HTTPException(status_code=502, detail="无法获取 Jellyfin 系统信息")
    return {
        "version": info.get('Version'),
        "operating_system": info.get('OperatingSystem'),
        "server_name": info.get('ServerName'),
        "id": info.get('Id'),
        "transcoding_temp_path": info.get('TranscodingTempPath'),
        "config_path": info.get('ConfigurationPath'),
        "data_path": info.get('DataPath'),
        "log_path": info.get('LogPath'),
    }


# ---------- 给其他模块用的辅助函数 ----------

def get_library_by_id(library_id: str) -> Optional[Dict]:
    """让其他 API 模块用：根据 ID 查库（含路径），失败抛 HTTPException。"""
    client = _client()
    libs = client.get_libraries_normalized()
    return next((l for l in libs if l['id'] == library_id), None)


# ---------- 路径 → Item 反查（给番号库等用） ----------

# 内存缓存：path → item，TTL 30 秒
import time as _time
_PATH_INDEX_CACHE = {"data": {}, "ts": 0.0}
_PATH_INDEX_TTL = 30  # 秒


def _build_path_index() -> Dict[str, Dict]:
    """
    构建 Jellyfin 路径 → 简要 Item 信息的索引。
    一次拉所有 Movie/Series 的 Path，30 秒内复用。
    """
    now = _time.time()
    if now - _PATH_INDEX_CACHE['ts'] < _PATH_INDEX_TTL and _PATH_INDEX_CACHE['data']:
        return _PATH_INDEX_CACHE['data']

    if not settings.jellyfin_api_key:
        return {}

    try:
        client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
        items = client.get_items_by_type(
            item_types='Movie,Series',
            fields='Path,ImageTags',
        ) if hasattr(client, 'get_items_by_type') else []
        # 兼容旧版本：用 get_library_items 全量拉
        if not items:
            try:
                items = client._request('GET', '/Items', params={
                    'Recursive': 'true',
                    'IncludeItemTypes': 'Movie,Series',
                    'Fields': 'Path,ImageTags',
                    'Limit': 0,
                }) or {}
                items = items.get('Items', [])
            except Exception:
                items = []
    except Exception as e:
        logger.warning(f"构建 Jellyfin 路径索引失败: {e}")
        return {}

    index = {}
    for it in items:
        path = it.get('Path')
        if not path:
            continue
        index[path] = {
            'id': it.get('Id'),
            'name': it.get('Name'),
            'type': it.get('Type'),
            'has_image': bool((it.get('ImageTags') or {}).get('Primary')),
        }

    _PATH_INDEX_CACHE['data'] = index
    _PATH_INDEX_CACHE['ts'] = now
    return index


def lookup_jellyfin_item(file_path: str) -> Optional[Dict]:
    """
    根据本地文件路径反查 Jellyfin Item。
    匹配策略：精确路径，否则匹配父目录（适合"一个文件夹一部电影"）。
    """
    if not file_path:
        return None
    index = _build_path_index()
    if file_path in index:
        return index[file_path]
    # 退化：检查父目录
    from pathlib import Path as _P
    parent = str(_P(file_path).parent)
    if parent in index:
        return index[parent]
    return None


def jellyfin_web_url(item_id: str) -> Optional[str]:
    """生成 Jellyfin Web 详情页 URL"""
    if not item_id or not settings.jellyfin_host:
        return None
    return f"{settings.jellyfin_host.rstrip('/')}/web/#/details?id={item_id}"


def invalidate_path_index():
    """让缓存失效（删除/同步操作后调用）"""
    _PATH_INDEX_CACHE['ts'] = 0.0


@router.get("/items/by-path")
async def query_item_by_path(path: str):
    """根据本地文件路径反查 Jellyfin Item"""
    item = lookup_jellyfin_item(path)
    if not item:
        return {"found": False}
    return {
        "found": True,
        "item": item,
        "web_url": jellyfin_web_url(item['id']),
    }


def trigger_refresh(library_id: str, mode: str = 'scan_changes'):
    """让其他 API 模块用：异步触发刷新，失败仅记日志不抛错。"""
    if not settings.jellyfin_api_key:
        return
    try:
        client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
        client.refresh_library(library_id, mode=mode)
    except Exception as e:
        logger.warning(f"触发库刷新失败: {library_id} - {e}")
