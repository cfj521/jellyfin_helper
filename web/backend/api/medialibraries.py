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
from typing import Any, Dict, List, Optional, Tuple

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
# TTL 从 settings.cache_library_stats_minutes（分钟）读取；改值后需重启后端
def _lib_stats_ttl():
    return _td(minutes=max(1, settings.cache_library_stats_minutes))


def _stats_cache_key(library_id: str, fields: Optional[set] = None) -> str:
    """缓存 key 加上 fields 维度，避免不同 fields 互相覆盖。"""
    if fields is None:
        return library_id
    return library_id + '|' + ','.join(sorted(fields))


def _get_cached_lib_stats(library_id: str, fields: Optional[set] = None) -> Optional[Dict]:
    key = _stats_cache_key(library_id, fields)
    with _LIB_STATS_LOCK:
        entry = _LIB_STATS_CACHE.get(key)
    if not entry:
        return None
    age = _dt.utcnow() - entry['cached_at']
    if age > _lib_stats_ttl():
        return None
    # 返回浅拷贝，附加 _cache 字段告知前端这是缓存
    out = dict(entry['data'])
    out['_cached'] = True
    out['_cached_at'] = entry['cached_at'].isoformat()
    out['_cache_age_seconds'] = int(age.total_seconds())
    return out


def _set_cached_lib_stats(library_id: str, data: Dict, fields: Optional[set] = None):
    key = _stats_cache_key(library_id, fields)
    with _LIB_STATS_LOCK:
        _LIB_STATS_CACHE[key] = {'data': data, 'cached_at': _dt.utcnow()}


def _invalidate_lib_stats_cache(library_id: Optional[str] = None):
    """library_id=None 时清空全部缓存（用于"全部强制刷新"）"""
    with _LIB_STATS_LOCK:
        if library_id is None:
            _LIB_STATS_CACHE.clear()
        else:
            # 删 library_id 下的所有 fields 变体
            for k in list(_LIB_STATS_CACHE.keys()):
                if k == library_id or k.startswith(library_id + '|'):
                    del _LIB_STATS_CACHE[k]


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
def list_libraries(check_paths: bool = True):
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
    # 标记成人库：前端用来分流详情页（普通线 vs AdultLibraryView）
    adult_ids = set(settings.adult_library_ids or [])
    for lib in libraries:
        item_id = lib.get('primary_image_item_id') or lib.get('id')
        if host and item_id:
            # 原图尺寸 + quality=90：局域网场景下不限制尺寸，保留最佳清晰度
            lib['cover_url'] = (
                f"{host}/Items/{item_id}/Images/Primary"
                f"?quality=90&api_key={api_key}"
            )
        else:
            lib['cover_url'] = None
        lib['is_adult'] = lib.get('id') in adult_ids

    return {
        "count": len(libraries),
        "libraries": libraries,
    }


@router.get("/libraries/{library_id}/items")
def get_library_items(
    library_id: str,
    item_type: Optional[str] = None,  # Movie / Series / Episode；不传按 collection_type 自动推断
    start_index: int = 0,
    limit: int = 50,
    search: Optional[str] = None,
    years: Optional[str] = None,        # 多个年份逗号分隔："2023,2024"
    genres: Optional[str] = None,       # 多个 genre 管道分隔："Action|Comedy"
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
            "CommunityRating,OfficialRating,RunTimeTicks,MediaSources,MediaStreams,ChildCount,Overview,"
            "OriginalTitle,Genres"
        ),
        search_term=search,
        years=years,
        genres=genres,
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


class _ItemIdsBatchReq(BaseModel):
    ids: List[str]


@router.post("/items/subtitle-langs")
def items_subtitle_langs(req: _ItemIdsBatchReq):
    """
    批量拉指定 item 的字幕语言列表。

    背景：jellyfin /Items 列表接口对 Fields=MediaStreams / MediaSources 经常返回精简
    版本（不含 MediaStreams 子字段）。要可靠拿到字幕流必须用 /Items?Ids=xxx,yyy 单条
    模式 + Fields=MediaSources（这个模式 jellyfin 会嵌入 MediaStreams）。

    Body: {"ids": ["abc...", "def..."]}
    Returns: {"langs": {"abc...": ["chs", "eng"], "def...": []}}
    """
    if not req.ids:
        return {"langs": {}}
    client = _client()
    # 一次性最多 200 个 ID（再多 jellyfin URL 太长）
    out: Dict[str, List[str]] = {}
    BATCH = 200
    for i in range(0, len(req.ids), BATCH):
        chunk = req.ids[i:i + BATCH]
        try:
            r = client._request('GET', '/Items', params={
                'Ids': ','.join(chunk),
                # Path / MediaSources / MediaStreams 在 Items?Ids 模式下能拿到完整 streams
                'Fields': 'Path,MediaSources,MediaStreams',
            }) or {}
            for it in r.get('Items') or []:
                out[it.get('Id') or ''] = _extract_subtitle_langs(it)
        except Exception as e:
            logger.warning(f"批量拉字幕语言失败 chunk={chunk[:3]}...: {e}")
            continue
    # 没命中的 ID 也填空数组（前端简化判定）
    for iid in req.ids:
        out.setdefault(iid, [])
    return {"langs": out}


