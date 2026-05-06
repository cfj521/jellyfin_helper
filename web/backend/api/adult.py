"""
成人内容管理 API
路由仅在 settings.adult_enabled = True 时挂载。
"""
import sys
import json
import logging
from pathlib import Path
from typing import List, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session

ROOT_DIR = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from web.backend.database import get_db, AdultItem
from web.backend.config import settings
from web.backend.api.tasks import create_task, update_task_progress, complete_task
from web.backend.task_restart import register_resumable
from web.backend.path_translator import translate_path_with_settings

logger = logging.getLogger(__name__)
router = APIRouter()


def _translate_paths(paths: List[str]) -> List[str]:
    """批量把 Jellyfin 视角路径翻译成本机路径。已是本机路径的会原样返回。"""
    return [translate_path_with_settings(p) or p for p in paths]


VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.wmv', '.mov', '.flv', '.webm', '.m4v', '.ts'}


class ScanRequest(BaseModel):
    path: Optional[str] = None  # 不填走 settings.adult_media_path
    library_id: Optional[str] = None  # 优先级最高：从 Jellyfin 库获取路径


class ScrapeRequest(BaseModel):
    only_unscraped: bool = True
    limit: Optional[int] = None
    write_nfo: bool = True
    download_cover: bool = True


class ManualUpdate(BaseModel):
    title: Optional[str] = None
    release_date: Optional[str] = None
    studio: Optional[str] = None
    director: Optional[str] = None
    actors: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    rating: Optional[float] = None


# ---------- 列表 / 详情 / 手动修改 ----------

