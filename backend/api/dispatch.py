"""
Dispatch API：配额状态、清理操作、流水线状态查询、加种预览。
"""
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.database import get_db, DownloadDispatchMap, SessionLocal
from backend.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


class QuotaStatus(BaseModel):
    used: int           # 盘真实已用（shutil）
    limit: int          # 盘真实容量
    ratio: float
    state: str
    torrents_count: int
    dispatch_used: int = 0   # 我们的种子总大小（仅展示用，区分"我们占用 vs 别人占用"）


@router.get('/quota', response_model=QuotaStatus)
def get_quota():
    """配额当前状态（用于 UI 顶部状态条）。"""
    from tools.dispatch.quota import get_disk_usage
    return get_disk_usage()


@router.post('/quota/cleanup-now')
def cleanup_now():
    """手动触发硬清（用户点 UI"立即清理"按钮）。"""
    from tools.dispatch.quota import quota_check_and_cleanup
    result = quota_check_and_cleanup()
    if result is None:
        return {'cleaned_count': 0, 'message': '当前配额未到清理阈值，未清理任何种子'}
    return result


@router.post('/quota/soft-cleanup')
def soft_cleanup_now():
    """手动触发常规软清（按分享率 / 做种天数）。"""
    from tools.dispatch.quota import regular_cleanup
    result = regular_cleanup() or {'cleaned_count': 0}
    return result


# ============================================================================
# 加种预览：metadata-only 预添加 → 文件列表 + 类型识别 + 重复检测 + 路径预填
# ============================================================================

class PreviewRequest(BaseModel):
    magnet: Optional[str] = None
    torrent_url: Optional[str] = None
    # 用户在热门页选好类型时透传，跳过启发式
    user_hint: Optional[Dict] = None
    # 等 metadata 的最长秒数
    metadata_timeout: int = 60


@router.post('/preview')
def preview_dispatch(request: PreviewRequest):
    """
    metadata-only 预添加：拿到文件列表 → 识别类型 → 重复检测 → 返回预览。
    用户在 UI 上确认后，调 /confirm 真正落库 dispatch_map 让 pipeline 接管。
    """
    src_hint = (request.magnet or request.torrent_url or '')[:120]
    logger.info(
        f"/preview 进入: src={src_hint!r} user_hint={request.user_hint!r} "
        f"timeout={request.metadata_timeout}s"
    )
    if not (request.magnet or request.torrent_url):
        raise HTTPException(status_code=400, detail='必须提供 magnet 或 torrent_url')

    from common.qbittorrent_client import QBittorrentClient
    import time

    qb = QBittorrentClient(
        host=settings.qbittorrent_host,
        username=settings.qbittorrent_username,
        password=settings.qbittorrent_password,
    )
    if not qb.login():
        raise HTTPException(status_code=502, detail='qB 登录失败')

    # 加种 paused-via-stop_condition 模式
    ok = qb.add_torrent(
        magnet=request.magnet,
        torrent_url=request.torrent_url,
        stop_condition='MetadataReceived',
        download_limit=1,
        paused=False,  # paused=True 会让 qB 不连 peer，永远拿不到 metadata
        category='dispatch-preview',
        tags=['dispatch-preview'],
    )
    if not ok:
        # 把 qB 原始拒绝原因带出去（'Fails.' / 网络异常等），方便前端定位
        qb_err = qb.last_add_error or '未知原因'
        raise HTTPException(status_code=502, detail=f'qB 添加失败：{qb_err}')

    # 从 magnet 抠 hash
    info_hash = _extract_hash(request.magnet) if request.magnet else None
    if not info_hash:
        # 从 list_torrents 找最新的 dispatch-preview tag 的种子
        for _ in range(5):
            time.sleep(0.5)
            for t in qb.list_torrents() or []:
                if 'dispatch-preview' in (t.get('tags') or ''):
                    info_hash = t.get('hash', '').lower()
                    break
            if info_hash:
                break
    if not info_hash:
        raise HTTPException(status_code=502, detail='无法定位刚加入的种子 hash')

    # 轮询 metadata
    files = []
    name = ''
    elapsed = 0
    poll_interval = 1
    deadline = time.time() + request.metadata_timeout
    while time.time() < deadline:
        time.sleep(poll_interval)
        elapsed = int(time.time() - (deadline - request.metadata_timeout))
        # 看 list_torrents 的 size 字段（properties 偶尔不可靠）
        match = None
        for t in qb.list_torrents() or []:
            if t.get('hash', '').lower() == info_hash:
                match = t
                break
        if not match:
            continue
        size = int(match.get('size') or 0)
        if size > 0:
            files = qb.get_files(info_hash) or []
            name = match.get('name') or ''
            if files:
                break

    if not files:
        # 超时未拿到 metadata → 删除种子退回
        logger.warning(
            f"/preview metadata_timeout: hash={info_hash[:16]}.. elapsed={elapsed}s "
            f"deadline={request.metadata_timeout}s → 删种回退"
        )
        qb.delete(info_hash, delete_files=True)
        return {
            'ok': False,
            'reason': 'metadata_timeout',
            'message': f'{request.metadata_timeout}s 内未拿到 metadata（DHT/tracker 不通？）',
            'elapsed': elapsed,
        }

    # 识别类型
    from tools.dispatch.identify import identify_media
    identified = identify_media(name or 'unknown', files, user_hint=request.user_hint)

    # 重复检测（电影 / 剧集）
    duplicates = _check_duplicates(identified, files)

    # 路径推断
    target_info = _resolve_target(identified)

    # 保留种子在 qB 里 paused，等用户 confirm 后再 resume；超时取消
    # 用户没 confirm 时由 trash_cleaner 兜底（dispatch-preview tag 30 分钟自动清）
    qb.pause(info_hash)

    logger.info(
        f"/preview 完成: hash={info_hash[:16]}.. name={name[:60]!r} "
        f"files={len(files)} dup={len(duplicates) if duplicates else 0} "
        f"target={target_info.get('target_path')!r} elapsed={elapsed}s"
    )

    return {
        'ok': True,
        'torrent_hash': info_hash,
        'torrent_name': name,
        'files': [
            {'name': f.get('name'), 'size': f.get('size'), 'index': i}
            for i, f in enumerate(files)
        ],
        'total_size': sum(int(f.get('size') or 0) for f in files),
        'identified': identified,
        'target': target_info,
        'duplicates': duplicates,
        'elapsed_seconds': elapsed,
    }


class ConfirmRequest(BaseModel):
    torrent_hash: str
    media_type: str
    title: Optional[str] = None
    year: Optional[int] = None
    series_name: Optional[str] = None
    series_tmdb_id: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    tmdb_id: Optional[str] = None
    imdb_id: Optional[str] = None
    target_library_id: Optional[str] = None
    target_path: Optional[str] = None
    move_mode: Optional[str] = None
    skip_existing_files: Optional[List[int]] = None  # qB filePrio=0 跳过的 file index 列表


@router.post('/confirm')
def confirm_dispatch(request: ConfirmRequest):
    """
    用户确认后：写 dispatch_map(phase=pending) + 移除 dispatch-preview tag + resume 种子。
    pipeline 会拣起来跑全流程。
    """
    logger.info(
        f"/confirm 进入: hash={request.torrent_hash[:16]}.. "
        f"media_type={request.media_type!r} title={request.title!r} year={request.year!r} "
        f"target_library={request.target_library_id!r} target_path={request.target_path!r}"
    )
    from common.qbittorrent_client import QBittorrentClient
    qb = QBittorrentClient(
        host=settings.qbittorrent_host,
        username=settings.qbittorrent_username,
        password=settings.qbittorrent_password,
    )
    if not qb.login():
        raise HTTPException(status_code=502, detail='qB 登录失败')

    h = request.torrent_hash.lower()

    # 跳过已存在的集（剧集场景）
    if request.skip_existing_files:
        qb.set_file_priority(h, request.skip_existing_files, 0)

    # 移除 preview tag + 加 dispatched tag
    qb.remove_tags(h, ['dispatch-preview'])
    qb.set_category(h, request.media_type or '')
    if request.move_mode:
        qb.add_tags(h, [f'mode:{request.move_mode}'])

    # 前端送回的 target_path 是 backend view（如 Z:/videos/...），
    # DB 约定存 Jellyfin view（如 /library/videos/...），所以入库前 reverse-translate。
    # 不命中映射规则的路径会原样返回，所以同机部署不受影响。
    from backend.path_translator import reverse_translate_path_with_settings
    target_path_for_db = (
        reverse_translate_path_with_settings(request.target_path)
        if request.target_path else request.target_path
    )

    # 写 dispatch_map
    new_row = False
    with SessionLocal() as db:
        row = db.query(DownloadDispatchMap).filter_by(torrent_hash=h).first()
        if not row:
            row = DownloadDispatchMap(torrent_hash=h)
            db.add(row)
            new_row = True
        row.media_type = request.media_type
        row.title = request.title
        row.year = request.year
        row.series_name = request.series_name
        row.series_tmdb_id = request.series_tmdb_id
        row.tmdb_id = request.tmdb_id
        row.imdb_id = request.imdb_id
        row.target_library_id = request.target_library_id
        row.target_path = target_path_for_db
        row.move_mode = request.move_mode or 'copy'
        # 用户在 AddTorrentDialog 确认 → phase=dispatch_queued，让 downloader-watcher 接管
        from tools.dispatch.phases import PHASE_DISPATCH_QUEUED, STATUS_RUNNING
        row.phase = PHASE_DISPATCH_QUEUED
        row.phase_status = STATUS_RUNNING
        row.status_message = '已确认，等待下载完成'
        db.commit()
    logger.info(
        f"/confirm 写库完成: hash={h[:16]}.. {'新建' if new_row else '更新'} "
        f"phase=dispatch_queued/running target_path={target_path_for_db!r}"
    )

    # resume 种子开始下载
    qb.resume(h)

    # 唤醒 downloader-watcher 立刻看 qB 进度
    try:
        from tools.dispatch.downloader_watcher import trigger as dl_trigger
        dl_trigger.set()
    except Exception:
        pass

    return {'ok': True, 'torrent_hash': h, 'status': PHASE_DISPATCH_QUEUED}


