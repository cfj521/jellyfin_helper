"""
媒体库维护：批量清理 / 修复任务
  - cleanup-samples：扫描范围内所有视频文件，用 sample_evidence 判定，删除真 sample 文件（仅文件，不删目录）
  - normalize-paths：把嵌套在 release 子目录里的主文件挪回 release 根

两者都通过后台任务系统跑（带进度），完成后触发受影响库的重扫。
"""
import logging
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

ROOT_DIR = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from web.backend.api._sample_evidence import gather_sample_evidence
from web.backend.api._item_health import (
    _looks_like_release_dir, _looks_like_file, _basename,
    compute_health, extract_suggested_title_year,
)
from web.backend.api.tasks import create_task, update_task_progress, complete_task
from web.backend.config import settings
from web.backend.database import get_db
from web.backend.path_translator import translate_path_with_settings

logger = logging.getLogger(__name__)
router = APIRouter()


# 视频扩展名（跟 audio_manager / sample_evidence 保持一致）
_VIDEO_EXTS = {'.mkv', '.mp4', '.m4v', '.mov', '.avi', '.wmv', '.flv', '.ts', '.rmvb'}


# ---------- 通用 scope 解析 ----------

class MaintenanceRequest(BaseModel):
    """
    任务作用范围（与 audio API / 其他批量任务保持一致）：
      - item_paths：直接传一组视频文件 / 目录路径（从 toolbar 选中条目）
      - library_id：单库
      - library_ids：多库
      - 都不传则视为"全库"，自动展开为 jellyfin 所有库的 locations
    """
    item_paths: Optional[List[str]] = None
    library_id: Optional[str] = None
    library_ids: Optional[List[str]] = None
    dry_run: bool = True


class TaskStartResponse(BaseModel):
    task_id: int
    status: str
    message: str


def _client():
    """复用 jellyfin.py 里的 client 工厂，避免循环 import 直接构造。"""
    from common.jellyfin_client import JellyfinClient
    return JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)


def _resolve_scope(req: MaintenanceRequest) -> Dict:
    """
    把 scope 解析为：
      {
        'mode': 'items' | 'paths',
        'paths': List[str],           # Jellyfin 视角的原始路径（未做映射转换）
        'library_id_by_path': Dict,   # 路径 → 包含该路径的库 id（用于完成后刷新）
        'label': str,
      }
    """
    client = _client()
    libraries = client.get_libraries_normalized()

    # 路径 → 库 id 映射（用于完成后选择性刷新；命中策略：库 location 是 path 的祖先或相等）
    def find_library(p: str) -> Optional[str]:
        p_norm = p.replace('\\', '/').rstrip('/').lower()
        for lib in libraries:
            for loc in lib.get('locations') or []:
                loc_norm = loc.replace('\\', '/').rstrip('/').lower()
                if p_norm == loc_norm or p_norm.startswith(loc_norm + '/'):
                    return lib['id']
        return None

    # 1. 直接给文件路径（item_paths 模式）
    if req.item_paths:
        paths = [p for p in req.item_paths if p]
        lib_map = {p: find_library(p) for p in paths}
        return {
            'mode': 'items',
            'paths': paths,
            'library_id_by_path': lib_map,
            'label': f'选中 {len(paths)} 个条目',
        }

    # 2. 多库
    if req.library_ids:
        all_paths: List[str] = []
        lib_map: Dict[str, Optional[str]] = {}
        for lid in req.library_ids:
            lib = next((l for l in libraries if l['id'] == lid), None)
            if lib:
                for loc in lib.get('locations') or []:
                    all_paths.append(loc)
                    lib_map[loc] = lid
        if not all_paths:
            raise HTTPException(status_code=400, detail="所选库均无可用路径")
        return {
            'mode': 'paths',
            'paths': all_paths,
            'library_id_by_path': lib_map,
            'label': f'{len(req.library_ids)} 个库（{len(all_paths)} 个路径）',
        }

    # 3. 单库
    if req.library_id:
        lib = next((l for l in libraries if l['id'] == req.library_id), None)
        if not lib:
            raise HTTPException(status_code=404, detail=f"库不存在: {req.library_id}")
        if not lib.get('locations'):
            raise HTTPException(status_code=400, detail=f"库 {lib['name']} 没有配置路径")
        return {
            'mode': 'paths',
            'paths': lib['locations'],
            'library_id_by_path': {p: lib['id'] for p in lib['locations']},
            'label': f"库 {lib['name']}",
        }

    # 4. 全库（默认）—— 展开所有库的 locations
    all_paths = []
    lib_map = {}
    for lib in libraries:
        for loc in lib.get('locations') or []:
            all_paths.append(loc)
            lib_map[loc] = lib['id']
    if not all_paths:
        raise HTTPException(status_code=400, detail="未找到任何已配置的媒体库")
    return {
        'mode': 'paths',
        'paths': all_paths,
        'library_id_by_path': lib_map,
        'label': f'全部 {len(libraries)} 个库（{len(all_paths)} 个路径）',
    }


def _walk_video_files(local_root: Path):
    """递归 yield 一个本地路径下的所有视频文件。"""
    if not local_root.exists():
        return
    if local_root.is_file():
        if local_root.suffix.lower() in _VIDEO_EXTS:
            yield local_root
        return
    try:
        for f in local_root.rglob('*'):
            if f.is_file() and f.suffix.lower() in _VIDEO_EXTS:
                yield f
    except (PermissionError, OSError) as e:
        logger.warning(f"扫描目录受限: {local_root} - {e}")


