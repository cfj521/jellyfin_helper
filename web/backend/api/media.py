"""
媒体库管理 API
"""
import logging
import os
import hashlib
from pathlib import Path
from typing import List, Optional
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from web.backend.database import get_db, Task, MediaItem
from web.backend.api.tasks import create_task, update_task_progress, complete_task
from web.backend.task_restart import register_resumable
from web.backend.path_translator import translate_path_with_settings

logger = logging.getLogger(__name__)
router = APIRouter()


def _resolve_local_path(p: str) -> str:
    """
    把前端传来的（可能是 Jellyfin 视角的）路径转成本后端实际能访问的路径。
    未命中映射规则的会原样返回，所以传 Windows 原生路径也安全。
    """
    return translate_path_with_settings(p) or p


class DirectoryInfo(BaseModel):
    path: str
    name: str
    total_size: int
    file_count: int
    video_count: int
    subtitle_count: int


class ScanMediaRequest(BaseModel):
    path: str
    recursive: bool = True


@router.get("/browse")
def browse_directory(path: str = ""):
    """浏览目录"""
    if not path:
        # 返回可用的驱动器或根目录
        if os.name == 'nt':  # Windows
            import string
            drives = []
            for letter in string.ascii_uppercase:
                drive = f"{letter}:\\"
                if os.path.exists(drive):
                    drives.append({"name": drive, "path": drive, "type": "drive"})
            return {"items": drives, "current_path": ""}
        else:
            path = "/"

    # 用户可能粘贴 Jellyfin 视角的路径（/library/videos/...），自动转成本机可访问的
    path = _resolve_local_path(path)
    target = Path(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在: {path}")

    if not target.is_dir():
        raise HTTPException(status_code=400, detail="不是目录")

    items = []
    try:
        for item in sorted(target.iterdir()):
            try:
                item_info = {
                    "name": item.name,
                    "path": str(item),
                    "type": "directory" if item.is_dir() else "file"
                }
                if item.is_file():
                    item_info["size"] = item.stat().st_size
                    item_info["extension"] = item.suffix.lower()
                items.append(item_info)
            except (PermissionError, OSError):
                continue
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权访问此目录")

    return {
        "items": items,
        "current_path": str(target),
        "parent_path": str(target.parent) if target.parent != target else None
    }


@router.post("/scan")
def scan_media(
    request: ScanMediaRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """扫描媒体目录"""
    logger.info(f"/media/scan 进入: path={request.path!r} recursive={request.recursive}")
    local_path = _resolve_local_path(request.path)
    scan_path = Path(local_path)
    if not scan_path.exists():
        logger.warning(f"/media/scan 路径不存在: 前端={request.path!r} 翻译后={local_path!r}")
        raise HTTPException(
            status_code=400,
            detail=f"路径不存在: {local_path}（前端传入: {request.path}）",
        )
    # 把翻译后的路径塞回 request，让下游 run_media_scan 拿到本机可访问的路径
    request.path = local_path

    task = create_task(
        db,
        "media_scan",
        f"扫描媒体: {request.path}",
        params={"path": request.path, "recursive": request.recursive},
    )

    background_tasks.add_task(
        run_media_scan,
        task.id,
        request.path,
        request.recursive
    )

    return {"task_id": task.id, "status": "started"}


@register_resumable("media_scan", ["path", "recursive"])
def run_media_scan(task_id: int, path: str, recursive: bool):
    """执行媒体扫描"""
    from web.backend.database import SessionLocal
    import time

    t0 = time.time()
    # 路径翻译幂等：API 入口已翻译过的本机路径再翻一遍仍是本机路径
    # 给重启恢复任务多做一层保险（DB 里旧的 params 可能是 jellyfin 路径）
    path = _resolve_local_path(path)
    logger.info(f"run_media_scan 开始: task={task_id} path={path!r} recursive={recursive}")

    VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.wmv', '.mov', '.flv', '.webm', '.m4v', '.ts', '.rmvb'}
    SUBTITLE_EXTENSIONS = {'.srt', '.ass', '.ssa', '.sub', '.idx', '.vtt'}

    db = SessionLocal()
    try:
        update_task_progress(db, task_id, 10, "扫描目录...")

        scan_path = Path(path)
        stats = {
            "total_size": 0,
            "video_count": 0,
            "subtitle_count": 0,
            "resolutions": defaultdict(int),
            "codecs": defaultdict(int),
            "directories": []
        }

        def scan_dir(dir_path: Path, depth: int = 0):
            if depth > 10:  # 防止过深递归
                return

            dir_stats = {
                "path": str(dir_path),
                "name": dir_path.name,
                "size": 0,
                "videos": 0,
                "subtitles": 0
            }

            try:
                for item in dir_path.iterdir():
                    if item.is_file():
                        ext = item.suffix.lower()
                        if ext in VIDEO_EXTENSIONS:
                            try:
                                size = item.stat().st_size
                                dir_stats["size"] += size
                                dir_stats["videos"] += 1
                                stats["total_size"] += size
                                stats["video_count"] += 1
                            except:
                                pass
                        elif ext in SUBTITLE_EXTENSIONS:
                            dir_stats["subtitles"] += 1
                            stats["subtitle_count"] += 1
                    elif item.is_dir() and recursive:
                        scan_dir(item, depth + 1)

                if dir_stats["videos"] > 0:
                    stats["directories"].append(dir_stats)

            except (PermissionError, OSError):
                pass

        scan_dir(scan_path)

        update_task_progress(db, task_id, 90, "生成报告...")

        elapsed = time.time() - t0
        logger.info(
            f"run_media_scan 完成: task={task_id} videos={stats['video_count']} "
            f"subs={stats['subtitle_count']} dirs={len(stats['directories'])} "
            f"size={stats['total_size']/1e9:.2f}GB elapsed={elapsed:.1f}s"
        )

        complete_task(db, task_id, {
            "total_size": stats["total_size"],
            "total_size_gb": round(stats["total_size"] / (1024**3), 2),
            "video_count": stats["video_count"],
            "subtitle_count": stats["subtitle_count"],
            "directory_count": len(stats["directories"]),
            "top_directories": sorted(
                stats["directories"],
                key=lambda x: x["size"],
                reverse=True
            )[:20]
        })

    except Exception as e:
        logger.exception(f"run_media_scan 失败: task={task_id} path={path!r}")
        complete_task(db, task_id, {"error": str(e)}, success=False)
    finally:
        db.close()


def _resolution_label_from_dims(width: int, height: int) -> str:
    """Width/Height → 标签（8K/4K/1080p/720p/480p/{H}p）。供 hash 与 metadata 两路共用。"""
    if not height and not width:
        return ''
    if height >= 4000:
        return '8K'
    if height >= 2000 or width >= 3800:
        return '4K'
    if height >= 1000:
        return '1080p'
    if height >= 700:
        return '720p'
    if height >= 400:
        return '480p'
    return f'{height}p' if height else ''


_FFPROBE_PATH = None
_FFPROBE_CHECKED = False


def _get_ffprobe() -> Optional[str]:
    """惰性查找 ffprobe；进程级缓存。"""
    global _FFPROBE_PATH, _FFPROBE_CHECKED
    if not _FFPROBE_CHECKED:
        import shutil
        _FFPROBE_PATH = shutil.which("ffprobe")
        _FFPROBE_CHECKED = True
        if _FFPROBE_PATH is None:
            logger.warning("未找到 ffprobe，hash 模式将不展示时长/分辨率")
    return _FFPROBE_PATH


def _probe_video_meta(path: Path, timeout: float = 8.0) -> dict:
    """跑 ffprobe 拉一条视频的 duration_sec / width / height。失败返回 {}。"""
    import subprocess
    import json
    ffprobe = _get_ffprobe()
    if not ffprobe or not path.exists():
        return {}
    cmd = [
        ffprobe, '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height:format=duration',
        '-of', 'json',
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding='utf-8', errors='replace',
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug(f"ffprobe 失败 {path}: {e}")
        return {}
    if proc.returncode != 0 or not proc.stdout:
        return {}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    streams = data.get('streams') or []
    width = height = 0
    if streams:
        v = streams[0]
        try:
            width = int(v.get('width') or 0)
            height = int(v.get('height') or 0)
        except (TypeError, ValueError):
            pass
    duration_sec = 0
    fmt = data.get('format') or {}
    try:
        d = float(fmt.get('duration') or 0)
        duration_sec = int(d) if d > 0 else 0
    except (TypeError, ValueError):
        pass
    return {
        'duration_sec': duration_sec,
        'width': width,
        'height': height,
    }


def _quick_hash(path: Path, chunk_size: int = 65536) -> Optional[str]:
    """
    对文件计算"首/中/尾 hash"——读首 64KB + 中 64KB + 末 64KB + 文件大小，做 sha1。

    剧集场景：同一剧集的不同集（或同集不同版本）头尾常带固定 OP/ED，仅靠首尾
    hash 会把"头尾相同正片不同"误判为重复。多采一个文件中段 chunk（基本不会
    跟 OP/ED 重叠），把正片差异采进 hash。

    完整 hash 太慢；同大小 + 首/中/尾 三段都一致 → 几乎确定真重复。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None

    h = hashlib.sha1()
    h.update(str(size).encode())
    try:
        with open(path, 'rb') as f:
            head = f.read(min(chunk_size, size))
            h.update(head)
            # 中段：文件大于 3 个 chunk 时才采（小于这个量首尾已经覆盖到中段）
            # 偏移点取文件中点对齐到 chunk_size 边界，避免读取跨块降低 OS 缓存命中
            if size > chunk_size * 3:
                mid_offset = (size // 2) & ~(chunk_size - 1)
                # 防御：mid 不能跟 head/tail 重叠
                if chunk_size <= mid_offset <= size - 2 * chunk_size:
                    f.seek(mid_offset)
                    mid = f.read(chunk_size)
                    h.update(mid)
            if size > chunk_size * 2:
                f.seek(-chunk_size, os.SEEK_END)
                tail = f.read(chunk_size)
                h.update(tail)
        return h.hexdigest()
    except OSError:
        return None


def _hash_dup_impl(path: str, use_hash: bool, progress=None) -> dict:
    """hash 模式重复检测核心。progress(event) 可选，event 形如
    {'phase': 'scanning'|'hashing'|'probing', 'message': str, 'percent': int, 'current': str?}"""
    import time
    progress = progress or (lambda _e: None)
    t0 = time.time()
    logger.info(f"hash-dup 启动: path={path!r} use_hash={use_hash}")

    local_path = _resolve_local_path(path)
    scan_path = Path(local_path)
    if not scan_path.exists():
        raise FileNotFoundError(f"路径不存在: {local_path}（前端传入: {path}）")

    progress({'phase': 'scanning', 'message': '扫描视频文件...', 'percent': 2})
    VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.wmv', '.mov', '.flv', '.webm', '.m4v'}
    videos = []
    for item in scan_path.rglob("*"):
        if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS:
            try:
                videos.append({
                    "path": str(item),
                    "name": item.stem,
                    "size": item.stat().st_size,
                    "_path_obj": item,
                })
            except OSError:
                continue
    progress({
        'phase': 'scanning',
        'message': f'扫描完成，共 {len(videos)} 个视频文件',
        'percent': 12,
    })

    if not use_hash:
        size_groups = defaultdict(list)
        for v in videos:
            size_mb = v["size"] // (1024 * 1024)
            size_groups[size_mb].append({k: v[k] for k in ("path", "name", "size")})
        duplicates = [
            {"match_type": "size_mb", "size_mb": size, "files": group}
            for size, group in size_groups.items() if len(group) > 1
        ]
        return {
            "total_videos": len(videos),
            "potential_duplicates": len(duplicates),
            "groups": sorted(duplicates, key=lambda x: x["size_mb"], reverse=True)[:50],
        }

    # hash 模式：先按精确字节大小分组
    size_groups = defaultdict(list)
    for v in videos:
        size_groups[v["size"]].append(v)

    # 进度估算：以"需要 hash 的文件总数"作分母（只有大小冲突组才会进 hash）
    candidates = [v for grp in size_groups.values() if len(grp) >= 2 for v in grp]
    total_hashable = len(candidates)
    progress({
        'phase': 'hashing',
        'message': f'同大小候选 {total_hashable} 个，准备计算 hash',
        'percent': 15,
    })

    confirmed = []
    size_only = []
    hashed = 0
    HASH_BAND = (15, 80)  # 进度区间

    for size, group in size_groups.items():
        if len(group) < 2:
            continue
        hash_groups = defaultdict(list)
        for v in group:
            h = _quick_hash(v["_path_obj"])
            if h:
                hash_groups[h].append(v)
            else:
                hash_groups[f"_no_hash_{v['path']}"].append(v)
            hashed += 1
            # 每 hash 一个就推一次进度（含当前文件名）；频繁但事件本身小
            if total_hashable:
                pct = HASH_BAND[0] + int((HASH_BAND[1] - HASH_BAND[0]) * hashed / total_hashable)
            else:
                pct = HASH_BAND[1]
            progress({
                'phase': 'hashing',
                'message': f'计算 hash {hashed}/{total_hashable}',
                'percent': pct,
                'current': Path(v['path']).name,
            })

        for h, hg in hash_groups.items():
            if len(hg) > 1:
                confirmed.append({
                    "match_type": "hash",
                    "hash": h,
                    "size": size,
                    "size_mb": size // (1024 * 1024),
                    "files": [{k: v[k] for k in ("path", "name", "size")} for v in hg],
                })

        if len(hash_groups) > 1:
            outliers = [hg[0] for hg in hash_groups.values() if len(hg) == 1]
            if len(outliers) > 1:
                size_only.append({
                    "match_type": "size_only",
                    "size": size,
                    "size_mb": size // (1024 * 1024),
                    "files": [{k: v[k] for k in ("path", "name", "size")} for v in outliers],
                })

    confirmed.sort(key=lambda x: x["size"], reverse=True)
    size_only.sort(key=lambda x: x["size"], reverse=True)
    final_groups = (confirmed + size_only)[:50]

    # 富化：ffprobe 拿时长 + 分辨率
    enrich_t0 = time.time()
    total_files = sum(len(g['files']) for g in final_groups)
    progress({
        'phase': 'probing',
        'message': f'ffprobe 富化 {total_files} 个文件...',
        'percent': 82,
    })
    PROBE_BAND = (82, 98)
    probed = 0
    seen_path_meta: dict = {}
    for g in final_groups:
        for f in g['files']:
            p = f.get('path')
            if not p:
                continue
            if p not in seen_path_meta:
                seen_path_meta[p] = _probe_video_meta(Path(p))
            meta = seen_path_meta[p] or {}
            if meta.get('duration_sec'):
                f['duration_sec'] = meta['duration_sec']
            label = _resolution_label_from_dims(meta.get('width', 0), meta.get('height', 0))
            if label:
                f['resolution'] = label
            probed += 1
            if total_files:
                pct = PROBE_BAND[0] + int((PROBE_BAND[1] - PROBE_BAND[0]) * probed / total_files)
            else:
                pct = PROBE_BAND[1]
            progress({
                'phase': 'probing',
                'message': f'ffprobe {probed}/{total_files}',
                'percent': pct,
                'current': Path(p).name,
            })

    logger.info(
        f"hash-dup 完成: path={local_path!r} videos={len(videos)} "
        f"confirmed_dup={len(confirmed)} size_only={len(size_only)} "
        f"ffprobe={len(seen_path_meta)} ({time.time()-enrich_t0:.1f}s) "
        f"total {time.time()-t0:.1f}s"
    )

    return {
        "total_videos": len(videos),
        "confirmed_duplicates": len(confirmed),
        "size_only_matches": len(size_only),
        "potential_duplicates": len(confirmed) + len(size_only),
        "groups": final_groups,
    }


@router.get("/duplicates")
def find_duplicates(
    path: str,
    use_hash: bool = True,
    db: Session = Depends(get_db),
):
    """同步版（一次性返回完整结果）。带进度的 SSE 见 /duplicates/stream。"""
    try:
        return _hash_dup_impl(path, use_hash)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _metadata_dup_impl(library_id: Optional[str] = None, progress=None) -> dict:
    """metadata 模式重复检测核心。progress(event) 可选。"""
    from web.backend.config import settings
    from common.jellyfin_client import JellyfinClient
    progress = progress or (lambda _e: None)

    if not settings.jellyfin_api_key:
        raise PermissionError("未配置 Jellyfin API Key")

    client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
    fields = "ProviderIds,ProductionYear,Path,MediaSources,MediaStreams,RunTimeTicks,SeriesName,SeriesId,ParentIndexNumber,IndexNumber"

    logger.info(f"metadata-dup 启动: library_id={library_id!r}")

    def _fetch(types: str) -> list:
        params = {
            'Recursive': 'true',
            'IncludeItemTypes': types,
            'Fields': fields,
            'Limit': 5000,
        }
        if library_id:
            params['ParentId'] = library_id
        try:
            r = client._request('GET', '/Items', params=params)
            return (r or {}).get('Items', []) or []
        except Exception as e:
            logger.warning(f"jellyfin /Items 拉 {types} 失败: {e}")
            return []

    progress({'phase': 'fetching', 'message': '从 Jellyfin 拉取 Movies...', 'percent': 10})
    movies = _fetch('Movie')
    progress({
        'phase': 'fetching',
        'message': f'已拉取 {len(movies)} 部电影，继续拉取剧集...',
        'percent': 40,
    })
    episodes = _fetch('Episode')
    progress({
        'phase': 'fetching',
        'message': f'已拉取 {len(episodes)} 集，准备分组',
        'percent': 70,
    })
    logger.info(
        f"metadata-dup: 拉到 movies={len(movies)} episodes={len(episodes)}"
    )

    def _file_size(item: dict) -> int:
        # MediaSources[0].Size 通常就是真实文件 byte
        ms = item.get('MediaSources') or []
        if ms and isinstance(ms, list):
            sz = ms[0].get('Size')
            if isinstance(sz, int):
                return sz
        return 0

    def _duration_seconds(item: dict) -> int:
        """从 RunTimeTicks（100ns 单位）取秒数，方便前端展示时长。
        MediaSources[0].RunTimeTicks 优先；item 顶层 RunTimeTicks 兜底（剧集偶尔不带 MediaSources）。"""
        ms = item.get('MediaSources') or []
        if ms and isinstance(ms, list):
            ticks = ms[0].get('RunTimeTicks')
            if isinstance(ticks, int) and ticks > 0:
                return ticks // 10_000_000
        ticks = item.get('RunTimeTicks')
        if isinstance(ticks, int) and ticks > 0:
            return ticks // 10_000_000
        return 0

    def _resolution_label(item: dict) -> str:
        """从 MediaSources[0].MediaStreams 第一个 Video 流取宽高，调共用映射。"""
        ms = item.get('MediaSources') or []
        streams = []
        if ms and isinstance(ms, list):
            streams = ms[0].get('MediaStreams') or []
        for s in streams:
            if (s.get('Type') or '').lower() == 'video':
                w = s.get('Width') or 0
                h = s.get('Height') or 0
                w = w if isinstance(w, int) else 0
                h = h if isinstance(h, int) else 0
                return _resolution_label_from_dims(w, h)
        return ''

    from web.backend.path_translator import translate_path_with_settings

    def _slim(item: dict, version_label: str = '') -> dict:
        return {
            "jellyfin_id": item.get('Id'),
            "name": item.get('Name'),
            "year": item.get('ProductionYear'),
            "path": translate_path_with_settings(item.get('Path')) if item.get('Path') else None,
            "size": _file_size(item),
            "duration_sec": _duration_seconds(item),
            "resolution": _resolution_label(item),
            "version_label": version_label,
        }

    def _normalize_title(s: str) -> str:
        if not s:
            return ''
        out = []
        for ch in s.lower():
            if ch.isalnum() or ('一' <= ch <= '鿿') or ('぀' <= ch <= 'ヿ'):
                out.append(ch)
        return ''.join(out)

    # ---- Movie 分组 ----
    by_tmdb = defaultdict(list)
    by_imdb = defaultdict(list)
    by_title_year = defaultdict(list)
    used_ids: set = set()  # 防止同一 movie 被多次归入不同组

    for m in movies:
        pids = m.get('ProviderIds') or {}
        tmdb = pids.get('Tmdb')
        if tmdb:
            by_tmdb[str(tmdb)].append(m)
            continue
        imdb = pids.get('Imdb')
        if imdb:
            by_imdb[str(imdb)].append(m)
            continue
        nt = _normalize_title(m.get('Name') or '')
        yr = m.get('ProductionYear')
        if nt:
            by_title_year[f"{nt}|{yr or ''}"].append(m)

    movie_groups = []
    for tmdb_id, items in by_tmdb.items():
        if len(items) < 2:
            continue
        for it in items:
            used_ids.add(it.get('Id'))
        movie_groups.append({
            "match_type": "tmdb",
            "key": f"tmdb:{tmdb_id}",
            "title": items[0].get('Name'),
            "year": items[0].get('ProductionYear'),
            "files": sorted(
                (_slim(it) for it in items),
                key=lambda x: -(x.get('size') or 0),
            ),
        })
    for imdb_id, items in by_imdb.items():
        if len(items) < 2:
            continue
        items = [it for it in items if it.get('Id') not in used_ids]
        if len(items) < 2:
            continue
        for it in items:
            used_ids.add(it.get('Id'))
        movie_groups.append({
            "match_type": "imdb",
            "key": f"imdb:{imdb_id}",
            "title": items[0].get('Name'),
            "year": items[0].get('ProductionYear'),
            "files": sorted(
                (_slim(it) for it in items),
                key=lambda x: -(x.get('size') or 0),
            ),
        })
    for k, items in by_title_year.items():
        if len(items) < 2:
            continue
        items = [it for it in items if it.get('Id') not in used_ids]
        if len(items) < 2:
            continue
        movie_groups.append({
            "match_type": "title_year",
            "key": f"title:{k}",
            "title": items[0].get('Name'),
            "year": items[0].get('ProductionYear'),
            "files": sorted(
                (_slim(it) for it in items),
                key=lambda x: -(x.get('size') or 0),
            ),
        })

    # ---- Episode 分组：同 SeriesId + S + E ----
    by_episode = defaultdict(list)
    for ep in episodes:
        sid = ep.get('SeriesId')
        s = ep.get('ParentIndexNumber')
        e = ep.get('IndexNumber')
        if sid is None or s is None or e is None:
            continue
        by_episode[f"{sid}|S{s:02d}E{e:02d}"].append(ep)

    episode_groups = []
    for k, items in by_episode.items():
        if len(items) < 2:
            continue
        first = items[0]
        episode_groups.append({
            "match_type": "episode",
            "key": k,
            "title": (
                f"{first.get('SeriesName') or '?'} - "
                f"S{first.get('ParentIndexNumber'):02d}E{first.get('IndexNumber'):02d}"
            ),
            "year": None,
            "files": sorted(
                (_slim(it, version_label=it.get('Name') or '') for it in items),
                key=lambda x: -(x.get('size') or 0),
            ),
        })

    progress({'phase': 'grouping', 'message': '按 TMDB/IMDB/标题分组...', 'percent': 90})

    all_groups = movie_groups + episode_groups
    # 按浪费空间排序：组内"非最大文件"的总大小 = 可释放空间
    def _waste(g):
        files = g['files']
        if len(files) < 2:
            return 0
        sizes = sorted((f.get('size') or 0 for f in files), reverse=True)
        return sum(sizes[1:])
    all_groups.sort(key=_waste, reverse=True)

    return {
        "total_items": len(movies) + len(episodes),
        "total_movies": len(movies),
        "total_episodes": len(episodes),
        "potential_duplicates": len(all_groups),
        "movie_dup_groups": len(movie_groups),
        "episode_dup_groups": len(episode_groups),
        "groups": all_groups[:200],
    }


@router.get("/duplicates-by-metadata")
def find_duplicates_by_metadata(library_id: Optional[str] = None):
    """同步版（一次性返回完整结果）。带进度的 SSE 见 /duplicates-by-metadata/stream。"""
    try:
        return _metadata_dup_impl(library_id)
    except PermissionError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ============================================================================
# SSE 进度流：跟同名 GET 一致的查询参数，返回 text/event-stream
# 事件 data 是 JSON: {phase, message, percent, current?, result?}
#   phase: 'scanning' / 'hashing' / 'probing' / 'fetching' / 'grouping' / 'done' / 'error'
#   done 事件携带 result（完整结果 dict）。前端拿到 done 即可关流。
# ============================================================================

_SSE_HEADERS = {
    'Cache-Control': 'no-cache, no-transform',
    'X-Accel-Buffering': 'no',
    'Connection': 'keep-alive',
}


class _WorkerAborted(Exception):
    """worker 线程检测到客户端断开 / 进程关闭 → 主动 bail。"""


def _make_dup_stream(work, request):
    """work(progress_cb) -> result_dict。把同步 impl 跑在线程池，progress 通过 asyncio.Queue 串到 SSE 流。

    关 reload / 客户端断开时：
      - gen() 周期 wait_for(timeout) 检查 request.is_disconnected()，超时时发 keepalive
      - 触发 abort_evt，下次 progress() 调用就抛 _WorkerAborted，impl 主动停
      - 处理 asyncio.CancelledError（uvicorn 给 handler 发的关闭信号）
    """
    import asyncio
    import threading
    import json
    from fastapi.responses import StreamingResponse

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    abort_evt = threading.Event()

    def progress(event):
        # worker 线程调用：先看 abort，再跨线程 put 到 asyncio.Queue
        if abort_evt.is_set():
            raise _WorkerAborted()
        try:
            asyncio.run_coroutine_threadsafe(queue.put(event), loop)
        except RuntimeError:
            # loop 已经关：进程在退出，直接 bail
            raise _WorkerAborted()

    async def runner():
        try:
            result = await asyncio.to_thread(work, progress)
            if not abort_evt.is_set():
                await queue.put({'phase': 'done', 'result': result, 'percent': 100})
        except _WorkerAborted:
            logger.info("dup-stream worker aborted（客户端断开 / 进程关闭）")
        except Exception as e:
            logger.exception("dup-stream worker 异常")
            if not abort_evt.is_set():
                try:
                    await queue.put({'phase': 'error', 'message': str(e)})
                except Exception:
                    pass

    runner_task = asyncio.create_task(runner())

    from web.backend.shutdown import is_shutting_down

    async def gen():
        try:
            while True:
                # 多信号：超时（5s）做 keepalive + disconnect 检查 + 进程级 shutdown 检查
                if is_shutting_down():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    if await request.is_disconnected() or is_shutting_down():
                        break
                    yield ': keepalive\n\n'
                    continue
                yield f'data: {json.dumps(event, default=str)}\n\n'
                if event.get('phase') in ('done', 'error'):
                    break
        except asyncio.CancelledError:
            # uvicorn 关进程 / 客户端强断
            raise
        finally:
            # 通知 worker bail（下次 progress() 抛 _WorkerAborted）
            abort_evt.set()
            if not runner_task.done():
                runner_task.cancel()

    return StreamingResponse(gen(), media_type='text/event-stream', headers=_SSE_HEADERS)


@router.get("/duplicates/stream")
async def find_duplicates_stream(request: Request, path: str, use_hash: bool = True):
    """SSE 进度流版的 /duplicates。"""
    return _make_dup_stream(
        lambda prog: _hash_dup_impl(path, use_hash, progress=prog),
        request,
    )


@router.get("/duplicates-by-metadata/stream")
async def find_duplicates_by_metadata_stream(request: Request, library_id: Optional[str] = None):
    """SSE 进度流版的 /duplicates-by-metadata。"""
    return _make_dup_stream(
        lambda prog: _metadata_dup_impl(library_id, progress=prog),
        request,
    )


@router.get("/storage")
def analyze_storage(path: str):
    """分析存储空间"""
    path = _resolve_local_path(path)
    scan_path = Path(path)
    if not scan_path.exists():
        raise HTTPException(status_code=400, detail="路径不存在")

    VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.wmv', '.mov', '.flv', '.webm', '.m4v', '.ts', '.rmvb'}

    # 按扩展名统计
    ext_stats = defaultdict(lambda: {"count": 0, "size": 0})

    # 按目录统计
    dir_stats = defaultdict(lambda: {"count": 0, "size": 0})

    for item in scan_path.rglob("*"):
        if item.is_file():
            try:
                size = item.stat().st_size
                ext = item.suffix.lower()

                ext_stats[ext]["count"] += 1
                ext_stats[ext]["size"] += size

                # 只统计视频文件的目录
                if ext in VIDEO_EXTENSIONS:
                    rel_dir = str(item.parent.relative_to(scan_path))
                    if rel_dir == ".":
                        rel_dir = "(根目录)"
                    dir_stats[rel_dir]["count"] += 1
                    dir_stats[rel_dir]["size"] += size
            except:
                pass

    # 转换为列表并排序
    ext_list = [
        {"extension": ext, **data}
        for ext, data in ext_stats.items()
    ]
    ext_list.sort(key=lambda x: x["size"], reverse=True)

    dir_list = [
        {"directory": d, **data}
        for d, data in dir_stats.items()
    ]
    dir_list.sort(key=lambda x: x["size"], reverse=True)

    total_size = sum(d["size"] for d in ext_stats.values())

    return {
        "total_size": total_size,
        "total_size_gb": round(total_size / (1024**3), 2),
        "by_extension": ext_list[:20],
        "by_directory": dir_list[:30]
    }
