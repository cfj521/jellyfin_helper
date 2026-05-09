"""
默认音轨管理 API
扫描视频音轨；按期望语言修改 mkv 默认音轨。
"""
import logging
import sys
from pathlib import Path
from typing import List, Optional, Dict

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

ROOT_DIR = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from web.backend.config import settings
from web.backend.database import get_db
from web.backend.api.tasks import create_task, update_task_progress, complete_task

from tools.audio_manager.scanner import (
    AudioTrackScanner, ALL_VIDEO_EXTS, MODIFIABLE_VIDEO_EXTS,
)
from tools.audio_manager.setter import set_default_audio_track, get_mkvpropedit

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------- Models ----------

class AudioScanRequest(BaseModel):
    """
    音轨扫描+修复请求。
    支持三种 scope（按优先级解析）：
      1. item_paths：直接给视频文件路径（"选中若干媒体"场景）
      2. library_id：单个库 → 展开为该库的 locations
      3. library_ids：多个库 → 展开为这些库的所有 locations
      4. path：手动路径
    """
    item_paths: Optional[List[str]] = None
    library_id: Optional[str] = None
    library_ids: Optional[List[str]] = None
    path: Optional[str] = None
    recursive: bool = True
    preferred_langs: Optional[List[str]] = None  # 不传用 settings.preferred_audio_langs
    skip_single_track: bool = True
    apply: bool = False                          # False=仅扫描预览，True=实际改 mkv
    # mkvpropedit 改 mkv 内部 default flag 后，Jellyfin 数据库里 IsDefault 是缓存值，
    # 不重扫客户端拿到的还是旧值。所以默认 True；run_all 模式会传 False 让编排器统一刷。
    refresh_jellyfin: bool = True


class TaskStartResponse(BaseModel):
    task_id: int
    status: str
    message: str


# ---------- 路径解析 ----------

def _resolve_refresh_libraries(req: AudioScanRequest) -> List[str]:
    """
    根据请求 scope 反查需要刷新的 Jellyfin 库 ID 列表。
      - library_ids / library_id 模式：直接用
      - item_paths / path 模式：按路径前缀反查所属库
      - 全都没给：返回空（调用方决定要不要全局刷）
    """
    if req.library_ids:
        return list(req.library_ids)
    if req.library_id:
        return [req.library_id]
    candidate_paths: List[str] = []
    if req.item_paths:
        candidate_paths.extend(req.item_paths)
    if req.path:
        candidate_paths.append(req.path)
    if not candidate_paths:
        return []
    try:
        from common.jellyfin_client import JellyfinClient
        client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
        libs = client.get_libraries_normalized()
        out: List[str] = []
        for p in candidate_paths:
            p_norm = (p or '').replace('\\', '/').rstrip('/').lower()
            if not p_norm:
                continue
            for lib in libs:
                for loc in lib.get('locations') or []:
                    loc_norm = loc.replace('\\', '/').rstrip('/').lower()
                    if p_norm == loc_norm or p_norm.startswith(loc_norm + '/'):
                        if lib['id'] not in out:
                            out.append(lib['id'])
                        break
        return out
    except Exception as e:
        logger.warning(f"反查音轨任务影响库失败: {e}")
        return []


def _translate(p: str) -> str:
    """Jellyfin 视角路径 → 后端可访问的本地路径（按 settings.path_mappings_rules）。"""
    if not p:
        return p
    try:
        from web.backend.path_translator import translate_path_with_settings
        return translate_path_with_settings(p) or p
    except Exception:
        return p