class RecomputeTargetRequest(BaseModel):
    """用户在 AddTorrentDialog 改了 media_type 后重算路径。"""
    media_type: str
    title: Optional[str] = None
    year: Optional[int] = None
    series_name: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    code: Optional[str] = None
    tmdb_id: Optional[str] = None


@router.post('/preview/recompute-target')
def recompute_target(request: RecomputeTargetRequest):
    """
    根据 (media_type, metadata) 重算 target_library_id + target_path。
    用户在 AddTorrentDialog 切换类型后调，前端拿新路径填回 confirmForm。
    """
    identified = {
        'media_type': request.media_type,
        'title': request.title or '',
        'year': request.year,
        'series_name': request.series_name or '',
        'season': request.season,
        'episode': request.episode,
        'code': request.code or request.title or '',
        'tmdb_id': request.tmdb_id,
    }
    return _resolve_target(identified)


@router.delete('/preview/{torrent_hash}')
def cancel_preview(torrent_hash: str, delete_files: bool = True):
    """用户取消预览：从 qB 删除 + 不写 dispatch_map。"""
    from common.qbittorrent_client import QBittorrentClient
    qb = QBittorrentClient(
        host=settings.qbittorrent_host,
        username=settings.qbittorrent_username,
        password=settings.qbittorrent_password,
    )
    qb.login()
    qb.delete(torrent_hash, delete_files=delete_files)
    return {'ok': True}


# ============================================================================
# helpers
# ============================================================================

def _extract_hash(magnet: str) -> Optional[str]:
    import re
    m = re.search(r'btih:([0-9a-fA-F]{40})', magnet or '')
    return m.group(1).lower() if m else None


def _match_existing_library_dir(identified: Dict, media_type: str, library_root: str):
    """tv/anime/movie：用 ID/name 匹配 jellyfin 库里已存在的同一作品目录。

    命中返回 jellyfin 视角的 target_path（剧目录 + 季子目录 / 电影目录），否则 None。
    匹配优先级 tmdb → imdb → name 归一化（见 library_matcher）。只复用目标库根下的目录。
    任何异常都吞掉返回 None（退回模板渲染），绝不阻断 dispatch。
    """
    if media_type not in ('tv', 'anime', 'movie'):
        return None
    try:
        from common.jellyfin_client import JellyfinClient
        from tools.dispatch.library_matcher import match_library_dir, choose_season_dirname

        jf = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
        items = jf.get_all_movies() if media_type == 'movie' else jf.get_all_series()

        root = (library_root or '').rstrip('/')
        candidates = []
        for it in items or []:
            path = (it.get('Path') or '').rstrip('/')
            if media_type == 'movie':
                # movie item 的 Path 是电影文件本身，取父目录作为电影目录
                path = path.rsplit('/', 1)[0] if '/' in path else ''
            if not path:
                continue
            # 只复用目标库根下的目录，避免落到别的库
            if root and not (path == root or path.startswith(root + '/')):
                continue
            pid = it.get('ProviderIds') or {}
            candidates.append({
                'name': it.get('Name'),
                'path': path,
                'tmdb': pid.get('Tmdb'),
                'imdb': pid.get('Imdb'),
                'id': it.get('Id'),
            })

        hit = match_library_dir(
            candidates,
            tmdb_id=identified.get('series_tmdb_id') or identified.get('tmdb_id'),
            imdb_id=identified.get('imdb_id'),
            name=identified.get('series_name') or identified.get('title'),
        )
        if not hit:
            return None

        series_dir = hit['path']
        if media_type == 'movie':
            logger.info(f"resolve_target: 复用已有电影目录 {series_dir}")
            return series_dir

        season = identified.get('season')
        if not season:
            logger.info(f"resolve_target: 复用已有剧目录 {series_dir}（无季号，落剧目录）")
            return series_dir

        # 季子目录风格跟随该剧已有季（用 jellyfin 已扫到的季 Path 末段推断）
        existing = []
        try:
            seasons = jf.get_seasons_of_series(hit['id']) if hit.get('id') else []
            existing = [(s.get('Path') or '').rstrip('/').rsplit('/', 1)[-1]
                        for s in seasons if s.get('Path')]
        except Exception:
            existing = []
        season_dir = choose_season_dirname(existing, int(season))
        full = f"{series_dir}/{season_dir}"
        logger.info(f"resolve_target: 复用已有剧目录 {series_dir} + 季目录 {season_dir} → {full}")
        return full
    except Exception as e:
        logger.warning(f"resolve_target: 匹配已有库目录失败，退回模板: {e}")
        return None


def _resolve_target(identified: Dict) -> Dict:
    """
    根据 identified 推断 target_library_id + target_path。

    关键：jellyfin 报的 library locations 可能是 jellyfin 视角的路径
    （如 Linux 容器里 /library/movies），后端运行在 Windows 上挂载为 Z:/movies。
    这里走 path_translator 把 library_root 转为后端可访问的本地路径。
    """
    media_type = identified.get('media_type')
    if not media_type or media_type == 'unknown':
        return {'library_id': '', 'library_name': '', 'target_path': '', 'rule': None}

    rule_obj = settings.dispatch.rules.get(media_type)
    if not rule_obj:
        return {'library_id': '', 'library_name': '', 'target_path': '', 'rule': None}

    rule = rule_obj.model_dump()
    lib_id = rule.get('library_id') or ''

    library_root = ''
    library_name = ''
    if lib_id:
        try:
            from backend.api.medialibraries import get_library_by_id
            lib = get_library_by_id(lib_id)
            if lib:
                library_name = lib.get('name', '')
                locations = lib.get('locations') or []
                if locations:
                    library_root = locations[0]
        except Exception as e:
            logger.warning(f"resolve_target: get_library_by_id 失败: {e}")

    # ⚠️ library_root 保持 Jellyfin 视角不翻译——DB 里 target_path 存"Jellyfin 视角的目标路径"，
    # 由消费方（pipeline._step_copy）自己 translate_path_with_settings 转本机；
    # 前端展示也由 API 层翻译。这样 DB 内容跟 Jellyfin 一致，不绑定具体宿主机的盘符。

    # 渲染 location_template
    # 关键防御：模板里某变量为空就不渲染。比如 movie 模板 '{library_root}/{title} ({year})'
    # 若 title=None 直接用 '' 填进去，会生成 '/library/videos/movie/ ()' 这种垃圾路径但 truthy，
    # 通过上游 auto_ok 校验直接落库 → adopt 流程吐出无效任务。
    target_path = ''
    if library_root:
        # 按 media_type 检查必填字段；任一缺失 → 不渲染 target_path
        title = identified.get('title')
        year = identified.get('year')
        series_name = identified.get('series_name')
        code = identified.get('code')
        anime_name = series_name or title  # anime 用 series_name 或 title 兜底
        required_ok = False
        if media_type == 'movie':
            # year 可缺（老片/未知年份种子允许只靠标题落地）
            required_ok = bool(title)
        elif media_type == 'tv':
            required_ok = bool(series_name)
        elif media_type == 'anime':
            required_ok = bool(anime_name)
        elif media_type == 'adult':
            required_ok = bool(code) or bool(title)
        if not required_ok:
            logger.info(
                f"resolve_target: 必填字段缺失，跳过 target_path 渲染 "
                f"(media_type={media_type}, title={title!r}, year={year!r}, "
                f"series_name={series_name!r}, code={code!r})"
            )
        else:
            # 先尝试复用 jellyfin 库里已存在的同一作品目录（ID 匹配优先，name 归一化兜底）；
            # 命中则用其真实目录，避免因命名差异（点/空格/年份）新建重复目录。未命中退回模板。
            matched = _match_existing_library_dir(identified, media_type, library_root)
            try:
                from tools.dispatch.template_render import render_template, sanitize_path
                if matched:
                    target_path = sanitize_path(matched)
                else:
                    # render_template 处理空值智能去括号 + 多空格折叠；空值不再传 '' 兜底，
                    # 直接传 None 让 render_template 决定要不要保留周围的字面字符
                    target_path = render_template(rule['location_template'], {
                        'library_root': library_root,
                        'title': title,
                        'year': year,
                        'series_name': series_name,
                        'anime_name': anime_name,
                        'season': identified.get('season'),
                        'episode': identified.get('episode'),
                        'code': code or title,
                    })
                    # sanitize 每个路径段（去 <>:"|?* + 控制字符 + Windows 保留名 + 截长度）；
                    # 保留 / 盘符
                    target_path = sanitize_path(target_path)
            except Exception as e:
                logger.warning(f"location_template 渲染失败: {e}")

    return {
        'library_id': lib_id,
        'library_name': library_name,
        'library_root': library_root,
        'target_path': target_path,
        # move_mode 改为全局 default_move_mode（不再 per-rule 覆盖）
        'move_mode': settings.dispatch.default_move_mode or 'copy',
        'rule': rule,
    }


