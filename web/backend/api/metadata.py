"""
元数据管理 API
演员照片修复、海报下载等
"""
import sys
import logging
from pathlib import Path
from typing import List, Optional, Dict
import json

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session

ROOT_DIR = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from web.backend.database import get_db, Task, ActorInfo, MediaItem
from web.backend.config import settings
# 批量端点：TMDBClient(..., batch=True) 启用 batch 配额（min/hour/day 滑窗）；
# 单条端点：默认 batch=False，仅受 hard delay + 限流暂停 约束。
from web.backend.api.tasks import (
    create_task, update_task_progress, complete_task,
    cancellable_task, TaskCancelledError, mark_task_cancelled,
)
from web.backend.task_restart import register_resumable

logger = logging.getLogger(__name__)


router = APIRouter()


def _make_wikidata_client(batch: bool = False):
    """按 settings 构造 Wikidata 客户端；未启用或 UA 缺失返回 None。

    batch=True 用于批量演员修复，启用 batch 配额（15/min, 600/h, 3000/d）。
    """
    if not settings.wikidata_enabled:
        return None
    if not settings.wikidata_user_agent:
        logger.warning("Wikidata 已启用但缺少 user_agent，跳过")
        return None
    try:
        from common.wikidata_client import WikidataClient
        from common.rate_limiter import WIKIDATA_DELAY
        return WikidataClient(
            user_agent=settings.wikidata_user_agent,
            language_order=settings.wikidata_language_order,
            delay=WIKIDATA_DELAY,
            batch=batch,
        )
    except Exception as e:
        logger.warning(f"初始化 Wikidata 客户端失败: {e}")
        return None


class ActorFixRequest(BaseModel):
    scan_only: bool = False
    limit: Optional[int] = None
    skip_existing: bool = True
    library_id: Optional[str] = None  # 仅处理出现在该库内容里的演员
    library_ids: Optional[List[str]] = None  # 多库（与 library_id 二选一）


class ActorResponse(BaseModel):
    id: int
    jellyfin_id: str
    name: str
    tmdb_id: Optional[int]
    has_image: bool
    image_url: Optional[str]
    image_source: Optional[str] = None

    class Config:
        from_attributes = True


