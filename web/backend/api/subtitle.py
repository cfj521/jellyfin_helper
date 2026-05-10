"""
字幕管理 API
"""
import sys
from pathlib import Path
from typing import List, Optional, Dict
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session

# 添加项目根目录
ROOT_DIR = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from web.backend.database import get_db, Task, ScanReport, VideoAnnotation
from web.backend.config import settings
from web.backend.api.tasks import create_task, update_task_progress, complete_task
from web.backend.task_restart import register_resumable
from tools.subtitle_manager.scanner import SubtitleScanner
from tools.subtitle_manager.renamer import SubtitleRenamer
from tools.subtitle_downloader.main import SubtitleDownloader

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------- Request / Response Models ----------

class ScanRequest(BaseModel):
    path: Optional[str] = None
    library_id: Optional[str] = None  # 提供 library_id 时自动展开为该库的所有 locations
    library_ids: Optional[List[str]] = None  # 多库
    item_paths: Optional[List[str]] = None   # 选中具体条目（视频文件路径）
    recursive: bool = True
    expected_langs: Optional[List[str]] = None  # None → 用 settings.preferred_langs


class RenameRequest(BaseModel):
    path: Optional[str] = None
    library_id: Optional[str] = None
    library_ids: Optional[List[str]] = None
    item_paths: Optional[List[str]] = None
    recursive: bool = True
    execute: bool = False
    force_lang: Optional[str] = None
    refresh_jellyfin: bool = True  # 重命名完成后刷新对应库


class DownloadRequest(BaseModel):
    report_id: int
    languages: Optional[List[str]] = None
    dry_run: bool = True
    limit: Optional[int] = None  # 调试用，限制处理数量


class AutoFixRequest(BaseModel):
    """一条龙：扫描 → 下载缺失字幕 → 重命名对齐 → （可选）刷新 Jellyfin。"""
    path: Optional[str] = None
    library_id: Optional[str] = None
    library_ids: Optional[List[str]] = None
    item_paths: Optional[List[str]] = None
    recursive: bool = True
    expected_langs: Optional[List[str]] = None  # None → 用 settings.preferred_langs
    dry_run: bool = True            # True=预览，不下载也不改名
    rename: bool = True             # 下载完后是否对齐裸名字幕
    refresh_jellyfin: bool = True
    limit: Optional[int] = None     # 调试用


class TaskStartResponse(BaseModel):
    task_id: int
    status: str
    message: str


# ---------- Scan ----------

def _any_subtitle_source_ready() -> bool:
    """
    `subtitle.sources` 中至少有一个 enabled（默认 True）且配置齐全的源。
    用于路由层的快速预检（避免任务起来再失败）。
    """
    sources = settings.subtitle_sources or []
    for s in sources:
        if not s.get('enabled', True):
            continue
        name = s.get('name')
        if name == 'opensubtitles' and settings.opensubtitles_api_key:
            return True
        if name == 'assrt' and settings.assrt_api_token:
            return True
    return False


def _translate(p: str) -> str:
    """Jellyfin 视角路径 → 后端可访问的本地路径（按 settings.path_mappings_rules）。
    没配规则或翻译失败则原样返回。"""
    if not p:
        return p
    try:
        from web.backend.path_translator import translate_path_with_settings
        return translate_path_with_settings(p) or p
    except Exception:
        return p


def _resolve_scope(
    *,
    path: Optional[str] = None,
    library_id: Optional[str] = None,
    library_ids: Optional[List[str]] = None,
    item_paths: Optional[List[str]] = None,
):
    """
    把 4 种 scope 解析为：(paths, recursive_override, label, refresh_library_ids)
      - paths：待扫描目录列表（**已经过 path_translator 映射，直接可用**）
      - recursive_override：item_paths 模式下应强制 False（只扫该文件所在目录）；
        其余模式返回 None（保留调用方自己的 recursive 设置）
      - label：日志/任务名用
      - refresh_library_ids：完成后刷新哪些库（item_paths 自动反查所属库）

    优先级：item_paths > library_ids > library_id > path
    都不传则展开为所有 jellyfin 库（"全部库"语义）。
    """
    from web.backend.api.medialibraries import get_library_by_id
    from common.jellyfin_client import JellyfinClient

    # 1. 选中具体条目：取每个文件的父目录去重，并强制 recursive=False
    if item_paths:
        parent_dirs: List[str] = []
        seen_dirs: set = set()
        for p in item_paths:
            if not p:
                continue
            pp = Path(p)
            parent = str(pp.parent) if pp.is_file() or pp.suffix else str(pp)
            key = parent.replace('\\', '/').rstrip('/').lower()
            if key and key not in seen_dirs:
                seen_dirs.add(key)
                parent_dirs.append(parent)
        if not parent_dirs:
            raise HTTPException(status_code=400, detail="item_paths 解析后无有效目录")
        # 反查所属库（用于完成后刷新）—— 用 Jellyfin 原始路径匹配 location
        refresh_ids: List[str] = []
        try:
            client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
            libs = client.get_libraries_normalized()
            for d in parent_dirs:
                d_norm = d.replace('\\', '/').rstrip('/').lower()
                for lib in libs:
                    for loc in lib.get('locations') or []:
                        loc_norm = loc.replace('\\', '/').rstrip('/').lower()
                        if d_norm == loc_norm or d_norm.startswith(loc_norm + '/'):
                            if lib['id'] not in refresh_ids:
                                refresh_ids.append(lib['id'])
                            break
        except Exception as e:
            logger.warning(f"反查 item_paths 所属库失败: {e}")
        # 翻译为后端本地路径再返回
        translated = [_translate(d) for d in parent_dirs]
        return translated, False, f'选中 {len(item_paths)} 个条目', refresh_ids

    # 2. 多库
    if library_ids:
        all_paths: List[str] = []
        for lid in library_ids:
            lib = get_library_by_id(lid)
            if lib and lib.get('locations'):
                all_paths.extend(_translate(loc) for loc in lib['locations'])
        if not all_paths:
            raise HTTPException(status_code=400, detail="所选库均无可用路径")
        return all_paths, None, f'{len(library_ids)} 个库', list(library_ids)

    # 3. 单库
    if library_id:
        lib = get_library_by_id(library_id)
        if not lib:
            raise HTTPException(status_code=404, detail=f"Jellyfin 库不存在: {library_id}")
        if not lib['locations']:
            raise HTTPException(status_code=400, detail=f"库 {lib['name']} 没有配置任何路径")
        translated = [_translate(loc) for loc in lib['locations']]
        return translated, None, f"库 {lib['name']}", [library_id]

    # 4. 手动路径（用户直接输入，假定已是本地路径，不再映射）
    if path:
        if not Path(path).exists():
            raise HTTPException(status_code=400, detail=f"路径不存在: {path}")
        return [path], None, path, []

    # 5. 全库回退
    try:
        client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
        libs = client.get_libraries_normalized()
        all_paths = []
        all_ids = []
        for lib in libs:
            if lib.get('locations'):
                all_paths.extend(_translate(loc) for loc in lib['locations'])
                all_ids.append(lib['id'])
        if not all_paths:
            raise HTTPException(status_code=400, detail="未找到任何可用 Jellyfin 库路径")
        return all_paths, None, '所有库', all_ids
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"展开全库失败: {e}")


def _resolve_paths(path: Optional[str], library_id: Optional[str]) -> List[str]:
    """旧入口（仅 path/library_id 两参数），保留向后兼容。"""
    paths, _, _, _ = _resolve_scope(path=path, library_id=library_id)
    return paths