def _check_duplicates(identified: Dict, files: List[Dict]) -> Dict:
    """
    重复检测：
      - 电影：按 tmdb_id / imdb_id 查 jellyfin
      - 剧集：列出已有 episodes，对比种子内的 SxxExx
    返回 {is_duplicate: bool, type: 'movie'|'tv'|'partial'|'none', existing: [...], new: [...]}
    """
    media_type = identified.get('media_type')
    out = {'is_duplicate': False, 'type': 'none', 'existing': [], 'new': []}

    # 通过 jellyfin /Items?AnyProviderIdEquals=... 查重
    # ⚠️ Jellyfin AnyProviderIdEquals 在某些版本/情况会被静默忽略（库里 item 没对应
    #    ProviderId 时降级返回全部 item），必须 Python 层二次校验 ProviderIds。
    def _search_by_tmdb(tmdb_id: str, item_type: str) -> List[Dict]:
        if not tmdb_id:
            return []
        tmdb_id_str = str(tmdb_id).strip()
        if not tmdb_id_str or tmdb_id_str in ('0', 'None'):
            return []
        try:
            from common.jellyfin_client import JellyfinClient
            c = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
            r = c._request('GET', '/Items', params={
                'Recursive': 'true',
                'IncludeItemTypes': item_type,
                'AnyProviderIdEquals': f'Tmdb.{tmdb_id_str}',
                'Fields': 'Path,ProviderIds,ParentIndexNumber,IndexNumber,SeriesId',
                'Limit': 200,
            })
            raw_items = (r or {}).get('Items') or []
            # ⚠️ 二次校验：query filter 可能被 jellyfin 忽略（返回全库 Movie），
            #    Python 层强制比对 ProviderIds.Tmdb 再认定为命中
            filtered = []
            for it in raw_items:
                pids = it.get('ProviderIds') or {}
                # ProviderIds key 大小写可能不一致，做不区分大小写的查找
                item_tmdb = None
                for k, v in pids.items():
                    if k.lower() == 'tmdb':
                        item_tmdb = str(v).strip()
                        break
                if item_tmdb == tmdb_id_str:
                    filtered.append(it)
            if len(raw_items) != len(filtered):
                logger.info(
                    f"jellyfin 查重 ({item_type} tmdb={tmdb_id_str}) 二次过滤："
                    f"原始 {len(raw_items)} 条 → 真匹配 {len(filtered)} 条 "
                    f"（query filter 被忽略，已 Python 端纠正）"
                )
            return filtered
        except Exception as e:
            logger.warning(f"jellyfin 查重 ({item_type} tmdb={tmdb_id}) 失败: {e}")
            return []

    if media_type == 'movie':
        tmdb_id = identified.get('tmdb_id')
        if tmdb_id:
            results = _search_by_tmdb(tmdb_id, 'Movie')
            if results:
                from backend.path_translator import translate_path_with_settings
                out['is_duplicate'] = True
                out['type'] = 'movie'
                out['existing'] = [
                    {'name': r.get('Name'), 'path': translate_path_with_settings(r.get('Path'))}
                    for r in results[:5]
                ]
    elif media_type in ('tv', 'anime'):
        import re
        ep_re = re.compile(r'[Ss](\d{1,2})[\s._-]?[Ee](\d{1,3})')
        torrent_eps = set()
        file_ep_map = {}
        for i, f in enumerate(files):
            m = ep_re.search(f.get('name') or '')
            if m:
                key = (int(m.group(1)), int(m.group(2)))
                torrent_eps.add(key)
                file_ep_map[key] = i
        if not torrent_eps:
            return out

        series_tmdb_id = identified.get('series_tmdb_id') or identified.get('tmdb_id')
        if not series_tmdb_id:
            return out

        # 拿 series 下所有 episodes
        eps = _search_by_tmdb(series_tmdb_id, 'Episode')
        existing_eps = set()
        for ep in eps:
            s = ep.get('ParentIndexNumber')
            e = ep.get('IndexNumber')
            if s is not None and e is not None:
                existing_eps.add((int(s), int(e)))

        new_eps = torrent_eps - existing_eps
        dup_eps = torrent_eps & existing_eps
        if dup_eps and not new_eps:
            out['type'] = 'tv'
            out['is_duplicate'] = True
        elif dup_eps and new_eps:
            out['type'] = 'partial'
            out['is_duplicate'] = True
        out['existing'] = sorted([f"S{s:02d}E{e:02d}" for s, e in dup_eps])
        out['new'] = sorted([f"S{s:02d}E{e:02d}" for s, e in new_eps])
        out['skip_file_indexes'] = [
            file_ep_map[ep] for ep in dup_eps if ep in file_ep_map
        ]

    elif media_type == 'adult':
        # 成人内容查重：按番号查我们自己的 AdultItem 表（不走 Jellyfin）
        # AdultItem.code 唯一约束；命中即视为完整重复
        code = (identified.get('code') or identified.get('title') or '').strip().upper()
        if code:
            try:
                from backend.database import SessionLocal, AdultItem
                with SessionLocal() as _db:
                    existing = _db.query(AdultItem).filter(AdultItem.code == code).first()
                    if existing:
                        out['is_duplicate'] = True
                        out['type'] = 'adult'
                        out['existing'] = [{
                            'name': existing.title or code,
                            'path': existing.file_path or '',
                            'code': code,
                        }]
            except Exception as e:
                logger.warning(f"adult 查重失败 code={code!r}: {e}")

    return out


# ============================================================================
# RSS 透传（read-only：feeds + rules + 命中历史）
# ============================================================================

def _qb():
    from common.qbittorrent_client import QBittorrentClient
    return QBittorrentClient(
        settings.qbittorrent_host, settings.qbittorrent_username, settings.qbittorrent_password,
    )


@router.get('/rss/feeds')
def rss_feeds():
    """qB 配置的 RSS feeds 列表 + 最新 articles（带数据）。

    with_data=True：让 qB 把每个 feed 的 articles 数组带回来，前端要列出条目。
    """
    client = _qb()
    if not client.login():
        raise HTTPException(status_code=502, detail='qB 登录失败')
    items = client.rss_items(with_data=True) or {}
    return items


@router.get('/rss/rules')
def rss_rules():
    """qB auto-download 规则列表。"""
    client = _qb()
    if not client.login():
        raise HTTPException(status_code=502, detail='qB 登录失败')
    return client.rss_rules() or {}


@router.get('/rss/matching/{rule_name}')
def rss_matching(rule_name: str):
    """看某条规则匹配了哪些 articles。"""
    client = _qb()
    if not client.login():
        raise HTTPException(status_code=502, detail='qB 登录失败')
    return client.rss_matching_articles(rule_name) or {}


class RssRefreshRequest(BaseModel):
    item_path: str


@router.post('/rss/refresh')
def rss_refresh(request: RssRefreshRequest):
    """强制刷新某个 feed。"""
    client = _qb()
    if not client.login():
        raise HTTPException(status_code=502, detail='qB 登录失败')
    return {'ok': client.rss_refresh_item(request.item_path)}


@router.post('/rss/refresh-all')
def rss_refresh_all():
    """让 qB 重新抓取所有 feed 的上游数据（不带 articles 数据，只看 feed 列表）。"""
    client = _qb()
    if not client.login():
        raise HTTPException(status_code=502, detail='qB 登录失败')
    items = client.rss_items(with_data=False) or {}
    feeds = [k for k in items.keys() if k and k != 'uid']
    success = 0
    for k in feeds:
        try:
            if client.rss_refresh_item(k):
                success += 1
        except Exception as e:
            logger.warning(f"刷新 RSS feed {k!r} 失败: {e}")
    return {'ok': True, 'total': len(feeds), 'success': success}


class RssAddFeedRequest(BaseModel):
    url: str
    path: Optional[str] = None  # qB 显示名，不传 qB 用 feed title