@router.get("/actors")
def list_actors(
    has_image: Optional[bool] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """获取演员列表"""
    query = db.query(ActorInfo)

    if has_image is not None:
        query = query.filter(ActorInfo.has_image == has_image)
    if search:
        query = query.filter(ActorInfo.name.contains(search))

    total = query.count()
    actors = query.order_by(ActorInfo.name).offset(offset).limit(limit).all()

    return {
        "total": total,
        "actors": [
            {
                "id": a.id,
                "jellyfin_id": a.jellyfin_id,
                "name": a.name,
                "tmdb_id": a.tmdb_id,
                "has_image": a.has_image,
                "image_url": a.image_url
            }
            for a in actors
        ]
    }


@router.post("/actors/scan")
def scan_actors(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """扫描 Jellyfin 中的演员"""
    if not settings.jellyfin_api_key:
        raise HTTPException(status_code=400, detail="未配置 Jellyfin API Key")

    logger.info("/actors/scan: 启动 Jellyfin 演员扫描任务")
    task = create_task(db, "actor_scan", "扫描 Jellyfin 演员...", params={})

    background_tasks.add_task(run_actor_scan, task.id)

    return {"task_id": task.id, "status": "started", "message": "扫描任务已启动"}


@register_resumable("actor_scan", [])
@cancellable_task
def run_actor_scan(task_id: int):
    """执行演员扫描（后台任务）"""
    from web.backend.database import SessionLocal
    from common.jellyfin_client import JellyfinClient

    db = SessionLocal()
    try:
        update_task_progress(db, task_id, 10, "连接 Jellyfin...")

        client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
        persons = client.get_all_persons()

        update_task_progress(db, task_id, 30, f"获取到 {len(persons)} 个演员")

        # 更新数据库
        new_count = 0
        updated_count = 0

        for i, person in enumerate(persons):
            jellyfin_id = person.get('Id', '')
            name = person.get('Name', '')

            if not jellyfin_id or not name:
                continue

            existing = db.query(ActorInfo).filter(ActorInfo.jellyfin_id == jellyfin_id).first()

            has_image = bool(person.get('ImageTags', {}).get('Primary'))

            # Jellyfin 已经在 ProviderIds 里返回了 TMDB ID（Jellyfin 抓元数据时拿到的），
            # 之前漏了没入库 —— 列表里 tmdb_id 全空就是这个原因
            tmdb_id = None
            tmdb_raw = (person.get('ProviderIds') or {}).get('Tmdb')
            if tmdb_raw:
                try:
                    tmdb_id = int(tmdb_raw)
                except (ValueError, TypeError):
                    tmdb_id = None

            if existing:
                existing.name = name
                existing.has_image = has_image
                # 只在原来没有时回填，避免覆盖"修复演员图片"任务校正过的值
                if tmdb_id and not existing.tmdb_id:
                    existing.tmdb_id = tmdb_id
                updated_count += 1
            else:
                actor = ActorInfo(
                    jellyfin_id=jellyfin_id,
                    name=name,
                    tmdb_id=tmdb_id,
                    has_image=has_image,
                )
                db.add(actor)
                new_count += 1

            if i % 100 == 0:
                progress = 30 + (i / len(persons)) * 60
                update_task_progress(db, task_id, progress, f"处理中 {i}/{len(persons)}")
                db.commit()

        db.commit()

        without_image = db.query(ActorInfo).filter(ActorInfo.has_image == False).count()

        logger.info(
            f"run_actor_scan 完成: task={task_id} jellyfin_persons={len(persons)} "
            f"new={new_count} updated={updated_count} without_image={without_image}"
        )

        complete_task(db, task_id, {
            "total": len(persons),
            "new": new_count,
            "updated": updated_count,
            "without_image": without_image
        })

    except Exception as e:
        logger.exception(f"run_actor_scan 失败: task={task_id}")
        complete_task(db, task_id, {"error": str(e)}, success=False)
    finally:
        db.close()


# ---------- 删除"中文译名 + 无 TMDB ID"的演员 ----------

class DeleteCjkActorsRequest(BaseModel):
    """预览模式不实删，只列出要删的演员；执行模式真的删。"""
    dry_run: bool = True


def _has_cjk(s: str) -> bool:
    """字符串是否含 CJK 字符（统一汉字 / 假名 / 谚文）。"""
    if not s:
        return False
    return (
        any('一' <= ch <= '鿿' for ch in s)
        or any('぀' <= ch <= 'ヿ' for ch in s)
        or any('가' <= ch <= '힯' for ch in s)
    )


@router.post("/actors/delete-cjk")
def delete_cjk_named_actors(
    req: DeleteCjkActorsRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    删除"中文/日文/韩文译名 且 没有 TMDB ID"的演员。
    这些通常是早期 zh-CN 语言抓元数据时缓存的西方演员中文译名，
    本地 DB 和 Jellyfin 都会清理；下次 Jellyfin 重扫电影元数据会按当前
    TMDB 语言（en-US）重新抓取，自动带回正确的英文名。

    保留：CJK 名但有 TMDB ID 的（如 井上幸 — 真正的日本演员）。
    """
    if not settings.jellyfin_api_key:
        raise HTTPException(status_code=400, detail="未配置 Jellyfin API Key")

    mode = "预览" if req.dry_run else "执行"
    logger.warning(f"/actors/delete-cjk: dry_run={req.dry_run}（破坏性操作）")
    task = create_task(db, "actor_delete_cjk", f"{mode}删除 CJK 名演员...")
    background_tasks.add_task(run_delete_cjk_actors, task.id, req.dry_run)
    return {"task_id": task.id, "status": "started", "message": f"{mode}任务已启动"}


@cancellable_task
def run_delete_cjk_actors(task_id: int, dry_run: bool):
    """后台任务：扫 → 筛 CJK + no tmdb → 删 Jellyfin Person + 本地 DB。"""
    from web.backend.database import SessionLocal
    from common.jellyfin_client import JellyfinClient

    db = SessionLocal()
    try:
        update_task_progress(db, task_id, 5, "扫描数据库 ...")
        client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)

        all_actors = db.query(ActorInfo).all()
        # 规则：名字含 CJK + 没有 TMDB ID 才删
        targets = [a for a in all_actors if _has_cjk(a.name) and not a.tmdb_id]

        total = len(targets)
        update_task_progress(
            db, task_id, 10,
            f"筛出 {total} 个待删演员（CJK 名 + 无 TMDB ID）"
        )

        if total == 0:
            complete_task(db, task_id, {
                'dry_run': dry_run, 'total': 0, 'deleted': 0,
                'errors': [], 'details': [],
            })
            return

        details = []
        deleted_count = 0

        for idx, actor in enumerate(targets):
            pct = 10 + int(85 * (idx + 1) / total)
            update_task_progress(
                db, task_id, pct,
                f"{'预览' if dry_run else '删除'} [{idx+1}/{total}] {actor.name}",
            )

            entry = {
                'name': actor.name,
                'jellyfin_id': actor.jellyfin_id,
                'has_image': actor.has_image,
                'jellyfin_deleted': False,
                'db_deleted': False,
                'error': None,
            }

            if dry_run:
                entry['error'] = '(预览模式)'
                details.append(entry)
                continue

            # 1. 删 Jellyfin Person item（被电影引用的 People 关系也会自动清掉；
            # 下次电影元数据刷新时 Jellyfin 会按当前 TMDB 语言重新抓回来）
            try:
                client.delete_item(actor.jellyfin_id)
                entry['jellyfin_deleted'] = True
            except Exception as e:
                # 可能 Jellyfin 已经没这个 person 了，记录错误但继续清本地
                entry['error'] = f'Jellyfin 删除失败: {e}'
                logger.warning(f"删除 Jellyfin Person 失败: {actor.name} - {e}")

            # 2. 删本地 DB
            try:
                db.delete(actor)
                entry['db_deleted'] = True
                if entry['jellyfin_deleted']:
                    deleted_count += 1
            except Exception as e:
                msg = f'本地删除失败: {e}'
                entry['error'] = (entry['error'] + '; ' + msg) if entry['error'] else msg
                logger.exception(f"本地删除失败: {actor.name}")

            details.append(entry)

        if not dry_run:
            db.commit()

        complete_task(db, task_id, {
            'dry_run': dry_run,
            'total': total,
            'deleted': deleted_count,
            'errors': [d for d in details if d.get('error') and d['error'] != '(预览模式)'],
            'details': details,
        })

    except Exception as e:
        logger.exception("delete-cjk-actors 任务失败")
        complete_task(db, task_id, {"error": str(e)}, success=False)
    finally:
        db.close()


def _make_scope_label(
    library_id: Optional[str] = None,
    library_ids: Optional[List[str]] = None,
    item_paths: Optional[List[str]] = None,
) -> str:
    """构造 actor/poster_fix 任务的作用范围标签（作为 task.message 后缀）"""
    if item_paths:
        return f'选中 {len(item_paths)} 个条目'
    if library_ids:
        try:
            from web.backend.api.medialibraries import get_library_by_id
            names = []
            for lid in library_ids[:3]:
                lib = get_library_by_id(lid)
                if lib and lib.get('name'):
                    names.append(lib['name'])
            if names:
                if len(library_ids) > len(names):
                    names.append('...')
                return f"{len(library_ids)} 个库（{', '.join(names)}）"
        except Exception:
            pass
        return f'{len(library_ids)} 个库'
    if library_id:
        try:
            from web.backend.api.medialibraries import get_library_by_id
            lib = get_library_by_id(library_id)
            if lib and lib.get('name'):
                return f"库 {lib['name']}"
        except Exception:
            pass
        return f'库 {library_id}'
    return '所有库'


@router.post("/actors/fix")
def fix_actor_images(
    request: ActorFixRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """修复演员图片"""
    if not settings.jellyfin_api_key:
        raise HTTPException(status_code=400, detail="未配置 Jellyfin API Key")
    if not settings.tmdb_api_key:
        raise HTTPException(status_code=400, detail="未配置 TMDB API Key")

    mode = "扫描" if request.scan_only else "修复"
    scope_label = _make_scope_label(request.library_id, request.library_ids)
    logger.info(
        f"/actors/fix: scope={scope_label!r} scan_only={request.scan_only} "
        f"limit={request.limit} skip_existing={request.skip_existing} "
        f"library_id={request.library_id!r} library_ids={request.library_ids!r}"
    )
    task = create_task(
        db,
        "actor_fix",
        f"{mode}演员图片: {scope_label}",
        params={
            "scan_only": request.scan_only,
            "limit": request.limit,
            "skip_existing": request.skip_existing,
            "library_id": request.library_id,
            "library_ids": request.library_ids,
            "skip_refresh": False,
        },
    )

    background_tasks.add_task(
        run_actor_fix,
        task.id,
        request.scan_only,
        request.limit,
        request.skip_existing,
        request.library_id,
        request.library_ids,
        False,  # skip_refresh，端点默认会刷
    )

    return {"task_id": task.id, "status": "started", "message": f"{mode}任务已启动"}


@register_resumable(
    "actor_fix",
    ["scan_only", "limit", "skip_existing", "library_id", "library_ids", "skip_refresh"],
)
@cancellable_task
def run_actor_fix(
    task_id: int,
    scan_only: bool,
    limit: Optional[int],
    skip_existing: bool,
    library_id: Optional[str] = None,
    library_ids: Optional[List[str]] = None,
    skip_refresh: bool = False,
):
    """执行演员图片修复（后台任务）。

    DB 仅在读 actor 字段、写回结果、进度回写时短期持有；
    TMDB / Wikidata HTTP 与 Jellyfin 上传期间一律不持 db。
    """
    from web.backend.database import SessionLocal
    from common.jellyfin_client import JellyfinClient
    from common.tmdb_client import TMDBClient

    def _progress(pct, msg, patch=None):
        with SessionLocal() as db:
            update_task_progress(db, task_id, pct, msg, result_patch=patch)

    try:
        _progress(5, "初始化...")

        jf_client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
        tmdb_client = TMDBClient(settings.tmdb_api_key, language=settings.tmdb_language, batch=True)
        wd_client = _make_wikidata_client(batch=True)

        # 如果指定了 library_id 或 library_ids，先从 Jellyfin 拉这些库下出现的所有演员 ID
        library_actor_ids: Optional[set] = None
        target_library_ids: List[str] = []
        if library_ids:
            target_library_ids = [lid for lid in library_ids if lid]
        elif library_id:
            target_library_ids = [library_id]
        if target_library_ids:
            try:
                ids: set = set()
                for lid in target_library_ids:
                    items = jf_client.get_library_items(lid, item_types='Movie,Series', fields='People')
                    for it in items:
                        for person in (it.get('People') or []):
                            if person.get('Type') == 'Actor' and person.get('Id'):
                                ids.add(person['Id'])
                library_actor_ids = ids
                logger.info(f"library_ids={target_library_ids} 命中演员 {len(ids)} 个（已自动按 jellyfin_id 去重）")
            except Exception as e:
                logger.warning(f"获取库内演员失败，回退到全部演员: {e}")

        # 获取需要处理的演员（短事务：只取 id/name/jellyfin_id/tmdb_id）
        with SessionLocal() as db:
            query = db.query(ActorInfo)
            if skip_existing:
                query = query.filter(ActorInfo.has_image == False)
            if library_actor_ids is not None:
                query = query.filter(ActorInfo.jellyfin_id.in_(library_actor_ids))
            actor_rows = query.all()
            if limit:
                actor_rows = actor_rows[:limit]
            actor_targets = [
                {'id': a.id, 'name': a.name, 'jellyfin_id': a.jellyfin_id, 'tmdb_id': a.tmdb_id}
                for a in actor_rows
            ]

        total = len(actor_targets)
        _progress(10, f"共 {total} 个演员需要处理")

        success_count = 0
        failed_count = 0           # Jellyfin 上传失败（真正的"出错"）
        no_image_count = 0         # TMDB + Wikidata 都没有该演员的图（不算 error，独立统计）
        skipped_count = 0
        details: List[dict] = []  # 记录每个演员的处理结果（成功/失败/原因）
        EMIT_EVERY = 5  # 每 5 个演员 emit partial result

        for i, actor_info in enumerate(actor_targets):
            entry = {
                'id': actor_info['id'],
                'name': actor_info['name'],
                'jellyfin_id': actor_info['jellyfin_id'],
                'tmdb_id': actor_info['tmdb_id'],
                'status': None,           # success / no_image / failed
                'source': None,           # 命中后填 'tmdb' / 'wikidata'，前端能区分图片来源
                'error': None,
                'image_url': None,
            }
            try:
                progress = 10 + (i / max(total, 1)) * 85
                should_emit = (i > 0 and i % EMIT_EVERY == 0)
                _progress(
                    progress, f"处理: {actor_info['name']}",
                    patch=({
                        "total": total,
                        "success": success_count,
                        "failed": failed_count,
                        "no_image": no_image_count,
                        "skipped": skipped_count,
                        "scan_only": scan_only,
                        "details": details,
                    } if should_emit else None),
                )

                # ---- Step 1: TMDB 主源（HTTP，无 db）----
                resolved_url: Optional[str] = None
                image_data: Optional[bytes] = None
                source: Optional[str] = None  # 命中后填 'tmdb' / 'wikidata'
                new_tmdb_id: Optional[int] = None

                tmdb_result = tmdb_client.search_person(actor_info['name'])
                if tmdb_result:
                    new_tmdb_id = tmdb_result.get('id')
                    entry['tmdb_id'] = new_tmdb_id
                    profile_path = tmdb_result.get('profile_path')
                    if profile_path:
                        resolved_url = f"https://image.tmdb.org/t/p/original{profile_path}"
                        if not scan_only:
                            image_data = tmdb_client.download_image(profile_path)
                        if scan_only or image_data:
                            source = 'tmdb'

                # ---- Step 2: Wikidata 兜底（HTTP，无 db）----
                if source is None and wd_client is not None:
                    try:
                        wd_url, _wd_qid = wd_client.search_person_image(actor_info['name'])
                    except Exception as e:
                        logger.warning(f"Wikidata 查询异常 {actor_info['name']}: {e}")
                        wd_url = None
                    if wd_url:
                        resolved_url = wd_url
                        if not scan_only:
                            image_data = wd_client.download_image(wd_url)
                        if scan_only or image_data:
                            source = 'wikidata'

                # ---- Step 3: 入库 + 上传 Jellyfin ----
                if source is None:
                    # 「无图」≠「失败」：TMDB 和 Wikidata 都没有该演员的照片，是数据源限制不是出错。
                    # 单列 no_image_count 统计，前端用独立"无图"卡片展示，避免污染"失败"数。
                    no_image_count += 1
                    entry['status'] = 'no_image'
                    entry['error'] = 'TMDB 与 Wikidata 都没找到图片'
                    with SessionLocal() as db:
                        a = db.query(ActorInfo).filter(ActorInfo.id == actor_info['id']).first()
                        if a:
                            a.image_source = 'none'
                            if new_tmdb_id is not None:
                                a.tmdb_id = new_tmdb_id
                            db.commit()
                    details.append(entry)
                    continue

                entry['image_url'] = resolved_url
                entry['source'] = source

                if scan_only:
                    success_count += 1
                    entry['status'] = 'success'
                    with SessionLocal() as db:
                        a = db.query(ActorInfo).filter(ActorInfo.id == actor_info['id']).first()
                        if a:
                            if new_tmdb_id is not None:
                                a.tmdb_id = new_tmdb_id
                            a.image_url = resolved_url
                            db.commit()
                    details.append(entry)
                    continue

                # 上传到 Jellyfin（HTTP，无 db）
                upload_ok = jf_client.upload_person_image(actor_info['jellyfin_id'], image_data)

                # 短事务回写
                with SessionLocal() as db:
                    a = db.query(ActorInfo).filter(ActorInfo.id == actor_info['id']).first()
                    if a:
                        if new_tmdb_id is not None:
                            a.tmdb_id = new_tmdb_id
                        a.image_url = resolved_url
                        if upload_ok:
                            a.has_image = True
                            a.image_source = source
                        db.commit()

                if upload_ok:
                    success_count += 1
                    entry['status'] = 'success'
                else:
                    failed_count += 1
                    entry['status'] = 'failed'
                    entry['error'] = '上传到 Jellyfin 失败'

                details.append(entry)

            except Exception as e:
                failed_count += 1
                entry['status'] = 'failed'
                entry['error'] = str(e)
                details.append(entry)

        # 演员图上传成功 → 触发刷新（run_all 模式会传 skip_refresh=True 让编排器统一刷）
        refreshed = False
        if not scan_only and success_count > 0 and not skip_refresh:
            _progress(99, "通知 Jellyfin 刷新...")
            _refresh_jellyfin_libraries()
            refreshed = True

        with SessionLocal() as db:
            complete_task(db, task_id, {
                "total": total,
                "success": success_count,
                "failed": failed_count,
                "no_image": no_image_count,
                "skipped": skipped_count,
                "scan_only": scan_only,
                "jellyfin_refreshed": refreshed,
                "details": details,  # 详情页用，最多保留 500 条
            })

    except Exception as e:
        with SessionLocal() as db:
            complete_task(db, task_id, {"error": str(e)}, success=False)


@router.post("/actors/{actor_id}/fix")
def fix_single_actor(
    actor_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """修复单个演员图片"""
    actor = db.query(ActorInfo).filter(ActorInfo.id == actor_id).first()
    if not actor:
        raise HTTPException(status_code=404, detail="演员不存在")

    logger.info(
        f"/actors/{actor_id}/fix: name={actor.name!r} tmdb_id={actor.tmdb_id!r} "
        f"jellyfin_id={actor.jellyfin_id!r}"
    )
    task = create_task(db, "actor_fix_single", f"修复演员: {actor.name}")

    background_tasks.add_task(run_single_actor_fix, task.id, actor_id)

    return {"task_id": task.id, "status": "started"}


@cancellable_task
def run_single_actor_fix(task_id: int, actor_id: int):
    """修复单个演员（TMDB 主源 + Wikidata 兜底）。HTTP 期间不持 db。"""
    from web.backend.database import SessionLocal
    from common.jellyfin_client import JellyfinClient
    from common.tmdb_client import TMDBClient

    def _progress(pct, msg):
        with SessionLocal() as db:
            update_task_progress(db, task_id, pct, msg)

    try:
        # 短事务读出 actor 元信息
        with SessionLocal() as db:
            actor = db.query(ActorInfo).filter(ActorInfo.id == actor_id).first()
            if not actor:
                complete_task(db, task_id, {"error": "演员不存在"}, success=False)
                return
            actor_name = actor.name
            actor_jellyfin_id = actor.jellyfin_id

        jf_client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
        # 单条端点 → batch=False（默认）
        tmdb_client = TMDBClient(settings.tmdb_api_key, language=settings.tmdb_language)
        wd_client = _make_wikidata_client()

        resolved_url: Optional[str] = None
        image_data: Optional[bytes] = None
        source: Optional[str] = None
        new_tmdb_id: Optional[int] = None

        # ---- TMDB 主源（HTTP，无 db）----
        _progress(20, f"搜索 TMDB: {actor_name}")
        tmdb_result = tmdb_client.search_person(actor_name)
        if tmdb_result:
            new_tmdb_id = tmdb_result.get('id')
            profile_path = tmdb_result.get('profile_path')
            if profile_path:
                resolved_url = f"https://image.tmdb.org/t/p/original{profile_path}"
                _progress(40, "下载 TMDB 图片...")
                image_data = tmdb_client.download_image(profile_path)
                if image_data:
                    source = 'tmdb'

        # ---- Wikidata 兜底（HTTP，无 db）----
        if source is None and wd_client is not None:
            _progress(60, f"搜索 Wikidata: {actor_name}")
            try:
                wd_url, _ = wd_client.search_person_image(actor_name)
            except Exception as e:
                logger.warning(f"Wikidata 查询异常 {actor_name}: {e}")
                wd_url = None
            if wd_url:
                resolved_url = wd_url
                _progress(70, "下载 Wikidata 图片...")
                image_data = wd_client.download_image(wd_url)
                if image_data:
                    source = 'wikidata'

        # ---- 处理结果 ----
        if source is None:
            with SessionLocal() as db:
                a = db.query(ActorInfo).filter(ActorInfo.id == actor_id).first()
                if a:
                    a.image_source = 'none'
                    if new_tmdb_id is not None:
                        a.tmdb_id = new_tmdb_id
                    db.commit()
                complete_task(
                    db, task_id,
                    {"error": "TMDB 与 Wikidata 都没找到图片", "actor": actor_name},
                    success=False,
                )
            return

        _progress(85, "上传到 Jellyfin...")
        upload_ok = jf_client.upload_person_image(actor_jellyfin_id, image_data)

        with SessionLocal() as db:
            a = db.query(ActorInfo).filter(ActorInfo.id == actor_id).first()
            if a:
                if new_tmdb_id is not None:
                    a.tmdb_id = new_tmdb_id
                a.image_url = resolved_url
                if upload_ok:
                    a.has_image = True
                    a.image_source = source
                db.commit()
                final_tmdb_id = a.tmdb_id
            else:
                final_tmdb_id = new_tmdb_id

            if upload_ok:
                complete_task(db, task_id, {
                    "actor": actor_name,
                    "tmdb_id": final_tmdb_id,
                    "image_url": resolved_url,
                    "source": source,
                })
            else:
                complete_task(db, task_id, {"error": "上传失败", "source": source}, success=False)

    except Exception as e:
        with SessionLocal() as db:
            complete_task(db, task_id, {"error": str(e)}, success=False)


@router.post("/actors/{actor_id}/upload-image")
async def upload_actor_image(
    actor_id: int,
    image: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    手动上传演员图片到 Jellyfin。

    适用场景：TMDB 与 Wikidata 都没找到此演员的图（image_source='none'），
    或者你不喜欢自动找到的那张图，想换一张本地准备好的。
    上传后该演员标记为 image_source='manual'，下次批量修复会被 skip_existing 跳过。
    """
    if not settings.jellyfin_api_key:
        raise HTTPException(status_code=400, detail="未配置 Jellyfin API Key")

    actor = db.query(ActorInfo).filter(ActorInfo.id == actor_id).first()
    if not actor:
        raise HTTPException(status_code=404, detail="演员不存在")

    # 内容类型校验：浏览器一般会带正确的 content-type；缺失或不是图片直接拒
    content_type = (image.content_type or '').lower()
    if not content_type.startswith('image/'):
        raise HTTPException(
            status_code=400,
            detail=f"文件类型不是图片: {image.content_type or '未知'}",
        )

    image_data = await image.read()
    if not image_data:
        raise HTTPException(status_code=400, detail="文件为空")
    # 防御性大小限制：演员头像很少超过几 MB；过大可能是误传的视频/RAW
    MAX_BYTES = 10 * 1024 * 1024
    if len(image_data) > MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"图片过大（{len(image_data)//1024//1024} MB），上限 {MAX_BYTES//1024//1024} MB",
        )

    from common.jellyfin_client import JellyfinClient
    jf_client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)

    if not jf_client.upload_person_image(actor.jellyfin_id, image_data):
        raise HTTPException(status_code=500, detail="上传到 Jellyfin 失败")

    actor.has_image = True
    actor.image_source = 'manual'
    # 手动上传的图片没有公开 URL，清掉之前可能记录的 TMDB/Wikidata URL
    actor.image_url = None
    db.commit()

    return {
        "status": "success",
        "actor_id": actor_id,
        "name": actor.name,
        "image_source": "manual",
        "size_bytes": len(image_data),
    }


# ==================== 海报管理 ====================

class PosterFixRequest(BaseModel):
    scan_only: bool = False
    limit: Optional[int] = None
    skip_existing: bool = True
    media_type: Optional[str] = None  # 'movie' | 'series'，None 表示全部
    # 作用范围（与 toolbar 的 scope 对齐）
    library_id: Optional[str] = None
    library_ids: Optional[List[str]] = None
    item_paths: Optional[List[str]] = None  # 视频文件路径，按 file_path 匹配 MediaItem


@router.get("/posters")
def list_media_for_posters(
    has_poster: Optional[bool] = None,
    media_type: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """获取媒体条目列表（用于海报管理）"""
    query = db.query(MediaItem).filter(MediaItem.media_type.in_(['movie', 'series']))

    if has_poster is not None:
        query = query.filter(MediaItem.has_poster == has_poster)
    if media_type:
        query = query.filter(MediaItem.media_type == media_type)
    if search:
        query = query.filter(MediaItem.title.contains(search))

    total = query.count()
    items = query.order_by(MediaItem.title).offset(offset).limit(limit).all()

    return {
        "total": total,
        "items": [
            {
                "id": m.id,
                "jellyfin_id": m.jellyfin_id,
                "title": m.title,
                "media_type": m.media_type,
                "production_year": m.production_year,
                "tmdb_id": m.tmdb_id,
                "has_poster": m.has_poster,
                "has_backdrop": m.has_backdrop,
                "poster_url": m.poster_url,
            }
            for m in items
        ],
    }


@router.post("/posters/scan")
def scan_posters(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """扫描 Jellyfin 中的电影和剧集，标记缺海报项"""
    if not settings.jellyfin_api_key:
        raise HTTPException(status_code=400, detail="未配置 Jellyfin API Key")

    logger.info("/posters/scan: 启动海报扫描任务")
    task = create_task(db, "poster_scan", "扫描 Jellyfin 电影/剧集...", params={})
    background_tasks.add_task(run_poster_scan, task.id)
    return {"task_id": task.id, "status": "started", "message": "扫描任务已启动"}


@register_resumable("poster_scan", [])
@cancellable_task
def run_poster_scan(task_id: int):
    """扫描媒体条目并入库（后台任务）。Jellyfin HTTP 期间不持 db。"""
    from web.backend.database import SessionLocal
    from common.jellyfin_client import JellyfinClient

    def _progress(pct, msg):
        with SessionLocal() as db:
            update_task_progress(db, task_id, pct, msg)

    try:
        _progress(5, "连接 Jellyfin...")
        client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)

        # ---- HTTP：拉所有电影和剧集（不持 db）----
        _progress(15, "获取电影列表...")
        movies = client.get_all_movies()
        _progress(40, f"电影 {len(movies)} 部，获取剧集列表...")
        series = client.get_all_series()

        total = len(movies) + len(series)
        _progress(60, f"共 {total} 个条目，写入数据库...")

        new_count = 0
        updated_count = 0
        without_poster = 0

        def upsert(db, item: dict, mtype: str):
            nonlocal new_count, updated_count, without_poster
            jellyfin_id = item.get('Id')
            name = item.get('Name')
            if not jellyfin_id or not name:
                return

            tmdb_str = client.get_tmdb_id(item)
            tmdb_int = int(tmdb_str) if tmdb_str and tmdb_str.isdigit() else None
            year = item.get('ProductionYear')
            poster = client.has_image(item, 'Primary')
            backdrop = client.has_backdrop(item)
            file_path = item.get('Path')

            if not poster:
                without_poster += 1

            existing = db.query(MediaItem).filter(MediaItem.jellyfin_id == jellyfin_id).first()
            if existing:
                existing.title = name
                existing.media_type = mtype
                existing.production_year = year
                existing.tmdb_id = tmdb_int
                existing.has_poster = poster
                existing.has_backdrop = backdrop
                existing.file_path = file_path
                updated_count += 1
            else:
                db.add(MediaItem(
                    jellyfin_id=jellyfin_id,
                    title=name,
                    media_type=mtype,
                    production_year=year,
                    tmdb_id=tmdb_int,
                    has_poster=poster,
                    has_backdrop=backdrop,
                    file_path=file_path,
                ))
                new_count += 1

        # ---- DB 批量 upsert：每 100 条一个短事务 ----
        BATCH = 100

        def _flush_batch(items, mtype):
            for chunk_start in range(0, len(items), BATCH):
                chunk = items[chunk_start:chunk_start + BATCH]
                with SessionLocal() as db:
                    for item in chunk:
                        upsert(db, item, mtype)
                    db.commit()

        _flush_batch(movies, 'movie')
        _flush_batch(series, 'series')

        logger.info(
            f"run_poster_scan 完成: task={task_id} total={total} "
            f"movies={len(movies)} series={len(series)} new={new_count} "
            f"updated={updated_count} without_poster={without_poster}"
        )
        with SessionLocal() as db:
            complete_task(db, task_id, {
                "total": total,
                "movies": len(movies),
                "series": len(series),
                "new": new_count,
                "updated": updated_count,
                "without_poster": without_poster,
            })

    except Exception as e:
        logger.exception(f"海报扫描失败: task={task_id}")
        with SessionLocal() as db:
            complete_task(db, task_id, {"error": str(e)}, success=False)


def _fix_one_poster_pure(jf_client, tmdb_client, item_data: dict, scan_only: bool) -> dict:
    """
    对单个条目走完 TMDB 搜索 → 下载 → Jellyfin 上传，**全程不持 DB 连接**。
    返回的 dict 既是日志条目（status/error/poster_url），也带回需要写库的字段：
      - new_tmdb_id：更新 MediaItem.tmdb_id
      - new_poster_url：更新 MediaItem.poster_url
      - mark_has_poster：True 时把 MediaItem.has_poster 置 True

    输入 item_data 为快照 dict：{id, title, media_type, tmdb_id, jellyfin_id, production_year}
    """
    out = {
        "id": item_data['id'],
        "title": item_data['title'],
        "media_type": item_data['media_type'],
    }

    tmdb_id = item_data.get('tmdb_id')
    poster_path: Optional[str] = None
    new_tmdb_id: Optional[int] = None

    if tmdb_id:
        if item_data['media_type'] == 'movie':
            poster_path = tmdb_client.get_movie_poster_path(tmdb_id)
        else:
            poster_path = tmdb_client.get_tv_poster_path(tmdb_id)

    if not poster_path:
        # 退化为搜索
        if item_data['media_type'] == 'movie':
            search = tmdb_client.search_movie(item_data['title'], year=item_data.get('production_year'))
        else:
            search = tmdb_client.search_tv(item_data['title'], year=item_data.get('production_year'))
        if not search:
            out.update({"status": "not_found", "error": "TMDB 未找到此条目"})
            return out
        new_tmdb_id = search.get('id')
        poster_path = search.get('poster_path')

    if not poster_path:
        out["new_tmdb_id"] = new_tmdb_id
        out.update({"status": "not_found", "error": "TMDB 条目无海报"})
        return out

    image_url = tmdb_client.get_image_url(poster_path)
    out["poster_url"] = image_url
    out["new_poster_url"] = image_url
    if new_tmdb_id is not None:
        out["new_tmdb_id"] = new_tmdb_id

    if scan_only:
        out["status"] = "found"
        return out

    image_data = tmdb_client.download_poster(poster_path)
    if not image_data:
        out.update({"status": "failed", "error": "海报下载失败"})
        return out

    if jf_client.upload_item_image(item_data['jellyfin_id'], image_data, image_type='Primary'):
        out["mark_has_poster"] = True
        out["status"] = "success"
        return out
    out.update({"status": "failed", "error": "上传到 Jellyfin 失败"})
    return out


def _apply_poster_result(db, item_id: int, result: dict) -> None:
    """把 _fix_one_poster_pure 返回里需要持久化的字段写回 MediaItem。"""
    item = db.query(MediaItem).filter(MediaItem.id == item_id).first()
    if not item:
        return
    if result.get('new_tmdb_id') is not None:
        item.tmdb_id = result['new_tmdb_id']
    if result.get('new_poster_url'):
        item.poster_url = result['new_poster_url']
    if result.get('mark_has_poster'):
        item.has_poster = True
    db.commit()


@router.post("/posters/fix")
def fix_posters(
    request: PosterFixRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """批量修复缺海报的电影/剧集"""
    if not settings.jellyfin_api_key:
        raise HTTPException(status_code=400, detail="未配置 Jellyfin API Key")
    if not settings.tmdb_api_key:
        raise HTTPException(status_code=400, detail="未配置 TMDB API Key")

    mode = "扫描" if request.scan_only else "修复"
    scope_label = _make_scope_label(
        request.library_id, request.library_ids, request.item_paths,
    )
    logger.info(
        f"/posters/fix: scope={scope_label!r} scan_only={request.scan_only} "
        f"limit={request.limit} skip_existing={request.skip_existing} "
        f"media_type={request.media_type!r} library_id={request.library_id!r} "
        f"library_ids={request.library_ids!r}"
    )
    task = create_task(
        db,
        "poster_fix",
        f"{mode}海报: {scope_label}",
        params={
            "scan_only": request.scan_only,
            "limit": request.limit,
            "skip_existing": request.skip_existing,
            "media_type": request.media_type,
            "library_id": request.library_id,
            "library_ids": request.library_ids,
            "item_paths": request.item_paths,
            "skip_refresh": False,
        },
    )

    background_tasks.add_task(
        run_poster_fix,
        task.id,
        request.scan_only,
        request.limit,
        request.skip_existing,
        request.media_type,
        request.library_id,
        request.library_ids,
        request.item_paths,
        False,  # skip_refresh，端点默认会刷
    )
    return {"task_id": task.id, "status": "started", "message": f"{mode}任务已启动"}


def _refresh_jellyfin_libraries():
    """海报/演员图修复后，触发 Jellyfin 全局刷新。"""
    if not settings.jellyfin_api_key:
        return
    try:
        from common.jellyfin_client import JellyfinClient
        client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
        client.refresh_all_libraries()
    except Exception as e:
        logger.warning(f"触发 Jellyfin 刷新失败: {e}")


def _normalize_path_for_match(p: str) -> str:
    """统一路径分隔符 + 小写，便于跨平台 / 大小写不敏感比对。"""
    if not p:
        return ''
    return p.replace('\\', '/').rstrip('/').lower()


def _filter_items_by_scope(
    query,
    library_id: Optional[str],
    library_ids: Optional[List[str]],
    item_paths: Optional[List[str]],
):
    """
    把 MediaItem 查询按 scope 过滤：
      - item_paths：按 file_path 匹配（路径前缀或完全等于；item_paths 通常是视频文件，
        而 MediaItem.file_path 对电影/剧集是文件夹或文件路径，因此用"祖先或相等"匹配）
      - library_id / library_ids：从 Jellyfin 拉对应库的 jellyfin_id 列表，按 in_ 过滤
      - 都没传：返回原 query
    """
    from common.jellyfin_client import JellyfinClient

    if item_paths:
        # 前端送的是 backend view（如 Z:/...），DB 里 MediaItem.file_path 是 Jellyfin view，
        # 双向都进 norms 集合保证两边都能命中（同机部署时 reverse 会原样返回，无影响）
        from web.backend.path_translator import reverse_translate_path_with_settings
        norms = set()
        for p in item_paths:
            if not p:
                continue
            norms.add(_normalize_path_for_match(p))
            jf = reverse_translate_path_with_settings(p)
            if jf and jf != p:
                norms.add(_normalize_path_for_match(jf))
        norms = {n for n in norms if n}
        if not norms:
            return query.filter(MediaItem.id == -1)  # 空集
        all_items = query.all()
        matched_ids = []
        for it in all_items:
            fp = _normalize_path_for_match(it.file_path or '')
            if not fp:
                continue
            for n in norms:
                # n 是文件路径或目录路径 —— 视频文件位于 MediaItem.file_path（目录）之下时也算命中
                if fp == n or n == fp or n.startswith(fp + '/') or fp.startswith(n + '/'):
                    matched_ids.append(it.id)
                    break
        if not matched_ids:
            return query.filter(MediaItem.id == -1)
        return query.filter(MediaItem.id.in_(matched_ids))

    target_library_ids: List[str] = []
    if library_ids:
        target_library_ids = [lid for lid in library_ids if lid]
    elif library_id:
        target_library_ids = [library_id]

    if target_library_ids:
        try:
            client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
            jellyfin_ids: set = set()
            for lid in target_library_ids:
                items = client.get_library_items(lid, item_types='Movie,Series', fields='Id')
                for it in items:
                    if it.get('Id'):
                        jellyfin_ids.add(it['Id'])
            if not jellyfin_ids:
                return query.filter(MediaItem.id == -1)
            return query.filter(MediaItem.jellyfin_id.in_(jellyfin_ids))
        except Exception as e:
            logger.warning(f"获取库内条目失败，回退到不过滤: {e}")

    return query


@register_resumable(
    "poster_fix",
    [
        "scan_only", "limit", "skip_existing", "media_type",
        "library_id", "library_ids", "item_paths", "skip_refresh",
    ],
)
@cancellable_task
def run_poster_fix(
    task_id: int,
    scan_only: bool,
    limit: Optional[int],
    skip_existing: bool,
    media_type: Optional[str],
    library_id: Optional[str] = None,
    library_ids: Optional[List[str]] = None,
    item_paths: Optional[List[str]] = None,
    skip_refresh: bool = False,
):
    """批量海报修复（后台任务）。HTTP 阶段不持 db。"""
    from web.backend.database import SessionLocal
    from common.jellyfin_client import JellyfinClient
    from common.tmdb_client import TMDBClient

    def _progress(pct, msg, patch=None):
        with SessionLocal() as db:
            update_task_progress(db, task_id, pct, msg, result_patch=patch)

    try:
        _progress(5, "初始化...")

        jf_client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
        tmdb_client = TMDBClient(settings.tmdb_api_key, language=settings.tmdb_language, batch=True)

        # ---- 短事务：把待处理条目快照成 dict 列表 ----
        with SessionLocal() as db:
            query = db.query(MediaItem).filter(MediaItem.media_type.in_(['movie', 'series']))
            if skip_existing:
                query = query.filter(MediaItem.has_poster == False)
            if media_type:
                query = query.filter(MediaItem.media_type == media_type)
            query = _filter_items_by_scope(query, library_id, library_ids, item_paths)
            items_raw = query.all()
            if limit:
                items_raw = items_raw[:limit]
            items_data = [
                {
                    'id': it.id,
                    'title': it.title,
                    'media_type': it.media_type,
                    'tmdb_id': it.tmdb_id,
                    'jellyfin_id': it.jellyfin_id,
                    'production_year': it.production_year,
                }
                for it in items_raw
            ]

        total = len(items_data)
        _progress(10, f"共 {total} 个条目需要处理")

        if total == 0:
            with SessionLocal() as db:
                complete_task(db, task_id, {
                    "total": 0, "success": 0, "failed": 0, "not_found": 0,
                    "scan_only": scan_only, "details": [],
                })
            return

        success = failed = not_found = 0
        details: List[dict] = []
        EMIT_EVERY = 5  # 每 5 个条目 emit partial result

        for i, item_data in enumerate(items_data):
            progress = 10 + int(85 * (i + 1) / total)
            should_emit = (i > 0 and i % EMIT_EVERY == 0)
            _progress(
                progress, f"[{i+1}/{total}] {item_data['title']}",
                patch=({
                    "total": total,
                    "success": success,
                    "failed": failed,
                    "not_found": not_found,
                    "scan_only": scan_only,
                    "details": details,
                } if should_emit else None),
            )

            try:
                result = _fix_one_poster_pure(jf_client, tmdb_client, item_data, scan_only)
            except Exception as e:
                logger.exception(f"处理失败: {item_data['title']}")
                result = {"id": item_data['id'], "title": item_data['title'], "status": "failed", "error": str(e)}

            # 短事务回写
            try:
                with SessionLocal() as db:
                    _apply_poster_result(db, item_data['id'], result)
            except Exception as e:
                logger.warning(f"回写 poster 字段失败 {item_data['id']}: {e}")

            details.append(result)
            status = result.get("status")
            if status in ("success", "found"):
                success += 1
            elif status == "not_found":
                not_found += 1
            else:
                failed += 1

        # 上传成功 → 触发刷新（仅 scan_only=False 模式；run_all 会传 skip_refresh=True）
        refreshed = False
        if not scan_only and success > 0 and not skip_refresh:
            _progress(99, "通知 Jellyfin 刷新...")
            _refresh_jellyfin_libraries()
            refreshed = True

        with SessionLocal() as db:
            complete_task(db, task_id, {
                "total": total,
                "success": success,
                "failed": failed,
                "not_found": not_found,
                "scan_only": scan_only,
                "jellyfin_refreshed": refreshed,
                "details": details,
            })

    except Exception as e:
        logger.exception("海报修复任务失败")
        with SessionLocal() as db:
            complete_task(db, task_id, {"error": str(e)}, success=False)


@router.post("/posters/{item_id}/fix")
def fix_single_poster(
    item_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """修复单个条目的海报"""
    item = db.query(MediaItem).filter(MediaItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")

    logger.info(
        f"/posters/{item_id}/fix: title={item.title!r} media_type={item.media_type!r} "
        f"tmdb_id={item.tmdb_id!r}"
    )
    task = create_task(db, "poster_fix_single", f"修复海报: {item.title}")
    background_tasks.add_task(run_single_poster_fix, task.id, item_id)
    return {"task_id": task.id, "status": "started"}


@cancellable_task
def run_single_poster_fix(task_id: int, item_id: int):
    """单个条目海报修复。HTTP 期间不持 db。"""
    from web.backend.database import SessionLocal
    from common.jellyfin_client import JellyfinClient
    from common.tmdb_client import TMDBClient

    try:
        # 读快照
        with SessionLocal() as db:
            item = db.query(MediaItem).filter(MediaItem.id == item_id).first()
            if not item:
                complete_task(db, task_id, {"error": "条目不存在"}, success=False)
                return
            item_data = {
                'id': item.id,
                'title': item.title,
                'media_type': item.media_type,
                'tmdb_id': item.tmdb_id,
                'jellyfin_id': item.jellyfin_id,
                'production_year': item.production_year,
            }
            update_task_progress(db, task_id, 30, f"搜索 TMDB: {item_data['title']}")

        jf_client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
        # 单条端点 → batch=False（默认）
        tmdb_client = TMDBClient(settings.tmdb_api_key, language=settings.tmdb_language)

        result = _fix_one_poster_pure(jf_client, tmdb_client, item_data, scan_only=False)

        # 回写 + 完成
        with SessionLocal() as db:
            _apply_poster_result(db, item_id, result)
            success = result.get("status") == "success"
            complete_task(db, task_id, result, success=success)

    except Exception as e:
        logger.exception("单个海报修复失败")
        with SessionLocal() as db:
            complete_task(db, task_id, {"error": str(e)}, success=False)


# ==================== Episode 缩略图修复 ====================
# 业务：剧集库里 Episode 的缩略图（Jellyfin 称为 Primary）。从 TMDB 取
# /tv/{tv_id}/season/{s}/episode/{e}.still_path，下载后上传到 Jellyfin。
# 与 Series/Movie 海报不同，Episode 缩略图是 16:9 截图风格，需要单独的取图链路。

class EpisodeStillFixRequest(BaseModel):
    scan_only: bool = False
    limit: Optional[int] = None
    skip_existing: bool = True
    library_id: Optional[str] = None
    library_ids: Optional[List[str]] = None
    # 选中部分 Episode 直接修（路径或 jellyfin_id）
    episode_ids: Optional[List[str]] = None


def _resolve_episode_targets(
    jf_client,
    library_id: Optional[str],
    library_ids: Optional[List[str]],
    episode_ids: Optional[List[str]],
) -> List[Dict]:
    """
    解析待处理的 Episode 列表。返回原始 Jellyfin Episode dict 列表（含
    SeriesId / IndexNumber / ParentIndexNumber / ImageTags / ProviderIds）。
    """
    fields = "ProviderIds,ImageTags,IndexNumber,ParentIndexNumber,SeriesId,SeriesName,SeasonName,Path"
    if episode_ids:
        # 指定 episode_id：逐条 get_item 拉
        out = []
        for eid in episode_ids:
            item = jf_client.get_item(eid, fields=fields)
            if item and item.get('Type') == 'Episode':
                out.append(item)
        return out

    target_libs: List[str] = []
    if library_ids:
        target_libs = [lid for lid in library_ids if lid]
    elif library_id:
        target_libs = [library_id]

    if not target_libs:
        # 全库：从所有 tvshows 类型库取 Episode
        try:
            libs = jf_client.get_libraries_normalized()
        except Exception as e:
            logger.warning(f"获取库列表失败: {e}")
            return []
        target_libs = [lib['id'] for lib in libs if lib.get('collection_type') == 'tvshows']

    out: List[Dict] = []
    for lib_id in target_libs:
        page = jf_client.get_library_items_page(
            lib_id, item_types='Episode', limit=500, fields=fields,
        )
        out.extend(page.get('items') or [])
        # 简易分页：如果有更多继续拉
        total = page.get('total') or 0
        start = 500
        while start < total:
            page = jf_client.get_library_items_page(
                lib_id, item_types='Episode', start_index=start, limit=500, fields=fields,
            )
            out.extend(page.get('items') or [])
            start += 500
    return out


@router.post("/episodes/fix-stills")
def fix_episode_stills(
    request: EpisodeStillFixRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """批量修复 Episode 缩略图（从 TMDB 取 still_path，上传到 Jellyfin Primary）。"""
    if not settings.jellyfin_api_key:
        raise HTTPException(status_code=400, detail="未配置 Jellyfin API Key")
    if not settings.tmdb_api_key:
        raise HTTPException(status_code=400, detail="未配置 TMDB API Key")

    mode = "扫描" if request.scan_only else "修复"
    if request.episode_ids:
        scope_label = f"{len(request.episode_ids)} 集"
    elif request.library_ids:
        scope_label = f"{len(request.library_ids)} 个库"
    elif request.library_id:
        scope_label = "1 个库"
    else:
        scope_label = "全部 TV 库"

    logger.info(
        f"/episodes/fix-stills: scope={scope_label!r} scan_only={request.scan_only} "
        f"limit={request.limit} skip_existing={request.skip_existing} "
        f"episode_ids={len(request.episode_ids or [])} "
        f"library_id={request.library_id!r} library_ids={request.library_ids!r}"
    )

    task = create_task(
        db,
        "episode_still_fix",
        f"{mode} Episode 缩略图: {scope_label}",
        params={
            "scan_only": request.scan_only,
            "limit": request.limit,
            "skip_existing": request.skip_existing,
            "library_id": request.library_id,
            "library_ids": request.library_ids,
            "episode_ids": request.episode_ids,
        },
    )

    background_tasks.add_task(
        run_episode_still_fix,
        task.id,
        request.scan_only,
        request.limit,
        request.skip_existing,
        request.library_id,
        request.library_ids,
        request.episode_ids,
    )
    return {"task_id": task.id, "status": "started", "message": f"{mode}任务已启动"}


@register_resumable(
    "episode_still_fix",
    ["scan_only", "limit", "skip_existing", "library_id", "library_ids", "episode_ids"],
)
@cancellable_task
def run_episode_still_fix(
    task_id: int,
    scan_only: bool,
    limit: Optional[int],
    skip_existing: bool,
    library_id: Optional[str] = None,
    library_ids: Optional[List[str]] = None,
    episode_ids: Optional[List[str]] = None,
):
    """
    Episode 缩略图修复后台任务。

    流程：
      1. 解析 Episode 列表（按 library_id/ids 或 episode_ids）
      2. 对每集：用 SeriesId 反查父剧获取 TMDB tv_id（带本任务内缓存）
      3. 拿 (tv_id, season_number, episode_number) 调 TMDB /tv/.../episode/...
      4. 下载 still_path，上传到 Jellyfin /Items/{episode_id}/Images/Primary

    DB 仅用于 progress / complete，HTTP 期间不持有连接。
    """
    from web.backend.database import SessionLocal
    from common.jellyfin_client import JellyfinClient
    from common.tmdb_client import TMDBClient

    def _progress(pct, msg, patch=None):
        with SessionLocal() as db:
            update_task_progress(db, task_id, pct, msg, result_patch=patch)

    try:
        _progress(5, "初始化...")

        jf_client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
        tmdb_client = TMDBClient(
            settings.tmdb_api_key,
            language=settings.tmdb_language,
            batch=True,
        )

        _progress(10, "解析 Episode 列表...")
        episodes = _resolve_episode_targets(jf_client, library_id, library_ids, episode_ids)
        if limit:
            episodes = episodes[:limit]

        total = len(episodes)
        if total == 0:
            with SessionLocal() as db:
                complete_task(db, task_id, {"total": 0, "message": "没有找到需要处理的 Episode"})
            return

        _progress(15, f"共 {total} 集需要处理")

        # SeriesId → TMDB tv_id 缓存（同一剧的多集免重复查）
        series_tmdb_cache: dict = {}

        def _resolve_series_tmdb(series_id: str) -> Optional[int]:
            if not series_id:
                return None
            if series_id in series_tmdb_cache:
                return series_tmdb_cache[series_id]
            try:
                series = jf_client.get_item(series_id, fields="ProviderIds")
            except Exception:
                series = None
            tv_id = None
            if series:
                pid = (series.get('ProviderIds') or {}).get('Tmdb')
                try:
                    tv_id = int(pid) if pid else None
                except (TypeError, ValueError):
                    tv_id = None
            series_tmdb_cache[series_id] = tv_id
            return tv_id

        success_count = 0
        skipped_count = 0
        failed_count = 0
        details: List[dict] = []
        EMIT_EVERY = 5

        for i, ep in enumerate(episodes):
            entry = {
                'id': ep.get('Id'),
                'name': ep.get('Name'),
                'series_name': ep.get('SeriesName'),
                'season_number': ep.get('ParentIndexNumber'),
                'episode_number': ep.get('IndexNumber'),
                'status': None,
                'error': None,
            }
            try:
                progress = 15 + (i / total) * 80
                if i > 0 and i % EMIT_EVERY == 0:
                    _progress(
                        progress,
                        f"处理: {ep.get('SeriesName')} S{ep.get('ParentIndexNumber'):02d}E{ep.get('IndexNumber'):02d}",
                        patch={
                            "total": total,
                            "success": success_count,
                            "skipped": skipped_count,
                            "failed": failed_count,
                            "scan_only": scan_only,
                            "details": details,
                        },
                    )

                # 已有缩略图则跳过
                has_primary = bool((ep.get('ImageTags') or {}).get('Primary'))
                if skip_existing and has_primary:
                    skipped_count += 1
                    entry['status'] = 'skipped'
                    entry['error'] = '已有缩略图'
                    details.append(entry)
                    continue

                # 父剧 TMDB id
                series_id = ep.get('SeriesId')
                tv_id = _resolve_series_tmdb(series_id)
                if not tv_id:
                    failed_count += 1
                    entry['status'] = 'no_series_tmdb'
                    entry['error'] = '父剧未绑定 TMDB ID'
                    details.append(entry)
                    continue

                season_n = ep.get('ParentIndexNumber')
                episode_n = ep.get('IndexNumber')
                still_path = tmdb_client.get_episode_still_path(tv_id, season_n, episode_n)
                if not still_path:
                    failed_count += 1
                    entry['status'] = 'no_still'
                    entry['error'] = 'TMDB 该集无 still 图'
                    details.append(entry)
                    continue

                if scan_only:
                    success_count += 1
                    entry['status'] = 'success'
                    entry['still_url'] = tmdb_client.get_image_url(still_path, "original")
                    details.append(entry)
                    continue

                image_data = tmdb_client.download_episode_still(still_path)
                if not image_data:
                    failed_count += 1
                    entry['status'] = 'failed'
                    entry['error'] = '下载失败'
                    details.append(entry)
                    continue

                if jf_client.upload_item_image(ep.get('Id'), image_data, image_type='Primary'):
                    success_count += 1
                    entry['status'] = 'success'
                else:
                    failed_count += 1
                    entry['status'] = 'failed'
                    entry['error'] = '上传到 Jellyfin 失败'

                details.append(entry)

            except Exception as e:
                failed_count += 1
                entry['status'] = 'failed'
                entry['error'] = str(e)
                details.append(entry)

        logger.info(
            f"run_episode_still_fix 完成: task={task_id} total={total} "
            f"success={success_count} skipped={skipped_count} failed={failed_count} "
            f"scan_only={scan_only}"
        )

        with SessionLocal() as db:
            complete_task(db, task_id, {
                "total": total,
                "success": success_count,
                "skipped": skipped_count,
                "failed": failed_count,
                "scan_only": scan_only,
                "details": details,
            })

    except Exception as e:
        logger.exception(f"Episode 缩略图修复失败: task={task_id}")
        with SessionLocal() as db:
            complete_task(db, task_id, {"error": str(e)}, success=False)


@router.post("/episodes/{episode_id}/fix-still")
def fix_single_episode_still(
    episode_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """单集缩略图修复（前端按钮触发）。"""
    if not settings.jellyfin_api_key:
        raise HTTPException(status_code=400, detail="未配置 Jellyfin API Key")
    if not settings.tmdb_api_key:
        raise HTTPException(status_code=400, detail="未配置 TMDB API Key")

    task = create_task(
        db,
        "episode_still_fix_single",
        f"修复缩略图: episode#{episode_id}",
        params={"episode_ids": [episode_id]},
    )
    # 复用 bulk 任务函数，传单条 episode_ids；skip_existing=False 让用户主动点
    # 的时候即便已有图也强制刷新
    background_tasks.add_task(
        run_episode_still_fix,
        task.id, False, None, False, None, None, [episode_id],
    )
    return {"task_id": task.id, "status": "started"}
