"""
下载流水线 API（拆自 discover.py）
- 推送种子到 qBittorrent + 写 dispatch_map
- 列出 / 暂停 / 恢复 / 删除下载任务
- 批量操作 / 强制启动 / 重新校验 / Re-announce
- 全局传输状态、速度限制、备用速度切换
"""
import sys
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

ROOT_DIR = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from web.backend.database import get_db
from web.backend.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


class DownloadRequest(BaseModel):
    title: str
    magnet: Optional[str] = None
    torrent_url: Optional[str] = None
    # 上传本地 .torrent 文件内容（base64 编码的 raw bytes）
    # magnet / torrent_url / torrent_file_b64 三者必须三选一
    torrent_file_b64: Optional[str] = None
    save_path: Optional[str] = None
    category: Optional[str] = None
    source: str = "jackett"
    # 用户在搜索页选的分类（form.category），作为 user_hint 帮助识别 —— 最高优先
    user_hint_media_type: Optional[str] = None
    # Jackett 每行的 CategoryDesc 原文（如 "TV/Documentary", "Movies/HD", "XXX/UHD"），
    # 作为 user_hint 兜底；只在 user_hint_media_type 缺失时启用
    category_desc: Optional[str] = None


def _jackett_category_to_media_type(desc: Optional[str]) -> Optional[str]:
    """把 Jackett CategoryDesc 文本映射成 media_type（'movie' / 'tv' / 'anime' / 'adult'）。
    无法判定返回 None，让 identify 链路自行兜底。

    形态示例：
      'TV/Documentary' → tv      'TV/Anime' → anime
      'Movies/HD'      → movie   'Movies/UHD' → movie
      'XXX/UHD'        → adult
      'Audio/...'      → None（不处理）
    """
    if not desc:
        return None
    parts = desc.split('/', 1)
    head = parts[0].strip().lower()
    sub = parts[1].strip().lower() if len(parts) > 1 else ''
    if head == 'tv':
        return 'anime' if 'anime' in sub else 'tv'
    if head == 'movies':
        # 极少数 indexer 把动画电影放 Movies/Anime；先按 movie 处理（identify 会再修）
        return 'movie'
    if head == 'xxx':
        return 'adult'
    return None


def _qb():
    from common.qbittorrent_client import QBittorrentClient
    return QBittorrentClient(
        settings.qbittorrent_host,
        settings.qbittorrent_username,
        settings.qbittorrent_password,
    )


def _extract_hash_from_magnet(magnet: Optional[str]) -> Optional[str]:
    """从 magnet:?xt=urn:btih:<hash> 抠出 info_hash（小写）。"""
    if not magnet:
        return None
    import re
    m = re.search(r'btih:([0-9a-fA-F]{40})', magnet)
    return m.group(1).lower() if m else None


# ---------- 推送种子 ----------