def _resolve_targets(req: AudioScanRequest) -> Dict:
    """
    返回:
      {
        'mode': 'items' | 'paths',
        'items': List[Path],     # mode=items 时直接的视频文件（已映射为本地路径）
        'paths': List[str],      # mode=paths 时要扫的目录（已映射为本地路径）
        'label': str,
      }
    """
    # 1. 选中若干媒体（前端传过来的视频文件路径）
    if req.item_paths:
        # toolbar 选中的 item.Path 是 Jellyfin 视角，先映射为本地路径再 stat
        items = [Path(_translate(p)) for p in req.item_paths if p]
        items = [p for p in items if p.exists()]
        if not items:
            raise HTTPException(status_code=400, detail="选中的视频文件均不存在（路径映射后仍找不到）")
        return {'mode': 'items', 'items': items, 'paths': [], 'label': f'选中 {len(items)} 个文件'}

    # 2. 多库
    if req.library_ids:
        from web.backend.api.jellyfin import get_library_by_id
        all_paths: List[str] = []
        for lid in req.library_ids:
            lib = get_library_by_id(lid)
            if lib:
                all_paths.extend(_translate(loc) for loc in (lib.get('locations') or []))
        if not all_paths:
            raise HTTPException(status_code=400, detail="所选库均无可用路径")
        return {
            'mode': 'paths', 'items': [], 'paths': all_paths,
            'label': f'{len(req.library_ids)} 个库（{len(all_paths)} 个路径）',
        }

    # 3. 单库
    if req.library_id:
        from web.backend.api.jellyfin import get_library_by_id
        lib = get_library_by_id(req.library_id)
        if not lib:
            raise HTTPException(status_code=404, detail=f"Jellyfin 库不存在: {req.library_id}")
        if not lib.get('locations'):
            raise HTTPException(status_code=400, detail=f"库 {lib['name']} 没有配置路径")
        translated = [_translate(loc) for loc in lib['locations']]
        return {
            'mode': 'paths', 'items': [], 'paths': translated,
            'label': f"库 {lib['name']}",
        }

    # 4. 手动路径（用户直接输入，假定已是本地路径，不再映射）
    if req.path:
        if not Path(req.path).exists():
            raise HTTPException(status_code=400, detail=f"路径不存在: {req.path}")
        return {'mode': 'paths', 'items': [], 'paths': [req.path], 'label': req.path}

    raise HTTPException(status_code=400, detail="必须提供 item_paths / library_id / library_ids / path 之一")


# ---------- 端点 ----------

@router.get("/check")
def check_tools():
    """前端用来判断 ffprobe / mkvpropedit 是否可用。"""
    from tools.audio_manager.scanner import _get_ffprobe
    return {
        'ffprobe_available': _get_ffprobe() is not None,
        'mkvpropedit_available': get_mkvpropedit() is not None,
    }