@router.post("/scan", response_model=TaskStartResponse)
def scan_subtitles(
    request: ScanRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """启动字幕扫描任务（item_paths > library_ids > library_id > path > 全库）"""
    paths, recursive_override, label, scope_lib_ids = _resolve_scope(
        path=request.path,
        library_id=request.library_id,
        library_ids=request.library_ids,
        item_paths=request.item_paths,
    )
    recursive = recursive_override if recursive_override is not None else request.recursive
    expected_langs = request.expected_langs or settings.preferred_langs

    library_ids_list = list(scope_lib_ids or [])
    task = create_task(
        db,
        "subtitle_scan",
        f"扫描: {label}（{len(paths)} 个路径）",
        params={
            "paths": paths,
            "recursive": recursive,
            "expected_langs": expected_langs,
            "library_ids": library_ids_list,
        },
    )

    # _resolve_scope 返回的 lib id 列表 = 该任务覆盖的 jellyfin 库
    # 写到 task.result.library_ids，方便其他 endpoint 按 lib id 复用扫描结果
    background_tasks.add_task(
        run_subtitle_scan,
        task.id,
        paths,
        recursive,
        expected_langs,
        library_ids_list,
    )

    return TaskStartResponse(
        task_id=task.id,
        status="started",
        message=f"扫描任务已启动（{len(paths)} 个路径）",
    )


def _dir_to_dict(d, format_episode, annotations_map: dict, expected_langs: List[str]):
    """
    ScanResult.directories 的单个 DirectoryInfo → 可 JSON 序列化的 dict。

    annotations_map: { normalized_file_path: hardcoded_langs[] }
        把硬字幕标注合并到每个 video.missing_langs 计算里：
        如果某个 lang 已被硬字幕覆盖，就从 missing_langs 移除。
    """
    videos_out = []
    without_required_adjusted = 0
    for v in d.videos:
        v_path_norm = str(v.path).replace('\\', '/').rstrip('/').lower()
        hardcoded = annotations_map.get(v_path_norm) or []
        # 重新计算 missing_langs：扣除硬字幕覆盖的语言
        if hardcoded:
            missing_langs = [lc for lc in (v.missing_langs or []) if lc not in hardcoded]
        else:
            missing_langs = list(v.missing_langs or [])
        if missing_langs:
            without_required_adjusted += 1
        videos_out.append({
            "path": str(v.path),
            "name": v.name,
            "episode": format_episode(v.episode),
            "subtitles": v.subtitles,
            "embedded_langs": v.embedded_langs,
            "hardcoded_langs": hardcoded,
            "missing_langs": missing_langs,
        })
    return {
        "path": str(d.path),
        "name": d.name,
        "media_type": d.media_type,
        "total_videos": d.total_videos,
        "with_subtitles": d.videos_with_sub,
        "without_subtitles": d.videos_without_sub,
        # 缺所需语言的视频数（已应用硬字幕标注扣减）
        "without_required": without_required_adjusted,
        "videos": videos_out,
    }


def _load_annotations_map(db) -> dict:
    """加载所有视频标注：{ normalized_file_path: hardcoded_langs[] }"""
    out = {}
    try:
        for a in db.query(VideoAnnotation).all():
            try:
                langs = json.loads(a.hardcoded_subtitle_langs) if a.hardcoded_subtitle_langs else []
            except Exception:
                langs = []
            if langs:
                out[a.file_path] = langs
    except Exception as e:
        logger.warning(f"加载视频标注失败: {e}")
    return out


@register_resumable("subtitle_scan", ["paths", "recursive", "expected_langs", "library_ids"])
def run_subtitle_scan(
    task_id: int,
    paths: List[str],
    recursive: bool,
    expected_langs: List[str],
    library_ids: Optional[List[str]] = None,
):
    """
    执行字幕扫描（后台任务），支持多路径合并到一个报告。

    扫描中持续把当前累积的 directories 写入 task.result，详情页可直接渲染。
    EMIT_EVERY 控制 DB 写入频率（每 N 个目录扫完写一次）。

    library_ids：该任务覆盖的 Jellyfin 库 ID 列表（写入 result，
    方便 LibraryDetail 等 endpoint 按 lib_id 复用最近扫描结果）。
    """
    from web.backend.database import SessionLocal
    from tools.subtitle_manager.scanner import format_episode

    def _progress(pct, msg, patch=None):
        with SessionLocal() as db:
            update_task_progress(db, task_id, pct, msg, result_patch=patch)

    try:
        scanner = SubtitleScanner(preferred_langs=expected_langs)
        # 短事务读取硬字幕标注
        with SessionLocal() as db:
            annotations_map = _load_annotations_map(db)
        logger.info(f"加载到 {len(annotations_map)} 条硬字幕标注")

        all_dirs = []           # 已完成路径累积的 dict 列表
        total_videos = 0
        total_with = 0
        total_without = 0
        total_without_required = 0
        total_subs = 0
        last_scan_time = ""

        EMIT_EVERY = 5  # 每扫完 5 个目录 emit 一次 partial result

        # 从 dict 列表里聚合 without_required（已扣除硬字幕的版本）
        def _aggregate_without_required(dir_dicts):
            return sum(d.get('without_required', 0) for d in dir_dicts)

        for idx, p in enumerate(paths):
            pct = 5 + int(75 * (idx + 1) / len(paths))
            _progress(pct, f"扫描 [{idx+1}/{len(paths)}] {p}")

            last_emit = [0]  # 当前路径上次 emit 时的目录数

            def _on_progress(partial_result, dirs_done, _idx=idx, _p=p, _pct=pct):
                last_dir_name = (
                    partial_result.directories[-1].name
                    if partial_result.directories else '...'
                )
                if dirs_done - last_emit[0] < EMIT_EVERY:
                    _progress(
                        _pct,
                        f"[{_idx+1}/{len(paths)}] 已扫 {dirs_done} 个目录 · 最近: {last_dir_name}",
                    )
                    return
                last_emit[0] = dirs_done
                # 当前进度快照
                current_dirs = list(all_dirs)
                for d in partial_result.directories:
                    current_dirs.append(_dir_to_dict(d, format_episode, annotations_map, expected_langs))
                _progress(
                    _pct,
                    f"[{_idx+1}/{len(paths)}] 已扫 {dirs_done} 个目录 · 最近: {last_dir_name}",
                    patch={
                        'total_videos': total_videos + partial_result.total_videos,
                        'with_subtitles': total_with + partial_result.total_with_sub,
                        'without_subtitles': total_without + partial_result.total_without_sub,
                        'without_required': _aggregate_without_required(current_dirs),
                        'paths_scanned': _idx,
                        'directories': current_dirs,
                    },
                )

            result = scanner.scan(Path(p), recursive=recursive, progress_cb=_on_progress)
            last_scan_time = result.scan_time
            total_videos += result.total_videos
            total_with += result.total_with_sub
            total_without += result.total_without_sub
            total_subs += result.total_subtitles
            for d in result.directories:
                all_dirs.append(_dir_to_dict(d, format_episode, annotations_map, expected_langs))

        # 应用了硬字幕标注的最终统计：从 all_dirs 里聚合
        total_without_required = _aggregate_without_required(all_dirs)

        _progress(85, "正在生成报告...")

        report_data = {
            "scan_paths": paths,
            "scan_time": last_scan_time,
            "total_directories": len(all_dirs),
            "total_videos": total_videos,
            "with_subtitles": total_with,
            "without_subtitles": total_without,
            "total_subtitles": total_subs,
            "directories": all_dirs,
        }

        # ScanReport + complete_task 写一次短事务
        with SessionLocal() as db:
            report = ScanReport(
                report_type="subtitle",
                scan_path=' | '.join(paths),
                total_items=total_videos,
                issues_count=total_without,
                report_data=json.dumps(report_data, ensure_ascii=False),
            )
            db.add(report)
            db.commit()
            db.refresh(report)

            complete_task(
                db, task_id,
                {
                    "report_id": report.id,
                    "total_videos": total_videos,
                    "with_subtitles": total_with,
                    "without_subtitles": total_without,
                    "without_required": total_without_required,
                    "paths_scanned": len(paths),
                    # 该任务覆盖的 Jellyfin 库 ID 列表（用于其他 endpoint 复用结果）
                    "library_ids": list(library_ids or []),
                    # 直接把全量 directories 写到 task.result，前端不再需要单独拉 ScanReport
                    "directories": all_dirs,
                },
                final_message=(
                    f"扫描完成：{total_videos} 个视频"
                    + (
                        f"，{total_without_required} 个缺所需语言字幕"
                        if total_without_required
                        else "，所需语言字幕都齐全"
                    )
                ),
            )

    except Exception as e:
        logger.exception("字幕扫描任务失败")
        with SessionLocal() as db:
            complete_task(db, task_id, {"error": str(e)}, success=False)


# ---------- Rename ----------

@router.post("/rename", response_model=TaskStartResponse)
def rename_subtitles(
    request: RenameRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """启动字幕重命名任务（item_paths > library_ids > library_id > path > 全库）"""
    paths, recursive_override, label, refresh_ids = _resolve_scope(
        path=request.path,
        library_id=request.library_id,
        library_ids=request.library_ids,
        item_paths=request.item_paths,
    )
    recursive = recursive_override if recursive_override is not None else request.recursive

    mode = "执行" if request.execute else "预览"
    task = create_task(db, "subtitle_rename", f"{mode}重命名: {label}（{len(paths)} 路径）")

    background_tasks.add_task(
        run_subtitle_rename,
        task.id,
        paths,
        recursive,
        request.execute,
        request.force_lang,
        refresh_ids if request.refresh_jellyfin else [],
    )

    return TaskStartResponse(
        task_id=task.id,
        status="started",
        message=f"重命名任务已启动（{mode}模式，{len(paths)} 路径）",
    )


def run_subtitle_rename(
    task_id: int,
    paths: List[str],
    recursive: bool,
    execute: bool,
    force_lang: Optional[str],
    refresh_library_ids: List[str],
):
    """执行字幕重命名（后台任务）。文件重命名 + Jellyfin refresh 期间不持 db。"""
    from web.backend.database import SessionLocal
    from web.backend.api.medialibraries import trigger_refresh

    def _progress(pct, msg, patch=None):
        with SessionLocal() as db:
            update_task_progress(db, task_id, pct, msg, result_patch=patch)

    try:
        renamer = SubtitleRenamer()
        all_results = []

        EMIT_EVERY = 10  # 每发现 10 个 rename pair 就 emit 一次 partial result

        for idx, p in enumerate(paths):
            pct = 5 + int(85 * (idx + 1) / len(paths))
            _progress(pct, f"处理 [{idx+1}/{len(paths)}] {p}")

            last_emit = [0]

            def _on_each(item, _idx=idx, _p=p, _pct=pct):
                # 实时把 result 加到全局
                all_results.append(item)
                # message 实时显示当前正在处理的目录
                _progress(
                    _pct,
                    f"[{_idx+1}/{len(paths)}] 已发现 {len(all_results)} 个待改名 · "
                    f"最近: {item.get('directory') or '...'}",
                )
                # result 写入控频
                if len(all_results) - last_emit[0] >= EMIT_EVERY:
                    last_emit[0] = len(all_results)
                    success_so_far = sum(1 for r in all_results if r.get('success'))
                    _progress(
                        _pct,
                        f"[{_idx+1}/{len(paths)}] 已发现 {len(all_results)} 个待改名",
                        patch={
                            'total': len(all_results),
                            'success': success_so_far,
                            'failed': len(all_results) - success_so_far,
                            'execute': execute,
                            'paths_processed': _idx,
                            'details': all_results[:300],
                        },
                    )

            # process_directory 会自己累积一份 results，但我们用 on_each 也已经累计了
            # 所以这里返回值忽略；不直接 extend 避免重复
            renamer.process_directory(
                Path(p),
                lang=force_lang,
                dry_run=not execute,
                recursive=recursive,
                verbose=False,
                on_each=_on_each,
            )

        success_count = sum(1 for r in all_results if r.get('success'))
        total_count = len(all_results)

        # 实际执行了重命名 → 通知 Jellyfin（精准 path 通知，整库扫兜底）
        refreshed = False
        if execute and success_count > 0:
            _progress(95, "通知 Jellyfin 刷新媒体库...")
            # 收集成功重命名后的字幕新路径（jellyfin 视角）
            new_paths = []
            try:
                from web.backend.path_translator import reverse_translate_path_with_settings
                for r in all_results:
                    if r.get('success') and r.get('new_path'):
                        new_paths.append(
                            reverse_translate_path_with_settings(r['new_path']) or r['new_path']
                        )
            except Exception:
                new_paths = [r['new_path'] for r in all_results
                             if r.get('success') and r.get('new_path')]

            notified_paths = False
            if new_paths:
                try:
                    from common.jellyfin_client import JellyfinClient
                    jf = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
                    if jf.notify_media_updated(new_paths, update_type='Modified'):
                        notified_paths = True
                        refreshed = True
                except Exception as e:
                    logger.warning(f"notify_media_updated 失败: {e}")

            # 精准通知失败才走整库扫兜底
            if not notified_paths and refresh_library_ids:
                for lid in refresh_library_ids:
                    try:
                        trigger_refresh(lid)
                    except Exception as e:
                        logger.warning(f"刷新库 {lid} 失败: {e}")
                refreshed = True

        # 最终消息：区分预览和执行
        if total_count == 0:
            final_msg = (
                f"{'预览' if not execute else '执行'}完成：未发现需要对齐的字幕"
            )
        elif execute:
            final_msg = f"重命名完成：成功 {success_count} / 共 {total_count}"
        else:
            final_msg = f"预览完成：发现 {total_count} 个待改名字幕（未实际改名）"

        with SessionLocal() as db:
            complete_task(
                db, task_id,
                {
                    "total": total_count,
                    "success": success_count,
                    "failed": total_count - success_count,
                    "execute": execute,
                    "paths_processed": len(paths),
                    "jellyfin_refreshed": refreshed,
                    "details": all_results[:300],
                },
                final_message=final_msg,
            )

    except Exception as e:
        logger.exception("字幕重命名任务失败")
        with SessionLocal() as db:
            complete_task(db, task_id, {"error": str(e)}, success=False)


# ---------- Download ----------

@router.post("/download", response_model=TaskStartResponse)
def download_subtitles(
    request: DownloadRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """根据扫描报告下载缺失字幕。按 subtitle.sources 优先级回退多个源。"""
    if not _any_subtitle_source_ready():
        raise HTTPException(
            status_code=400,
            detail="未配置任何可用字幕源（请在配置页填 OpenSubtitles API Key 或 assrt API Token）",
        )

    report = db.query(ScanReport).filter(ScanReport.id == request.report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail=f"扫描报告不存在: id={request.report_id}")
    if report.report_type != "subtitle":
        raise HTTPException(status_code=400, detail="该报告不是字幕扫描报告")

    mode = "预览" if request.dry_run else "执行"
    task = create_task(
        db,
        "subtitle_download",
        f"{mode}下载: 报告#{request.report_id}",
        params={
            "report_id": request.report_id,
            "languages": request.languages,
            "dry_run": request.dry_run,
            "limit": request.limit,
        },
    )

    background_tasks.add_task(
        run_subtitle_download,
        task.id,
        request.report_id,
        request.languages,
        request.dry_run,
        request.limit,
    )

    return TaskStartResponse(
        task_id=task.id,
        status="started",
        message=f"下载任务已启动（{mode}模式）",
    )


@register_resumable("subtitle_download", ["report_id", "languages", "dry_run", "limit"])
def run_subtitle_download(
    task_id: int,
    report_id: int,
    languages: Optional[List[str]],
    dry_run: bool,
    limit: Optional[int],
):
    """执行字幕下载（后台任务）。OpenSubtitles HTTP 期间不持 db。"""
    from web.backend.database import SessionLocal

    def _progress_msg(pct, msg):
        with SessionLocal() as db:
            update_task_progress(db, task_id, pct, msg)

    try:
        _progress_msg(5, "正在加载报告...")

        # 短事务读取扫描报告
        with SessionLocal() as db:
            report = db.query(ScanReport).filter(ScanReport.id == report_id).first()
            if not report:
                complete_task(db, task_id, {"error": "报告不存在"}, success=False)
                return
            report_data = json.loads(report.report_data) if report.report_data else {}

        videos = SubtitleDownloader.collect_videos_from_report(report_data)
        if limit:
            videos = videos[:limit]

        if not videos:
            with SessionLocal() as db:
                complete_task(db, task_id, {
                    "total": 0,
                    "success": 0,
                    "failed": 0,
                    "skipped": 0,
                    "dry_run": dry_run,
                    "details": [],
                    "message": "没有需要下载字幕的视频",
                })
            return

        _progress_msg(10, f"准备下载 {len(videos)} 个视频的字幕...")

        downloader = SubtitleDownloader(settings.to_dict())

        def _on_each(idx, total, item):
            # 进度从 10% 推进到 95%
            pct = 10 + int(85 * idx / max(total, 1))
            msg = f"[{idx}/{total}] {item.get('video', '')} - {item.get('status', '')}"
            _progress_msg(pct, msg)

        details = downloader.process_videos(
            videos,
            languages=languages,
            dry_run=dry_run,
            progress_cb=_on_each,
        )

        with SessionLocal() as db:
            complete_task(db, task_id, {
                "total": downloader.stats['total'],
                "success": downloader.stats['success'],
                "failed": downloader.stats['failed'],
                "skipped": downloader.stats['skipped'],
                "dry_run": dry_run,
                "details": details[:500],  # 保留前 500 条
            })

    except ValueError as e:
        logger.error(f"字幕下载配置错误: {e}")
        with SessionLocal() as db:
            complete_task(db, task_id, {"error": str(e)}, success=False)
    except Exception as e:
        logger.exception("字幕下载任务失败")
        with SessionLocal() as db:
            complete_task(db, task_id, {"error": str(e)}, success=False)


# ---------- Auto-fix（一条龙）----------

@router.post("/auto-fix", response_model=TaskStartResponse)
def auto_fix_subtitles(
    request: AutoFixRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    自动字幕修复：
      1) 扫描指定路径/库（含 mkv 内嵌检测 + 裸名字幕内容识别）
      2) 对每个视频按自身缺失语言走 subtitle.sources 链下载（assrt / OpenSubtitles 兜底）
      3) （可选）对齐字幕文件名为 video.{lang}.{ext}
      4) （可选）通知 Jellyfin 重扫媒体库
    """
    if not request.dry_run and not _any_subtitle_source_ready():
        raise HTTPException(
            status_code=400,
            detail="未配置任何可用字幕源（请在配置页填 OpenSubtitles API Key 或 assrt API Token）",
        )

    paths, recursive_override, label, refresh_ids = _resolve_scope(
        path=request.path,
        library_id=request.library_id,
        library_ids=request.library_ids,
        item_paths=request.item_paths,
    )
    recursive = recursive_override if recursive_override is not None else request.recursive
    expected_langs = request.expected_langs or settings.preferred_langs

    mode = "预览" if request.dry_run else "执行"
    task = create_task(db, "subtitle_auto_fix", f"{mode}自动修复: {label}（{len(paths)} 路径）")

    background_tasks.add_task(
        run_subtitle_auto_fix,
        task.id,
        paths,
        recursive,
        expected_langs,
        request.dry_run,
        request.rename,
        refresh_ids if request.refresh_jellyfin else [],
        request.limit,
    )

    return TaskStartResponse(
        task_id=task.id,
        status="started",
        message=f"自动修复任务已启动（{mode}模式，{len(paths)} 路径）",
    )


def run_subtitle_auto_fix_inline(
    paths: List[str],
    recursive: bool = False,
    expected_langs: Optional[List[str]] = None,
    dry_run: bool = True,
    do_rename: bool = True,
    refresh_library_ids: Optional[List[str]] = None,
    limit: Optional[int] = None,
    progress_cb: Optional[callable] = None,
) -> Dict:
    """
    同步执行字幕 auto-fix 全流程，返回结果 dict —— **不写 task / 不写 ScanReport**。

    供 dispatch pipeline / 其他后端模块直接 import 使用。

    paths 支持两种形态混用：
      - 目录路径：scanner 按目录扫
      - 文件路径：自动转父目录扫描，且**仅处理白名单内的视频**
        （剧集场景关键：dispatched_files=['/library/.../S01E12.mkv'] 只动 E12，
         不会因为父目录里有 E01-E11 而把已有旧集也重处理）

    progress_cb(pct: int, msg: str) 进度回调，可选。

    返回:
    {
      "scan": {total_videos, with_subtitles, without_subtitles, total_subtitles},
      "download": {total, success, failed, skipped},
      "rename": {total, success},
      "dry_run": bool,
      "jellyfin_refreshed": bool,
      "download_details": [...],
      "rename_details": [...],
      "report_data": <full nested dirs>,   # 已按白名单过滤
      "error": Optional[str],
    }
    """
    from tools.subtitle_manager.scanner import format_episode
    from web.backend.api.medialibraries import trigger_refresh

    if expected_langs is None:
        expected_langs = settings.preferred_langs
    if refresh_library_ids is None:
        refresh_library_ids = []

    def _emit(pct, msg):
        if progress_cb:
            try:
                progress_cb(pct, msg)
            except Exception:
                logger.exception("progress_cb 抛错，已忽略")

    # ---- 文件 / 目录 paths 归一化 ----
    # 文件路径 → 转父目录扫描 + 加入 file_whitelist
    # 同一父目录的多个文件去重；目录路径原样保留
    def _norm(p: str) -> str:
        return str(Path(p).resolve()).replace('\\', '/').lower()

    scan_dirs: List[str] = []
    seen_dirs: set = set()
    file_whitelist: Optional[set] = None  # None = 不过滤；非空 = 仅保留白名单内的视频
    for p in paths:
        pp = Path(p)
        try:
            is_file = pp.is_file()
        except OSError:
            is_file = False
        if is_file:
            parent = str(pp.parent)
            key = _norm(parent)
            if key not in seen_dirs:
                seen_dirs.add(key)
                scan_dirs.append(parent)
            if file_whitelist is None:
                file_whitelist = set()
            file_whitelist.add(_norm(str(pp)))
        else:
            key = _norm(p)
            if key not in seen_dirs:
                seen_dirs.add(key)
                scan_dirs.append(p)

    if not scan_dirs:
        scan_dirs = paths  # 兜底（即使路径不存在）

    # ============ Step 1: 扫描 ============
    _emit(2, "开始扫描...")
    scanner = SubtitleScanner(preferred_langs=expected_langs)

    all_dirs = []
    total_videos = 0
    total_with = 0
    total_without = 0
    total_subs = 0
    last_scan_time = ""

    for idx, p in enumerate(scan_dirs):
        pct = 2 + int(28 * (idx + 1) / max(len(scan_dirs), 1))  # 扫描占 2%-30%
        _emit(pct, f"扫描 [{idx+1}/{len(scan_dirs)}] {p}")
        result = scanner.scan(Path(p), recursive=recursive)
        last_scan_time = result.scan_time

        for d in result.directories:
            videos_filtered = []
            for v in d.videos:
                # 文件白名单过滤：只保留 dispatched_files 指定的视频
                if file_whitelist is not None and _norm(str(v.path)) not in file_whitelist:
                    continue
                videos_filtered.append({
                    "path": str(v.path),
                    "name": v.name,
                    "episode": format_episode(v.episode),
                    "subtitles": v.subtitles,
                    "embedded_langs": v.embedded_langs,
                    "missing_langs": v.missing_langs,
                })
            if not videos_filtered:
                continue

            with_sub = sum(1 for vd in videos_filtered if vd['subtitles'] or vd['embedded_langs'])
            without_sub = len(videos_filtered) - with_sub

            total_videos += len(videos_filtered)
            total_with += with_sub
            total_without += without_sub
            total_subs += sum(len(vd['subtitles']) for vd in videos_filtered)

            all_dirs.append({
                "path": str(d.path),
                "name": d.name,
                "media_type": d.media_type,
                "total_videos": len(videos_filtered),
                "with_subtitles": with_sub,
                "without_subtitles": without_sub,
                "videos": videos_filtered,
            })

    report_data = {
        "scan_paths": paths,
        "scan_time": last_scan_time,
        "total_directories": len(all_dirs),
        "total_videos": total_videos,
        "with_subtitles": total_with,
        "without_subtitles": total_without,
        "total_subtitles": total_subs,
        "directories": all_dirs,
    }

    # ============ Step 2: 下载缺失字幕（按 per-video missing_langs）============
    _emit(32, "开始下载缺失字幕...")

    download_details: List[Dict] = []
    download_stats = {"total": 0, "success": 0, "failed": 0, "skipped": 0}
    init_error: Optional[str] = None

    try:
        downloader = SubtitleDownloader(settings.to_dict())
        targets = downloader.collect_targets_from_report(report_data)
        if limit:
            targets = targets[:limit]

        if targets:
            # 简化 report：仅保留 targets 视频路径供 auto_fix_from_report 复用
            fake_dirs = [{
                'videos': [
                    {'path': str(t['path']), 'missing_langs': t['missing_langs']}
                    for t in targets
                ],
            }]
            trimmed_report = dict(report_data)
            trimmed_report['directories'] = fake_dirs

            def _dl_progress(idx, total, item):
                pct = 32 + int(48 * idx / max(total, 1))  # 下载占 32%-80%
                msg = f"下载 [{idx}/{total}] {item.get('video', '')} - {item.get('status', '')}"
                _emit(pct, msg)

            download_details = downloader.auto_fix_from_report(
                trimmed_report,
                dry_run=dry_run,
                progress_cb=_dl_progress,
            )
            download_stats = dict(downloader.stats)
        else:
            _emit(80, "无缺失字幕，跳过下载")
    except ValueError as e:
        # API key 等配置错误：dry_run 模式不致命
        logger.warning(f"字幕下载初始化失败: {e}（dry_run={dry_run}）")
        init_error = str(e)
        if not dry_run:
            return {
                "error": init_error,
                "scan": {"total_videos": total_videos,
                         "with_subtitles": total_with,
                         "without_subtitles": total_without,
                         "total_subtitles": total_subs},
                "download": download_stats,
                "rename": {"total": 0, "success": 0},
                "dry_run": dry_run,
                "jellyfin_refreshed": False,
                "download_details": [],
                "rename_details": [],
                "report_data": report_data,
            }

    # ============ Step 3: 重命名对齐（裸名字幕识别语言后改名）============
    rename_details: List[Dict] = []
    if do_rename:
        _emit(82, "对齐字幕文件名...")
        try:
            renamer = SubtitleRenamer()
            for p in paths:
                rename_details.extend(
                    renamer.process_directory(
                        Path(p),
                        lang=None,
                        dry_run=dry_run,
                        recursive=recursive,
                        verbose=False,
                    )
                )
        except Exception as e:
            logger.warning(f"重命名步骤异常: {e}")

    # ============ Step 4: 通知 Jellyfin（精准 path 通知，整库扫兜底）============
    refreshed = False
    if not dry_run:
        actually_changed = (
            download_stats.get("success", 0) > 0
            or any(r.get("success") for r in rename_details)
        )
        if actually_changed:
            _emit(95, "通知 Jellyfin 刷新媒体库...")
            # 收集涉及的视频路径（jellyfin 视角）
            #   - download_details[i].video_path：新增字幕对应的视频
            #   - rename_details[i].new_path：改名的字幕文件本身
            from web.backend.path_translator import reverse_translate_path_with_settings
            paths_for_jf = set()
            for d in (download_details or []):
                if d.get('status') in ('success',) and d.get('video_path'):
                    paths_for_jf.add(reverse_translate_path_with_settings(d['video_path']) or d['video_path'])
            for r in (rename_details or []):
                if r.get('success') and r.get('new_path'):
                    paths_for_jf.add(reverse_translate_path_with_settings(r['new_path']) or r['new_path'])

            notified_paths = False
            if paths_for_jf:
                try:
                    from common.jellyfin_client import JellyfinClient
                    jf = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
                    if jf.notify_media_updated(list(paths_for_jf), update_type='Modified'):
                        notified_paths = True
                        refreshed = True
                except Exception as e:
                    logger.warning(f"notify_media_updated 失败: {e}")

            if not notified_paths and refresh_library_ids:
                for lid in refresh_library_ids:
                    try:
                        trigger_refresh(lid)
                    except Exception as e:
                        logger.warning(f"刷新库 {lid} 失败: {e}")
                refreshed = True

    return {
        "scan": {
            "total_videos": total_videos,
            "with_subtitles": total_with,
            "without_subtitles": total_without,
            "total_subtitles": total_subs,
        },
        "download": download_stats,
        "rename": {
            "total": len(rename_details),
            "success": sum(1 for r in rename_details if r.get("success")),
        },
        "dry_run": dry_run,
        "jellyfin_refreshed": refreshed,
        "download_details": download_details,
        "rename_details": rename_details,
        "report_data": report_data,
    }


def run_subtitle_auto_fix(
    task_id: int,
    paths: List[str],
    recursive: bool,
    expected_langs: List[str],
    dry_run: bool,
    do_rename: bool,
    refresh_library_ids: List[str],
    limit: Optional[int],
):
    """auto-fix 后台任务（task wrapper）：调 inline + 写 ScanReport + complete_task。"""
    from web.backend.database import SessionLocal

    def _progress(pct, msg):
        with SessionLocal() as db:
            update_task_progress(db, task_id, pct, msg)

    try:
        result = run_subtitle_auto_fix_inline(
            paths=paths,
            recursive=recursive,
            expected_langs=expected_langs,
            dry_run=dry_run,
            do_rename=do_rename,
            refresh_library_ids=refresh_library_ids,
            limit=limit,
            progress_cb=_progress,
        )

        if result.get("error") and not dry_run:
            with SessionLocal() as db:
                complete_task(db, task_id, {"error": result["error"]}, success=False)
            return

        # 持久化 scan report + 完成 task（短事务）
        report_data = result["report_data"]
        with SessionLocal() as db:
            report = ScanReport(
                report_type="subtitle",
                scan_path=' | '.join(paths),
                total_items=report_data["total_videos"],
                issues_count=report_data["without_subtitles"],
                report_data=json.dumps(report_data, ensure_ascii=False),
            )
            db.add(report)
            db.commit()
            db.refresh(report)

            complete_task(db, task_id, {
                "report_id": report.id,
                "scan": result["scan"],
                "download": result["download"],
                "rename": result["rename"],
                "dry_run": result["dry_run"],
                "jellyfin_refreshed": result["jellyfin_refreshed"],
                "download_details": result["download_details"][:200],
                "rename_details": result["rename_details"][:200],
            })

    except Exception as e:
        logger.exception("auto-fix 任务失败")
        with SessionLocal() as db:
            complete_task(db, task_id, {"error": str(e)}, success=False)


# ---------- 射手字幕 assrt.net ----------
# 与 OpenSubtitles 不同，assrt 走 "搜索 → 选择 → 下载（含解压）" 三步交互。
# 这里同时支持：自动模式（一步到位 by video_path）+ 手动模式（前端给 id）。

from tools.subtitle_downloader import assrt as _assrt


class AssrtSearchRequest(BaseModel):
    query: Optional[str] = None        # 关键词（≥3 字符）
    video_path: Optional[str] = None   # 给视频路径时自动用文件名（is_file=1）
    is_file: bool = False              # 强制按文件名搜
    cnt: int = 15
    pos: int = 0
    no_muxer: bool = False
    with_filelist: bool = False


class AssrtDownloadRequest(BaseModel):
    sub_id: int                        # 字幕 ID
    video_path: str                    # 目标视频绝对路径（落盘用）
    preferred_langs: Optional[List[str]] = None  # None → settings.preferred_langs
    overwrite: bool = False


def _get_assrt_client() -> "_assrt.AssrtClient":
    if not settings.assrt_api_token:
        raise HTTPException(
            status_code=400,
            detail="未配置 assrt API Token，请在 config.yaml 中设置 subtitle.assrt_api_token",
        )
    return _assrt.AssrtClient(
        token=settings.assrt_api_token,
        request_delay=settings.assrt_request_delay,
    )


def _flatten_sub(sub: Dict) -> Dict:
    """assrt sub 对象 → 前端友好结构。"""
    lang_block = sub.get('lang') or {}
    lang_codes = _assrt.parse_langlist(
        lang_block.get('langlist'), lang_block.get('desc') or ''
    )
    return {
        'id': sub.get('id'),
        'native_name': sub.get('native_name'),
        'videoname': sub.get('videoname'),
        'title': sub.get('title'),
        'subtype': sub.get('subtype'),
        'release_site': sub.get('release_site'),
        'vote_score': sub.get('vote_score'),
        'view_count': sub.get('view_count'),
        'down_count': sub.get('down_count'),
        'upload_time': sub.get('upload_time'),
        'revision': sub.get('revision'),
        'size': sub.get('size'),
        'filename': sub.get('filename'),
        'lang_desc': lang_block.get('desc'),
        'lang_codes': lang_codes,
        'filelist': sub.get('filelist'),  # 仅 with_filelist 时有
    }


class MultiSearchRequest(BaseModel):
    # query 必给：用户输入的搜索词
    query: str
    # video_path 可选：仅当用户希望基于文件 stem 自动搜（OpenSubtitles 还会用它推断 year/season/episode）
    video_path: Optional[str] = None
    # 外部 ID（OpenSubtitles 优先用，命中精度远高于 query；同时用于打分匹配 +60）
    imdb_id: Optional[str] = None
    tmdb_id: Optional[str] = None
    # jellyfin 的 ProductionYear（用于打分 +20，胜过从 release 名 regex 拆出的不可靠 year）
    year: Optional[int] = None
    sources: Optional[List[str]] = None  # 默认全部启用源；可指定 ['assrt', 'opensubtitles']
    cnt: int = 15
    preferred_langs: Optional[List[str]] = None


@router.post("/multi-search")
def multi_search(request: MultiSearchRequest):
    """
    多源聚合搜字幕。当前覆盖：assrt（按文件名搜）+ opensubtitles（按 query 搜）。
    Shooter 是文件 hash 命中机制，不接入这个"用户输入搜词"的入口。

    搜索阶段**只用 query**，不需要 video_path 真实存在。
    video_path 仅作为 OpenSubtitles 提取 year/episode_number 等附加 hint（可空）。
    每条结果带 source 字段，前端下载时根据 source 路由到对应 download endpoint。
    """
    query = (request.query or '').strip()
    if len(query) < 3:
        raise HTTPException(status_code=400, detail="query 长度需 ≥ 3 字符")

    # video_path 可选：用于 OS 推断 episode/year（以及未来 hash 命中）
    # 翻译路径但不强制校验文件存在 —— 搜索本身不读文件
    video_path: Optional[Path] = None
    if request.video_path:
        from web.backend.path_translator import translate_path_with_settings
        local = translate_path_with_settings(request.video_path) or request.video_path
        video_path = Path(local)

    # 默认启用 assrt + opensubtitles（按 settings.subtitle_sources 中已启用的）
    enabled = set(request.sources or [])
    if not enabled:
        for entry in (settings.subtitle_sources or []):
            name = entry.get('name')
            if name in ('assrt', 'opensubtitles') and entry.get('enabled', True):
                enabled.add(name)
        if not enabled:
            enabled = {'assrt', 'opensubtitles'}

    preferred = request.preferred_langs or settings.preferred_langs or ['chs', 'eng']

    # 构造 video info（用于跨源 Bazarr 风格打分）
    # release tag 从 video_path stem 解析（resolution / source / release_group / SxxExx）；
    # year / imdb_id / tmdb_id 优先用 jellyfin 元数据透传的（精度远高于 release 名 regex）
    from tools.subtitle_downloader.scoring import (
        SubtitleCandidate, VideoInfo, score_candidate,
    )
    video_info = VideoInfo()
    if video_path:
        video_info = VideoInfo.from_filename(video_path.name)
    if request.imdb_id:
        video_info.imdb_id = str(request.imdb_id).lower().lstrip('t')
    if request.tmdb_id:
        video_info.tmdb_id = str(request.tmdb_id)
    if request.year:
        video_info.year = int(request.year)

    # 收集打分前的"原始候选 + 输出 dict"对，最后统一打分排序后再返回
    # raw_pairs: List[(out_dict, SubtitleCandidate)]
    raw_pairs: List[tuple] = []

    # ---- assrt ----
    if 'assrt' in enabled:
        try:
            client = _get_assrt_client()
            # is_file=True 让 assrt 按 release 文件名匹配；query 当短名时关掉 is_file
            looks_like_release = '.' in query or '_' in query or '-' in query
            # with_filelist=True 让响应包含 filelist（字幕包内的 .srt/.ass 文件清单）
            # —— 用户最关心的就是这个，方便判断是否对得上视频版本
            subs = client.search(
                query, is_file=looks_like_release, cnt=request.cnt,
                with_filelist=True,
            )
            for s in subs:
                flat = _flatten_sub(s)
                # 优先显示包内首个真实字幕文件名（如 movie.chs.srt）；没有再退到 sub.filename（包名）
                filelist = flat.get('filelist') or []
                first_in_pack = filelist[0].get('f') if filelist and isinstance(filelist[0], dict) else None
                display_filename = first_in_pack or flat['filename']

                # 包装成 SubtitleCandidate 供 score_candidate 打分
                # release_name：assrt 没单独字段，用 videoname / title / 包内首个文件名兜底
                release_name = (
                    flat.get('videoname')
                    or display_filename
                    or flat.get('native_name')
                    or flat.get('title')
                    or ''
                )
                # assrt 的 lang_codes 可能是 ['chs','eng']（双语）；打分时取首个，
                # lang_match 已在 score_candidate 内按 preferred 序处理
                cand_lang = (flat.get('lang_codes') or [None])[0] or ''
                cand = SubtitleCandidate(
                    source='assrt',
                    raw=flat,
                    language=cand_lang,
                    release_name=release_name,
                    hash_match=False,                # assrt 不是 hash 匹配
                    # 不传 vote —— 投票数据稀疏（多数 0），用进打分会误导
                )
                # 集号 / 年份从 release_name 解析
                rel_info = VideoInfo.from_filename(release_name)
                cand.season = rel_info.season
                cand.episode = rel_info.episode
                cand.year = rel_info.year

                out_item = {
                    'source': 'assrt',
                    'id': str(flat['id']) if flat['id'] else '',
                    'title': flat['native_name'] or flat['videoname'] or flat['title'],
                    'filename': display_filename,
                    'filelist': [f.get('f') for f in filelist if isinstance(f, dict) and f.get('f')],
                    'lang_codes': flat['lang_codes'],
                    'download_count': flat['down_count'],
                    'upload_time': flat['upload_time'],
                    'subtype': flat['subtype'],
                    'release_site': flat['release_site'],
                    'raw': flat,
                }
                raw_pairs.append((out_item, cand))
        except _assrt.AssrtRateLimitError as e:
            logger.warning(f"[multi-search] assrt 配额超限: {e}")
        except Exception as e:
            logger.warning(f"[multi-search] assrt 搜索异常: {e}")

    # ---- opensubtitles ----
    if 'opensubtitles' in enabled and settings.opensubtitles_api_key:
        try:
            from tools.subtitle_downloader.opensubtitles import OpenSubtitlesClient
            os_client = OpenSubtitlesClient(
                api_key=settings.opensubtitles_api_key,
                username=settings.opensubtitles_username,
                password=settings.opensubtitles_password,
                request_delay=settings.opensubtitles_request_delay,
            )
            # OpenSubtitles 优先 ID 搜（精度远高于 query）；都没给才退回 query
            results = os_client.search(
                video_path=video_path,
                languages=preferred,
                imdb_id=request.imdb_id,
                tmdb_id=request.tmdb_id,
                query=query if not (request.imdb_id or request.tmdb_id) else None,
            )
            _OS_LANG = {
                'zh-cn': 'chs', 'zh-tw': 'cht', 'en': 'eng',
                'ja': 'jpn', 'ko': 'kor',
            }
            # OS 服务端无总条数限制；前端 cnt 限制在我们这里截断 —— 但截断之前
            # 先按下载量预排序，否则 OS 默认顺序可能把高质量字幕放后面，被截掉。
            results.sort(
                key=lambda r: -int((r.get('attributes') or {}).get('download_count') or 0),
            )
            # 截断：取每语言 top N（防止 50 条都是 eng 把 chs 挤到 cnt 之外）
            #   每语言至少保留 cnt//3 条，全集合最多 cnt*2 条进打分阶段；
            #   最终排序后 [:cnt] 截给前端
            from collections import defaultdict
            per_lang_cap = max(request.cnt // 3, 5)
            per_lang_count: Dict[str, int] = defaultdict(int)
            kept = []
            for r in results:
                lang = ((r.get('attributes') or {}).get('language') or '').lower()
                code = _OS_LANG.get(lang, lang[:5])
                if per_lang_count[code] >= per_lang_cap:
                    continue
                per_lang_count[code] += 1
                kept.append(r)
                if len(kept) >= request.cnt * 3:
                    break

            for r in kept:
                attrs = r.get('attributes') or {}
                files = attrs.get('files') or []
                file0 = files[0] if files else {}
                lang = attrs.get('language') or ''
                lang_code = _OS_LANG.get(lang.lower(), lang.lower()[:5])

                # 构造 SubtitleCandidate
                release_name = attrs.get('release') or ''
                fd = attrs.get('feature_details') or {}
                cand = SubtitleCandidate(
                    source='opensubtitles',
                    raw=r,
                    language=lang_code,
                    release_name=release_name,
                    hash_match=bool(attrs.get('moviehash_match')),  # OS 有 hash 匹配标志
                    imdb_id=str(fd.get('imdb_id') or '').lower().lstrip('t'),
                    tmdb_id=str(fd.get('tmdb_id') or ''),
                    season=fd.get('season_number'),
                    episode=fd.get('episode_number'),
                    year=fd.get('year'),
                    # 不传 vote —— 数据稀疏（多数 votes=0 → ratings 默认 0.0）会误导评分
                )

                out_item = {
                    'source': 'opensubtitles',
                    'id': str(file0.get('file_id') or r.get('id') or ''),
                    'title': release_name or fd.get('title'),
                    'filename': file0.get('file_name'),
                    'lang_codes': [lang_code] if lang_code else [],
                    'download_count': attrs.get('download_count'),
                    'upload_time': attrs.get('upload_date'),
                    'raw': r,
                }
                raw_pairs.append((out_item, cand))
        except Exception as e:
            logger.warning(f"[multi-search] opensubtitles 搜索异常: {e}")

    # ---- 统一打分（Bazarr 风格 SubtitleCandidate.score / score_breakdown）----
    # 各源候选混在一起，按 score 降序；同分按 download_count 降序兜底
    #
    # 100 分制换算基准 = 280：
    #   电影完美匹配 chs/eng（imdb 60 + tmdb 60 + group 50 + res 30 + src 25 + year 20 + lang 40~50）≈ 285~295 → cap 100
    #   剧集完美匹配（含 episode +15）≈ 300+ → cap 100
    #   仅有 ID 匹配 + lang chs +40（160）= 57；仅 release_group + 分辨率 + lang chs（120）= 43
    #   命中 hash（额外 +100）→ 仍 cap 100
    SCORE_NORMALIZE_BASE = 280
    for out_item, cand in raw_pairs:
        score_candidate(cand, video_info, preferred)
        out_item['score'] = cand.score
        out_item['score_breakdown'] = cand.score_breakdown
        out_item['score_pct'] = min(100, round(cand.score / SCORE_NORMALIZE_BASE * 100))

    raw_pairs.sort(key=lambda p: (
        -p[0]['score'],
        -int(p[0].get('download_count') or 0),
    ))

    out = [pair[0] for pair in raw_pairs[:request.cnt]]

    return {
        'video': str(video_path) if video_path else None,
        'video_info': {
            'release_group': video_info.release_group or None,
            'resolution': video_info.resolution or None,
            'source': video_info.source or None,
            'year': video_info.year,
            'season': video_info.season,
            'episode': video_info.episode,
            'imdb_id': video_info.imdb_id or None,
            'tmdb_id': video_info.tmdb_id or None,
        },
        'sources_used': sorted(enabled),
        'count': len(out),
        'results': out,
    }


class MultiDownloadRequest(BaseModel):
    source: str           # 'assrt' / 'opensubtitles'
    sub_id: str           # 各源各自的 ID（assrt 是 sub.id；opensubtitles 是 file_id）
    video_path: str
    preferred_langs: Optional[List[str]] = None
    overwrite: bool = False


@router.post("/multi-download")
def multi_download(request: MultiDownloadRequest):
    """统一下载入口：按 source 路由到对应实现。"""
    if request.source == 'assrt':
        # 路径翻译：前端传 jellyfin 视角路径，转成本机
        from web.backend.path_translator import translate_path_with_settings
        local_path = translate_path_with_settings(request.video_path) or request.video_path
        # 直接复用现有 assrt_download 逻辑（AssrtDownloadRequest 同款字段）
        sub = AssrtDownloadRequest(
            sub_id=int(request.sub_id),
            video_path=local_path,
            preferred_langs=request.preferred_langs,
            overwrite=request.overwrite,
        )
        return assrt_download(sub)
    if request.source == 'opensubtitles':
        if not settings.opensubtitles_api_key:
            raise HTTPException(status_code=400, detail="未配置 opensubtitles api_key")
        from tools.subtitle_downloader.opensubtitles import OpenSubtitlesClient
        from web.backend.path_translator import translate_path_with_settings
        client = OpenSubtitlesClient(
            api_key=settings.opensubtitles_api_key,
            username=settings.opensubtitles_username,
            password=settings.opensubtitles_password,
            request_delay=settings.opensubtitles_request_delay,
        )
        local_path = translate_path_with_settings(request.video_path) or request.video_path
        video_path = Path(local_path)
        try:
            file_id = int(request.sub_id)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"opensubtitles sub_id 应为整数: {request.sub_id}")
        # 选语言：用文件信息推断 / preferred 第一个
        preferred = request.preferred_langs or settings.preferred_langs or ['chs']
        lang = preferred[0] if preferred else 'chs'
        # 输出名 <stem>.<lang>.srt（OpenSubtitles 多数字幕是 .srt）
        target = video_path.parent / f"{video_path.stem}.{lang}.srt"
        if target.exists() and not request.overwrite:
            return {
                'status': 'exists',
                'message': f'已存在: {target.name}',
                'saved_files': [{'path': str(target), 'lang': lang}],
            }
        try:
            ok = client.download(file_id, target)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"下载失败: {e}")
        if not ok:
            raise HTTPException(status_code=502, detail="下载请求未成功")
        return {
            'status': 'success',
            'saved_files': [{'path': str(target), 'lang': lang}],
        }
    raise HTTPException(status_code=400, detail=f"未知 source: {request.source}")


@router.post("/assrt/search")
def assrt_search(request: AssrtSearchRequest):
    """搜索 assrt 字幕。query 与 video_path 至少给一个。"""
    query = (request.query or '').strip()
    is_file = request.is_file
    if request.video_path and not query:
        # 拿视频文件名（不带扩展）作为 query，并强制按文件名搜
        query = Path(request.video_path).stem
        is_file = True
    if not query or len(query) < 3:
        raise HTTPException(status_code=400, detail="query 长度需 >= 3 字符")

    client = _get_assrt_client()
    try:
        subs = client.search(
            query,
            cnt=request.cnt,
            pos=request.pos,
            is_file=is_file,
            no_muxer=request.no_muxer,
            with_filelist=request.with_filelist,
        )
    except _assrt.AssrtRateLimitError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except _assrt.AssrtError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {
        'query': query,
        'is_file': is_file,
        'count': len(subs),
        'results': [_flatten_sub(s) for s in subs],
    }


@router.post("/assrt/download")
def assrt_download(request: AssrtDownloadRequest):
    """
    下载指定 id 的字幕，自动解压并落到视频同目录。
    命名约定：<video_stem>.<lang>.<ext>
    """
    video_path = Path(request.video_path)
    if not video_path.parent.exists():
        raise HTTPException(status_code=400, detail=f"视频所在目录不存在: {video_path.parent}")

    preferred = request.preferred_langs or settings.preferred_langs
    preferred_formats = settings.preferred_subtitle_formats

    client = _get_assrt_client()

    # 1. 取详情拿临时下载 url
    try:
        detail = client.detail(request.sub_id)
    except _assrt.AssrtRateLimitError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except _assrt.AssrtError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if not detail:
        raise HTTPException(status_code=404, detail=f"字幕不存在: id={request.sub_id}")
    download_url = detail.get('url')
    if not download_url:
        raise HTTPException(status_code=502, detail="详情未返回下载 URL")

    # 2. 拉字节
    try:
        content, ext = client.fetch_archive(download_url)
    except _assrt.AssrtError as e:
        raise HTTPException(status_code=502, detail=str(e))

    archive_filename = detail.get('filename') or f"assrt_{request.sub_id}{ext}"
    saved_files: List[dict] = []

    # 3a. 直接是字幕文件 → 直接落地
    if ext.lower() in _assrt.SUB_EXTENSIONS:
        lang = _assrt.guess_lang_from_filename(archive_filename) or (preferred[0] if preferred else 'chs')
        target = video_path.parent / f"{video_path.stem}.{lang}{ext.lower()}"
        if target.exists() and not request.overwrite:
            return {
                'status': 'exists',
                'message': f'已存在: {target.name}',
                'saved_files': [{'path': str(target), 'lang': lang}],
                'sub': _flatten_sub(detail),
            }
        was_existing = target.exists()
        target.write_bytes(content)
        saved_files.append({'path': str(target), 'lang': lang, 'overwritten': was_existing})
        return {
            'status': 'success',
            'saved_files': saved_files,
            'sub': _flatten_sub(detail),
        }

    # 3b. 压缩包 → 解压后挑最匹配的
    files = _assrt.extract_subtitles_from_archive(content, ext)
    if not files:
        # 不支持的压缩格式（rar/7z）—— 把原始包落地，提示用户手动处理
        archive_target = video_path.parent / f"{video_path.stem}.assrt{ext or '.bin'}"
        archive_target.write_bytes(content)
        return {
            'status': 'archive_unhandled',
            'message': f'未识别的压缩格式（{ext or "unknown"}），原始包已保存为 {archive_target.name}，请手动解压',
            'archive_path': str(archive_target),
            'sub': _flatten_sub(detail),
        }

    # 包元数据 fallback：filelist 里很多文件是裸 release 名（无 lang token），
    # 但 detail.lang.langlist 已声明整包语言 —— 用它当 fallback 才能正确打分。
    lang_block = detail.get('lang') or {}
    pkg_lang_codes = _assrt.parse_langlist(
        lang_block.get('langlist'), lang_block.get('desc') or ''
    )
    pkg_fallback = '.'.join(pkg_lang_codes) if pkg_lang_codes else None

    best = _assrt.pick_best_subtitle(
        files, preferred, preferred_formats,
        fallback_lang=pkg_fallback,
        video_filename=video_path.name,
    )
    if not best:
        raise HTTPException(status_code=502, detail="压缩包内未发现字幕文件")
    best_name, best_data, best_lang = best
    sub_ext = Path(best_name).suffix.lower() or '.srt'
    target = video_path.parent / f"{video_path.stem}.{best_lang}{sub_ext}"
    if target.exists() and not request.overwrite:
        return {
            'status': 'exists',
            'message': f'已存在: {target.name}',
            'saved_files': [{'path': str(target), 'lang': best_lang, 'source': best_name}],
            'sub': _flatten_sub(detail),
        }
    was_existing = target.exists()
    target.write_bytes(best_data)
    saved_files.append({
        'path': str(target),
        'lang': best_lang,
        'source': best_name,
        'overwritten': was_existing,
    })

    # 包内还有其他语言时按 (语言,格式) 去重再落 ——
    # 同一语言只保留 preferred_formats 排名最高的那一份；
    # best 已覆盖的语言（含双语 combo 中的所有 token）跳过，避免重复。
    best_tokens = set(best_lang.split('.'))
    extras = _assrt.pick_one_per_lang(files, preferred, preferred_formats)
    for fname, fdata, lang2 in extras:
        if fname == best_name:
            continue
        # best 是双语包时，单语 chs/eng 已被双语 combo 覆盖，不再额外落
        lang2_tokens = set(lang2.split('.'))
        if lang2_tokens.issubset(best_tokens):
            continue
        ext2 = Path(fname).suffix.lower() or '.srt'
        target2 = video_path.parent / f"{video_path.stem}.{lang2}{ext2}"
        if target2.exists() and not request.overwrite:
            continue
        was_existing2 = target2.exists()
        target2.write_bytes(fdata)
        saved_files.append({
            'path': str(target2),
            'lang': lang2,
            'source': fname,
            'overwritten': was_existing2,
        })

    return {
        'status': 'success',
        'saved_files': saved_files,
        'sub': _flatten_sub(detail),
    }


@router.get("/assrt/quota")
def assrt_quota():
    """查 assrt token 当前剩余配额。"""
    client = _get_assrt_client()
    try:
        info = client.quota()
    except _assrt.AssrtError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return info


# ---------- Reports ----------

@router.get("/reports")
def list_reports(
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """获取扫描报告列表"""
    query = db.query(ScanReport).filter(ScanReport.report_type == "subtitle")
    total = query.count()
    reports = query.order_by(ScanReport.created_at.desc()).offset(offset).limit(limit).all()

    return {
        "total": total,
        "reports": [
            {
                "id": r.id,
                "scan_path": r.scan_path,
                "total_items": r.total_items,
                "issues_count": r.issues_count,
                "created_at": r.created_at.isoformat(),
            }
            for r in reports
        ],
    }


@router.get("/reports/{report_id}")
def get_report(report_id: int, db: Session = Depends(get_db)):
    """获取报告详情"""
    report = db.query(ScanReport).filter(ScanReport.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")

    return {
        "id": report.id,
        "scan_path": report.scan_path,
        "total_items": report.total_items,
        "issues_count": report.issues_count,
        "created_at": report.created_at.isoformat(),
        "data": json.loads(report.report_data) if report.report_data else None,
    }


# ==================== 硬字幕标注（VideoAnnotation）====================
# 用户对视频做的"我看过画面，里面有 chs 硬字幕"等标记。
# 字幕扫描时把这些 hardcoded_langs 计入"已覆盖"，避免被误判为缺字幕。

class AnnotationItem(BaseModel):
    file_path: str
    hardcoded_langs: List[str] = []   # eg ['chs'], ['chs','eng']
    note: Optional[str] = None


class AnnotationQuery(BaseModel):
    paths: List[str]


class AnnotationDelete(BaseModel):
    file_path: str


def _annotation_to_dict(a: VideoAnnotation) -> dict:
    try:
        langs = json.loads(a.hardcoded_subtitle_langs) if a.hardcoded_subtitle_langs else []
    except Exception:
        langs = []
    return {
        'id': a.id,
        'file_path': a.file_path,
        'hardcoded_langs': langs,
        'note': a.note,
        'updated_at': a.updated_at.isoformat() if a.updated_at else None,
    }


def _normalize_path(p: str) -> str:
    """规范化 file_path：统一存 Jellyfin view（reverse-translate 任何 backend-view 入参），
    再做 forward slash + 去尾斜杠 + 小写。约定：DB 一律存 Jellyfin view。
    """
    if not p:
        return ''
    from web.backend.path_translator import reverse_translate_path_with_settings
    canonical = reverse_translate_path_with_settings(p) or p
    return canonical.replace('\\', '/').rstrip('/').lower()


@router.post("/annotations/query")
def query_annotations(req: AnnotationQuery, db: Session = Depends(get_db)):
    """
    批量查询：按 file_path 列表返回所有有标注的视频。
    返回 dict: { normalized_file_path: {hardcoded_langs, note, ...} }
    前端可用 _normalize_path 同款逻辑做匹配。
    """
    if not req.paths:
        return {'annotations': {}}
    normalized_keys = [_normalize_path(p) for p in req.paths if p]
    # 数据库存的也是 normalized
    annos = (
        db.query(VideoAnnotation)
        .filter(VideoAnnotation.file_path.in_(normalized_keys))
        .all()
    )
    return {
        'annotations': {a.file_path: _annotation_to_dict(a) for a in annos},
    }


@router.put("/annotations")
def upsert_annotations(
    req: List[AnnotationItem],
    db: Session = Depends(get_db),
):
    """批量保存（insert or update）。空 hardcoded_langs 会删除该条记录。"""
    if not isinstance(req, list):
        raise HTTPException(status_code=400, detail="body 应为 AnnotationItem 数组")
    saved = []
    deleted = []
    for item in req:
        key = _normalize_path(item.file_path)
        if not key:
            continue
        existing = (
            db.query(VideoAnnotation).filter(VideoAnnotation.file_path == key).first()
        )
        # 空 langs 且无 note → 视为删除请求
        if not item.hardcoded_langs and not (item.note or '').strip():
            if existing:
                db.delete(existing)
                deleted.append(key)
            continue
        if existing:
            existing.hardcoded_subtitle_langs = json.dumps(item.hardcoded_langs)
            existing.note = item.note
        else:
            existing = VideoAnnotation(
                file_path=key,
                hardcoded_subtitle_langs=json.dumps(item.hardcoded_langs),
                note=item.note,
            )
            db.add(existing)
        saved.append(key)
    db.commit()
    return {'saved': saved, 'deleted': deleted, 'count': len(saved)}


@router.delete("/annotations")
def delete_annotation(req: AnnotationDelete, db: Session = Depends(get_db)):
    """删除单个标注。"""
    key = _normalize_path(req.file_path)
    existing = db.query(VideoAnnotation).filter(VideoAnnotation.file_path == key).first()
    if not existing:
        return {'deleted': False, 'reason': 'not_found'}
    db.delete(existing)
    db.commit()
    return {'deleted': True}