@router.post("/download")
def push_download(
    request: DownloadRequest,
    db: Session = Depends(get_db),
):
    """推送种子到 qB（stop_condition=MetadataReceived）+ 写 dispatch_map(phase='analyzing')。

    分析由 scheduler 的 analyzer 后台跑：高置信自动入流水线，低置信落 needs_review。
    """
    src_hint = (request.magnet or request.torrent_url or ('torrent_file' if request.torrent_file_b64 else ''))[:120]
    # 用户 form 显式选 > Jackett 分类兜底；最终落到 dispatch_map.media_type
    effective_hint = (
        request.user_hint_media_type
        or _jackett_category_to_media_type(request.category_desc)
    )
    logger.info(
        f"/pipeline/download POST: title={request.title!r} src={src_hint!r} "
        f"category={request.category!r} user_hint={request.user_hint_media_type!r} "
        f"category_desc={request.category_desc!r} → effective_hint={effective_hint!r}"
    )
    if not settings.qbittorrent_host or not settings.qbittorrent_username:
        raise HTTPException(status_code=400, detail="未配置 qBittorrent")
    if not (request.magnet or request.torrent_url or request.torrent_file_b64):
        raise HTTPException(status_code=400, detail="必须提供 magnet / torrent_url / torrent_file_b64 之一")

    # 上传的 .torrent 文件：解 base64 → bytes
    torrent_file_bytes: Optional[bytes] = None
    if request.torrent_file_b64:
        import base64
        try:
            torrent_file_bytes = base64.b64decode(request.torrent_file_b64, validate=True)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"torrent_file_b64 解码失败: {e}")
        if len(torrent_file_bytes) < 20 or not torrent_file_bytes.startswith(b'd'):
            # .torrent 是 bencode dict，必然以 'd' 开头。不是的话基本是坏文件
            raise HTTPException(status_code=400, detail="不是有效的 .torrent 文件（bencode 头校验失败）")

    from common.qbittorrent_client import QBittorrentClient
    from web.backend.database import DownloadDispatchMap

    client = QBittorrentClient(
        settings.qbittorrent_host,
        settings.qbittorrent_username,
        settings.qbittorrent_password,
    )

    # save_path 仅当 request 显式指定才传 —— 否则让 qB 用自己的默认下载路径。
    # 前端送的是 backend view（如 Z:/downloads），qB 那边要的是它自己视角（如 /downloads），
    # reverse-translate 一次。同机部署时 reverse 不命中规则会原样返回，无影响。
    from web.backend.path_translator import reverse_translate_path_with_settings
    save_path = (
        reverse_translate_path_with_settings(request.save_path)
        if request.save_path else request.save_path
    )

    # 上传文件场景：add_torrent 前先快照 qB 已有 hash 集合，添加后 diff 出新增的，
    # 因为 qB add API 不返回 hash，文件场景也没 magnet 可抠。
    hashes_before: set = set()
    if torrent_file_bytes is not None:
        try:
            hashes_before = {
                (t.get('hash') or '').lower()
                for t in (client.list_torrents() or [])
                if t.get('hash')
            }
        except Exception:
            pass

    ok = client.add_torrent(
        magnet=request.magnet,
        torrent_url=request.torrent_url,
        torrent_file=torrent_file_bytes,
        save_path=save_path,
        category=request.category,
        stop_condition='MetadataReceived',
    )
    if not ok:
        raise HTTPException(status_code=500, detail="qBittorrent 推送失败（检查登录或种子链接）")

    # 拿到 hash —— magnet 里直接抠
    info_hash = _extract_hash_from_magnet(request.magnet) or ''
    if not info_hash and request.torrent_url:
        try:
            for t in client.list_torrents() or []:
                if (t.get('name') or '').strip() == request.title.strip():
                    info_hash = (t.get('hash') or '').lower()
                    break
        except Exception:
            pass
    if not info_hash and torrent_file_bytes is not None:
        # 文件上传场景：snapshot diff 拿新增的 hash（qB 添加是异步的，给点重试时间）
        import time as _t
        deadline = _t.time() + 3.0
        while _t.time() < deadline:
            try:
                current = {
                    (t.get('hash') or '').lower()
                    for t in (client.list_torrents() or [])
                    if t.get('hash')
                }
                new_hashes = current - hashes_before
                if new_hashes:
                    info_hash = next(iter(new_hashes))
                    break
            except Exception:
                pass
            _t.sleep(0.2)

    # 写 dispatch_map(phase=analyzing) + 触发 analyzer 立刻处理
    if info_hash:
        from tools.dispatch.phases import PHASE_ANALYZING, PHASE_DISMISSED, STATUS_RUNNING
        existing = db.query(DownloadDispatchMap).filter_by(torrent_hash=info_hash).first()

        if existing and existing.phase == PHASE_DISMISSED:
            logger.info(f"push: 重置已拒绝的 dispatch_map 行 hash={info_hash[:16]}..")
            existing.phase = PHASE_ANALYZING
            existing.phase_status = STATUS_RUNNING
            existing.media_type = effective_hint or 'unknown'
            existing.title = request.title
            existing.status_message = '从已拒绝重新提交，等待分析...'
            existing.cleaned_at = None
            db.commit()
            try:
                from tools.dispatch.analyzer import trigger
                trigger.set()
            except Exception:
                pass
        elif not existing:
            db.add(DownloadDispatchMap(
                torrent_hash=info_hash,
                phase=PHASE_ANALYZING,
                phase_status=STATUS_RUNNING,
                media_type=effective_hint or 'unknown',
                title=request.title,
                status_message='等待分析中...',
                created_at=datetime.utcnow(),
            ))
            db.commit()
            try:
                from tools.dispatch.analyzer import trigger
                trigger.set()
            except Exception:
                pass

    return {
        "ok": True,
        "torrent_hash": info_hash,
        "message": "已推送到分析队列，识别后自动入流水线",
    }