@router.post('/rss/add')
def rss_add_feed(request: RssAddFeedRequest):
    """添加 RSS feed 订阅。"""
    client = _qb()
    if not client.login():
        raise HTTPException(status_code=502, detail='qB 登录失败')
    if not request.url or not request.url.strip():
        raise HTTPException(status_code=400, detail='feed URL 必填')
    ok = client.rss_add_feed(request.url.strip(), (request.path or '').strip() or None)
    if not ok:
        raise HTTPException(status_code=502, detail='qB 添加 feed 失败（URL 是否有效？）')
    return {'ok': True}


class RssRemoveRequest(BaseModel):
    item_path: str


@router.post('/rss/remove')
def rss_remove(request: RssRemoveRequest):
    """删除 RSS feed / 文件夹。"""
    client = _qb()
    if not client.login():
        raise HTTPException(status_code=502, detail='qB 登录失败')
    return {'ok': client.rss_remove_item(request.item_path)}


class RssMarkReadRequest(BaseModel):
    item_path: str
    article_id: Optional[str] = None  # 不传则整 feed 全标已读


@router.post('/rss/mark-read')
def rss_mark_read(request: RssMarkReadRequest):
    """把 article 标记为已读（不传 article_id 则整 feed 全标）。"""
    client = _qb()
    if not client.login():
        raise HTTPException(status_code=502, detail='qB 登录失败')
    return {'ok': client.rss_mark_as_read(request.item_path, request.article_id)}


@router.get('/rss/settings')
def rss_settings_get():
    """读 qB 当前 RSS 相关设置（启用状态、上限、间隔）。"""
    client = _qb()
    if not client.login():
        raise HTTPException(status_code=502, detail='qB 登录失败')
    prefs = client.app_preferences() or {}
    return {
        'rss_processing_enabled': bool(prefs.get('rss_processing_enabled')),
        'rss_auto_downloading_enabled': bool(prefs.get('rss_auto_downloading_enabled')),
        'rss_max_articles_per_feed': int(prefs.get('rss_max_articles_per_feed') or 50),
        'rss_refresh_interval': int(prefs.get('rss_refresh_interval') or 30),
    }


class RssSettingsUpdateRequest(BaseModel):
    rss_processing_enabled: Optional[bool] = None
    rss_auto_downloading_enabled: Optional[bool] = None
    rss_max_articles_per_feed: Optional[int] = None
    rss_refresh_interval: Optional[int] = None


@router.post('/rss/settings')
def rss_settings_set(request: RssSettingsUpdateRequest):
    """改 qB RSS 设置。仅传需要变更的字段。"""
    client = _qb()
    if not client.login():
        raise HTTPException(status_code=502, detail='qB 登录失败')
    payload = {k: v for k, v in request.model_dump().items() if v is not None}
    if not payload:
        return {'ok': True, 'changed': {}}
    ok = client.app_set_preferences(payload)
    if not ok:
        raise HTTPException(status_code=502, detail='qB 写入设置失败')
    return {'ok': True, 'changed': payload}


# ============ RSS 规则 CRUD ============

class RssRuleDef(BaseModel):
    """qB auto-download 规则字段。仅必要字段；未传字段沿用 qB 当前值/默认。"""
    enabled: Optional[bool] = True
    mustContain: Optional[str] = ''
    mustNotContain: Optional[str] = ''
    useRegex: Optional[bool] = False
    episodeFilter: Optional[str] = ''
    smartFilter: Optional[bool] = False
    affectedFeeds: Optional[List[str]] = None
    savePath: Optional[str] = ''
    assignedCategory: Optional[str] = ''
    ignoreDays: Optional[int] = 0
    addPaused: Optional[bool] = None  # null/true/false


@router.post('/rss/rules/{rule_name}')
def rss_rule_set(rule_name: str, rule: RssRuleDef):
    """创建或更新规则（同名覆盖）。"""
    client = _qb()
    if not client.login():
        raise HTTPException(status_code=502, detail='qB 登录失败')
    if not rule_name.strip():
        raise HTTPException(status_code=400, detail='规则名不能为空')

    # 拼 ruleDef：跳过 None 字段（让 qB 用默认）
    rule_dict = {k: v for k, v in rule.model_dump().items() if v is not None}
    # affectedFeeds 必须是 list（None 已过滤），qB 接收空 list 表示对所有 feed 生效
    if 'affectedFeeds' not in rule_dict:
        rule_dict['affectedFeeds'] = []
    ok = client.rss_set_rule(rule_name.strip(), rule_dict)
    if not ok:
        raise HTTPException(status_code=502, detail='qB 写入规则失败')
    return {'ok': True}


class LLMRegexRequest(BaseModel):
    """LLM 辅助生成 RSS 规则的必含 / 必不含正则。"""
    description: str    # 用户自然语言描述意图，如 "1080p 蓝光整季的电视剧，排单集 / 重制"
    sample_titles: Optional[List[str]] = None  # 可选：贴几条 feed 标题给 LLM 参考


@router.post('/rss/llm-regex')
def rss_llm_regex(request: LLMRegexRequest):
    """
    用 LLM 把"自然语言意图 → 必含 / 必不含正则"。
    返回 { must_contain, must_not_contain, use_regex, explanation }。
    use_regex 由 LLM 自决：简单关键词组合给 false（让 qB 走默认 AND 语义），
    需要回溯 / 量词时给 true。
    """
    desc = (request.description or '').strip()
    if not desc:
        raise HTTPException(status_code=400, detail='description 不能为空')

    from common.llm_client import get_default_client
    if not (settings.llm and settings.llm.api_key):
        raise HTTPException(
            status_code=400,
            detail='LLM 未配置。请先去设置 → LLM 配置 api_key + base_url',
        )

    sys_prompt = (
        '你是 qBittorrent RSS 自动下载规则的专家，专门把用户的自然语言描述转换成 '
        'qB 规则的「必含」和「必不含」字段。\n\n'
        'qB 规则字段语义：\n'
        '- mustContain：标题必须命中（默认词与词之间是 AND，多关键词空格分隔）\n'
        '- mustNotContain：标题命中即排除（同样 AND 语义）\n'
        '- useRegex=false 时按「空格分词 AND」解析；true 时整段当 Python 风格正则（PCRE 子集）\n\n'
        '**输出 JSON 严格如下**（不要 markdown 围栏）：\n'
        '{\n'
        '  "must_contain": "...",\n'
        '  "must_not_contain": "...",\n'
        '  "use_regex": true | false,\n'
        '  "explanation": "1-2 句解释你的选择"\n'
        '}\n\n'
        '决策原则：\n'
        '- 简单关键词组合（如「1080p WEB-DL 简体」）→ use_regex=false，空格分词\n'
        '- 需要量词 / 选择 / 锚点（如 \\d{2}E\\d{2}、(720p|HDTV)、^.*\\.S\\d+\\.）→ use_regex=true\n'
        '- mustContain 和 mustNotContain 的 use_regex 是**同一开关**：要正则就两边都正则\n'
        '- 集数过滤建议**留空**（qB 有独立的 episodeFilter 字段处理 SxxExx，比正则简洁）\n'
        '- 不要写 ^ $ 边界（qB 规则匹配的是子串）\n'
        '- 字段写空字符串表示「不限」\n\n'
        '常见模板举例：\n'
        '- 「蓝光 1080p 简体」→ must_contain=「1080p BluRay 简体」/ must_not_contain=「」/ use_regex=false\n'
        '- 「整季排单集」→ must_contain=「\\.S\\d{2}\\.」/ must_not_contain=「S\\d{2}E\\d{2}」/ use_regex=true\n'
        '- 「排低画质和重制」→ must_not_contain=「(720p|480p|HDTV|REPACK|PROPER|CAM|TS)」/ use_regex=true\n'
    )

    user_msg = f"用户意图：{desc}"
    if request.sample_titles:
        sample = '\n'.join(f"  - {t}" for t in request.sample_titles[:10])
        user_msg += f"\n\nfeed 中已有的样本标题（参考）：\n{sample}"
    user_msg += "\n\n按上述 JSON 格式输出，只输出 JSON。"

    try:
        client = get_default_client()
        # 关掉 reasoning：生成正则是结构化短任务，思考链会让响应翻倍变慢且大概率超时
        result = client.chat_json(
            sys_prompt, user_msg,
            temperature=0.2,
            disable_reasoning=True,
        )
    except Exception as e:
        logger.exception("LLM 生成 RSS 规则失败")
        raise HTTPException(status_code=502, detail=f'LLM 调用失败: {e}')

    # 字段标准化
    return {
        'must_contain': str(result.get('must_contain') or '').strip(),
        'must_not_contain': str(result.get('must_not_contain') or '').strip(),
        'use_regex': bool(result.get('use_regex')),
        'explanation': str(result.get('explanation') or '').strip(),
    }


class RssRuleRenameRequest(BaseModel):
    new_name: str