@router.post("/default-track", response_model=TaskStartResponse)
def default_track_action(
    req: AudioScanRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    扫描+（可选）修复默认音轨。
    apply=False 仅返回建议；apply=True 会实际调 mkvpropedit 写入。
    """
    target = _resolve_targets(req)

    if req.apply and not get_mkvpropedit():
        raise HTTPException(
            status_code=400,
            detail="未找到 mkvpropedit，无法应用修改。请安装 mkvtoolnix。",
        )

    mode_label = "执行" if req.apply else "预览"
    task = create_task(db, "audio_default_track", f"{mode_label}音轨修复: {target['label']}")

    refresh_library_ids = _resolve_refresh_libraries(req) if req.refresh_jellyfin else []

    background_tasks.add_task(
        run_default_track_task,
        task.id,
        target,
        req.recursive,
        req.preferred_langs or settings.preferred_audio_langs,
        req.skip_single_track,
        req.apply,
        refresh_library_ids,
    )

    return TaskStartResponse(
        task_id=task.id,
        status="started",
        message=f"音轨修复任务已启动（{mode_label}模式）",
    )


def run_default_track_inline(
    item_paths: Optional[List[str]] = None,
    paths: Optional[List[str]] = None,
    recursive: bool = True,
    preferred_langs: Optional[List[str]] = None,
    skip_single_track: bool = True,
    apply: bool = False,
    refresh_library_ids: Optional[List[str]] = None,
    progress_cb: Optional[callable] = None,
) -> Dict:
    """
    同步执行音轨扫描 +（可选）修复默认轨。**不写 task / 不写 db**。

    供 dispatch pipeline 等模块直接 import 使用。

    输入二选一（或都给）：
      item_paths: 视频文件路径列表（应为本地可访问路径，调用方负责映射）
      paths    : 目录路径列表

    apply=False 仅扫描返回建议；apply=True 调 mkvpropedit 写入。
    apply=True 但 mkvpropedit 不可用 → result['error'] 返回提示，candidates 仍含建议。

    返回:
    {
      "scan": {total, skipped, already_ok, candidates},
      "apply": {executed, success, failed},
      "jellyfin_refreshed": [lib_ids...],
      "candidates": [...],          # 推荐改默认轨的视频明细
      "applied_details": [...],     # apply=True 时的逐文件结果
      "skipped_sample": [...],      # 跳过的示例（前 100 条）
      "error": Optional[str],
    }
    """
    from web.backend.api.jellyfin import trigger_refresh

    if preferred_langs is None:
        preferred_langs = settings.preferred_audio_langs
    if refresh_library_ids is None:
        refresh_library_ids = []

    def _emit(pct: int, msg: str):
        if progress_cb:
            try:
                progress_cb(pct, msg)
            except Exception:
                logger.exception("progress_cb 抛错，已忽略")

    # apply=True 时检查 mkvpropedit 可用性
    error_msg: Optional[str] = None
    if apply and not get_mkvpropedit():
        error_msg = "mkvpropedit 不可用（未安装 mkvtoolnix），仅返回建议不实际修改"
        logger.warning(error_msg)
        apply = False  # 退化为预览模式

    scanner = AudioTrackScanner(
        preferred_langs=preferred_langs,
        skip_single_track=skip_single_track,
        concurrency=5,
    )

    # ---- Step 1: 扫描 ----
    _emit(5, "开始扫描音轨...")

    results: List[Dict] = []
    if item_paths:
        videos = [Path(p) for p in item_paths if Path(p).exists()]
        if videos:
            results.extend(scanner.scan_videos(videos))

    if paths:
        for idx, p in enumerate(paths):
            pct = 5 + int(45 * (idx + 1) / max(len(paths), 1))
            _emit(pct, f"扫描 [{idx+1}/{len(paths)}] {p}")
            results.extend(scanner.scan_directory(Path(p), recursive=recursive))

    # ---- 汇总 ----
    total = len(results)
    skipped = sum(1 for r in results if r.get('skipped'))
    already_ok = sum(
        1 for r in results
        if not r.get('skipped') and not r.get('should_change')
    )
    candidates = [r for r in results if r.get('should_change')]

    _emit(55, f"扫描完成：{total} 个文件，{len(candidates)} 个需要改默认音轨")

    # ---- Step 2: 应用修改 ----
    applied = []
    apply_success = 0
    apply_failed = 0
    if apply and candidates:
        for idx, item in enumerate(candidates):
            pct = 55 + int(40 * (idx + 1) / max(len(candidates), 1))
            video_path = Path(item['path'])
            _emit(pct, f"修改 [{idx+1}/{len(candidates)}] {video_path.name}")
            try:
                res = set_default_audio_track(
                    video_path,
                    target_track_id=item['target_track'],
                    total_tracks=len(item['tracks']),
                )
            except Exception as e:
                logger.exception(f"音轨写入异常: {video_path}")
                res = {'success': False, 'error': str(e), 'cmd': '', 'stdout': '', 'stderr': ''}

            applied.append({
                'path': str(video_path),
                'name': video_path.name,
                'target_track': item['target_track'],
                'target_lang': item['target_lang'],
                'success': res.get('success', False),
                'error': res.get('error'),
            })
            if res.get('success'):
                apply_success += 1
            else:
                apply_failed += 1

    # ---- Step 3: 通知 Jellyfin（精准 path 通知，整库扫兜底）----
    refreshed: List[str] = []
    if apply and apply_success > 0:
        _emit(96, "通知 Jellyfin 刷新...")
        # 收集成功修改的文件路径（jellyfin 视角）
        modified_paths = []
        try:
            from web.backend.path_translator import reverse_translate_path_with_settings
            for item in applied:
                if item.get('success') and item.get('path'):
                    modified_paths.append(
                        reverse_translate_path_with_settings(item['path']) or item['path']
                    )
        except Exception:
            modified_paths = [it['path'] for it in applied if it.get('success') and it.get('path')]

        # ① 精准通知：mkvpropedit 改的是元数据，UpdateType=Modified
        notified_paths = False
        if modified_paths:
            try:
                from common.jellyfin_client import JellyfinClient
                jf = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
                if jf.notify_media_updated(modified_paths, update_type='Modified'):
                    notified_paths = True
                    refreshed = ['(media_updated)']
            except Exception as e:
                logger.warning(f"notify_media_updated 失败: {e}")

        # ② 兜底：精准通知失败才走整库扫
        if not notified_paths and refresh_library_ids:
            for lid in refresh_library_ids:
                try:
                    if trigger_refresh(lid):
                        refreshed.append(lid)
                except Exception as e:
                    logger.warning(f"刷新库 {lid} 失败: {e}")

    return {
        "scan": {
            "total": total,
            "skipped": skipped,
            "already_ok": already_ok,
            "candidates": len(candidates),
        },
        "apply": {
            "executed": apply,
            "success": apply_success,
            "failed": apply_failed,
        },
        "jellyfin_refreshed": refreshed,
        "candidates": [
            {
                'path': c['path'],
                'name': c['name'],
                'current_default_track': c.get('current_default_track'),
                'current_default_lang': c.get('current_default_lang'),
                'target_track': c.get('target_track'),
                'target_lang': c.get('target_lang'),
                'tracks': [
                    {
                        'mkv_track_id': t.get('mkv_track_id'),
                        'language': t.get('language'),
                        'title': t.get('title'),
                        'lang_code': t.get('lang_code'),
                        'is_default': t.get('is_default'),
                        'channels': t.get('channels'),
                        'codec': t.get('codec'),
                    }
                    for t in c.get('tracks', [])
                ],
            }
            for c in candidates[:200]
        ],
        "applied_details": applied[:200],
        "skipped_sample": [
            {'path': r['path'], 'name': r['name'], 'reason': r.get('skip_reason')}
            for r in results if r.get('skipped')
        ][:100],
        "error": error_msg,
    }


def run_default_track_task(
    task_id: int,
    target: Dict,
    recursive: bool,
    preferred_langs: List[str],
    skip_single_track: bool,
    apply: bool,
    refresh_library_ids: Optional[List[str]] = None,
):
    """后台 task wrapper：调 inline + 写 task 进度 / complete_task。"""
    from web.backend.database import SessionLocal

    def _progress(pct: int, msg: str):
        with SessionLocal() as db:
            update_task_progress(db, task_id, pct, msg)

    try:
        # 把 target dict 拆成 inline 接受的两路输入
        if target['mode'] == 'items':
            inline_kwargs = {'item_paths': [str(p) for p in target['items']]}
        else:
            inline_kwargs = {'paths': target['paths']}

        result = run_default_track_inline(
            **inline_kwargs,
            recursive=recursive,
            preferred_langs=preferred_langs,
            skip_single_track=skip_single_track,
            apply=apply,
            refresh_library_ids=refresh_library_ids,
            progress_cb=_progress,
        )

        with SessionLocal() as db:
            complete_task(db, task_id, result)

    except Exception as e:
        logger.exception("音轨任务失败")
        with SessionLocal() as db:
            complete_task(db, task_id, {"error": str(e)}, success=False)
