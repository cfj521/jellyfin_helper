"""
媒体库管理 API
"""
import os
import hashlib
from pathlib import Path
from typing import List, Optional
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session

from web.backend.database import get_db, Task, MediaItem
from web.backend.api.tasks import create_task, update_task_progress, complete_task
from web.backend.task_restart import register_resumable
from web.backend.path_translator import translate_path_with_settings


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
    local_path = _resolve_local_path(request.path)
    scan_path = Path(local_path)
    if not scan_path.exists():
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

    # 路径翻译幂等：API 入口已翻译过的本机路径再翻一遍仍是本机路径
    # 给重启恢复任务多做一层保险（DB 里旧的 params 可能是 jellyfin 路径）
    path = _resolve_local_path(path)

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
        complete_task(db, task_id, {"error": str(e)}, success=False)
    finally:
        db.close()


def _quick_hash(path: Path, chunk_size: int = 65536) -> Optional[str]:
    """
    对文件计算"首尾 hash"——读首 64KB + 末 64KB + 文件大小，做 sha1。
    完整 hash 太慢；同大小文件 + 首尾内容相同则几乎确定相同。
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
            if size > chunk_size * 2:
                f.seek(-chunk_size, os.SEEK_END)
                tail = f.read(chunk_size)
                h.update(tail)
        return h.hexdigest()
    except OSError:
        return None


@router.get("/duplicates")
def find_duplicates(
    path: str,
    use_hash: bool = True,
    db: Session = Depends(get_db),
):
    """
    查找重复视频文件。

    use_hash=True 时做两层检测：
      1. 按精确字节大小聚类
      2. 对同大小文件计算首尾 hash，仅 hash 也相同的才算"完全相同"

    use_hash=False 时退化为旧逻辑（仅按 MB 大小聚类，速度快但误报多）。
    """
    local_path = _resolve_local_path(path)
    scan_path = Path(local_path)
    if not scan_path.exists():
        raise HTTPException(
            status_code=400,
            detail=f"路径不存在: {local_path}（前端传入: {path}）",
        )

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

    if not use_hash:
        # 旧逻辑：MB 级别聚类
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

    confirmed = []  # hash 相同 → 高置信
    size_only = []  # 大小相同但 hash 不同 → 低置信

    for size, group in size_groups.items():
        if len(group) < 2:
            continue
        # 对同大小组做 hash 二次分组
        hash_groups = defaultdict(list)
        for v in group:
            h = _quick_hash(v["_path_obj"])
            if h:
                hash_groups[h].append(v)
            else:
                hash_groups[f"_no_hash_{v['path']}"].append(v)

        for h, hg in hash_groups.items():
            if len(hg) > 1:
                confirmed.append({
                    "match_type": "hash",
                    "hash": h,
                    "size": size,
                    "size_mb": size // (1024 * 1024),
                    "files": [{k: v[k] for k in ("path", "name", "size")} for v in hg],
                })

        # 检查"大小相同但分到不同 hash 组"的情况
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

    return {
        "total_videos": len(videos),
        "confirmed_duplicates": len(confirmed),
        "size_only_matches": len(size_only),
        # 兼容旧前端字段
        "potential_duplicates": len(confirmed) + len(size_only),
        "groups": (confirmed + size_only)[:50],
    }


@router.get("/duplicates-by-metadata")
def find_duplicates_by_metadata(
    library_id: Optional[str] = None,
):
    """
    基于 Jellyfin 元数据检测重复（语义重复，相对 /duplicates 的字节级重复）。

    分组规则（按优先级，命中即停）：
      Movie:
        1. ProviderIds.Tmdb 相同 → match_type='tmdb'   （最强信号）
        2. ProviderIds.Imdb 相同 → match_type='imdb'
        3. normalize(Name) + ProductionYear 相同 → match_type='title_year'
      Episode:
        SeriesId + ParentIndexNumber + IndexNumber 相同 → match_type='episode'
        （即 同剧·同季·同集 出现多次）

    比起字节级 hash：能识别"同一作品的不同清晰度版本"等真正语义重复，
    且不需要扫盘，对几千项目的库瞬时返回。
    """
    from web.backend.config import settings
    from common.jellyfin_client import JellyfinClient

    if not settings.jellyfin_api_key:
        raise HTTPException(status_code=400, detail="未配置 Jellyfin API Key")

    client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
    fields = "ProviderIds,ProductionYear,Path,MediaSources,SeriesName,SeriesId,ParentIndexNumber,IndexNumber"

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
        except Exception:
            return []

    movies = _fetch('Movie')
    episodes = _fetch('Episode')

    def _file_size(item: dict) -> int:
        # MediaSources[0].Size 通常就是真实文件 byte
        ms = item.get('MediaSources') or []
        if ms and isinstance(ms, list):
            sz = ms[0].get('Size')
            if isinstance(sz, int):
                return sz
        return 0

    from web.backend.path_translator import translate_path_with_settings

    def _slim(item: dict, version_label: str = '') -> dict:
        return {
            "jellyfin_id": item.get('Id'),
            "name": item.get('Name'),
            "year": item.get('ProductionYear'),
            "path": translate_path_with_settings(item.get('Path')) if item.get('Path') else None,
            "size": _file_size(item),
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