@router.get("/items")
async def list_items(
    search: Optional[str] = None,
    has_metadata: Optional[bool] = None,
    actor: Optional[str] = None,
    tag: Optional[str] = None,
    in_jellyfin: Optional[bool] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """番号库列表

    in_jellyfin: True=只看已被 Jellyfin 收录的；False=只看没被收录的；None=全部
    """
    query = db.query(AdultItem)
    if search:
        query = query.filter((AdultItem.code.contains(search)) | (AdultItem.title.contains(search)))
    if has_metadata is True:
        query = query.filter(AdultItem.title != None)  # noqa: E711
    elif has_metadata is False:
        query = query.filter(AdultItem.title == None)  # noqa: E711
    if actor:
        query = query.filter(AdultItem.actors.contains(actor))
    if tag:
        query = query.filter(AdultItem.tags.contains(tag))

    # 不过滤 Jellyfin 时直接 SQL 翻页；过滤时需要先全部拉再筛
    if in_jellyfin is None:
        total = query.count()
        items = query.order_by(AdultItem.code).offset(offset).limit(limit).all()
        return {
            "total": total,
            "items": [_to_dict_with_jellyfin(i) for i in items],
        }

    # in_jellyfin 过滤需要每条反查
    from web.backend.api.jellyfin import lookup_jellyfin_item, jellyfin_web_url
    all_items = query.order_by(AdultItem.code).all()
    filtered = []
    for i in all_items:
        jf = lookup_jellyfin_item(i.file_path) if i.file_path else None
        in_jf = jf is not None
        if in_jellyfin == in_jf:
            d = _to_dict(i)
            if jf:
                d['jellyfin_id'] = jf['id']
                d['jellyfin_url'] = jellyfin_web_url(jf['id'])
            filtered.append(d)
    total = len(filtered)
    return {"total": total, "items": filtered[offset:offset + limit]}


def _to_dict_with_jellyfin(item: AdultItem) -> dict:
    """生成列表 dict 并附带 Jellyfin 状态"""
    from web.backend.api.jellyfin import lookup_jellyfin_item, jellyfin_web_url
    d = _to_dict(item)
    if item.file_path:
        jf = lookup_jellyfin_item(item.file_path)
        if jf:
            d['jellyfin_id'] = jf['id']
            d['jellyfin_url'] = jellyfin_web_url(jf['id'])
            d['jellyfin_name'] = jf.get('name')
    return d


@router.get("/items/{item_id}")
async def get_item(item_id: int, db: Session = Depends(get_db)):
    item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    d = _to_dict(item, full=True)
    # 附加 Jellyfin 状态
    if item.file_path:
        from web.backend.api.jellyfin import lookup_jellyfin_item, jellyfin_web_url
        jf = lookup_jellyfin_item(item.file_path)
        if jf:
            d['jellyfin_id'] = jf['id']
            d['jellyfin_url'] = jellyfin_web_url(jf['id'])
            d['jellyfin_name'] = jf.get('name')
    return d


@router.put("/items/{item_id}")
async def update_item(item_id: int, payload: ManualUpdate, db: Session = Depends(get_db)):
    """手动修正元数据"""
    item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")

    if payload.title is not None:
        item.title = payload.title
    if payload.release_date is not None:
        item.release_date = payload.release_date
    if payload.studio is not None:
        item.studio = payload.studio
    if payload.director is not None:
        item.director = payload.director
    if payload.actors is not None:
        item.actors = json.dumps(payload.actors, ensure_ascii=False)
    if payload.tags is not None:
        item.tags = json.dumps(payload.tags, ensure_ascii=False)
    if payload.rating is not None:
        item.rating = payload.rating

    db.commit()
    return _to_dict(item, full=True)


@router.delete("/items/{item_id}")
async def delete_item(
    item_id: int,
    delete_files: bool = False,        # 同时删除硬盘上的视频 + nfo + 封面
    delete_in_jellyfin: bool = False,  # 同时从 Jellyfin 库中删除条目
    db: Session = Depends(get_db),
):
    item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")

    deleted_files: List[str] = []
    failed_deletes: List[str] = []
    jellyfin_deleted = False

    # 1. 先从 Jellyfin 删（避免删了文件但 Jellyfin 还引用）
    if delete_in_jellyfin and settings.jellyfin_api_key and item.file_path:
        from web.backend.api.jellyfin import lookup_jellyfin_item, invalidate_path_index
        from common.jellyfin_client import JellyfinClient
        jf = lookup_jellyfin_item(item.file_path)
        if jf:
            try:
                client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
                client._request('DELETE', f'/Items/{jf["id"]}')
                jellyfin_deleted = True
                invalidate_path_index()
            except Exception as e:
                logger.warning(f"删除 Jellyfin 条目失败 {jf['id']}: {e}")
                failed_deletes.append(f"Jellyfin item {jf['id']}: {e}")

    # 2. 删本地文件
    if delete_files:
        targets = []
        if item.file_path:
            targets.append(Path(item.file_path))
        if item.nfo_path:
            targets.append(Path(item.nfo_path))
        if item.poster_path:
            targets.append(Path(item.poster_path))
        # 也尝试找 fanart / 同名 nfo
        if item.file_path:
            stem_path = Path(item.file_path).with_suffix('')
            for ext in ['-fanart.jpg', '-poster.jpg', '.nfo']:
                p = Path(str(stem_path) + ext)
                if p.exists() and p not in targets:
                    targets.append(p)

        for p in targets:
            try:
                if p.exists():
                    p.unlink()
                    deleted_files.append(str(p))
            except Exception as e:
                logger.warning(f"删除文件失败 {p}: {e}")
                failed_deletes.append(f"{p}: {e}")

    # 3. 最后删数据库记录
    db.delete(item)
    db.commit()

    return {
        "ok": True,
        "jellyfin_deleted": jellyfin_deleted,
        "deleted_files": deleted_files,
        "failed_deletes": failed_deletes,
    }


# ---------- Watcher 状态 / 控制 ----------

@router.get("/watcher/status")
async def watcher_status():
    """获取 watcher 当前状态"""
    from web.backend.services.adult_watcher import watcher
    return watcher.status()


@router.post("/watcher/run-now")
async def watcher_run_now(library_id: Optional[str] = None):
    """
    立即扫描配置的所有成人库（绕过冷却）。

    Args:
        library_id: 仅扫指定库；不传则扫全部 settings.adult_library_ids
    """
    from web.backend.services.adult_watcher import watcher
    target_ids = [library_id] if library_id else (settings.adult_library_ids or [])
    if not target_ids:
        raise HTTPException(status_code=400, detail="未配置成人库")

    scheduled = watcher.trigger_libraries(target_ids, bypass_cooldown=True)
    return {
        "ok": True,
        "scheduled": scheduled,
        "skipped": [lib for lib in target_ids if lib not in scheduled],
        "message": f"已触发 {len(scheduled)} 个库的扫描" + (
            f"（跳过 {len(target_ids) - len(scheduled)} 个已在跑中的库）"
            if len(scheduled) < len(target_ids) else ""
        ),
    }


# ---------- 智能识别成人库 ----------

# 关键词命中视为"看起来像成人库"
_ADULT_HINT_KEYWORDS = [
    '番号', 'JAV', 'jav', 'Adult', 'adult', 'AV', '成人', '18+', 'XXX', 'xxx',
    'JavBus', 'JavDB', 'R18', 'r18',
]


@router.get("/detect-libraries")
async def detect_adult_libraries():
    """
    智能识别 Jellyfin 中"看起来像番号库"的库。
    判断依据：库名称包含 番号 / JAV / 成人 / Adult / R18 等关键词，或路径中含相同词。
    返回带匹配理由的候选列表，供前端弹"首次确认"对话框。
    """
    if not settings.jellyfin_api_key:
        raise HTTPException(status_code=400, detail="未配置 Jellyfin API Key")

    from common.jellyfin_client import JellyfinClient
    client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
    libraries = client.get_libraries_normalized()

    candidates = []
    for lib in libraries:
        reasons = []
        # 1. 名字命中
        for kw in _ADULT_HINT_KEYWORDS:
            if kw in (lib['name'] or ''):
                reasons.append(f"库名包含 {kw!r}")
                break
        # 2. 路径命中
        for loc in lib['locations']:
            for kw in _ADULT_HINT_KEYWORDS:
                if kw.lower() in loc.lower():
                    reasons.append(f"路径含 {kw!r}")
                    break
            if reasons:
                break

        candidates.append({
            **lib,
            "is_adult_candidate": bool(reasons),
            "reasons": reasons,
            "currently_selected": lib['id'] in settings.adult_library_ids,
        })

    return {
        "configured": list(settings.adult_library_ids),
        "auto_detect_done": not settings.adult_auto_detect,  # auto_detect=False 视为"已确认过"
        "libraries": candidates,
    }


# ---------- 扫描入库 ----------

@router.post("/scan")
async def scan_directory(
    payload: ScanRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    扫描媒体目录，识别番号入库。
    路径来源优先级：
      1. payload.library_id 指定的单个 Jellyfin 库
      2. payload.path 直接指定的目录
      3. settings.adult_library_ids 中所有库（多库聚合）
      4. settings.adult_extra_paths 额外路径
      5. settings.adult_media_path（兼容旧字段）
    """
    paths: List[str] = []

    if payload.library_id:
        from web.backend.api.jellyfin import get_library_by_id
        lib = get_library_by_id(payload.library_id)
        if not lib:
            raise HTTPException(status_code=404, detail=f"Jellyfin 库不存在: {payload.library_id}")
        paths = _translate_paths(lib['locations'])
        label = f"库 {lib['name']}"
    elif payload.path:
        local = translate_path_with_settings(payload.path) or payload.path
        if not Path(local).exists():
            raise HTTPException(
                status_code=400,
                detail=f"路径不存在: {local}（前端传入: {payload.path}）",
            )
        paths = [local]
        label = payload.path
    else:
        # 自动从 config 读：所有配置过的成人库 + extra_paths
        if settings.adult_library_ids and settings.jellyfin_api_key:
            from common.jellyfin_client import JellyfinClient
            client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
            libs = client.get_libraries_normalized()
            for lib in libs:
                if lib['id'] in settings.adult_library_ids:
                    paths.extend(_translate_paths(lib['locations']))
        if settings.adult_extra_paths:
            paths.extend(_translate_paths(settings.adult_extra_paths))
        if not paths and settings.adult_media_path:
            paths.append(translate_path_with_settings(settings.adult_media_path) or settings.adult_media_path)

        if not paths:
            raise HTTPException(
                status_code=400,
                detail="未配置成人内容库。请在设置中选择 Jellyfin 媒体库或填写自定义路径"
            )
        label = f"{len(paths)} 个路径"

    task = create_task(
        db,
        "adult_scan",
        f"扫描番号: {label}",
        params={"scan_paths": paths},
    )
    background_tasks.add_task(run_adult_scan, task.id, paths)
    return {"task_id": task.id, "status": "started", "paths_count": len(paths)}


@register_resumable("adult_scan", ["scan_paths"])
def run_adult_scan(task_id: int, scan_paths: List[str]):
    from web.backend.database import SessionLocal
    from tools.adult_manager.code_extractor import extract_code

    # 路径翻译幂等：API 入口已翻译，恢复任务再翻一次保险（旧 params 可能是 jellyfin 路径）
    scan_paths = _translate_paths(scan_paths)

    db = SessionLocal()
    try:
        update_task_progress(db, task_id, 10, f"扫描 {len(scan_paths)} 个目录...")

        videos: List[Path] = []
        for p in scan_paths:
            videos.extend(
                [v for v in Path(p).rglob("*") if v.is_file() and v.suffix.lower() in VIDEO_EXTS]
            )
        update_task_progress(db, task_id, 30, f"发现 {len(videos)} 个视频，识别番号...")

        new_count = 0
        updated_count = 0
        unrecognized = 0

        for i, p in enumerate(videos):
            code = extract_code(p.name)
            if not code:
                unrecognized += 1
                continue

            existing = db.query(AdultItem).filter(AdultItem.code == code).first()
            if existing:
                existing.file_path = str(p)
                updated_count += 1
            else:
                db.add(AdultItem(code=code, file_path=str(p)))
                new_count += 1

            if i % 100 == 0:
                progress = 30 + int(60 * i / max(len(videos), 1))
                update_task_progress(db, task_id, progress, f"处理中 {i}/{len(videos)}")
                db.commit()

        db.commit()

        complete_task(db, task_id, {
            "total_videos": len(videos),
            "new": new_count,
            "updated": updated_count,
            "unrecognized": unrecognized,
        })

    except Exception as e:
        logger.exception("番号扫描失败")
        complete_task(db, task_id, {"error": str(e)}, success=False)
    finally:
        db.close()


# ---------- 单条刮削 / 批量刮削 ----------

@router.post("/scrape/{code}")
async def scrape_one(
    code: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """刮削单个番号（按 code 查找）"""
    item = db.query(AdultItem).filter(AdultItem.code == code).first()
    if not item:
        raise HTTPException(status_code=404, detail=f"番号不存在: {code}")

    task = create_task(db, "adult_scrape", f"刮削: {code}")
    background_tasks.add_task(run_adult_scrape_batch, task.id, [item.id], True, True)
    return {"task_id": task.id, "status": "started"}


@router.post("/scrape/batch")
async def scrape_batch(
    payload: ScrapeRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """批量刮削"""
    query = db.query(AdultItem)
    if payload.only_unscraped:
        query = query.filter(AdultItem.title == None)  # noqa: E711
    items = query.all()
    if payload.limit:
        items = items[:payload.limit]

    if not items:
        raise HTTPException(status_code=400, detail="没有需要刮削的条目")

    task = create_task(db, "adult_scrape_batch", f"批量刮削 {len(items)} 条")
    background_tasks.add_task(
        run_adult_scrape_batch,
        task.id,
        [i.id for i in items],
        payload.write_nfo,
        payload.download_cover,
    )
    return {"task_id": task.id, "status": "started"}


def run_adult_scrape_batch(task_id: int, item_ids: List[int], write_nfo: bool, download_cover: bool):
    from web.backend.database import SessionLocal
    from tools.adult_manager.scrapers.manager import ScraperManager
    from tools.adult_manager.nfo_writer import write_nfo as do_write_nfo

    db = SessionLocal()
    try:
        manager = ScraperManager(
            delay=settings.adult_scraper_delay,
            proxy=settings.adult_proxy or None,
            sources=settings.adult_sources,
        )
        if not manager.scrapers:
            complete_task(db, task_id, {"error": "没有启用任何刮削源（检查 config.yaml.adult.sources）"}, success=False)
            return
        logger.info(f"刮削启用源: {manager.active_sources}")

        total = len(item_ids)
        success = failed = not_found = 0
        details = []

        for i, item_id in enumerate(item_ids):
            item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
            if not item:
                continue

            progress = 5 + int(90 * (i + 1) / total)
            update_task_progress(db, task_id, progress, f"[{i+1}/{total}] {item.code}")

            try:
                result = manager.scrape(item.code)
                if not result:
                    not_found += 1
                    details.append({"code": item.code, "status": "not_found"})
                    continue

                # 写入数据库
                d = result.to_dict()
                item.title = d.get('title')
                item.release_date = d.get('release_date')
                item.studio = d.get('studio')
                item.director = d.get('director')
                item.actors = json.dumps(d.get('actors') or [], ensure_ascii=False)
                item.tags = json.dumps(d.get('tags') or [], ensure_ascii=False)
                item.cover_url = d.get('cover_url')
                item.rating = d.get('rating')
                item.source = d.get('source')

                # 下载封面
                if download_cover and item.cover_url and item.file_path:
                    try:
                        cover_path = _download_cover(item.cover_url, Path(item.file_path))
                        if cover_path:
                            item.poster_path = str(cover_path)
                    except Exception as e:
                        logger.warning(f"封面下载失败 {item.code}: {e}")

                # 生成 NFO
                if write_nfo and item.file_path:
                    try:
                        nfo_path = do_write_nfo(Path(item.file_path), d)
                        item.nfo_path = str(nfo_path)
                    except Exception as e:
                        logger.warning(f"NFO 写入失败 {item.code}: {e}")

                db.commit()
                success += 1
                details.append({"code": item.code, "status": "success", "title": item.title})

            except Exception as e:
                logger.exception(f"刮削异常 {item.code}")
                failed += 1
                details.append({"code": item.code, "status": "failed", "error": str(e)})

        # 写入 NFO/封面后通知 Jellyfin 重新扫描
        refreshed = False
        if write_nfo or download_cover:
            if success > 0 and settings.jellyfin_api_key:
                try:
                    from common.jellyfin_client import JellyfinClient
                    update_task_progress(db, task_id, 99, "通知 Jellyfin 刷新...")
                    JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key).refresh_all_libraries()
                    refreshed = True
                except Exception as e:
                    logger.warning(f"触发 Jellyfin 刷新失败: {e}")

        complete_task(db, task_id, {
            "total": total,
            "success": success,
            "failed": failed,
            "not_found": not_found,
            "jellyfin_refreshed": refreshed,
            "details": details[:200],
        })

    except Exception as e:
        logger.exception("批量刮削任务失败")
        complete_task(db, task_id, {"error": str(e)}, success=False)
    finally:
        db.close()


@router.post("/items/{item_id}/sync-from-jellyfin")
async def sync_from_jellyfin(item_id: int, db: Session = Depends(get_db)):
    """
    从 Jellyfin 同步元数据回番号库（防止重新刮削覆盖手动修改）。
    通过 file_path 反查 Jellyfin Item，拉取其 People/Genres/Tags/Overview 等。
    """
    item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    if not item.file_path:
        raise HTTPException(status_code=400, detail="番号没有关联视频文件")

    from web.backend.api.jellyfin import lookup_jellyfin_item
    from common.jellyfin_client import JellyfinClient

    jf = lookup_jellyfin_item(item.file_path)
    if not jf:
        raise HTTPException(status_code=404, detail="Jellyfin 中未找到此番号对应的条目")

    if not settings.jellyfin_api_key:
        raise HTTPException(status_code=400, detail="未配置 Jellyfin API Key")

    client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
    try:
        full_item = client._request(
            'GET',
            f'/Items/{jf["id"]}',
            params={'Fields': 'Overview,People,Genres,Tags,Studios,ProductionYear,PremiereDate'},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取 Jellyfin 详情失败: {e}")

    if not full_item:
        raise HTTPException(status_code=502, detail="Jellyfin 没返回详情")

    # 同步字段
    changed_fields = []
    if full_item.get('Name') and full_item['Name'] != item.title:
        item.title = full_item['Name']
        changed_fields.append('title')
    if full_item.get('Overview'):
        # adult_items 表没有 plot 字段，简介忽略（或者可以写到 NFO 但不入库）
        pass
    if full_item.get('PremiereDate'):
        date = full_item['PremiereDate'][:10]
        if date != item.release_date:
            item.release_date = date
            changed_fields.append('release_date')
    if full_item.get('ProductionYear'):
        # 如果只有年份没日期，构造 yyyy-01-01
        if not item.release_date:
            item.release_date = f"{full_item['ProductionYear']}-01-01"
            changed_fields.append('release_date')
    studios = full_item.get('Studios') or []
    if studios:
        new_studio = studios[0].get('Name')
        if new_studio and new_studio != item.studio:
            item.studio = new_studio
            changed_fields.append('studio')
    people = full_item.get('People') or []
    actors_list = [p['Name'] for p in people if p.get('Type') == 'Actor' and p.get('Name')]
    director_list = [p['Name'] for p in people if p.get('Type') == 'Director' and p.get('Name')]
    if actors_list:
        item.actors = json.dumps(actors_list, ensure_ascii=False)
        changed_fields.append('actors')
    if director_list and not item.director:
        item.director = director_list[0]
        changed_fields.append('director')
    tags_list = (full_item.get('Tags') or []) + (full_item.get('Genres') or [])
    if tags_list:
        # 去重
        tags_list = list(dict.fromkeys(tags_list))
        item.tags = json.dumps(tags_list, ensure_ascii=False)
        changed_fields.append('tags')

    db.commit()
    return {
        "ok": True,
        "changed_fields": changed_fields,
        "item": _to_dict(item, full=True),
    }


@router.post("/items/{item_id}/nfo")
async def regenerate_nfo(item_id: int, db: Session = Depends(get_db)):
    """重新生成 NFO"""
    from tools.adult_manager.nfo_writer import write_nfo as do_write_nfo

    item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    if not item.file_path:
        raise HTTPException(status_code=400, detail="条目没有关联视频文件")

    data = _to_dict(item, full=True)
    try:
        nfo_path = do_write_nfo(Path(item.file_path), data)
        item.nfo_path = str(nfo_path)
        db.commit()
        return {"ok": True, "nfo_path": str(nfo_path)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入失败: {e}")


# ---------- 工具 ----------

def _to_dict(item: AdultItem, full: bool = False) -> dict:
    out = {
        "id": item.id,
        "code": item.code,
        "title": item.title,
        "release_date": item.release_date,
        "studio": item.studio,
        "director": item.director,
        "rating": item.rating,
        "cover_url": item.cover_url,
        "source": item.source,
        "has_metadata": bool(item.title),
    }
    if full:
        out.update({
            "actors": json.loads(item.actors) if item.actors else [],
            "tags": json.loads(item.tags) if item.tags else [],
            "file_path": item.file_path,
            "nfo_path": item.nfo_path,
            "poster_path": item.poster_path,
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        })
    else:
        # 列表模式只返回 actors/tags 简要
        out["actors"] = json.loads(item.actors) if item.actors else []
        out["tags"] = json.loads(item.tags) if item.tags else []
    return out


def _download_cover(url: str, video_path: Path) -> Optional[Path]:
    """下载封面到 <video_stem>-poster.jpg"""
    try:
        r = requests.get(url, timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
        r.raise_for_status()
        cover_path = video_path.with_name(video_path.stem + '-poster.jpg')
        cover_path.write_bytes(r.content)
        return cover_path
    except Exception as e:
        logger.warning(f"封面下载失败 {url}: {e}")
        return None