@router.get("/libraries/{library_id}/genres")
def get_library_genres(library_id: str):
    """
    返回该库下所有 Genre 名称（用于前端"风格"过滤下拉的 options）。
    Jellyfin /Genres?ParentId=lib&Recursive=true 直接给汇总。
    """
    client = _client()
    try:
        result = client._request('GET', '/Genres', params={
            'ParentId': library_id,
            'Recursive': 'true',
            'IncludeItemTypes': 'Movie,Series',  # 只统计真正的内容层
            'Fields': '',
            'Limit': 500,
        }) or {}
    except Exception as e:
        logger.warning(f"拉取库 genres 失败 lib={library_id}: {e}")
        return {"genres": []}

    items = result.get('Items') or []
    # 结果按 Name 排序后返回（Jellyfin 自身可能无序）
    genres = sorted({(it.get('Name') or '').strip() for it in items if it.get('Name')})
    return {"genres": list(genres)}


# ============================================================
# Series → Season → Episode 钻取（带 30 分钟内存缓存）
# ============================================================

import threading
import time
from common.jellyfin_client import JellyfinClient as _JfClientType  # alias for typing

# TTL：从 settings.cache_tree_children_minutes 读，模块加载时一次性算成秒
# 改值需要重启后端（_TTLCache 实例上的 ttl 是 init 时定的，不会跟 settings 同步）
_CHILDREN_CACHE_TTL = max(1, settings.cache_tree_children_minutes) * 60


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


_JF_LANG_NORMALIZE = {
    # 中文：ISO + 常见非标
    'chi': 'chs', 'zho': 'chs', 'zh': 'chs', 'zh-cn': 'chs', 'zh_cn': 'chs',
    'zh-hans': 'chs', 'zh_hans': 'chs', 'cmn': 'chs',
    'chs': 'chs', 'gb': 'chs', 'gb2312': 'chs', 'gbk': 'chs',
    # 繁体
    'cht': 'cht', 'zh-tw': 'cht', 'zh_tw': 'cht', 'zh-hk': 'cht',
    'zh-hant': 'cht', 'zh_hant': 'cht', 'big5': 'cht',
    # 英语
    'eng': 'eng', 'en': 'eng', 'en-us': 'eng', 'en-gb': 'eng',
    # 其它常见
    'jpn': 'jpn', 'jap': 'jpn', 'ja': 'jpn',
    'kor': 'kor', 'ko': 'kor',
    'fre': 'fre', 'fra': 'fre', 'fr': 'fre',
    'ger': 'ger', 'deu': 'ger', 'de': 'ger',
    'spa': 'spa', 'es': 'spa',
    'rus': 'rus', 'ru': 'rus',
    'ita': 'ita', 'it': 'ita',
    # 未定义 / 未识别（jellyfin 给 'und' 表示无语言信息，常见于外挂字幕没标 metadata）
    'und': 'und', 'undefined': 'und', 'mis': 'und', 'mul': 'und', 'unknown': 'und',
}


# 简体关键词（任一命中 → chs）
_CHS_HINTS = (
    '简体', '簡體', '简中', '简化', 'chs', 'simplified',
    'gb2312', 'gbk', 'gb18030', '.cn.', '-cn.', '_cn.',
    'sc.', 'zh-cn', 'zh-hans', 'zhs',
)
# 繁体关键词
_CHT_HINTS = (
    '繁体', '繁體', '繁中', 'cht', 'traditional',
    'big5', '.tw.', '-tw.', '_tw.', '.hk.',
    'tc.', 'zh-tw', 'zh-hk', 'zh-hant', 'zht',
)
# 通用中文（不分简繁）→ 默认归 chs；外挂字幕常这种状态
_GENERIC_CHINESE_HINTS = ('chinese', '中文', 'mandarin')