# ---------- 列出 / 单条操作 ----------

@router.get("/downloads")
def list_downloads(
    filter_status: str = "all",
    refresh: bool = True,
    db: Session = Depends(get_db),
):
    """列出下载任务。refresh=True 时同步 qB 实时状态。"""
    qbit_torrents = []
    if refresh and settings.qbittorrent_host and settings.qbittorrent_username:
        try:
            from common.qbittorrent_client import QBittorrentClient
            client = QBittorrentClient(
                settings.qbittorrent_host,
                settings.qbittorrent_username,
                settings.qbittorrent_password,
            )
            qbit_torrents = client.list_torrents(filter_status if filter_status != 'all' else None)
        except Exception as e:
            logger.warning(f"qBittorrent 状态获取失败: {e}")

    from web.backend.database import DownloadDispatchMap
    hashes = [(t.get('hash') or '').lower() for t in qbit_torrents if t.get('hash')]
    dispatch_by_hash: Dict[str, DownloadDispatchMap] = {}
    if hashes:
        rows = (
            db.query(DownloadDispatchMap)
            .filter(DownloadDispatchMap.torrent_hash.in_(hashes))
            .all()
        )
        dispatch_by_hash = {r.torrent_hash: r for r in rows}

    lib_id_to_name: Dict[str, str] = {}
    try:
        from common.jellyfin_client import JellyfinClient
        if settings.jellyfin_api_key:
            jc = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
            for lib in (jc.get_libraries_normalized() or []):
                lib_id_to_name[lib['id']] = lib['name']
    except Exception as e:
        logger.warning(f"加载 Jellyfin 库列表失败: {e}")

    from web.backend.path_translator import translate_path_with_settings

    def _build_dispatch_info(t: Dict) -> Optional[Dict]:
        h = (t.get('hash') or '').lower()
        dm = dispatch_by_hash.get(h)
        if not dm:
            return None
        timings = dm.phase_timings or {}
        return {
            "media_type": dm.media_type,
            "title": dm.title,
            "year": dm.year,
            "series_name": dm.series_name,
            "target_library_id": dm.target_library_id,
            "target_library_name": lib_id_to_name.get(dm.target_library_id) if dm.target_library_id else None,
            "target_path": translate_path_with_settings(dm.target_path) if dm.target_path else None,
            "move_mode": dm.move_mode,
            "phase": dm.phase,
            "phase_status": dm.phase_status,
            "status_message": dm.status_message,
            "duplicates": timings.get('duplicates'),
        }

    return {
        "qbittorrent": [
            {
                "hash": t.get('hash'),
                "name": t.get('name'),
                "size": t.get('size'),
                "progress": t.get('progress') or 0,
                "state": t.get('state'),
                "dlspeed": t.get('dlspeed'),
                "upspeed": t.get('upspeed'),
                "save_path": translate_path_with_settings(t.get('save_path')) if t.get('save_path') else None,
                "content_path": translate_path_with_settings(t.get('content_path')) if t.get('content_path') else None,
                "added_on": t.get('added_on'),
                "ratio": t.get('ratio') or 0,
                "eta": t.get('eta'),
                "seeding_time": t.get('seeding_time'),
                "completion_on": t.get('completion_on'),
                "downloaded": t.get('downloaded'),
                "uploaded": t.get('uploaded'),
                "dispatch": _build_dispatch_info(t),
            }
            for t in qbit_torrents
        ],
    }


@router.post("/downloads/{torrent_hash}/pause")
def pause_download(torrent_hash: str):
    if not settings.qbittorrent_host:
        raise HTTPException(status_code=400, detail="未配置 qBittorrent")
    logger.info(f"/downloads/pause: hash={torrent_hash[:16]}..")
    return {"ok": _qb().pause(torrent_hash)}


@router.post("/downloads/{torrent_hash}/resume")
def resume_download(torrent_hash: str):
    if not settings.qbittorrent_host:
        raise HTTPException(status_code=400, detail="未配置 qBittorrent")
    logger.info(f"/downloads/resume: hash={torrent_hash[:16]}..")
    return {"ok": _qb().resume(torrent_hash)}


