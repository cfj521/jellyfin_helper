"""
字幕管理 API
"""
import sys
from pathlib import Path
from typing import List, Optional
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
    from web.backend.api.jellyfin import get_library_by_id
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
async def scan_subtitles(
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

    db = SessionLocal()
    try:
        scanner = SubtitleScanner(preferred_langs=expected_langs)
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
            update_task_progress(db, task_id, pct, f"扫描 [{idx+1}/{len(paths)}] {p}")

            last_emit = [0]  # 当前路径上次 emit 时的目录数

            def _on_progress(partial_result, dirs_done, _idx=idx, _p=p, _pct=pct):
                last_dir_name = (
                    partial_result.directories[-1].name
                    if partial_result.directories else '...'
                )
                if dirs_done - last_emit[0] < EMIT_EVERY:
                    update_task_progress(
                        db, task_id, _pct,
                        f"[{_idx+1}/{len(paths)}] 已扫 {dirs_done} 个目录 · 最近: {last_dir_name}",
                    )
                    return
                last_emit[0] = dirs_done
                # 当前进度快照
                current_dirs = list(all_dirs)
                for d in partial_result.directories:
                    current_dirs.append(_dir_to_dict(d, format_episode, annotations_map, expected_langs))
                update_task_progress(
                    db, task_id, _pct,
                    f"[{_idx+1}/{len(paths)}] 已扫 {dirs_done} 个目录 · 最近: {last_dir_name}",
                    result_patch={
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

        update_task_progress(db, task_id, 85, "正在生成报告...")

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

        # ScanReport 仍然保存（subtitle_auto_fix 等其他流程会按 report_id 引用）
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
        complete_task(db, task_id, {"error": str(e)}, success=False)
    finally:
        db.close()


# ---------- Rename ----------

@router.post("/rename", response_model=TaskStartResponse)
async def rename_subtitles(
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
    """执行字幕重命名（后台任务）。"""
    from web.backend.database import SessionLocal
    from web.backend.api.jellyfin import trigger_refresh

    db = SessionLocal()
    try:
        renamer = SubtitleRenamer()
        all_results = []

        EMIT_EVERY = 10  # 每发现 10 个 rename pair 就 emit 一次 partial result

        for idx, p in enumerate(paths):
            pct = 5 + int(85 * (idx + 1) / len(paths))
            update_task_progress(db, task_id, pct, f"处理 [{idx+1}/{len(paths)}] {p}")

            last_emit = [0]

            def _on_each(item, _idx=idx, _p=p, _pct=pct):
                # 实时把 result 加到全局
                all_results.append(item)
                # message 实时显示当前正在处理的目录
                update_task_progress(
                    db, task_id, _pct,
                    f"[{_idx+1}/{len(paths)}] 已发现 {len(all_results)} 个待改名 · "
                    f"最近: {item.get('directory') or '...'}",
                )
                # result 写入控频
                if len(all_results) - last_emit[0] >= EMIT_EVERY:
                    last_emit[0] = len(all_results)
                    success_so_far = sum(1 for r in all_results if r.get('success'))
                    update_task_progress(
                        db, task_id, _pct,
                        f"[{_idx+1}/{len(paths)}] 已发现 {len(all_results)} 个待改名",
                        result_patch={
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

        # 实际执行了重命名 → 触发 Jellyfin 重新扫描（每个相关库）
        refreshed = False
        if execute and refresh_library_ids and success_count > 0:
            update_task_progress(db, task_id, 95, "通知 Jellyfin 刷新媒体库...")
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
        complete_task(db, task_id, {"error": str(e)}, success=False)
    finally:
        db.close()


# ---------- Download ----------

@router.post("/download", response_model=TaskStartResponse)
async def download_subtitles(
    request: DownloadRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """根据扫描报告下载缺失字幕"""
    if not settings.opensubtitles_api_key:
        raise HTTPException(
            status_code=400,
            detail="未配置 OpenSubtitles API Key，请在 config.yaml 中设置 subtitle.opensubtitles_api_key"
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
    """执行字幕下载（后台任务）"""
    from web.backend.database import SessionLocal

    db = SessionLocal()
    try:
        update_task_progress(db, task_id, 5, "正在加载报告...")

        report = db.query(ScanReport).filter(ScanReport.id == report_id).first()
        if not report:
            complete_task(db, task_id, {"error": "报告不存在"}, success=False)
            return

        report_data = json.loads(report.report_data) if report.report_data else {}
        videos = SubtitleDownloader.collect_videos_from_report(report_data)

        if limit:
            videos = videos[:limit]

        if not videos:
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

        update_task_progress(db, task_id, 10, f"准备下载 {len(videos)} 个视频的字幕...")

        downloader = SubtitleDownloader(settings.to_dict())

        def _progress(idx, total, item):
            # 进度从 10% 推进到 95%
            pct = 10 + int(85 * idx / max(total, 1))
            msg = f"[{idx}/{total}] {item.get('video', '')} - {item.get('status', '')}"
            update_task_progress(db, task_id, pct, msg)

        details = downloader.process_videos(
            videos,
            languages=languages,
            dry_run=dry_run,
            progress_cb=_progress,
        )

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
        complete_task(db, task_id, {"error": str(e)}, success=False)
    except Exception as e:
        logger.exception("字幕下载任务失败")
        complete_task(db, task_id, {"error": str(e)}, success=False)
    finally:
        db.close()


# ---------- Auto-fix（一条龙）----------

@router.post("/auto-fix", response_model=TaskStartResponse)
async def auto_fix_subtitles(
    request: AutoFixRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    自动字幕修复：
      1) 扫描指定路径/库（含 mkv 内嵌检测 + 裸名字幕内容识别）
      2) 对每个视频按自身缺失语言调用 OpenSubtitles 下载
      3) （可选）对齐字幕文件名为 video.{lang}.{ext}
      4) （可选）通知 Jellyfin 重扫媒体库
    """
    if not request.dry_run and not settings.opensubtitles_api_key:
        raise HTTPException(
            status_code=400,
            detail="未配置 OpenSubtitles API Key，请在 config.yaml 设置 subtitle.opensubtitles_api_key 后再执行下载",
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
    """auto-fix 后台任务：扫描 → 下载 → 重命名 → 刷新。"""
    from web.backend.database import SessionLocal
    from tools.subtitle_manager.scanner import format_episode
    from web.backend.api.jellyfin import trigger_refresh

    db = SessionLocal()
    try:
        # ============ Step 1: 扫描 ============
        update_task_progress(db, task_id, 2, "开始扫描...")
        scanner = SubtitleScanner(preferred_langs=expected_langs)

        all_dirs = []
        total_videos = 0
        total_with = 0
        total_without = 0
        total_subs = 0
        last_scan_time = ""

        for idx, p in enumerate(paths):
            pct = 2 + int(28 * (idx + 1) / len(paths))  # 扫描占 2%-30%
            update_task_progress(db, task_id, pct, f"扫描 [{idx+1}/{len(paths)}] {p}")
            result = scanner.scan(Path(p), recursive=recursive)
            last_scan_time = result.scan_time
            total_videos += result.total_videos
            total_with += result.total_with_sub
            total_without += result.total_without_sub
            total_subs += result.total_subtitles
            for d in result.directories:
                all_dirs.append({
                    "path": str(d.path),
                    "name": d.name,
                    "media_type": d.media_type,
                    "total_videos": d.total_videos,
                    "with_subtitles": d.videos_with_sub,
                    "without_subtitles": d.videos_without_sub,
                    "videos": [
                        {
                            "path": str(v.path),
                            "name": v.name,
                            "episode": format_episode(v.episode),
                            "subtitles": v.subtitles,
                            "embedded_langs": v.embedded_langs,
                            "missing_langs": v.missing_langs,
                        }
                        for v in d.videos
                    ],
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
        update_task_progress(db, task_id, 32, "开始下载缺失字幕...")

        download_details: List[Dict] = []
        download_stats = {"total": 0, "success": 0, "failed": 0, "skipped": 0}

        try:
            downloader = SubtitleDownloader(settings.to_dict())
            targets = downloader.collect_targets_from_report(report_data)
            if limit:
                targets = targets[:limit]

            if targets:
                # 为了能复用既有的 progress_cb 形态，临时把 targets 塞回 report
                trimmed_report = dict(report_data)
                _seen = {id(t['path']) for t in targets}
                # 简单写回：保留 targets 列表中的视频路径，rebuild 一个简化 report
                # （下面 auto_fix_from_report 不需要完整目录结构，只看 videos.missing_langs）
                fake_dirs = [{
                    'videos': [
                        {'path': str(t['path']), 'missing_langs': t['missing_langs']}
                        for t in targets
                    ],
                }]
                trimmed_report['directories'] = fake_dirs

                def _dl_progress(idx, total, item):
                    pct = 32 + int(48 * idx / max(total, 1))  # 下载占 32%-80%
                    msg = f"下载 [{idx}/{total}] {item.get('video', '')} - {item.get('status', '')}"
                    update_task_progress(db, task_id, pct, msg)

                download_details = downloader.auto_fix_from_report(
                    trimmed_report,
                    dry_run=dry_run,
                    progress_cb=_dl_progress,
                )
                download_stats = dict(downloader.stats)
            else:
                update_task_progress(db, task_id, 80, "无缺失字幕，跳过下载")
        except ValueError as e:
            # API key 等配置错误：dry_run 模式不致命
            logger.warning(f"OpenSubtitles 下载初始化失败: {e}（dry_run={dry_run}）")
            if not dry_run:
                complete_task(db, task_id, {"error": str(e)}, success=False)
                return

        # ============ Step 3: 重命名对齐（裸名字幕识别语言后改名）============
        rename_details: List[Dict] = []
        if do_rename:
            update_task_progress(db, task_id, 82, "对齐字幕文件名...")
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

        # ============ Step 4: 刷新 Jellyfin ============
        refreshed = False
        if not dry_run and refresh_library_ids:
            actually_changed = (
                download_stats.get("success", 0) > 0
                or any(r.get("success") for r in rename_details)
            )
            if actually_changed:
                update_task_progress(db, task_id, 95, "通知 Jellyfin 刷新媒体库...")
                for lid in refresh_library_ids:
                    try:
                        trigger_refresh(lid)
                    except Exception as e:
                        logger.warning(f"刷新库 {lid} 失败: {e}")
                refreshed = True

        # ============ 持久化扫描报告（方便用户回溯）============
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

        complete_task(db, task_id, {
            "report_id": report.id,
            "scan": {
                "total_videos": total_videos,
                "with_subtitles": total_with,
                "without_subtitles": total_without,
            },
            "download": download_stats,
            "rename": {
                "total": len(rename_details),
                "success": sum(1 for r in rename_details if r.get("success")),
            },
            "dry_run": dry_run,
            "jellyfin_refreshed": refreshed,
            "download_details": download_details[:200],
            "rename_details": rename_details[:200],
        })

    except Exception as e:
        logger.exception("auto-fix 任务失败")
        complete_task(db, task_id, {"error": str(e)}, success=False)
    finally:
        db.close()


# ---------- Reports ----------

@router.get("/reports")
async def list_reports(
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
async def get_report(report_id: int, db: Session = Depends(get_db)):
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
    """统一路径格式：替换反斜杠为正斜杠，去尾部斜杠，全小写。
    用于 file_path 比对（数据库存什么样查什么样要一致）。"""
    if not p:
        return ''
    return p.replace('\\', '/').rstrip('/').lower()


@router.post("/annotations/query")
async def query_annotations(req: AnnotationQuery, db: Session = Depends(get_db)):
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
async def upsert_annotations(
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
async def delete_annotation(req: AnnotationDelete, db: Session = Depends(get_db)):
    """删除单个标注。"""
    key = _normalize_path(req.file_path)
    existing = db.query(VideoAnnotation).filter(VideoAnnotation.file_path == key).first()
    if not existing:
        return {'deleted': False, 'reason': 'not_found'}
    db.delete(existing)
    db.commit()
    return {'deleted': True}