def _extract_subtitle_langs(i: Dict) -> List[str]:
    """
    从 jellyfin Item 的 MediaStreams / MediaSources 中提取字幕语言代码列表。

    判定优先级（任一命中即停）：
      1. Title / DisplayTitle / Path（含外挂字幕文件名）含简繁关键词 → chs / cht
      2. 同上含通用 "Chinese" / "中文" 但不分简繁 → 兜底 chs
      3. Language 字段映射 → chs / cht / eng / jpn / ...
      4. 都没识别出 → 原 Language 小写截断（保留小众语言）；空则丢弃
    """
    out: List[str] = []
    seen: set = set()

    def _push(code):
        if not code:
            return
        if code in seen:
            return
        seen.add(code)
        out.append(code)

    streams = i.get('MediaStreams') or []
    if not streams:
        ms_list = i.get('MediaSources') or []
        if ms_list and isinstance(ms_list[0], dict):
            streams = ms_list[0].get('MediaStreams') or []

    for s in streams:
        if not isinstance(s, dict):
            continue
        if s.get('Type') != 'Subtitle':
            continue

        # 把可识别的字符串都串起来一起判（Title / DisplayTitle / Path 文件名）
        haystack_parts = [
            s.get('Title') or '',
            s.get('DisplayTitle') or '',
            s.get('Path') or '',  # 外挂字幕文件名常带 .chs.srt 这种线索
        ]
        haystack = ' '.join(p.lower() for p in haystack_parts if p)

        # 1. 简繁明确关键词
        if any(h in haystack for h in _CHT_HINTS):
            _push('cht')
            continue
        if any(h in haystack for h in _CHS_HINTS):
            _push('chs')
            continue

        # 2. Language 字段（三字母 / 双字母 / 带连字符的）
        raw_lang = (s.get('Language') or '').strip().lower()
        if raw_lang:
            mapped = _JF_LANG_NORMALIZE.get(raw_lang)
            if mapped:
                _push(mapped)
                continue

        # 3. 通用中文（"Chinese" / "中文" 不分简繁）→ 兜底 chs
        if any(h in haystack for h in _GENERIC_CHINESE_HINTS):
            _push('chs')
            continue

        # 4. 兜底：原 Language 截断（保留小众语言）
        if raw_lang:
            _push(raw_lang[:5])
        else:
            # jellyfin 确认这是 Subtitle 流但 Language/Title/Path 全无线索
            # （典型：外挂 .ass 没标 metadata，DisplayTitle="未定义 - ASS - 外部"）
            # 至少标记一个"未知"，让用户知道"有字幕只是不确定语言"——比啥都不显示好
            _push('und')
    return out