@router.post('/rss/rules/{rule_name}/rename')
def rss_rule_rename(rule_name: str, request: RssRuleRenameRequest):
    """重命名规则。"""
    client = _qb()
    if not client.login():
        raise HTTPException(status_code=502, detail='qB 登录失败')
    new_name = (request.new_name or '').strip()
    if not new_name:
        raise HTTPException(status_code=400, detail='新名字不能为空')
    if new_name == rule_name:
        return {'ok': True, 'unchanged': True}
    ok = client.rss_rename_rule(rule_name, new_name)
    if not ok:
        raise HTTPException(status_code=502, detail='qB 重命名失败')
    return {'ok': True}


@router.delete('/rss/rules/{rule_name}')
def rss_rule_delete(rule_name: str):
    """删除规则。"""
    client = _qb()
    if not client.login():
        raise HTTPException(status_code=502, detail='qB 登录失败')
    ok = client.rss_remove_rule(rule_name)
    if not ok:
        raise HTTPException(status_code=502, detail='qB 删除规则失败')
    return {'ok': True}




@router.get('/items')
def list_dispatch_items(
    phase: Optional[str] = Query(None, description='filter by phase'),
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """列出 dispatch_map 行（dispatch UI 详情用）。"""
    q = db.query(DownloadDispatchMap)
    if phase:
        q = q.filter(DownloadDispatchMap.phase == phase)
    rows = q.order_by(DownloadDispatchMap.created_at.desc()).limit(limit).all()

    return {
        'count': len(rows),
        'items': [
            {
                'torrent_hash': r.torrent_hash,
                'title': r.title,
                'media_type': r.media_type,
                'phase': r.phase,
                'phase_status': r.phase_status,
                'status_message': r.status_message,
                'phase_timings': r.phase_timings,
                'copy_bytes_done': int(r.copy_bytes_done or 0),
                'copy_bytes_total': int(r.copy_bytes_total or 0),
                'last_seen_ratio': float(r.last_seen_ratio or 0),
                'seeded_seconds': int(r.seeded_seconds or 0),
                'created_at': r.created_at.isoformat() if r.created_at else None,
                'dispatched_at': r.dispatched_at.isoformat() if r.dispatched_at else None,
                'cleaned_at': r.cleaned_at.isoformat() if r.cleaned_at else None,
                'target_path': r.target_path,
            }
            for r in rows
        ],
    }


# ============================================================================
# 待审核（needs_review）：来自 adopt 自动认领时低置信的孤儿种子
# ============================================================================

@router.get('/needs-review')
def list_needs_review(db: Session = Depends(get_db)):
    """
    列出 phase_status=needs_review 的种子，给前端"待审核"面板用。
    待审核期间 phase=analyzing。
    """
    from tools.dispatch.phases import PHASE_ANALYZING, STATUS_NEEDS_REVIEW
    rows = (
        db.query(DownloadDispatchMap)
        .filter(DownloadDispatchMap.phase == PHASE_ANALYZING)
        .filter(DownloadDispatchMap.phase_status == STATUS_NEEDS_REVIEW)
        .order_by(DownloadDispatchMap.created_at.desc())
        .all()
    )
    if not rows:
        return {'count': 0, 'items': []}

    # 从 qB 拉一次种子列表，取 name / size / state 等给前端展示
    qb_by_hash: Dict[str, Dict] = {}
    try:
        from common.qbittorrent_client import QBittorrentClient
        if settings.qbittorrent_configured:
            qb = QBittorrentClient(
                settings.qbittorrent_host,
                settings.qbittorrent_username,
                settings.qbittorrent_password,
            )
            for t in (qb.list_torrents() or []):
                qb_by_hash[(t.get('hash') or '').lower()] = t
    except Exception as e:
        logger.warning(f"needs-review 拉 qB 失败: {e}")

    # Jellyfin 库 id → name 映射
    lib_id_to_name: Dict[str, str] = {}
    try:
        from common.jellyfin_client import JellyfinClient
        if settings.jellyfin_api_key:
            jc = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
            for lib in (jc.get_libraries_normalized() or []):
                lib_id_to_name[lib['id']] = lib['name']
    except Exception as e:
        logger.warning(f"needs-review 拉库列表失败: {e}")

    items = []
    for r in rows:
        qb_t = qb_by_hash.get(r.torrent_hash) or {}
        items.append({
            'torrent_hash': r.torrent_hash,
            'torrent_name': qb_t.get('name'),
            'size': qb_t.get('size'),
            'state': qb_t.get('state'),
            'progress': qb_t.get('progress') or 0,
            # 我们的最佳猜测
            'media_type': r.media_type,
            'title': r.title,
            'year': r.year,
            'series_name': r.series_name,
            'target_library_id': r.target_library_id,
            'target_library_name': lib_id_to_name.get(r.target_library_id) if r.target_library_id else None,
            'target_path': r.target_path,
            'move_mode': r.move_mode,
            'status_message': r.status_message,    # 人话原因
            'created_at': r.created_at.isoformat() if r.created_at else None,
        })

    return {'count': len(items), 'items': items}


class NeedsReviewConfirm(BaseModel):
    """用户审核后的最终决策。"""
    media_type: str                              # 必填：movie / tv / anime / adult
    target_library_id: Optional[str] = None      # 留空则按 media_type 规则自动算
    target_path: Optional[str] = None            # 同上
    title: Optional[str] = None
    year: Optional[int] = None
    series_name: Optional[str] = None
    move_mode: Optional[str] = None              # 不传走规则默认


@router.post('/needs-review/{torrent_hash}/confirm')
def confirm_needs_review(
    torrent_hash: str,
    request: NeedsReviewConfirm,
    db: Session = Depends(get_db),
):
    """
    用户确认审核 → 把行里 phase_status 从 'needs_review' 改成 'running'，
    流水线下一轮 _claim_next_pending 就会接管。
    """
    h = (torrent_hash or '').lower()
    row = db.query(DownloadDispatchMap).filter_by(torrent_hash=h).first()
    if not row:
        raise HTTPException(status_code=404, detail='dispatch_map 行不存在')
    if row.phase_status != 'needs_review':
        raise HTTPException(status_code=400, detail=f'当前状态非 needs_review（{row.phase_status}）')

    row.media_type = request.media_type
    if request.title is not None:
        row.title = request.title
    if request.year is not None:
        row.year = request.year
    if request.series_name is not None:
        row.series_name = request.series_name

    # target_library_id / target_path：用户没填就走规则重算（基于新 media_type）
    if request.target_library_id and request.target_path:
        # 同样：前端送的 backend view → reverse-translate 成 Jellyfin view 再入库
        from backend.path_translator import reverse_translate_path_with_settings
        row.target_library_id = request.target_library_id
        row.target_path = reverse_translate_path_with_settings(request.target_path) or request.target_path
    else:
        identified = {
            'media_type': request.media_type,
            'title': request.title or row.title,
            'year': request.year or row.year,
            'series_name': request.series_name or row.series_name,
        }
        target_info = _resolve_target(identified)
        if not target_info.get('library_id') or not target_info.get('target_path'):
            raise HTTPException(
                status_code=400,
                detail=f'无法为 media_type={request.media_type} 推算目标，请在 UI 手动填写',
            )
        row.target_library_id = target_info['library_id']
        row.target_root = target_info.get('library_root')
        row.target_path = target_info['target_path']

    # 审核通过 → 看 qB 当前进度决定起始 phase：
    #   ① 已下完（adopt 来的孤儿在审核期间可能下完，或本来就是已下完才进审核） → 直接 download_done
    #   ② 还在下/还没下 → dispatch_queued，让 downloader-watcher 接管
    from tools.dispatch.phases import (
        PHASE_DISPATCH_QUEUED, PHASE_DOWNLOAD_DONE, STATUS_RUNNING,
        is_qb_download_done,
    )

    qb = None
    is_done = False
    try:
        from common.qbittorrent_client import QBittorrentClient
        if settings.qbittorrent_configured:
            qb = QBittorrentClient(
                settings.qbittorrent_host,
                settings.qbittorrent_username,
                settings.qbittorrent_password,
            )
            qb_t = qb.get_torrent_info(h) or {}
            is_done = is_qb_download_done(
                qb_t.get('state') or '',
                float(qb_t.get('progress') or 0.0),
            )
    except Exception as e:
        logger.warning(f"审核通过时查 qB 状态失败（按未下完处理）: {e}")

    if is_done:
        row.phase = PHASE_DOWNLOAD_DONE
        row.phase_status = STATUS_RUNNING
        row.status_message = '已通过审核（qB 已下完），等待流水线接管'
    else:
        row.phase = PHASE_DISPATCH_QUEUED
        row.phase_status = STATUS_RUNNING
        row.status_message = '已通过审核，等待下载完成'

    if request.move_mode:
        row.move_mode = request.move_mode

    # 顺手把 qB 端的 category 同步成新 media_type，方便人工调试时辨认
    if qb is not None:
        try:
            qb.set_category(h, request.media_type)
            # 审核通过 → 恢复种子下载（analyzing 阶段 stop_condition='MetadataReceived' 让它停在那）
            # 已下完的种子 resume 也是无副作用的（state=pausedUP 时 resume 转回 uploading）
            qb.resume(h)
        except Exception as e:
            logger.warning(f"set_category / resume 失败（不致命）: {e}")

    db.commit()
    # 已下完 → 直接喊 pipeline；否则喊 downloader-watcher
    try:
        if is_done:
            from tools.dispatch.pipeline_worker import trigger as pipeline_trigger
            pipeline_trigger.set()
        else:
            from tools.dispatch.downloader_watcher import trigger as dl_trigger
            dl_trigger.set()
    except Exception:
        pass
    return {'ok': True, 'torrent_hash': h}


@router.post('/needs-review/{torrent_hash}/dismiss')
def dismiss_needs_review(
    torrent_hash: str,
    delete_torrent: bool = Query(False, description='同时从 qB 删除种子'),
    delete_files: bool = Query(False, description='delete_torrent=True 时是否连文件一起删'),
    db: Session = Depends(get_db),
):
    """
    用户拒绝审核：把 dispatch_map 行标记为 phase=dismissed 而不是物理删除。
    下次 adopt 扫描时该 hash 在 known_set 里 → 不会被重新当成孤儿认领。

    三档语义：
      ① delete_torrent=False                 → 仅 dismiss（种子保留 + 文件保留）
      ② delete_torrent=True, delete_files=False → 移除 qB 种子（文件保留）
      ③ delete_torrent=True, delete_files=True  → 移除 qB 种子 + 删文件
    """
    h = (torrent_hash or '').lower()
    row = db.query(DownloadDispatchMap).filter_by(torrent_hash=h).first()
    if not row:
        raise HTTPException(status_code=404, detail='dispatch_map 行不存在')
    from tools.dispatch.phases import PHASE_DISMISSED, STATUS_NEEDS_REVIEW, STATUS_SUCCEEDED
    if row.phase_status != STATUS_NEEDS_REVIEW:
        raise HTTPException(status_code=400, detail=f'当前状态非 needs_review（{row.phase_status}）')

    # 三档处理 dispatch_map 行：
    #   ① 仅拒绝（种子+文件都留）→ 标 dismissed，让 adopt 跳过这条还在 qB 的种子
    #   ② 移除种子/移除文件 → **直接删行**，用户已主动清理，以后重加同 hash 应能从头跑
    if delete_torrent:
        db.delete(row)
        db.commit()
        logger.info(f"dismiss: 删除 dispatch_map 行 hash={h[:16]}.. (用户选择移除种子/文件)")
    else:
        row.phase = PHASE_DISMISSED
        row.phase_status = STATUS_SUCCEEDED
        row.status_message = '用户拒绝审核'
        row.cleaned_at = datetime.utcnow()
        db.commit()
        logger.info(f"dismiss: 标记 dispatch_map 为 dismissed hash={h[:16]}.. (种子和文件都保留)")

    qb_delete_ok = False
    if delete_torrent:
        try:
            from common.qbittorrent_client import QBittorrentClient
            if settings.qbittorrent_configured:
                qb = QBittorrentClient(
                    settings.qbittorrent_host,
                    settings.qbittorrent_username,
                    settings.qbittorrent_password,
                )
                logger.info(f"dismiss: 调用 qB delete hash={h[:16]}.. delete_files={delete_files}")
                qb.delete(h, delete_files=delete_files)
                # 后置校验：再查一次确认真没了
                import time
                time.sleep(0.5)
                still_there = any(
                    (t.get('hash') or '').lower() == h
                    for t in (qb.list_torrents() or [])
                )
                if still_there:
                    logger.warning(
                        f"dismiss: qB delete 调用 200 但 0.5s 后种子仍在 qB 上 "
                        f"(hash={h[:16]}..)，可能 qB 内部正在处理或被 lock"
                    )
                else:
                    qb_delete_ok = True
                    logger.info(f"dismiss: qB delete 已生效 hash={h[:16]}..")
        except Exception as e:
            logger.warning(f"qB 删除失败（不致命）: {e}")

    return {
        'ok': True, 'torrent_hash': h,
        'deleted_qb_torrent': delete_torrent,
        'deleted_files': delete_torrent and delete_files,
        'qb_delete_verified': qb_delete_ok,
    }


@router.post('/needs-review/{torrent_hash}/restore')
def restore_dismissed(torrent_hash: str, db: Session = Depends(get_db)):
    """
    把已拒绝（phase=dismissed）的行恢复到 needs_review 状态。
    用户反悔时用：phase 改回 analyzing + phase_status=needs_review，
    重新出现在待审核列表里供处理。
    """
    h = (torrent_hash or '').lower()
    row = db.query(DownloadDispatchMap).filter_by(torrent_hash=h).first()
    if not row:
        raise HTTPException(status_code=404, detail='dispatch_map 行不存在')
    from tools.dispatch.phases import PHASE_DISMISSED, PHASE_ANALYZING, STATUS_NEEDS_REVIEW
    if row.phase != PHASE_DISMISSED:
        raise HTTPException(status_code=400, detail=f'当前 phase 非 dismissed（{row.phase}）')

    row.phase = PHASE_ANALYZING
    row.phase_status = STATUS_NEEDS_REVIEW
    row.status_message = '从已拒绝列表恢复，等待重新审核'
    row.cleaned_at = None
    db.commit()
    return {'ok': True, 'torrent_hash': h}


@router.post('/adopt/scan-now')
def scan_now():
    """手动触发一次孤儿认领扫描（管理 UI 的"立即扫描"按钮用）。"""
    try:
        from tools.dispatch.adopt import scan_and_adopt_orphans
        result = scan_and_adopt_orphans()
        return result
    except Exception as e:
        logger.exception("手动扫描失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.post('/dispatch-map/{torrent_hash}/retry')
def retry_dispatch_row(torrent_hash: str, db: Session = Depends(get_db)):
    """
    重试一个 sweeper 已放弃的 dispatch_map 行。

    用户排查完底层问题（路径映射 / 磁盘 / qB 配置）后，从 UI 点"重试"：
      - 清 phase_timings.sweeper_gave_up + sweeper_attempts.<phase> 计数
      - 把 phase 重置回 _recover_target_phase（链式段→copying，后处理→subtitle_fetching）
      - phase_status=running，让对应 worker 立即接走
    """
    from tools.dispatch.phases import (
        STATUS_RUNNING, PHASE_CLEANED, PHASE_DISMISSED, PHASE_ALL_JOBS_DONE,
    )
    from tools.dispatch.sweeper import _recover_target_phase, _fire_all_triggers

    h = (torrent_hash or '').lower()
    row = db.query(DownloadDispatchMap).filter_by(torrent_hash=h).first()
    if not row:
        raise HTTPException(status_code=404, detail='dispatch_map 行不存在')

    if row.phase in (PHASE_CLEANED, PHASE_DISMISSED, PHASE_ALL_JOBS_DONE):
        raise HTTPException(status_code=400, detail=f'当前 phase 已是终态（{row.phase}），不可重试')

    new_phase = _recover_target_phase(row.phase) or row.phase
    old_phase = row.phase

    # 清重试计数和放弃标记
    timings = dict(row.phase_timings or {})
    timings.pop('sweeper_gave_up', None)
    attempts_map = dict(timings.get('sweeper_attempts') or {})
    attempts_map.pop(old_phase, None)
    attempts_map.pop(new_phase, None)
    timings['sweeper_attempts'] = attempts_map

    row.phase = new_phase
    row.phase_status = STATUS_RUNNING
    row.phase_timings = timings
    row.status_message = f'用户重试：{old_phase} → {new_phase}/running'
    db.commit()

    _fire_all_triggers()
    logger.info(f"retry: {h[:16]}.. {old_phase} → {new_phase}（用户从 UI 重试）")
    return {'ok': True, 'torrent_hash': h, 'old_phase': old_phase, 'new_phase': new_phase}


# ============================================================================
# 通用手动编辑：dispatch_map 任意行改 media_type / 标题 / 目标库 / 目标路径
# 跟 /needs-review/confirm 区别：不强制 phase 流转，仅就地更新字段
# 主要场景：用户发现某条已自动入流水线但识别错了，想 mid-flight 修正
# ============================================================================

class DispatchEditRequest(BaseModel):
    """手动编辑 dispatch_map 行。所有字段都是 optional：传谁改谁。"""
    media_type: Optional[str] = None           # movie / tv / anime / adult
    title: Optional[str] = None
    year: Optional[int] = None
    series_name: Optional[str] = None
    target_library_id: Optional[str] = None
    target_path: Optional[str] = None          # backend view；reverse-translate 后入库
    # True：忽略上面 target_library_id/target_path，按规则按 media_type+title+year 重算
    recompute_target: bool = False


# 仍可改的 phase：未开始落盘的阶段。落盘后改路径 / 库需要先迁文件，目前 UI 不做这事
_EDITABLE_PHASES = {
    'analyzing', 'dispatch_queued', 'downloading', 'download_done',
}


@router.post('/dispatch-map/{torrent_hash}/edit')
def edit_dispatch_row(
    torrent_hash: str,
    request: DispatchEditRequest,
    db: Session = Depends(get_db),
):
    """手动覆盖 dispatch_map 行的识别 / 目标字段。

    行为：
      - 只更新显式传入的字段；其它保持
      - recompute_target=True 或者 (改了 media_type 但没传 target_*) → 按规则重算目标
      - phase ∈ {copying, organizing, jellyfin_recognizing, ...} → 返回 warning：
        文件可能已落到旧路径，本接口只改 DB 不迁文件，需用户自己处理
      - phase ∈ {cleaned, dismissed, all_jobs_done} → 拒绝（再编辑没意义）
    """
    h = (torrent_hash or '').lower()
    row = db.query(DownloadDispatchMap).filter_by(torrent_hash=h).first()
    if not row:
        raise HTTPException(status_code=404, detail='dispatch_map 行不存在')

    if row.phase in ('cleaned', 'dismissed', 'all_jobs_done'):
        raise HTTPException(
            status_code=400,
            detail=f'当前 phase={row.phase}，已是终态，无法编辑（请删除后重新添加）',
        )

    warning = None
    if row.phase not in _EDITABLE_PHASES:
        # copying / organizing / jellyfin_recognizing / subtitle_fetching / ...
        # 允许改字段，但提示用户：文件已落到旧路径
        warning = (
            f'当前 phase={row.phase}，文件可能已落到旧目标位置（{row.target_path!r}）。'
            f'本接口只改 DB 不迁移文件；如需重新落库请手动删除旧文件后从 download_done 阶段重试。'
        )

    old = {
        'media_type': row.media_type,
        'title': row.title,
        'year': row.year,
        'series_name': row.series_name,
        'target_library_id': row.target_library_id,
        'target_path': row.target_path,
    }

    # 1. 字段更新：传谁改谁
    if request.media_type is not None:
        row.media_type = request.media_type
    if request.title is not None:
        row.title = request.title
    if request.year is not None:
        row.year = request.year
    if request.series_name is not None:
        row.series_name = request.series_name

    # 2. 目标解析：三档
    #    (a) 显式传了 target_library_id + target_path → 直接用（reverse-translate）
    #    (b) recompute_target=True → 强制按规则重算
    #    (c) 否则：改了 media_type/title/year/series_name 之一 但没传目标 → 也重算（最常用）
    media_type_changed = request.media_type is not None and request.media_type != old['media_type']
    title_or_meta_changed = any(
        request.__dict__.get(k) is not None and getattr(row, k) != old[k]
        for k in ('title', 'year', 'series_name')
    )

    if request.target_library_id and request.target_path:
        from backend.path_translator import reverse_translate_path_with_settings
        row.target_library_id = request.target_library_id
        row.target_path = reverse_translate_path_with_settings(request.target_path) or request.target_path
    elif request.recompute_target or media_type_changed or title_or_meta_changed:
        identified = {
            'media_type': row.media_type,
            'title': row.title,
            'year': row.year,
            'series_name': row.series_name,
        }
        target_info = _resolve_target(identified)
        if not target_info.get('library_id') or not target_info.get('target_path'):
            raise HTTPException(
                status_code=400,
                detail=f'无法为 media_type={row.media_type} 推算目标，请在 UI 手动填写 target_library_id + target_path',
            )
        row.target_library_id = target_info['library_id']
        row.target_root = target_info.get('library_root')
        row.target_path = target_info['target_path']

    # 3. 写一条短消息说明被手动改了
    row.status_message = f'用户手动编辑（{old["media_type"]}→{row.media_type}）'
    db.commit()

    logger.info(
        f"edit: {h[:16]}.. 字段更新 "
        f"media_type {old['media_type']!r}→{row.media_type!r} "
        f"target {old['target_path']!r}→{row.target_path!r}"
    )

    return {
        'ok': True,
        'torrent_hash': h,
        'before': old,
        'after': {
            'media_type': row.media_type,
            'title': row.title,
            'year': row.year,
            'series_name': row.series_name,
            'target_library_id': row.target_library_id,
            'target_path': row.target_path,
        },
        'warning': warning,
    }


@router.post('/dispatch-map/{torrent_hash}/redispatch')
def redispatch_completed(
    torrent_hash: str,
    db: Session = Depends(get_db),
):
    """完整重跑：把已完结（或被跳过/拒绝）的种子从分析阶段重新走一遍流水线。

    跟 /retry 的区别：
      - /retry 只重跑当前 phase（适用 failed 状态）；终态行拒绝
      - /redispatch 把 phase 重置回 analyzing/running，让 analyzer 重识别 → 高置信
        自动入流水线 → 拷贝 → 通知 Jellyfin → 后处理。识别失败照常落 needs_review
        等用户审核。

    适用：phase=all_jobs_done / cleaned / dismissed；以及 phase=copying & status=skipped
    （之前因 duplicate_policy 跳过的，用户想重做）。
    """
    from tools.dispatch.phases import (
        PHASE_ANALYZING, PHASE_ALL_JOBS_DONE, PHASE_CLEANED, PHASE_DISMISSED,
        PHASE_COPYING, STATUS_RUNNING, STATUS_SKIPPED,
    )

    h = (torrent_hash or '').lower()
    row = db.query(DownloadDispatchMap).filter_by(torrent_hash=h).first()
    if not row:
        raise HTTPException(status_code=404, detail='dispatch_map 行不存在')

    # 允许的源 phase：终态 + duplicate_policy 跳过的 copying
    redispatchable = (
        row.phase in (PHASE_ALL_JOBS_DONE, PHASE_CLEANED, PHASE_DISMISSED)
        or (row.phase == PHASE_COPYING and row.phase_status == STATUS_SKIPPED)
    )
    if not redispatchable:
        raise HTTPException(
            status_code=400,
            detail=f'当前 phase={row.phase}/{row.phase_status}，仅"已完成 / 已清理 / 已跳过"的任务可重新入库',
        )

    old_phase = row.phase
    old_status = row.phase_status

    # 重置：phase 回 analyzing，清掉所有上轮的识别 + 目标 + 中间产物
    # （analyzer 看 row.media_type 当 user_hint 用，不清就会带着上次偏见重识别 —— 跟"重新入库"语义不一致）
    row.phase = PHASE_ANALYZING
    row.phase_status = STATUS_RUNNING
    row.status_message = f'用户手动重新入库（原 phase={old_phase}）'
    row.error_log = None
    # 识别信息
    row.media_type = None
    row.tmdb_id = None
    row.imdb_id = None
    row.series_tmdb_id = None
    row.series_name = None
    row.title = None
    row.year = None
    # 目标位置
    row.target_library_id = None
    row.target_root = None
    row.target_path = None
    # 中间产物
    row.dispatched_files = []
    row.copy_bytes_done = 0
    row.copy_bytes_total = 0
    row.dispatched_at = None
    row.cleaned_at = None
    # 清重试计数（避免 sweeper 误认为这是老的失败任务）
    timings = dict(row.phase_timings or {})
    timings.pop('sweeper_gave_up', None)
    timings.pop('sweeper_attempts', None)
    # 清重复检测缓存（analyzer 会重新跑）
    timings.pop('duplicates', None)
    row.phase_timings = timings
    db.commit()

    # 如果之前 qB 端是暂停态（dismissed 流程会暂停），resume 让它继续做种 / 验证元数据
    qb_resumed = False
    try:
        from common.qbittorrent_client import QBittorrentClient
        if settings.qbittorrent_configured:
            qb = QBittorrentClient(
                settings.qbittorrent_host,
                settings.qbittorrent_username,
                settings.qbittorrent_password,
            )
            qb.resume(h)
            qb_resumed = True
    except Exception as e:
        logger.warning(f"redispatch: qB resume 失败（不致命，analyzer 会自己拿状态）: {e}")

    # 触发 analyzer 立刻处理
    try:
        from tools.dispatch.analyzer import trigger as analyzer_trigger
        analyzer_trigger.set()
    except Exception:
        pass

    logger.info(
        f"redispatch: {h[:16]}.. {old_phase}/{old_status} → analyzing/running "
        f"(用户手动重新入库，qb_resumed={qb_resumed})"
    )
    return {
        'ok': True,
        'torrent_hash': h,
        'old_phase': old_phase,
        'new_phase': PHASE_ANALYZING,
    }


@router.post('/dispatch-map/{torrent_hash}/reidentify')
def reidentify_dispatch_row(
    torrent_hash: str,
    db: Session = Depends(get_db),
):
    """重新走识别链路。返回新识别结果但**不写库**，由前端决定是否保存。

    跟 /edit 的区别：edit 是用户在对话框里手填后写库，本接口是"刷新建议"——
    让用户点对话框左下的"重新识别"按钮就地拿到一份新结果回填表单。

    步骤：拉 qB 文件列表 → 调 identify_media（不传 user_hint，让规则/TMDB/LLM 完整跑）
    → 返回 dict（媒体类型 / 标题 / 年份 / 剧名 / source / confidence / 推算目标）
    """
    h = (torrent_hash or '').lower()
    row = db.query(DownloadDispatchMap).filter_by(torrent_hash=h).first()
    if not row:
        raise HTTPException(status_code=404, detail='dispatch_map 行不存在')

    if not settings.qbittorrent_configured:
        raise HTTPException(status_code=400, detail='未配置 qBittorrent')

    # 拉 qB 文件列表
    files: List[Dict] = []
    try:
        from common.qbittorrent_client import QBittorrentClient
        qb = QBittorrentClient(
            settings.qbittorrent_host,
            settings.qbittorrent_username,
            settings.qbittorrent_password,
        )
        qb_info = qb.get_torrent_info(h) or {}
        torrent_name = qb_info.get('name') or row.title or ''
        try:
            raw_files = qb.get_files(h) or []
            files = [
                {'index': i, 'name': f.get('name'), 'size': f.get('size')}
                for i, f in enumerate(raw_files)
            ]
        except Exception as e:
            logger.warning(f"reidentify: 拉 {h[:16]}.. 文件列表失败: {e}（用空文件列表跑）")
    except Exception as e:
        logger.warning(f"reidentify: 连 qB 失败 {h[:16]}..: {e}（仍用 row.title 跑识别）")
        torrent_name = row.title or ''

    if not torrent_name:
        raise HTTPException(status_code=400, detail='种子名为空，无法识别')

    # 跑识别链 + 推算目标
    from tools.dispatch.identify import identify_media
    from tools.dispatch.adopt import _resolve_target_safe
    identified = identify_media(torrent_name, files=files, user_hint=None)

    target_info = _resolve_target_safe(identified)

    logger.info(
        f"reidentify: {h[:16]}.. → media_type={identified.get('media_type')!r} "
        f"title={identified.get('title')!r} year={identified.get('year')!r} "
        f"source={identified.get('source')!r} conf={identified.get('confidence')}"
    )

    return {
        'ok': True,
        'torrent_hash': h,
        'torrent_name': torrent_name,
        'identified': {
            'media_type': identified.get('media_type'),
            'title': identified.get('title'),
            'year': identified.get('year'),
            'series_name': identified.get('series_name'),
            'season': identified.get('season'),
            'episode': identified.get('episode'),
            'source': identified.get('source'),
            'confidence': identified.get('confidence'),
            'tmdb_id': identified.get('tmdb_id'),
            'series_tmdb_id': identified.get('series_tmdb_id'),
        },
        'target': {
            'target_library_id': target_info.get('library_id'),
            'target_path': target_info.get('target_path'),
            'library_name': target_info.get('library_name'),
        },
    }


# ============================================================================
# copy-phase 冲突决策（phase=copying, phase_status=needs_review）
# 由 organizer 的 duplicate_resolver 或 copier 的 CrossTorrentCollisionError 触发
# ============================================================================

def _load_copy_conflict(row: DownloadDispatchMap) -> Dict:
    """从 row.error_log JSON 反序列化冲突上下文（duplicate.py 写入的格式）。"""
    import json as _json
    if not row.error_log:
        raise HTTPException(status_code=400, detail='缺少冲突上下文（error_log 为空）')
    try:
        info = _json.loads(row.error_log)
        if not isinstance(info, dict):
            raise ValueError('error_log 不是 dict')
    except Exception as e:
        raise HTTPException(status_code=400, detail=f'error_log 解析失败: {e}')
    return info


def _assert_copy_conflict(row: DownloadDispatchMap) -> Dict:
    from tools.dispatch.phases import PHASE_COPYING, STATUS_NEEDS_REVIEW
    if row.phase != PHASE_COPYING or row.phase_status != STATUS_NEEDS_REVIEW:
        raise HTTPException(
            status_code=400,
            detail=f'当前不在 copy-phase needs_review（phase={row.phase}/{row.phase_status}）',
        )
    return _load_copy_conflict(row)


@router.get('/copy-conflict/{torrent_hash}')
def get_copy_conflict(torrent_hash: str, db: Session = Depends(get_db)):
    """返回 copy-phase 冲突上下文，给前端审核 modal 展示用。"""
    h = (torrent_hash or '').lower()
    row = db.query(DownloadDispatchMap).filter_by(torrent_hash=h).first()
    if not row:
        raise HTTPException(status_code=404, detail='dispatch_map 行不存在')
    info = _assert_copy_conflict(row)
    return {
        'torrent_hash': h,
        'phase': row.phase,
        'phase_status': row.phase_status,
        'title': row.title,
        'media_type': row.media_type,
        'conflict': info,
    }


@router.post('/copy-conflict/{torrent_hash}/replace')
def resolve_copy_conflict_replace(torrent_hash: str, db: Session = Depends(get_db)):
    """
    用户选择"用新种子覆盖"：
      1. 把对家行（占用 dst 的那条）的 dispatched_files 里这条路径移除（仍保留行，
         它的副本还在 NVMe，仍可做种 / 配额清理）
      2. 如果对家 dst 文件还在，移到 trash（_replaced/<ts>_<old_hash[:8]>/）
      3. 本行 phase 重置回 copying/running，pipeline_worker 下一轮重跑
    """
    import json as _json
    import shutil as _shutil
    import time as _time
    from pathlib import Path as _Path
    from tools.dispatch.phases import PHASE_COPYING, STATUS_RUNNING

    h = (torrent_hash or '').lower()
    row = db.query(DownloadDispatchMap).filter_by(torrent_hash=h).first()
    if not row:
        raise HTTPException(status_code=404, detail='dispatch_map 行不存在')
    info = _assert_copy_conflict(row)

    dst_str = info.get('dst')
    existing_hash = (info.get('existing_hash') or '').lower()
    if not dst_str or not existing_hash:
        raise HTTPException(status_code=400, detail='冲突上下文不完整（缺 dst / existing_hash）')

    # 1. 把对家 dispatched_files 里这条路径移除
    other = db.query(DownloadDispatchMap).filter_by(torrent_hash=existing_hash).first()
    if other and other.dispatched_files:
        new_list = [p for p in other.dispatched_files if p != dst_str]
        if len(new_list) != len(other.dispatched_files):
            other.dispatched_files = new_list
            logger.info(
                f"copy-conflict replace: 对家 {existing_hash[:16]}.. dispatched_files "
                f"移除 {dst_str}（剩 {len(new_list)} 条）"
            )

    # 2. 旧物理文件入 trash
    trash_dir = _Path(settings.dispatch.trash_dir) if settings.dispatch.trash_dir else None
    dst_path = _Path(dst_str)
    moved_to = None
    if dst_path.exists() and trash_dir:
        ts = _time.strftime('%Y%m%d-%H%M%S')
        sub = trash_dir / '_replaced' / f"{ts}_{existing_hash[:8]}"
        sub.mkdir(parents=True, exist_ok=True)
        target = sub / dst_path.name
        try:
            _shutil.move(str(dst_path), str(target))
            moved_to = str(target)
            logger.info(f"copy-conflict replace: 旧文件入 trash {dst_path} → {target}")
        except Exception as e:
            logger.warning(f"copy-conflict replace: 入 trash 失败 {dst_path} → {target}: {e}")

    # 3. 本行重置到 copying/running，让 worker 重新跑
    row.phase = PHASE_COPYING
    row.phase_status = STATUS_RUNNING
    row.status_message = f'用户决策"覆盖" → 重新复制（旧文件已入 trash）'
    row.error_log = None
    db.commit()

    try:
        from tools.dispatch.pipeline_worker import trigger as _pt
        _pt.set()
    except Exception:
        pass
    return {'ok': True, 'torrent_hash': h, 'displaced_to': moved_to}


@router.post('/copy-conflict/{torrent_hash}/skip')
def resolve_copy_conflict_skip(torrent_hash: str, db: Session = Depends(get_db)):
    """
    用户选择"保留旧的，跳过本种子"：本行标 succeeded/skipped + 跳出 copy 进入终态。
    不动 qB 那边（用户仍可手动从 qB 移除）。
    """
    from tools.dispatch.phases import PHASE_COPYING, STATUS_SKIPPED

    h = (torrent_hash or '').lower()
    row = db.query(DownloadDispatchMap).filter_by(torrent_hash=h).first()
    if not row:
        raise HTTPException(status_code=404, detail='dispatch_map 行不存在')
    _assert_copy_conflict(row)

    row.phase_status = STATUS_SKIPPED
    row.status_message = '用户决策"跳过" — 旧版本仍在库'
    row.dispatched_files = []
    # phase 保留 copying：跟"已完成→all_jobs_done"区分开，这是用户拒收的终态分支
    # sweeper 不会再 retry skipped 状态
    db.commit()
    logger.info(f"copy-conflict skip: {h[:16]}.. 用户跳过")
    return {'ok': True, 'torrent_hash': h}
