"""
统计分析 API
"""
from datetime import datetime, timedelta
from typing import Optional
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func

from web.backend.database import get_db, Task, ActorInfo, ScanReport, MediaItem, AdultItem, DownloadDispatchMap
from web.backend.config import settings


router = APIRouter()


@router.get("/overview")
def get_overview(db: Session = Depends(get_db)):
    """
    仪表盘总览数据，分模块返回：
      - jellyfin: 连接状态、库数量
      - discover: TMDB 连接、推荐数量、正在下载数
      - metadata: 演员/海报覆盖率
      - adult: 番号匹配比例
      - tasks: 进行中数、平均进度等
    """
    # ========== 任务 ==========
    task_stats = db.query(Task.status, func.count(Task.id)).group_by(Task.status).all()
    task_counts = {status: count for status, count in task_stats}

    # 进行中任务的平均进度（用于"总进度"）
    running_progress = 0.0
    if task_counts.get('running'):
        avg = db.query(func.avg(Task.progress)).filter(Task.status == 'running').scalar()
        running_progress = round(float(avg or 0), 1)

    # ========== 演员 / 海报 ==========
    total_actors = db.query(ActorInfo).count()
    actors_with_image = db.query(ActorInfo).filter(ActorInfo.has_image == True).count()

    poster_filter = MediaItem.media_type.in_(['movie', 'series'])
    total_posters = db.query(MediaItem).filter(poster_filter).count()
    posters_with = db.query(MediaItem).filter(poster_filter, MediaItem.has_poster == True).count()

    # ========== 番号 ==========
    total_adult = db.query(AdultItem).count()
    adult_matched = db.query(AdultItem).filter(AdultItem.title != None).count()  # noqa: E711

    # ========== 下载（活跃中的）==========
    # "活跃" = 在主流水线/等待 worker 接管的所有 phase（不含终态：cleaned/dismissed/all_jobs_done）
    from tools.dispatch.phases import (
        ACTIVE_PHASES, PHASE_ANALYZING, PHASE_DISPATCH_QUEUED,
    )
    active_phase_set = ACTIVE_PHASES | {PHASE_ANALYZING, PHASE_DISPATCH_QUEUED}
    downloads_running = db.query(DownloadDispatchMap).filter(
        DownloadDispatchMap.phase.in_(list(active_phase_set))
    ).count()

    # ========== Jellyfin 库数量（实时拉，可能慢/失败，try-except）==========
    library_count = 0
    if settings.jellyfin_api_key:
        try:
            from common.jellyfin_client import JellyfinClient
            client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
            libs = client.get_libraries_normalized()
            library_count = len(libs)
        except Exception:
            pass

    # ========== 最近任务 ==========
    recent_tasks = db.query(Task).order_by(Task.created_at.desc()).limit(10).all()
    recent_reports = db.query(ScanReport).order_by(ScanReport.created_at.desc()).limit(5).all()

    return {
        # ============ 第一行：5 张卡片 ============
        "jellyfin": {
            "connected": bool(settings.jellyfin_api_key),
            "host": settings.jellyfin_host,
            "library_count": library_count,
        },
        "discover": {
            "tmdb_connected": bool(settings.tmdb_api_key),
            "downloads_running": downloads_running,
            "downloads_total": db.query(DownloadDispatchMap).count(),
            # 日/周/月推荐数依赖 trending API，前端实时拉，这里给 placeholder
        },
        "metadata": {
            "actors_total": total_actors,
            "actors_with_image": actors_with_image,
            "actors_coverage": round(actors_with_image / total_actors * 100, 1) if total_actors else 0,
            "posters_total": total_posters,
            "posters_with": posters_with,
            "posters_coverage": round(posters_with / total_posters * 100, 1) if total_posters else 0,
        },
        "adult": {
            "enabled": settings.adult_enabled,
            "total": total_adult,
            "matched": adult_matched,
            "match_rate": round(adult_matched / total_adult * 100, 1) if total_adult else 0,
        },
        "tasks": {
            "total": sum(task_counts.values()),
            "pending": task_counts.get("pending", 0),
            "running": task_counts.get("running", 0),
            "completed": task_counts.get("completed", 0),
            "failed": task_counts.get("failed", 0),
            "avg_progress": running_progress,
        },

        # ============ 详情列表 ============
        "recent_reports": [
            {
                "id": r.id, "type": r.report_type, "scan_path": r.scan_path,
                "total_items": r.total_items, "issues_count": r.issues_count,
                "created_at": r.created_at.isoformat(),
            }
            for r in recent_reports
        ],
        "recent_tasks": [
            {
                "id": t.id, "type": t.task_type, "status": t.status,
                "message": t.message, "progress": t.progress,
                "created_at": t.created_at.isoformat(),
            }
            for t in recent_tasks
        ],

        # ============ 兼容旧前端（保留 actors / config 字段）============
        "actors": {
            "total": total_actors,
            "with_image": actors_with_image,
            "without_image": total_actors - actors_with_image,
            "coverage": round(actors_with_image / total_actors * 100, 1) if total_actors else 0,
        },
        "config": {
            "jellyfin_connected": bool(settings.jellyfin_api_key),
            "tmdb_connected": bool(settings.tmdb_api_key),
            "opensubtitles_connected": bool(settings.opensubtitles_api_key),
            "jackett_connected": bool(settings.jackett_api_key),
            "qbittorrent_connected": bool(settings.qbittorrent_username),
        },
    }