def _build_item_dict(i: Dict, host: str) -> Dict:
    """
    把 Jellyfin 原始 item dict 转成前端表格需要的扁平 dict。
    Series / Season / Episode 共用此函数，按 type 字段填不同语义。
    """
    from web.backend.api._item_health import compute_health, extract_suggested_title_year
    from common.label_cleaner import clean_label_list

    item_id = i.get('Id')
    item_type = i.get('Type')
    image_tag = (i.get('ImageTags') or {}).get('Primary')
    # Episode 的缩略图是 Type='Primary' 但语义上是"thumb"
    # 列表视图 56x80 缩略图 + 网格视图卡片 ~315px 高都用同一个 URL；
    # fillHeight=600 兼顾两种场景：缩略图 CSS 缩小一样清晰，网格卡片不糊
    poster_url = (
        f"{host}/Items/{item_id}/Images/Primary?fillHeight=600&tag={image_tag}&quality=90"
        if (host and image_tag) else None
    )
    detail_url = f"{host}/web/index.html#!/details?id={item_id}" if host else None

    # 演员统计：仅 Series/Movie 层级才有意义
    people = i.get('People') or []
    actors = [p for p in people if p.get('Type') == 'Actor']
    actors_total = len(actors)
    actors_with_image = sum(1 for p in actors if p.get('PrimaryImageTag'))

    suggested_title, suggested_year = extract_suggested_title_year(i)

    # 顶层 RunTimeTicks 在某些 jellyfin 版本对 Movie 返回 None，
    # 但 MediaSources[0].RunTimeTicks 通常有值 —— 双重兜底
    runtime_ticks = i.get('RunTimeTicks') or 0
    if not runtime_ticks:
        ms = i.get('MediaSources') or []
        if ms and isinstance(ms[0], dict):
            runtime_ticks = ms[0].get('RunTimeTicks') or 0
    runtime_min = round(runtime_ticks / 600_000_000.0, 1) if runtime_ticks else None

    # 树层级：Series=0, Season=1, Episode=2；用于前端 padding-left 缩进
    level = {'Season': 1, 'Episode': 2}.get(item_type, 0)
    return {
        "id": item_id,
        "name": i.get('Name'),
        "type": item_type,
        "level": level,
        "year": i.get('ProductionYear'),
        "path": i.get('Path'),
        "tmdb_id": (i.get('ProviderIds') or {}).get('Tmdb'),
        "imdb_id": (i.get('ProviderIds') or {}).get('Imdb'),
        "has_image": bool(image_tag),
        "poster_url": poster_url,
        "detail_url": detail_url,
        "edit_url": detail_url,
        "actors_total": actors_total,
        "actors_with_image": actors_with_image,
        "community_rating": i.get('CommunityRating'),
        "official_rating": i.get('OfficialRating'),
        # 风格类型（动作/喜剧/科幻 等），表格"风格"列用；过 label_cleaner 去标点 / 重复
        "genres": clean_label_list(i.get('Genres')),
        # 字幕语言：MediaStreams 里 Type=Subtitle 的归一化语言代码（chs/cht/eng/jpn/...）
        "subtitle_langs": _extract_subtitle_langs(i),
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
def get_series_seasons(series_id: str, force: bool = False):
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
def get_season_episodes(season_id: str, force: bool = False):
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
def clear_children_cache():
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
def get_series_aggregates(
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
def identify_search(item_id: str, req: IdentifySearchRequest):
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
def get_sample_evidence(item_id: str):
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


# ============================================================================
# 物理删除 fallback 的目录安全策略
# ============================================================================

# 黑名单关键字：目录名（不区分大小写）含这些词时绝不允许 rmtree
# 这些通常是用户的"暂存 / 待整理"容器，里面塞了多部不同作品
_DIRECTORY_DELETE_BLACKLIST = (
    'unorganized', 'unsorted', 'inbox', 'staging', 'incoming', 'temp', 'tmp',
    'mixed', 'working', 'dump', 'sandbox', 'misc', 'todo',
    '待整理', '未整理', '暂存', '混合',
)

# 视频扩展名（同 _build_path_index 里 Jellyfin 关心的）
_VIDEO_EXTS_FOR_DELETE = {'.mp4', '.mkv', '.avi', '.wmv', '.mov', '.flv', '.webm', '.m4v', '.ts', '.rmvb'}
# 常见作品附件扩展（允许跟主文件一起被 rmtree）
_ATTACHMENT_EXTS = {
    '.nfo', '.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif',
    '.srt', '.ass', '.ssa', '.vtt', '.sub', '.idx', '.smi',
    '.txt', '.url',
}


def _safe_directory_delete(p: Path, item_id: str) -> Tuple[str, List[str]]:
    """
    严格作品目录安全删除。
    入参 p 是要尝试删除的目录；item_id 是当前要删的 jellyfin item。

    返回:
      (kind, deleted_paths)
        kind: '目录' 或 '文件（容器目录保留）'
        deleted_paths: 实际被删除的路径列表
        如果不满足任何安全条件，返回 ('', [])，调用方会抛 400 拒绝执行
    """
    # 黑名单：目录名 / 任一祖先目录名命中关键字 → 绝对拒绝 rmtree
    parts_lower = [seg.lower() for seg in p.parts]
    for kw in _DIRECTORY_DELETE_BLACKLIST:
        if any(kw in seg for seg in parts_lower):
            logger.warning(
                f"_safe_directory_delete: 目录 {p} 命中黑名单关键字 '{kw}'，"
                f"拒绝 rmtree。仅删除 item 直接关联的视频文件 + 同 stem 附属"
            )
            return _delete_only_item_files(p, item_id)

    # 收集目录内所有视频文件
    try:
        all_videos = [
            f for f in p.rglob('*')
            if f.is_file() and f.suffix.lower() in _VIDEO_EXTS_FOR_DELETE
        ]
    except OSError as e:
        logger.warning(f"_safe_directory_delete: 扫描目录失败 {p}: {e}")
        return '', []

    if not all_videos:
        # 没有视频文件 —— 这个目录大概率不是作品目录，也不该被 rmtree
        logger.warning(f"_safe_directory_delete: 目录 {p} 没有视频文件，拒绝 rmtree")
        return '', []

    # 反查每个视频文件对应的 jellyfin item，检查是否都属于"当前 item"
    # path index 里 1 个目录可能映射到 1 个 Movie/Series；只要有任何一个视频
    # 反查到的 jellyfin id 与当前不同（即另一部作品也在这个目录里），就不能 rmtree
    other_items: set = set()
    for v in all_videos:
        info = lookup_jellyfin_item(str(v))
        if not info:
            # 这个视频在 jellyfin DB 里没记录 → 陌生文件 → 拒绝
            logger.warning(
                f"_safe_directory_delete: 目录 {p} 含 jellyfin 未记录的视频 {v}，"
                f"拒绝 rmtree（可能是用户尚未刮削 / 别处的作品）"
            )
            return _delete_only_item_files(p, item_id)
        other_id = info.get('id')
        if other_id and other_id != item_id:
            other_items.add(other_id)

    if other_items:
        logger.warning(
            f"_safe_directory_delete: 目录 {p} 还含其它 jellyfin item "
            f"({len(other_items)} 个: {list(other_items)[:3]})，拒绝 rmtree"
        )
        return _delete_only_item_files(p, item_id)

    # 检查"陌生大文件"：目录里除了视频/附件外，有没有别的大于 1MB 的文件
    try:
        for f in p.rglob('*'):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext in _VIDEO_EXTS_FOR_DELETE or ext in _ATTACHMENT_EXTS:
                continue
            try:
                if f.stat().st_size > 1024 * 1024:
                    logger.warning(
                        f"_safe_directory_delete: 目录 {p} 含陌生大文件 {f}，拒绝 rmtree"
                    )
                    return _delete_only_item_files(p, item_id)
            except OSError:
                continue
    except OSError:
        pass

    # 全部条件通过 → 允许 rmtree
    logger.info(f"_safe_directory_delete: 严格作品目录校验通过，rmtree {p}")
    shutil.rmtree(p)
    return '目录', [str(p)]


def _delete_only_item_files(p: Path, item_id: str) -> Tuple[str, List[str]]:
    """
    退化方案：不动容器目录，只删 item 关联的视频文件 + 同 stem 附属。
    用于"目录不安全 rmtree"的场景。
    """
    # 通过 lookup 反向找出该 item 对应的具体 video 路径
    try:
        all_videos = [
            f for f in p.rglob('*')
            if f.is_file() and f.suffix.lower() in _VIDEO_EXTS_FOR_DELETE
        ]
    except OSError:
        return '', []

    targets: List[Path] = []
    for v in all_videos:
        info = lookup_jellyfin_item(str(v))
        if info and info.get('id') == item_id:
            targets.append(v)
    if not targets:
        # 找不到本 item 关联文件，安全起见放弃
        logger.warning(f"_delete_only_item_files: {p} 内找不到 item {item_id} 的视频，放弃")
        return '', []

    deleted: List[str] = []
    for v in targets:
        # 主视频
        try:
            v.unlink()
            deleted.append(str(v))
        except OSError as e:
            logger.warning(f"删除视频失败 {v}: {e}")
            continue
        # 同 stem 附件
        stem = v.stem
        for ext in _ATTACHMENT_EXTS:
            for cand in v.parent.glob(f"{stem}*{ext}"):
                if not cand.is_file():
                    continue
                try:
                    cand.unlink()
                    deleted.append(str(cand))
                except OSError:
                    continue
    return '文件（容器目录保留）', deleted


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
def delete_item(item_id: str):
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
    # ⚠️ 安全策略：只删单文件；目录场景仅在"严格作品目录"下允许 rmtree。
    # 此前 fallback 直接 shutil.rmtree(任意目录) 导致用户的 !!unorganized 暂存目录被
    # 整个清空（多部不同电影的容器被一并删除）。新规则下，目录被 rmtree 必须同时满足：
    #   a. 目录内**所有**视频文件都属于本次要删的 item（jellyfin path-index 反查不到其它 item）
    #   b. 目录内没有 jellyfin DB 之外的"陌生大文件"（不是常规附件 nfo/jpg/srt/png/...）
    #   c. 目录名不属于"暂存关键词"黑名单（unorganized / inbox / staging / temp / 待整理 等）
    # 任一条件不满足 → 退化为"只删单个视频文件 + 同 stem 附属"，保留容器目录
    deleted_paths: List[str] = []
    try:
        if p.is_file() or p.is_symlink():
            p.unlink()
            kind = '文件'
            deleted_paths = [str(p)]
        elif p.is_dir():
            kind, deleted_paths = _safe_directory_delete(p, item_id)
            if not deleted_paths:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Jellyfin 删除失败: {jellyfin_error}；"
                        f"物理 fallback 拒绝执行：目录 {local_path_str} 不符合"
                        f"\"严格作品目录\"安全条件（含其它 item / 暂存关键词 / 陌生大文件）"
                    ),
                )
        else:
            raise RuntimeError(f"未知路径类型: {local_path_str}")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"物理删除失败: {local_path_str}")
        raise HTTPException(
            status_code=500,
            detail=(
                f"Jellyfin 删除失败: {jellyfin_error}；"
                f"物理删除也失败: {e}"
            ),
        )

    # 通知 Jellyfin 路径已删除（Deleted 精准通知 → jellyfin 清理孤儿 item，比整库扫快）
    refreshed_lib_id = None
    notified_paths = False
    try:
        # 反向翻译：本机视角 → jellyfin 视角
        from web.backend.path_translator import reverse_translate_path_with_settings
        paths_to_notify = []
        for p in (deleted_paths or [local_path_str]):
            if p:
                paths_to_notify.append(reverse_translate_path_with_settings(p) or p)
        if paths_to_notify and client.notify_media_updated(paths_to_notify, update_type='Deleted'):
            notified_paths = True
            refreshed_lib_id = '(media_updated)'
    except Exception as e:
        logger.warning(f"notify_media_updated(Deleted) 失败: {e}")

    # 兜底：精准通知失败 → 整库 scan_changes
    if not notified_paths:
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
        "deleted_paths": deleted_paths,
        "library_refreshed": refreshed_lib_id,
        "warning": (
            f"Jellyfin DELETE 不可用（{jellyfin_error}），已通过物理删除 + 重扫库清理"
        ),
    }