def _enumerate_items_in_scope(target: Dict) -> List[Dict]:
    """
    根据已解析的 scope 拉对应的 Jellyfin items（带健康检查需要的字段）。

    - mode='items'：只取 path 命中的 item（按 Path 完全匹配）
    - mode='paths'：取所有相关库的所有 items
    """
    client = _client()
    library_ids = sorted({lid for lid in target['library_id_by_path'].values() if lid})
    if not library_ids:
        return []

    fields = (
        "Path,ProductionYear,ImageTags,ProviderIds,People,"
        "CommunityRating,OfficialRating,RunTimeTicks,ChildCount,Overview,"
        "OriginalTitle"
    )

    all_items: List[Dict] = []
    for lid in library_ids:
        start = 0
        while True:
            try:
                page = client.get_library_items_page(
                    lid, start_index=start, limit=500, fields=fields,
                )
            except Exception as e:
                logger.warning(f"读取库 items 失败 {lid}: {e}")
                break
            items = page.get('items') or []
            if not items:
                break
            for it in items:
                it['_library_id'] = lid  # 内部标记，用于刷新
            all_items.extend(items)
            total = page.get('total') or 0
            start += len(items)
            if start >= total:
                break

    if target['mode'] == 'items':
        # 只保留 path 命中的 item
        wanted = {
            p.replace('\\', '/').rstrip('/').lower()
            for p in target['paths']
        }
        all_items = [
            it for it in all_items
            if it.get('Path')
            and it['Path'].replace('\\', '/').rstrip('/').lower() in wanted
        ]

    return all_items


def _refresh_libraries(
    library_ids: List[str],
    affected_paths: Optional[List[str]] = None,
    update_type: str = 'Modified',
):
    """通知 Jellyfin 重扫。
    - affected_paths 给定 → 优先用 /Library/Media/Updated 精准通知（毫秒触发，针对性扫描）
    - 失败或 paths 为空 → 回落到 /Items/{lib_id}/Refresh 整库扫
    - update_type: 'Created' / 'Modified' / 'Deleted'

    返回已触发的标识列表（库 id 或 '(media_updated)'）。
    """
    seen = []
    client = _client()

    # ① 精准通知（path 已知场景）
    if affected_paths:
        from web.backend.path_translator import reverse_translate_path_with_settings
        paths_for_jf = []
        for p in affected_paths:
            if not p:
                continue
            try:
                paths_for_jf.append(reverse_translate_path_with_settings(p) or p)
            except Exception:
                paths_for_jf.append(p)
        if paths_for_jf:
            try:
                if client.notify_media_updated(paths_for_jf, update_type=update_type):
                    return ['(media_updated)']
            except Exception as e:
                logger.warning(f"notify_media_updated 失败，回落整库扫: {e}")

    # ② 整库扫兜底
    if not library_ids:
        return seen
    for lid in library_ids:
        if not lid or lid in seen:
            continue
        try:
            client.refresh_library(lid)
            seen.append(lid)
        except Exception as e:
            logger.warning(f"刷新库失败 {lid}: {e}")
    return seen


# ---------- cleanup-samples ----------