@router.delete("/downloads/{torrent_hash}")
def delete_download(torrent_hash: str, delete_files: bool = False):
    if not settings.qbittorrent_host:
        raise HTTPException(status_code=400, detail="未配置 qBittorrent")
    # 删种子：delete_files=True 时连本地文件一起删，是潜在数据丢失点，升 WARNING
    logger.warning(
        f"/downloads/delete: hash={torrent_hash[:16]}.. delete_files={delete_files}"
    )
    return {"ok": _qb().delete(torrent_hash, delete_files=delete_files)}


# ---------- 批量操作 / 单条增强 ----------

class BulkActionRequest(BaseModel):
    hashes: List[str]
    action: str   # pause / resume / delete / force_start / recheck / reannounce
    delete_files: bool = False


@router.post("/downloads/bulk")
def bulk_action(request: BulkActionRequest):
    if not request.hashes:
        return {"ok": False, "message": "未选中任何种子"}
    client = _qb()
    client.login()
    a = request.action.lower()
    # 批量操作：delete 标 WARNING（破坏性 + delete_files 可能带）；其它 INFO
    log_msg = (
        f"/downloads/bulk: action={a!r} count={len(request.hashes)}"
        + (f" delete_files={request.delete_files}" if a == 'delete' else '')
    )
    if a == 'delete':
        logger.warning(log_msg)
    else:
        logger.info(log_msg)
    ok = False
    if a == 'pause':
        ok = all(client.pause(h) for h in request.hashes)
    elif a == 'resume':
        ok = all(client.resume(h) for h in request.hashes)
    elif a == 'delete':
        ok = all(client.delete(h, delete_files=request.delete_files) for h in request.hashes)
    elif a == 'force_start':
        ok = client.set_force_start(request.hashes, force=True)
    elif a == 'recheck':
        ok = client.recheck(request.hashes)
    elif a == 'reannounce':
        ok = client.reannounce(request.hashes)
    else:
        raise HTTPException(status_code=400, detail=f'未知 action: {a}')
    return {"ok": ok, "count": len(request.hashes), "action": a}


@router.post("/downloads/{torrent_hash}/recheck")
def recheck(torrent_hash: str):
    logger.info(f"/downloads/recheck: hash={torrent_hash[:16]}..")
    return {"ok": _qb().recheck(torrent_hash)}


@router.post("/downloads/{torrent_hash}/reannounce")
def reannounce(torrent_hash: str):
    logger.info(f"/downloads/reannounce: hash={torrent_hash[:16]}..")
    return {"ok": _qb().reannounce(torrent_hash)}


class ForceStartRequest(BaseModel):
    force: bool = True


@router.post("/downloads/{torrent_hash}/force-start")
def force_start(torrent_hash: str, request: ForceStartRequest):
    logger.info(f"/downloads/force-start: hash={torrent_hash[:16]}.. force={request.force}")
    return {"ok": _qb().set_force_start(torrent_hash, force=request.force)}


# ---------- 全局传输 / 限速 ----------

@router.get("/transfer-info")
def transfer_info():
    """全局传输状态（速度 / 限速 / 备用速度模式 / 总下载上传量）。"""
    client = _qb()
    info = client.transfer_info() or {}
    return {
        'dl_info_speed': info.get('dl_info_speed', 0),
        'up_info_speed': info.get('up_info_speed', 0),
        'dl_info_data': info.get('dl_info_data', 0),
        'up_info_data': info.get('up_info_data', 0),
        'connection_status': info.get('connection_status'),
        'global_dl_limit': client.get_global_download_limit(),
        'global_up_limit': client.get_global_upload_limit(),
        'alt_speed_enabled': client.get_alt_speed_limits_enabled(),
    }


class SpeedLimitRequest(BaseModel):
    download_limit: Optional[int] = None  # bytes/s
    upload_limit: Optional[int] = None


@router.post("/transfer-info/speed-limit")
def set_speed_limit(request: SpeedLimitRequest):
    logger.info(
        f"/transfer-info/speed-limit: download={request.download_limit} upload={request.upload_limit} (bytes/s)"
    )
    client = _qb()
    results = {}
    if request.download_limit is not None:
        results['download'] = client.set_global_download_limit(request.download_limit)
    if request.upload_limit is not None:
        results['upload'] = client.set_global_upload_limit(request.upload_limit)
    return results


@router.post("/transfer-info/toggle-alt-speed")
def toggle_alt_speed():
    logger.info("/transfer-info/toggle-alt-speed: 切换备用限速")
    return {"ok": _qb().toggle_alt_speed_limits()}