@router.post("/items/{item_id}/identify-apply")
def identify_apply(item_id: str, req: IdentifyApplyRequest):
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


_OPTIONAL_STATS_FIELDS = {'health', 'poster', 'tmdb'}


def _parse_stats_fields(fields: Optional[str]) -> set:
    """
    fields 入参解析：
      None → 全开（向后兼容）
      ''   → 全关（用户把所有可选项都隐藏了）
      'health,poster' → 只算这两项
    """
    if fields is None:
        return set(_OPTIONAL_STATS_FIELDS)
    return {f.strip().lower() for f in fields.split(',') if f.strip()} & _OPTIONAL_STATS_FIELDS


@router.get("/libraries/{library_id}/stats")
def library_stats(
    library_id: str,
    force_refresh: bool = False,
    fields: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    单个库的聚合统计：视频数 / 总大小 / 缺海报 / 健康度 / 总时长。

    fields: 可选，逗号分隔；指定后只计算这些可选指标，跳过其他贵的计算。
            可选项：health（健康度，最贵）/ poster（缺海报）/ tmdb（TMDB 绑定）
            不传 = 全部计算（默认）；传空字符串 = 都不算

    缓存：内存级 2 小时 TTL，按 (library_id, fields) 分桶。force_refresh=true 跳过缓存重算。
    """
    included = _parse_stats_fields(fields)

    if not force_refresh:
        cached = _get_cached_lib_stats(library_id, included)
        if cached is not None:
            return cached

    client = _client()
    libs = client.get_libraries_normalized()
    target = next((l for l in libs if l['id'] == library_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="库不存在")

    collection_type = target['collection_type']

    # ---- 成人库分流：走专用 stats（基于本地 AdultItem DB），跳过 Jellyfin compute_health ----
    # 原因：成人库的"健康度"语义是刮削完整度（番号识别 + 封面下载 + NFO 写出），
    # 跟普通库的 compute_health（Jellyfin 元数据匹配质量）完全是两套口径。
    # Jellyfin 对成人库的 Movie/Series 大概率匹配不到 TMDB → name_mismatch 一片
    # 红，跟用户真实关心的指标对不上。
    adult_ids = set(settings.adult_library_ids or [])
    if library_id in adult_ids:
        from web.backend.api.adult import adult_library_stats
        ad = adult_library_stats(library_id=library_id, db=db)
        ad_total = ad.get('total', 0)
        ad_healthy = ad.get('healthy', 0)
        result = {
            "library": {
                "id": target['id'],
                "name": target['name'],
                "collection_type": collection_type,
                "locations": target['locations'],
                "accessible": True,
                "is_adult": True,
            },
            "filesystem": {
                "video_count": ad_total,  # 用 AdultItem 数量近似
                "subtitle_count": 0,
                "audio_count": 0,
                "image_count": 0,
                "total_size_bytes": ad.get('total_size_bytes', 0),
                "total_size_gb": ad.get('total_size_gb', 0.0),
            },
            "jellyfin": {
                "movies": ad_total,
                "series": 0,
                "total_items": ad_total,
                "with_poster": (ad_total - ad.get('missing_cover', 0)) if 'poster' in included else None,
                "without_poster": ad.get('missing_cover', 0) if 'poster' in included else None,
                "with_tmdb_id": None,  # 成人库不绑定 TMDB
                "items_healthy": ad_healthy if 'health' in included else None,
                "items_with_issues": (ad_total - ad_healthy) if 'health' in included else None,
                "health_ratio": (
                    round(ad_healthy / ad_total, 3) if ad_total else 1.0
                ) if 'health' in included else None,
                "total_runtime_seconds": ad.get('total_duration_seconds', 0),
                "actors_total": 0,
                "actors_with_image": 0,
            },
        }
        _set_cached_lib_stats(library_id, result, included)
        return result

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
        # MediaSources：兜底拿 RunTimeTicks（某些 jellyfin 版本顶层 RunTimeTicks 为 None 时
        # MediaSources[0].RunTimeTicks 有值），People：算演员图汇总
        items = client.get_library_items(
            library_id,
            item_types=item_types,
            fields='ImageTags,ProviderIds,Path,RunTimeTicks,MediaSources,ChildCount,People',
        )
    except Exception:
        items = []

    movies_count = sum(1 for i in items if i.get('Type') == 'Movie')
    series_count = sum(1 for i in items if i.get('Type') == 'Series')
    items_total = len(items)

    # 仅在 fields 包含相应项时计算，否则置 None（前端被隐藏的项不消耗后端 CPU）
    items_with_poster = (
        sum(1 for i in items if (i.get('ImageTags') or {}).get('Primary'))
        if 'poster' in included else None
    )
    items_with_tmdb = (
        sum(1 for i in items if (i.get('ProviderIds') or {}).get('Tmdb'))
        if 'tmdb' in included else None
    )

    if 'health' in included:
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
        items_healthy = items_total - items_with_issues
    else:
        items_with_issues = None
        items_healthy = None

    # ---- 3. 总时长（RunTimeTicks 累加；剧集库 Series 本身没有时长，需额外拉 Episode）----
    # fallback：某些 jellyfin 版本对 Movie 顶层 RunTimeTicks 返回 None，但 MediaSources[0].RunTimeTicks 有值
    def _runtime_of(item):
        rt = item.get('RunTimeTicks')
        if rt:
            return int(rt)
        ms = item.get('MediaSources') or []
        if ms and isinstance(ms, list):
            v = ms[0].get('RunTimeTicks') if isinstance(ms[0], dict) else None
            if v:
                return int(v)
        return 0
    total_runtime_ticks = sum(_runtime_of(i) for i in items)
    if collection_type == 'tvshows' or (collection_type == 'mixed' and series_count > 0):
        try:
            episodes = client.get_library_items(
                library_id,
                item_types='Episode',
                fields='RunTimeTicks,MediaSources',
            )
            total_runtime_ticks += sum(_runtime_of(e) for e in episodes)
        except Exception as e:
            logger.warning(f"拉取 Episode 时长失败: {e}")
    # 1 RunTimeTick = 100ns，转秒：÷ 10_000_000
    total_runtime_seconds = total_runtime_ticks // 10_000_000

    # ---- 4. 演员图汇总：所有 items 的 actor 总数 / 有图 actor 数 ----
    # 按"作品"层级累加（Series + Movie；Episode 通常没独立 People）
    actors_total = 0
    actors_with_image = 0
    for it in items:
        people = it.get('People') or []
        for p in people:
            if p.get('Type') != 'Actor':
                continue
            actors_total += 1
            if p.get('PrimaryImageTag'):
                actors_with_image += 1

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
            # 可选指标：被前端隐藏时为 None，前端不渲染对应卡片
            "with_poster": items_with_poster,
            "without_poster": (items_total - items_with_poster) if items_with_poster is not None else None,
            "with_tmdb_id": items_with_tmdb,
            "items_healthy": items_healthy,
            "items_with_issues": items_with_issues,
            "health_ratio": (
                round(items_healthy / items_total, 3) if items_total else 1.0
            ) if items_healthy is not None else None,
            # 总时长（秒）：始终算（不在可选项里，且基础指标"总时长"卡片要用）
            "total_runtime_seconds": total_runtime_seconds,
            # 演员图汇总：整库 actor 总数 / 有图 actor 数（统计区域用）
            "actors_total": actors_total,
            "actors_with_image": actors_with_image,
        },
    }
    _set_cached_lib_stats(library_id, result, included)
    return result


@router.get("/libraries/{library_id}/subtitle-stats")
def library_subtitle_stats(
    library_id: str,
    background_tasks: BackgroundTasks,
    max_age_minutes: int = -1,  # -1 = 用 settings.cache_subtitle_scan_minutes
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

    # max_age_minutes < 0 视为"使用配置默认值"
    effective_max_age = max_age_minutes if max_age_minutes >= 0 else settings.cache_subtitle_scan_minutes
    cutoff = datetime.utcnow() - timedelta(minutes=effective_max_age)

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
    # auto_triggered 标记：这是 LibraryDetail 页面打开时静默触发的字幕扫描，
    # 默认不显示在 /tasks 列表里（用户没主动发起）
    task = create_task(
        db, "subtitle_scan", f"扫描: {label}（{len(paths)} 个路径）",
        params={
            "auto_triggered": True,
            "paths": paths,
            "recursive": True,
            "expected_langs": expected_langs,
            "library_ids": list(refresh_ids or []),
        },
    )
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
def refresh_library(library_id: str, mode: str = 'scan_changes'):
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
def refresh_all():
    """触发全局媒体库刷新（耗时操作，Jellyfin 内部异步执行）"""
    client = _client()
    ok = client.refresh_all_libraries()
    if not ok:
        raise HTTPException(status_code=500, detail="全局刷新触发失败")
    return {"ok": True}


# ---------- 系统信息 ----------

@router.get("/system")
def get_system():
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


@router.post("/check-api-key")
def check_api_key():
    """
    检查当前配置的 Jellyfin API key 权限是否够用。

    需要的权限：管理员级（IsAdministrator=true）—— /Library/Media/Updated 端点
    要求 RequiresElevation，普通 user key 会 401。

    探测方式：
      ① GET /System/Info     → 验证 host + key 基本可用
      ② GET /Auth/Keys       → 仅管理员可访问；返回 200 即说明 key 是 admin 级
                              （一些版本里 /Users 也行，但 /Auth/Keys 更明确）
    """
    if not settings.jellyfin_host or not settings.jellyfin_api_key:
        return {
            'ok': False,
            'reachable': False,
            'is_admin': False,
            'message': '未配置 jellyfin host / api_key',
        }

    try:
        client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
    except Exception as e:
        return {
            'ok': False,
            'reachable': False,
            'is_admin': False,
            'message': f'jellyfin 客户端初始化失败：{e}',
        }

    # ① 基本连通性 + key 有效性
    try:
        info = client.get_system_info()
    except Exception as e:
        err_str = str(e)
        if '401' in err_str or 'Unauthorized' in err_str:
            return {
                'ok': False, 'reachable': True, 'is_admin': False,
                'message': 'API key 无效（401 Unauthorized）',
            }
        return {
            'ok': False, 'reachable': False, 'is_admin': False,
            'message': f'连不上 jellyfin：{e}',
        }
    if not info:
        return {
            'ok': False, 'reachable': False, 'is_admin': False,
            'message': '/System/Info 返回空，连接异常',
        }

    server_version = info.get('Version', '?')
    server_name = info.get('ServerName', '?')

    # ② 管理员权限探测（用 /Auth/Keys —— 严格要求管理员）
    try:
        client._request('GET', '/Auth/Keys')
        is_admin = True
        admin_check_msg = '已确认管理员权限'
    except Exception as e:
        err_str = str(e)
        if '401' in err_str or '403' in err_str or 'Unauthorized' in err_str or 'Forbidden' in err_str:
            is_admin = False
            admin_check_msg = (
                'API key 不是管理员级 —— /Library/Media/Updated 等端点会失败。'
                '请到 Jellyfin Dashboard → API Keys 重新创建（用管理员账号登录后创建的就是）。'
            )
        else:
            is_admin = False
            admin_check_msg = f'权限探测异常：{e}'

    return {
        'ok': is_admin,
        'reachable': True,
        'is_admin': is_admin,
        'server_name': server_name,
        'server_version': server_version,
        'message': admin_check_msg,
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
        from web.backend.diagnostics import timed
        with timed('jellyfin _build_path_index get_items_by_type', slow_ms=2000):
            items = client.get_items_by_type(
                include_types='Movie,Series',
                # Path / ImageTags / RunTimeTicks / MediaSources / ProviderIds
                # —— 字幕下载等流程需要从这里反查 tmdb_id / imdb_id
                fields='Path,ImageTags,RunTimeTicks,MediaSources,ProviderIds,ProductionYear',
            )
    except Exception as e:
        logger.warning(f"构建 Jellyfin 路径索引失败: {e}")
        # 失败也写一个短 TTL 缓存，避免 N 条 item 刷 N 次告警
        _PATH_INDEX_CACHE['data'] = {}
        _PATH_INDEX_CACHE['ts'] = now
        return {}

    # 同一 Item 用三种 key 入索引，便于 AdultItem.file_path 这种本机路径反查：
    #   1) 原始 Jellyfin 路径（如 /library/videos/adult/X.mp4）
    #   2) 翻译后的本机路径（如 Z:/videos/adult/X.mp4，依 path_mappings）
    #   3) 本机反斜杠形式（Z:\videos\adult\X.mp4，Windows 实际存的形式）
    from web.backend.path_translator import translate_path_with_settings as _tr
    index = {}
    for it in items:
        path = it.get('Path')
        if not path:
            continue
        # 时长（分钟）：顶层 RunTimeTicks 优先，否则 MediaSources[0].RunTimeTicks
        rt = it.get('RunTimeTicks') or 0
        if not rt:
            ms = it.get('MediaSources') or []
            if ms and isinstance(ms[0], dict):
                rt = ms[0].get('RunTimeTicks') or 0
        runtime_min = round(rt / 600_000_000.0, 1) if rt else None
        provider_ids = it.get('ProviderIds') or {}
        info = {
            'id': it.get('Id'),
            'name': it.get('Name'),
            'type': it.get('Type'),
            'year': it.get('ProductionYear'),
            'has_image': bool((it.get('ImageTags') or {}).get('Primary')),
            # 外部 ID：字幕下载流程需要（OpenSubtitles 用 ID 命中精度远高于 query）
            'tmdb_id': provider_ids.get('Tmdb'),
            'imdb_id': provider_ids.get('Imdb'),
            'runtime_min': runtime_min,
        }
        index[path] = info
        # 翻译成本机路径并入索引（含正/反斜杠两种变体）
        try:
            local = _tr(path)
            if local and local != path:
                index[local] = info
                fwd = local.replace('\\', '/')
                bwd = local.replace('/', '\\')
                if fwd != local: index[fwd] = info
                if bwd != local: index[bwd] = info
        except Exception:
            pass

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
def query_item_by_path(path: str):
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