@router.get("/tasks/history")
def get_task_history(
    days: int = 7,
    db: Session = Depends(get_db)
):
    """获取任务历史统计"""
    start_date = datetime.utcnow() - timedelta(days=days)

    tasks = db.query(Task).filter(
        Task.created_at >= start_date
    ).all()

    # 按日期和类型分组
    daily_stats = defaultdict(lambda: defaultdict(int))
    type_stats = defaultdict(lambda: {"total": 0, "success": 0, "failed": 0})

    for task in tasks:
        date_key = task.created_at.strftime("%Y-%m-%d")
        daily_stats[date_key][task.task_type] += 1

        type_stats[task.task_type]["total"] += 1
        if task.status == "completed":
            type_stats[task.task_type]["success"] += 1
        elif task.status == "failed":
            type_stats[task.task_type]["failed"] += 1

    # 生成日期序列
    dates = []
    current = start_date
    while current <= datetime.utcnow():
        dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)

    return {
        "period": f"最近 {days} 天",
        "daily": [
            {
                "date": date,
                "counts": dict(daily_stats.get(date, {}))
            }
            for date in dates
        ],
        "by_type": dict(type_stats)
    }


@router.get("/posters/stats")
def get_poster_stats(db: Session = Depends(get_db)):
    """获取海报统计"""
    media_filter = MediaItem.media_type.in_(['movie', 'series'])
    total = db.query(MediaItem).filter(media_filter).count()
    with_poster = db.query(MediaItem).filter(media_filter, MediaItem.has_poster == True).count()
    movies_total = db.query(MediaItem).filter(MediaItem.media_type == 'movie').count()
    movies_with = db.query(MediaItem).filter(MediaItem.media_type == 'movie', MediaItem.has_poster == True).count()
    series_total = db.query(MediaItem).filter(MediaItem.media_type == 'series').count()
    series_with = db.query(MediaItem).filter(MediaItem.media_type == 'series', MediaItem.has_poster == True).count()

    return {
        "total": total,
        "with_poster": with_poster,
        "without_poster": total - with_poster,
        "coverage_percent": round(with_poster / total * 100, 1) if total > 0 else 0,
        "movies": {
            "total": movies_total,
            "with_poster": movies_with,
            "without_poster": movies_total - movies_with,
        },
        "series": {
            "total": series_total,
            "with_poster": series_with,
            "without_poster": series_total - series_with,
        },
    }


@router.get("/actors/stats")
def get_actor_stats(db: Session = Depends(get_db)):
    """获取演员统计"""
    total = db.query(ActorInfo).count()
    with_image = db.query(ActorInfo).filter(ActorInfo.has_image == True).count()
    with_tmdb = db.query(ActorInfo).filter(ActorInfo.tmdb_id != None).count()

    # 最近更新的演员
    recent = db.query(ActorInfo).order_by(
        ActorInfo.updated_at.desc()
    ).limit(10).all()

    # 没有图片的演员（按名字排序）
    missing = db.query(ActorInfo).filter(
        ActorInfo.has_image == False
    ).order_by(ActorInfo.name).limit(50).all()

    return {
        "total": total,
        "with_image": with_image,
        "without_image": total - with_image,
        "with_tmdb_id": with_tmdb,
        "coverage_percent": round(with_image / total * 100, 1) if total > 0 else 0,
        "recent_updates": [
            {
                "id": a.id,
                "name": a.name,
                "has_image": a.has_image,
                "updated_at": a.updated_at.isoformat() if a.updated_at else None
            }
            for a in recent
        ],
        "missing_images": [
            {"id": a.id, "name": a.name, "jellyfin_id": a.jellyfin_id}
            for a in missing
        ]
    }