@router.post("/cleanup-samples", response_model=TaskStartResponse)
def cleanup_samples(
    req: MaintenanceRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    扫描作用范围内所有视频文件，用 sample_evidence 判定是否 sample；
    verdict ∈ {sample, sample-likely} 的视频文件会被**删除**（仅文件，不动目录）。
    完成后刷新受影响的库。
    """
    target = _resolve_scope(req)
    mode_label = "预览" if req.dry_run else "执行"
    logger.warning(
        f"/maintenance/cleanup-samples: scope={target['label']!r} "
        f"paths={len(target['paths'])} dry_run={req.dry_run}"
    )
    task = create_task(db, "cleanup_samples", f"{mode_label}清理 sample: {target['label']}")
    background_tasks.add_task(
        run_cleanup_samples,
        task.id,
        target['paths'],
        target['library_id_by_path'],
        req.dry_run,
    )
    return TaskStartResponse(task_id=task.id, status="started",
                             message=f"清理 sample 任务已启动（{mode_label}）")


def _build_library_root_set() -> set:
    """所有库 location（含路径映射后的本地形式）的归一化集合，用于"绝不删库根"防护。"""
    out = set()
    try:
        client = _client()
        for lib in client.get_libraries_normalized():
            for loc in lib.get('locations') or []:
                out.add(loc.replace('\\', '/').rstrip('/').lower())
                translated = translate_path_with_settings(loc)
                out.add(translated.replace('\\', '/').rstrip('/').lower())
    except Exception as e:
        logger.warning(f"读取库列表失败: {e}")
    return out


def _try_cleanup_empty_dir(
    dir_path: Path, library_root_set: set, dry_run: bool,
) -> Optional[Dict]:
    """
    尝试删除一个目录：仅在目录内**没有视频文件**时删（用 sample_evidence 的 non-media 判定）。
    安全：永远不删库根。
    返回 {path, deleted, error} 或 None（不该删/无法删）。
    """
    dir_norm = str(dir_path).replace('\\', '/').rstrip('/').lower()
    if dir_norm in library_root_set:
        return None
    if not dir_path.exists() or not dir_path.is_dir():
        return None

    evidence = gather_sample_evidence({'Path': str(dir_path), 'RunTimeTicks': 0})
    if evidence.get('verdict') != 'non-media':
        return None

    item = {'path': str(dir_path), 'deleted': False, 'error': None}
    if dry_run:
        item['error'] = '(预览模式)'
        return item
    try:
        shutil.rmtree(dir_path)
        item['deleted'] = True
    except Exception as e:
        item['error'] = str(e)
        logger.exception(f"删除目录失败: {dir_path}")
    return item


def run_cleanup_samples(
    task_id: int,
    paths: List[str],
    library_id_by_path: Dict[str, Optional[str]],
    dry_run: bool,
    skip_refresh: bool = False,
):
    """
    后台任务：
      Phase 1: 扫描 -> sample 判定 -> 删除 sample 视频文件
      Phase 2: 对每个被删 sample 的所在目录，向上递归 —— 目录内没视频就删（直到遇到有视频的或库根停）
      Phase 3: 刷新受影响的库
    """
    from web.backend.database import SessionLocal

    db = SessionLocal()
    try:
        update_task_progress(db, task_id, 2, "开始扫描视频文件...")

        # 收集所有视频文件
        videos: List[Dict] = []  # [{local_path, library_id}]
        for jf_path in paths:
            local_root = Path(translate_path_with_settings(jf_path))
            for f in _walk_video_files(local_root):
                videos.append({
                    'local_path': f,
                    'library_id': library_id_by_path.get(jf_path),
                })

        total = len(videos)
        update_task_progress(db, task_id, 5, f"扫描完成：找到 {total} 个视频，正在判定...")

        if total == 0:
            complete_task(db, task_id, {
                'dry_run': dry_run, 'scanned': 0,
                'sample_count': 0, 'deleted_file_count': 0,
                'deleted_dir_count': 0, 'skipped_count': 0,
                'errors': [], 'details': [], 'dir_details': [],
                'libraries_refreshed': [],
            })
            return

        # ===== Phase 1: 视频文件级 sample 删除 =====
        sample_results = []
        skipped = []
        deleted_file_count = 0
        affected_libs: List[str] = []

        EMIT_EVERY = 5  # 每 5 个视频 emit 一次 partial result

        for idx, v in enumerate(videos):
            pct = 5 + int(70 * (idx + 1) / total)  # Phase 1 占 5-75%
            file_path: Path = v['local_path']
            should_emit = (idx > 0 and idx % EMIT_EVERY == 0)
            update_task_progress(
                db, task_id, pct, f"判定 [{idx+1}/{total}] {file_path.name}",
                result_patch=({
                    'dry_run': dry_run,
                    'scanned': idx,
                    'sample_count': len(sample_results),
                    'deleted_file_count': deleted_file_count,
                    'deleted_dir_count': 0,  # phase 2 还没开始
                    'skipped_count': len(skipped),
                    # 注意 partial 期间还有 _library_id 内部字段，结尾会清掉，
                    # 但 partial 状态下让前端看到没影响（前端不渲染未知字段）
                    'details': sample_results[:500],
                    'dir_details': [],
                } if should_emit else None),
            )

            evidence = gather_sample_evidence({'Path': str(file_path), 'RunTimeTicks': 0})
            verdict = evidence.get('verdict')

            if verdict in ('sample', 'sample-likely'):
                item = {
                    'path': str(file_path),
                    'verdict': verdict,
                    'size_display': evidence.get('file_size_display'),
                    'reasons': evidence.get('high_confidence', []) + evidence.get('medium_confidence', []),
                    'deleted': False,
                    'error': None,
                    '_library_id': v['library_id'],
                }
                if dry_run:
                    item['error'] = '(预览模式)'
                else:
                    try:
                        file_path.unlink()
                        item['deleted'] = True
                        deleted_file_count += 1
                        if v['library_id']:
                            affected_libs.append(v['library_id'])
                    except Exception as e:
                        item['error'] = str(e)
                        logger.exception(f"删除失败: {file_path}")
                sample_results.append(item)
            else:
                skipped.append({'path': str(file_path), 'verdict': verdict})

        # ===== Phase 2: 父目录清理（仅当 dir 没视频时删） =====
        update_task_progress(db, task_id, 80, "开始清理空 sample 目录...")
        library_root_set = _build_library_root_set()

        # 收集要查的目录：被删 sample 的所在目录
        # 预览模式下也要给出预测：用 sample_results 全部（不论是否真删）
        candidates: set = set()
        for r in sample_results:
            # 只考虑 verdict 是 sample 的（sample-likely 的也加，跟文件删除范围一致）
            candidates.add(Path(r['path']).parent)

        # 按路径深度从深到浅处理（先删最里层，再尝试删上层）
        ordered = sorted(candidates, key=lambda p: -len(str(p)))

        dir_results: List[Dict] = []
        processed: set = set()
        for d in ordered:
            current = d
            while current and current not in processed:
                processed.add(current)
                # 安全：不上溯到任何不在 paths 范围里的祖先
                # 这里用一个保险：不能上溯过 paths 里任意路径的本地映射
                result = _try_cleanup_empty_dir(current, library_root_set, dry_run)
                if not result:
                    break  # 这层有视频或是库根 —— 停
                # 找到对应的 library_id（用 candidates 来源映射）
                # 简化：用 sample_results 中匹配的第一个
                lid = next(
                    (r['_library_id'] for r in sample_results
                     if Path(r['path']).parent == d or Path(r['path']).parent.is_relative_to(current)),
                    None,
                ) if hasattr(Path, 'is_relative_to') else None
                result['_library_id'] = lid
                dir_results.append(result)

                if not result['deleted']:
                    # 预览或失败 —— 不再上溯（避免幻觉）
                    break
                # 实删成功 —— 继续往上一层试
                current = current.parent

        deleted_dir_count = sum(1 for r in dir_results if r.get('deleted'))
        # 把目录删除影响的库也加进刷新列表
        for r in dir_results:
            if r.get('deleted') and r.get('_library_id'):
                affected_libs.append(r['_library_id'])

        # ===== Phase 3: 刷新库（run_all 会传 skip_refresh=True 让编排器统一刷）=====
        # 精准通知：删 sample 文件 + 删空目录 → 收集这些路径让 jellyfin 仅扫这些位置
        deleted_paths = [r['path'] for r in sample_results if r.get('deleted') and r.get('path')]
        deleted_paths += [r['path'] for r in dir_results if r.get('deleted') and r.get('path')]
        refreshed = (
            _refresh_libraries(affected_libs, affected_paths=deleted_paths, update_type='Deleted')
            if (not dry_run and not skip_refresh) else []
        )

        # 清掉内部字段，再返回结果
        for r in sample_results:
            r.pop('_library_id', None)
        for r in dir_results:
            r.pop('_library_id', None)

        complete_task(db, task_id, {
            'dry_run': dry_run,
            'scanned': total,
            'sample_count': len(sample_results),
            'deleted_file_count': deleted_file_count,
            'deleted_dir_count': deleted_dir_count,
            'skipped_count': len(skipped),
            'errors': [r for r in sample_results + dir_results
                       if r.get('error') and r['error'] != '(预览模式)'],
            'details': sample_results[:500],
            'dir_details': dir_results[:200],
            'libraries_refreshed': refreshed,
        })

    except Exception as e:
        logger.exception("cleanup-samples 任务失败")
        complete_task(db, task_id, {'error': str(e)}, success=False)
    finally:
        db.close()


# ---------- auto-identify ----------

@router.post("/auto-identify", response_model=TaskStartResponse)
def auto_identify(
    req: MaintenanceRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    扫描作用范围，找出健康度=`unrecognized`（type=Folder 没识别）的条目，
    用自动提取的标题+年份在 TMDB 搜索，把第一个匹配 apply 上去。
    搜不到的回报 error；完成后刷新受影响库。
    """
    target = _resolve_scope(req)
    mode_label = "预览" if req.dry_run else "执行"
    logger.info(
        f"/maintenance/auto-identify: scope={target['label']!r} "
        f"paths={len(target['paths'])} dry_run={req.dry_run}"
    )
    task = create_task(db, "auto_identify", f"{mode_label}修复识别错误: {target['label']}")
    background_tasks.add_task(run_auto_identify, task.id, target, req.dry_run)
    return TaskStartResponse(task_id=task.id, status="started",
                             message=f"自动识别任务已启动（{mode_label}）")


def run_auto_identify(task_id: int, target: Dict, dry_run: bool, skip_refresh: bool = False):
    """
    后台任务：
      Step 1: 枚举 scope 内所有 Jellyfin items
      Step 2: compute_health 过滤 unrecognized
      Step 3: 对每个 → extract_suggested_title_year → remote_search → apply first
      Step 4: 刷新受影响库

    DB 只用于进度/完成回写，不持有连接跨 Jellyfin / TMDB HTTP。
    """
    from web.backend.database import SessionLocal

    def _progress(pct: int, msg: str, patch: Optional[Dict] = None):
        with SessionLocal() as db:
            update_task_progress(db, task_id, pct, msg, result_patch=patch)

    try:
        _progress(2, "开始拉取条目列表...")
        client = _client()

        all_items = _enumerate_items_in_scope(target)
        _progress(10, f"共 {len(all_items)} 个条目，正在筛选未识别项...")

        # 过滤未识别
        unrecognized = []
        for it in all_items:
            health = compute_health(it)
            if any(iss['code'] == 'unrecognized' for iss in health.get('issues', [])):
                unrecognized.append(it)

        total = len(unrecognized)
        _progress(15, f"找到 {total} 个未识别条目，开始搜索 + 应用...")

        if total == 0:
            with SessionLocal() as db:
                complete_task(db, task_id, {
                    'dry_run': dry_run,
                    'scanned': len(all_items),
                    'unrecognized_count': 0,
                    'fixed_count': 0,
                    'no_match_count': 0,
                    'errors': [],
                    'details': [],
                    'libraries_refreshed': [],
                })
            return

        results: List[Dict] = []
        fixed_count = 0
        no_match_count = 0
        affected_libs: List[str] = []
        language = settings.tmdb_language or 'en-US'

        # 增量写入 partial result 的频率：每 N 条把 details 合并写一次 task.result，
        # 让详情页运行中也能看到已处理条目，而不必等任务结束
        EMIT_EVERY = 3

        for idx, it in enumerate(unrecognized):
            pct = 15 + int(80 * (idx + 1) / total)
            item_id = it.get('Id')
            item_name = it.get('Name') or '(无标题)'
            item_type = it.get('Type') or 'Folder'
            progress_msg = f"识别 [{idx+1}/{total}] {item_name}"

            # 处理上一轮累积的 results：每 EMIT_EVERY 个就 emit partial
            should_emit = (idx > 0 and idx % EMIT_EVERY == 0)
            _progress(
                pct, progress_msg,
                patch=({
                    'dry_run': dry_run,
                    'scanned': len(all_items),
                    'unrecognized_count': total,
                    'fixed_count': fixed_count,
                    'no_match_count': no_match_count,
                    'details': results[:500],
                } if should_emit else None),
            )

            entry = {
                'item_id': item_id,
                'item_name': item_name,
                'item_type': item_type,
                'path': translate_path_with_settings(it.get('Path')) if it.get('Path') else None,
                'extracted_title': None,
                'extracted_year': None,
                'first_candidate': None,
                'applied': False,
                'error': None,
            }

            # 自动提取
            title, year = extract_suggested_title_year(it)
            entry['extracted_title'] = title
            entry['extracted_year'] = year
            if not title:
                entry['error'] = '无法从路径提取标题'
                results.append(entry)
                continue

            # 远端搜索
            try:
                # Folder 类型在 client 内部会自动按 Movie 端点处理
                candidates = client.remote_search(
                    item_id=item_id,
                    item_type=item_type,
                    name=title,
                    year=year,
                    language=language,
                )
            except Exception as e:
                entry['error'] = f'搜索失败: {e}'
                logger.exception(f"remote_search 失败 {item_id}")
                results.append(entry)
                continue

            if not candidates:
                entry['error'] = f'未找到匹配：{title} ({year})'
                no_match_count += 1
                results.append(entry)
                continue

            first = candidates[0]
            entry['first_candidate'] = {
                'name': first.get('Name'),
                'year': first.get('ProductionYear'),
                'tmdb_id': (first.get('ProviderIds') or {}).get('Tmdb'),
                'overview': (first.get('Overview') or '')[:200],
                'image_url': first.get('ImageUrl'),
            }

            if dry_run:
                entry['error'] = '(预览模式)'
                results.append(entry)
                continue

            # apply first
            try:
                client.remote_search_apply(item_id, first, replace_all_images=True)
                entry['applied'] = True
                fixed_count += 1
                if it.get('_library_id'):
                    affected_libs.append(it['_library_id'])
            except Exception as e:
                entry['error'] = f'应用失败: {e}'
                logger.exception(f"remote_search_apply 失败 {item_id}")

            results.append(entry)

        refreshed = _refresh_libraries(affected_libs) if (not dry_run and not skip_refresh) else []

        with SessionLocal() as db:
            complete_task(db, task_id, {
                'dry_run': dry_run,
                'scanned': len(all_items),
                'unrecognized_count': total,
                'fixed_count': fixed_count,
                'no_match_count': no_match_count,
                'errors': [r for r in results if r.get('error') and r['error'] != '(预览模式)'],
                'details': results[:500],
                'libraries_refreshed': refreshed,
            })

    except Exception as e:
        logger.exception("auto-identify 任务失败")
        with SessionLocal() as db:
            complete_task(db, task_id, {'error': str(e)}, success=False)


# ---------- normalize-paths ----------

@router.post("/normalize-paths", response_model=TaskStartResponse)
def normalize_paths(
    req: MaintenanceRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    扫描作用范围，找出"嵌套在 release 子目录里的主文件"（health 中的 nested_main_file），
    把这些主文件挪回 release 根目录。会跳过 sample 文件。完成后刷新受影响库。
    """
    target = _resolve_scope(req)
    mode_label = "预览" if req.dry_run else "执行"
    logger.info(
        f"/maintenance/normalize-paths: scope={target['label']!r} "
        f"paths={len(target['paths'])} dry_run={req.dry_run}"
    )
    task = create_task(db, "normalize_paths", f"{mode_label}规范路径: {target['label']}")
    background_tasks.add_task(
        run_normalize_paths,
        task.id,
        target['paths'],
        target['library_id_by_path'],
        req.dry_run,
    )
    return TaskStartResponse(task_id=task.id, status="started",
                             message=f"规范路径任务已启动（{mode_label}）")


def run_normalize_paths(
    task_id: int,
    paths: List[str],
    library_id_by_path: Dict[str, Optional[str]],
    dry_run: bool,
    skip_refresh: bool = False,
):
    """
    后台任务：扫描视频文件，对每个文件检查嵌套：
      file in release_dir/sub/file.mkv  且 sub 不像 release 名 / 上级像 release 名
    → 把 file 移到 release_dir 根。
    跳过 sample 文件（避免把 sample 也搬上去）。
    """
    from web.backend.database import SessionLocal

    db = SessionLocal()
    try:
        update_task_progress(db, task_id, 2, "开始扫描视频文件...")

        videos: List[Dict] = []
        for jf_path in paths:
            local_root = Path(translate_path_with_settings(jf_path))
            for f in _walk_video_files(local_root):
                videos.append({'local_path': f, 'library_id': library_id_by_path.get(jf_path)})

        total = len(videos)
        update_task_progress(db, task_id, 10, f"扫描完成：找到 {total} 个视频，正在分析嵌套...")

        if total == 0:
            complete_task(db, task_id, {
                'scanned': 0, 'nested_count': 0, 'moved_count': 0,
                'skipped_count': 0, 'errors': [], 'details': [],
                'libraries_refreshed': [],
            })
            return

        results = []
        moved_count = 0
        affected_libs: List[str] = []

        EMIT_EVERY = 5

        for idx, v in enumerate(videos):
            pct = 10 + int(80 * (idx + 1) / total)
            file_path: Path = v['local_path']
            should_emit = (idx > 0 and idx % EMIT_EVERY == 0)
            update_task_progress(
                db, task_id, pct, f"分析 [{idx+1}/{total}] {file_path.name}",
                result_patch=({
                    'dry_run': dry_run,
                    'scanned': idx,
                    'nested_count': len(results),
                    'moved_count': moved_count,
                    'skipped_count': idx - len(results),
                    'details': results[:500],
                } if should_emit else None),
            )

            # 跳过 sample 和 extras 文件（extras = Featurettes / Trailers / 花絮等合法附加内容）
            evidence = gather_sample_evidence({'Path': str(file_path), 'RunTimeTicks': 0})
            if evidence.get('verdict') in ('sample', 'sample-likely', 'extras'):
                continue

            # 检查嵌套：parent 不像 release 名，grandparent 像
            parts = str(file_path).replace('\\', '/').rstrip('/').split('/')
            if len(parts) < 3:
                continue
            parent_name = parts[-2]
            grandparent_name = parts[-3]
            if not (
                not _looks_like_release_dir(parent_name)
                and _looks_like_release_dir(grandparent_name)
            ):
                continue

            # 是嵌套主文件 → 计算目标路径
            release_dir = file_path.parent.parent
            target_path = release_dir / file_path.name

            item = {
                'from': str(file_path),
                'to': str(target_path),
                'release_dir': str(release_dir),
                'moved': False,
                'error': None,
            }

            if target_path.exists():
                item['error'] = '目标已存在，跳过'
                results.append(item)
                continue

            if dry_run:
                item['error'] = '(预览模式)'
                results.append(item)
                continue

            try:
                shutil.move(str(file_path), str(target_path))
                item['moved'] = True
                moved_count += 1
                if v['library_id']:
                    affected_libs.append(v['library_id'])
            except Exception as e:
                item['error'] = str(e)
                logger.exception(f"移动失败: {file_path} -> {target_path}")

            results.append(item)

        # 精准通知：移动文件 → 通知 from（Deleted）+ to（Created）；jellyfin 自己合并成"重命名"
        # （注意 notify_media_updated 单次调用只接收一个 update_type，故拆两次：先 Created 通知新位置）
        moved_to_paths = [r['to'] for r in results if r.get('moved') and r.get('to')]
        if not dry_run and not skip_refresh:
            refreshed = _refresh_libraries(
                affected_libs,
                affected_paths=moved_to_paths,
                update_type='Created',
            )
        else:
            refreshed = []

        complete_task(db, task_id, {
            'dry_run': dry_run,
            'scanned': total,
            'nested_count': len(results),
            'moved_count': moved_count,
            'skipped_count': total - len(results),
            'errors': [r for r in results if r.get('error') and r['error'] != '(预览模式)'],
            'details': results[:500],
            'libraries_refreshed': refreshed,
        })

    except Exception as e:
        logger.exception("normalize-paths 任务失败")
        complete_task(db, task_id, {'error': str(e)}, success=False)
    finally:
        db.close()


# ========== run-all：一键修复所有问题（串行编排） ==========

class RunAllRequest(BaseModel):
    """
    一键修复请求：串行执行 toolbar 上从左到右的所有按钮流程。
      - 与其它 maintenance 接口 scope 字段保持一致（item_paths > library_ids > library_id > 全库）
      - audio_lang：默认音轨步骤的目标语言；为空则跳过该步骤
      - dry_run：测试模式，所有 step 仅扫描/查询，不实际写入/删除/上传
    """
    item_paths: Optional[List[str]] = None
    library_id: Optional[str] = None
    library_ids: Optional[List[str]] = None
    audio_lang: Optional[str] = None  # eng / chs / yue / jpn；None=跳过音轨步骤
    dry_run: bool = False


@router.post("/run-all", response_model=TaskStartResponse)
def run_all(
    req: RunAllRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    创建 1 个父任务，串行执行 9 个 step。每个 step 单独建一个子任务，
    实时可查；父任务的 result 汇总所有 step 状态摘要。
    """
    mreq = MaintenanceRequest(
        item_paths=req.item_paths,
        library_id=req.library_id,
        library_ids=req.library_ids,
        dry_run=False,
    )
    target = _resolve_scope(mreq)

    label_prefix = "一键修复（测试模式）" if req.dry_run else "一键修复"
    logger.info(
        f"/maintenance/run-all: scope={target['label']!r} paths={len(target['paths'])} "
        f"audio_lang={req.audio_lang!r} dry_run={req.dry_run}"
    )
    parent = create_task(db, "run_all", f"{label_prefix}: {target['label']}")
    background_tasks.add_task(
        run_all_orchestrator,
        parent.id,
        req.item_paths or None,
        req.library_id or None,
        req.library_ids or None,
        req.audio_lang or None,
        req.dry_run,
    )
    return TaskStartResponse(
        task_id=parent.id,
        status="started",
        message=f"{label_prefix}任务已启动（共 {9 if req.audio_lang else 8} 个步骤）",
    )


def _summarize_step_result(step_name: str, result: Optional[dict]) -> dict:
    """从子任务的 result 里抽取一句话摘要，避免把整个 result 塞进父任务。"""
    if not result:
        return {}
    if 'error' in result and len(result) <= 2:
        return {'error': result['error']}
    summary = {}
    if step_name == 'cleanup_samples':
        summary = {
            'sample_count': result.get('sample_count'),
            'deleted_file_count': result.get('deleted_file_count'),
            'deleted_dir_count': result.get('deleted_dir_count'),
        }
    elif step_name == 'normalize_paths':
        summary = {
            'nested_count': result.get('nested_count'),
            'moved_count': result.get('moved_count'),
        }
    elif step_name == 'auto_identify':
        summary = {
            'unrecognized_count': result.get('unrecognized_count'),
            'fixed_count': result.get('fixed_count'),
            'no_match_count': result.get('no_match_count'),
        }
    elif step_name == 'poster_fix':
        summary = {
            'total': result.get('total'),
            'success': result.get('success'),
            'failed': result.get('failed'),
            'not_found': result.get('not_found'),
        }
    elif step_name == 'actor_fix':
        summary = {
            'total': result.get('total'),
            'success': result.get('success'),
            'failed': result.get('failed'),
        }
    elif step_name == 'subtitle_scan':
        summary = {
            'total_videos': result.get('total_videos'),
            'with_subtitles': result.get('with_subtitles'),
            'without_subtitles': result.get('without_subtitles'),
        }
    elif step_name == 'subtitle_auto_fix':
        dl = (result.get('download') or {})
        rn = (result.get('rename') or {})
        summary = {
            'download_success': dl.get('success'),
            'download_total': dl.get('total'),
            'rename_success': rn.get('success'),
        }
    elif step_name == 'subtitle_rename':
        summary = {
            'total': result.get('total'),
            'success': result.get('success'),
            'failed': result.get('failed'),
        }
    elif step_name == 'audio_default_track':
        sc = (result.get('scan') or {})
        ap = (result.get('apply') or {})
        summary = {
            'candidates': sc.get('candidates'),
            'apply_success': ap.get('success'),
            'apply_failed': ap.get('failed'),
        }
    return summary


def run_all_orchestrator(
    parent_task_id: int,
    item_paths: Optional[List[str]],
    library_id: Optional[str],
    library_ids: Optional[List[str]],
    audio_lang: Optional[str],
    dry_run: bool = False,
):
    """
    一键修复编排器（同步串行）。每个 step 直接复用现有 run_xxx 后台函数，
    每个 step 创建独立子任务（task_id 可在 /tasks 单独查看）。
    """
    import json as _json
    from web.backend.database import SessionLocal, Task as _Task
    from web.backend.api.metadata import run_poster_fix, run_actor_fix
    from web.backend.api.subtitle import (
        run_subtitle_scan, run_subtitle_auto_fix, run_subtitle_rename,
        _resolve_scope as _resolve_sub_scope,
    )
    from web.backend.api.audio import (
        run_default_track_task, _resolve_targets, AudioScanRequest,
    )
    from web.backend.config import settings as _settings

    db = SessionLocal()
    try:
        mreq = MaintenanceRequest(
            item_paths=item_paths,
            library_id=library_id,
            library_ids=library_ids,
            dry_run=False,
        )
        target = _resolve_scope(mreq)
        scope_paths = target['paths']
        lib_map = target['library_id_by_path']

        # 字幕步骤的 scope（subtitle 接口有自己的 _resolve_scope，对 item_paths 会取父目录 + 强制非递归）
        try:
            sub_paths, sub_recursive_override, _, sub_refresh_ids = _resolve_sub_scope(
                item_paths=item_paths,
                library_id=library_id,
                library_ids=library_ids,
            )
            sub_recursive = sub_recursive_override if sub_recursive_override is not None else True
        except Exception as e:
            logger.warning(f"字幕 scope 解析失败（将跳过字幕步骤）: {e}")
            sub_paths = None
            sub_recursive = True
            sub_refresh_ids = []

        # 音轨步骤的 target（仅当 audio_lang 给定时需要）
        audio_target = None
        if audio_lang:
            try:
                areq = AudioScanRequest(
                    item_paths=item_paths,
                    library_id=library_id,
                    library_ids=library_ids,
                    recursive=True,
                    apply=True,
                )
                audio_target = _resolve_targets(areq)
            except Exception as e:
                logger.warning(f"音轨 scope 解析失败（将跳过音轨步骤）: {e}")
                audio_target = None

        expected_langs = _settings.preferred_langs

        # run_all 模式下：
        #   - 所有 step 跳过单独刷新（skip_refresh=True / 空 refresh_library_ids）
        #     由编排器最后根据实际改动汇总刷新一次
        #   - dry_run 透传到各 step（字段名各异：dry_run / scan_only / execute / apply）
        steps: List[Dict] = [
            {
                'name': 'cleanup_samples',
                'label': '清理 Sample 内容',
                'runner': lambda tid: run_cleanup_samples(tid, scope_paths, lib_map, dry_run, skip_refresh=True),
            },
            {
                'name': 'normalize_paths',
                'label': '扫描并规范路径',
                'runner': lambda tid: run_normalize_paths(tid, scope_paths, lib_map, dry_run, skip_refresh=True),
            },
            {
                'name': 'auto_identify',
                'label': '扫描并修复识别错误',
                'runner': lambda tid: run_auto_identify(tid, target, dry_run, skip_refresh=True),
            },
            {
                'name': 'poster_fix',
                'label': '扫描并修复缺失海报',
                # poster_fix 的 scan_only 等价 dry_run（仅查 TMDB，不下载/上传）
                'runner': lambda tid: run_poster_fix(
                    tid, dry_run, None, True, None, library_id, library_ids, item_paths,
                    skip_refresh=True,
                ),
            },
            {
                'name': 'actor_fix',
                'label': '扫描并修复演员图片',
                # actor_fix 的 scan_only 等价 dry_run
                'runner': lambda tid: run_actor_fix(
                    tid, dry_run, None, True, library_id, library_ids,
                    skip_refresh=True,
                ),
            },
        ]
        if sub_paths:
            steps.extend([
                {
                    'name': 'subtitle_scan',
                    'label': '扫描字幕缺失',
                    # subtitle_scan 本身只读，dry_run 不影响
                    'runner': lambda tid: run_subtitle_scan(tid, sub_paths, sub_recursive, expected_langs),
                },
                {
                    'name': 'subtitle_auto_fix',
                    'label': '自动下载字幕',
                    # 字幕外挂文件即时发现 → 单独跑也不刷，run_all 更不刷
                    'runner': lambda tid: run_subtitle_auto_fix(
                        tid, sub_paths, sub_recursive, expected_langs,
                        dry_run, True, [], None,
                    ),
                },
                {
                    'name': 'subtitle_rename',
                    'label': '字幕文件名对齐',
                    # rename 的 execute 与 dry_run 取反
                    'runner': lambda tid: run_subtitle_rename(
                        tid, sub_paths, sub_recursive, not dry_run, None, [],
                    ),
                },
            ])
        if audio_lang and audio_target:
            steps.append({
                'name': 'audio_default_track',
                'label': '设置默认音轨',
                # apply 与 dry_run 取反；refresh_library_ids 传 None → 内部不刷新
                'runner': lambda tid: run_default_track_task(
                    tid, audio_target, True, [audio_lang], True, not dry_run, None,
                ),
            })

        total_steps = len(steps)
        step_records: List[Dict] = []
        success_count = 0
        failed_count = 0

        for idx, step in enumerate(steps):
            base_pct = int(100 * idx / total_steps)
            update_task_progress(
                db, parent_task_id, base_pct,
                f"[{idx+1}/{total_steps}] {step['label']}",
            )

            child = create_task(db, step['name'], f"[run-all #{parent_task_id}] {step['label']}")
            record = {
                'step': step['name'],
                'label': step['label'],
                'task_id': child.id,
                'status': 'running',
                'started_at': None,
                'completed_at': None,
                'message': None,
                'summary': None,
            }
            try:
                step['runner'](child.id)
            except Exception as e:
                logger.exception(f"step {step['name']} 抛异常")
                try:
                    complete_task(db, child.id, {'error': str(e)}, success=False)
                except Exception:
                    pass

            db.expire_all()
            ch = db.query(_Task).filter(_Task.id == child.id).first()
            if ch:
                child_result = None
                if ch.result:
                    try:
                        child_result = _json.loads(ch.result)
                    except Exception:
                        child_result = {'raw': ch.result}
                record['status'] = ch.status
                record['started_at'] = ch.started_at.isoformat() if ch.started_at else None
                record['completed_at'] = ch.completed_at.isoformat() if ch.completed_at else None
                record['message'] = ch.message
                record['summary'] = _summarize_step_result(step['name'], child_result)
                if ch.status == 'completed':
                    success_count += 1
                elif ch.status == 'failed':
                    failed_count += 1

            step_records.append(record)

            # 增量写入父任务 result —— 运行中的详情页就能看到瀑布图
            # 注意：合并 create_task 时写入的 initial_message 等元信息，避免覆盖
            try:
                parent = db.query(_Task).filter(_Task.id == parent_task_id).first()
                if parent:
                    merged: Dict = {}
                    if parent.result:
                        try:
                            existing = _json.loads(parent.result)
                            if isinstance(existing, dict):
                                merged.update(existing)
                        except Exception:
                            pass
                    merged.update({
                        'total_steps': total_steps,
                        'success_steps': success_count,
                        'failed_steps': failed_count,
                        'audio_lang': audio_lang,
                        'dry_run': dry_run,
                        'scope_label': target['label'],
                        'steps': step_records,
                    })
                    parent.result = _json.dumps(merged, ensure_ascii=False)
                    db.commit()
            except Exception as e:
                logger.warning(f"父任务增量写入失败: {e}")

        # ===== 最后统一触发 Jellyfin 刷新 =====
        # 判断哪些 step 实际产生了 Jellyfin 元数据/文件层面的改动；
        # 字幕类不计（外挂文件即时发现，无需重扫）。
        needs_refresh = False
        for rec in step_records:
            if rec['status'] != 'completed':
                continue
            s = rec.get('summary') or {}
            name = rec['step']
            if name == 'cleanup_samples':
                if (s.get('deleted_file_count') or 0) > 0 or (s.get('deleted_dir_count') or 0) > 0:
                    needs_refresh = True
            elif name == 'normalize_paths':
                if (s.get('moved_count') or 0) > 0:
                    needs_refresh = True
            elif name == 'auto_identify':
                if (s.get('fixed_count') or 0) > 0:
                    needs_refresh = True
            elif name in ('poster_fix', 'actor_fix'):
                if (s.get('success') or 0) > 0:
                    needs_refresh = True
            elif name == 'audio_default_track':
                if (s.get('apply_success') or 0) > 0:
                    needs_refresh = True

        refreshed_libs: List[str] = []
        if needs_refresh:
            # 受影响库 = scope 内涉及的所有库（去重）
            affected = list({lid for lid in lib_map.values() if lid})
            if affected:
                update_task_progress(
                    db, parent_task_id, 99,
                    f"统一刷新 Jellyfin（{len(affected)} 个库）...",
                )
                refreshed_libs = _refresh_libraries(affected)

        complete_task(db, parent_task_id, {
            'total_steps': total_steps,
            'success_steps': success_count,
            'failed_steps': failed_count,
            'audio_lang': audio_lang,
            'dry_run': dry_run,
            'scope_label': target['label'],
            'steps': step_records,
            'jellyfin_refreshed': refreshed_libs,
            'refresh_skipped_reason': None if needs_refresh else '所有 step 均未产生需要 Jellyfin 重扫的改动',
        }, success=(failed_count == 0))

    except Exception as e:
        logger.exception("run-all 编排失败")
        complete_task(db, parent_task_id, {'error': str(e)}, success=False)
    finally:
        db.close()
