"""
成人内容管理 API
路由仅在 settings.adult_enabled = True 时挂载。
"""
import sys
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session

ROOT_DIR = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from web.backend.database import get_db, SessionLocal, AdultItem
from web.backend.config import settings
from common.rate_limiter import ADULT_SCRAPER_DELAY
from web.backend.api.tasks import (
    create_task, update_task_progress, complete_task,
    cancellable_task, TaskCancelledError, mark_task_cancelled,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _local_path(jf_path) -> Path:
    """DB 里的 file_path 是 Jellyfin view，磁盘 op 之前 forward-translate 到本机视角。
    入参可以是 str 或 Path；同机部署时 translator 不命中规则会原样返回。"""
    from web.backend.path_translator import translate_path_with_settings
    s = str(jf_path) if jf_path else ''
    return Path(translate_path_with_settings(s) or s)


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
    # cover_url：传新 URL → 后端下载到 <video>-poster.jpg 替换；
    #           传 ""（空字符串）→ 清除本地封面 + DB 字段
    #           传 None / 不传 → 不动
    cover_url: Optional[str] = None


class IdentifyRequest(BaseModel):
    code: str
    auto_scrape: bool = True  # 指定后立即触发刮削


# ---------- 列表 / 详情 / 手动修改 ----------

# "无码" 判断关键字：在 title / file_path 任一命中即视为无码
_UNCENSORED_KEYWORDS = [
    'uncensored', 'uncen', 'no-mosaic', 'nomosaic',
    '无码', '無碼', '无修正', '無修正', '未経審査', '未經審查',
    'leaked', '流出', '破解', '洩露', '泄露',
    'ノーモザ',
    # 知名无码厂牌 / 番号前缀（命中即视为无码）
    'fc2', 'fc2-ppv', 'fc2ppv', '1pondo', 'caribbean', 'caribbeancom', 'caribean',
]


def _is_item_uncensored(item) -> bool:
    """
    判断单条 AdultItem 是否无码：title 或 file_path 命中 _UNCENSORED_KEYWORDS 任一。
    跟 stats 接口的判定逻辑保持一致。
    """
    if not item:
        return False
    title = (item.title or '').lower()
    fp = (item.file_path or '').lower()
    return any(kw.lower() in title or kw.lower() in fp for kw in _UNCENSORED_KEYWORDS)


def _resolve_uncensored(item) -> Optional[bool]:
    """
    解析单条 AdultItem 的最终"有/无码"标志：
      - is_uncensored_override 不为 NULL → 用户手动指定，直接用
      - 否则 code 不为空 → 走关键字自动判定
      - code 为空 → None（未识别条目不参与判定）
    """
    if item is None:
        return None
    override = getattr(item, 'is_uncensored_override', None)
    if override is not None:
        return bool(override)
    if not item.code:
        return None
    return _is_item_uncensored(item)


def _library_path_filter(library_id: str):
    r"""
    把 library_id → SQLAlchemy 过滤条件（OR 形式），匹配 file_path 在该库下。
    返回 None 表示库无路径（外层应直接返回空结果）。

    DB 约定：AdultItem.file_path 存 Jellyfin view（如 /library/videos/adult/...）。
    所以前缀匹配用 Jellyfin 视角的 locations 直接拿（_get_library_paths_raw）。
    历史数据可能含反斜杠 / Windows 盘符 → 同时 OR 一次本机视角的备份匹配，
    迁移期能兼容；新数据全是 Jellyfin view 即可。
    """
    from web.backend.services.adult_watcher import watcher
    from web.backend.path_translator import translate_path_with_settings
    from sqlalchemy import or_
    raw_paths = watcher._get_library_paths_raw(library_id)
    if not raw_paths:
        return None
    conds = []
    for p in raw_paths:
        # Jellyfin view（DB canonical）
        norm = str(p).rstrip('/').rstrip('\\')
        conds.append(AdultItem.file_path.ilike(f'{norm}%', escape='|'))
        # 兼容历史数据：本机视角两种 separator
        local = translate_path_with_settings(p) or p
        if local and local != p:
            l_norm = str(local).rstrip('/').rstrip('\\')
            fwd = l_norm.replace('\\', '/')
            bwd = l_norm.replace('/', '\\')
            conds.append(AdultItem.file_path.ilike(f'{fwd}%', escape='|'))
            if bwd != fwd:
                conds.append(AdultItem.file_path.ilike(f'{bwd}%', escape='|'))
    return or_(*conds)


# 注意：以下读 DB 的端点都用 sync `def` 而非 `async def`。
# FastAPI 会自动把同步函数扔进 threadpool 跑，event loop 主线程不被阻塞。
# 之前用 async def + 同步 SQLAlchemy 会阻塞 event loop —— 刮削 worker 让 PG 繁忙时，
# 列表查询从几十 ms 变到几百 ms，期间所有 async 请求都被卡住。

@router.get("/items")
def list_items(
    search: Optional[str] = None,
    has_metadata: Optional[bool] = None,
    recognized: Optional[bool] = None,
    show_unrecognized: bool = True,
    show_excluded: bool = False,  # 默认隐藏用户主动排除的条目
    uncensored: Optional[bool] = None,
    actor: Optional[str] = None,
    actresses: Optional[List[str]] = Query(default=None),
    tag: Optional[str] = None,
    in_jellyfin: Optional[bool] = None,
    library_id: Optional[str] = None,
    data_source: str = 'adult',
    limit: int = 50,
    offset: int = 0,
    sort_by: str = 'code',         # code / title / release_date
    sort_order: str = 'asc',       # asc / desc
    has_health_issue: bool = False, # 只看健康有问题（未识别/未刮削/封面或NFO缺失）
    db: Session = Depends(get_db),
):
    """番号库列表

    recognized: True=只看已识别（有番号）；False=只看未识别（code IS NULL）；None=全部
    uncensored: True=只看无码（title/file_path 命中关键字）；False=只看有码；None=全部
    in_jellyfin: True=只看已被 Jellyfin 收录的；False=只看没被收录的；None=全部
    library_id: 给定时只返回 file_path 在该 Jellyfin 库 location 下的条目
    data_source: 'adult'（默认，从我们 AdultItem 表）或 'jellyfin'（从 Jellyfin 该库的 items 拉，
                  反查 AdultItem 做 cross-ref）；后者要求 library_id
    """
    if data_source == 'jellyfin':
        if not library_id:
            raise HTTPException(status_code=400, detail="使用 Jellyfin 数据库时必须指定 library_id")
        return _list_items_from_jellyfin(library_id, search, limit, offset, db)

    query = db.query(AdultItem)
    if library_id:
        cond = _library_path_filter(library_id)
        if cond is None:
            return {"total": 0, "items": []}
        query = query.filter(cond)
    if search:
        query = query.filter(
            (AdultItem.code.contains(search))
            | (AdultItem.title.contains(search))
            | (AdultItem.file_path.contains(search))
        )
    if has_metadata is True:
        query = query.filter(AdultItem.title != None)  # noqa: E711
    elif has_metadata is False:
        query = query.filter(AdultItem.title == None)  # noqa: E711
    if recognized is True:
        query = query.filter(AdultItem.code != None)  # noqa: E711
    elif recognized is False:
        query = query.filter(AdultItem.code == None)  # noqa: E711
    elif not show_unrecognized:
        # 默认行为可由 show_unrecognized=False 控制：只列已识别的（隐藏未识别文件）
        query = query.filter(AdultItem.code != None)  # noqa: E711
    if not show_excluded:
        # 默认隐藏 excluded（用户主动排除）；冷却中（cooldown_until）继续显示
        query = query.filter(AdultItem.excluded == False)  # noqa: E712
    if uncensored is not None:
        # 判定逻辑跟 _resolve_uncensored 保持一致：
        #   - is_uncensored_override 不为 NULL → 直接用 override
        #   - 否则 → 关键字（title / file_path）命中即"无码"
        from sqlalchemy import or_, and_, not_
        kw_conds = []
        for kw in _UNCENSORED_KEYWORDS:
            kw_conds.append(AdultItem.title.ilike(f'%{kw}%'))
            kw_conds.append(AdultItem.file_path.ilike(f'%{kw}%'))
        any_kw = or_(*kw_conds)
        if uncensored:
            # 无码：override=True 或 (override IS NULL 且关键字命中)
            query = query.filter(
                or_(
                    AdultItem.is_uncensored_override == True,  # noqa: E712
                    and_(
                        AdultItem.is_uncensored_override == None,  # noqa: E711
                        any_kw,
                    ),
                )
            )
        else:
            # 有码：override=False 或 (override IS NULL 且关键字不命中)
            query = query.filter(
                or_(
                    AdultItem.is_uncensored_override == False,  # noqa: E712
                    and_(
                        AdultItem.is_uncensored_override == None,  # noqa: E711
                        not_(any_kw),
                    ),
                )
            )
    if actor:
        query = query.filter(AdultItem.actors.contains(actor))
    if actresses:
        # actors 是 JSON 字符串数组；用 contains 做近似 OR 匹配（任一命中即留）
        from sqlalchemy import or_
        cond = or_(*[AdultItem.actors.contains(a) for a in actresses if a])
        query = query.filter(cond)
    if tag:
        query = query.filter(AdultItem.tags.contains(tag))

    # 派生字段过滤「仅看健康有问题」：等价于前端 gridHealthState != 'green'/'excluded'/'cooldown'
    # 即不是完全完整的条目（缺 code / 缺 title / 没刮削 / cover 或 nfo 本地缺失）
    # 用 SQL OR 表达；isnot(True) 在 PG 上是 'IS NOT TRUE'，包含 NULL+False
    if has_health_issue:
        from sqlalchemy import or_
        query = query.filter(or_(
            AdultItem.code.is_(None),
            AdultItem.title.is_(None),
            AdultItem.source.is_(None),
            AdultItem.source.in_(['not_found', 'pending']),
            AdultItem.cover_local_ok.isnot(True),
            AdultItem.nfo_local_ok.isnot(True),
        ))

    # 排序下推：之前 hard-code 按 code 排，前端切换无效；现在支持 code/title/release_date
    # 派生字段（health）不在表里——已改成上面的 has_health_issue filter，不再作为排序维度
    _SORT_COL_MAP = {
        'code': AdultItem.code,
        'title': AdultItem.title,
        'release_date': AdultItem.release_date,
    }
    _sort_col = _SORT_COL_MAP.get(sort_by, AdultItem.code)
    if sort_order == 'desc':
        _sort_col = _sort_col.desc().nullslast()
    else:
        _sort_col = _sort_col.asc().nullslast()

    # 不过滤 Jellyfin 时直接 SQL 翻页
    # **关键**：先把 ORM 对象 snapshot 成 dict 释放 db；
    # _to_dict_with_jellyfin 内部会调 lookup_jellyfin_item（缓存过期时触发 Jellyfin HTTP，
    # 1-3s 不等），不能一边持 db 一边等 HTTP，否则连接池在 Jellyfin 慢时会被吃完
    if in_jellyfin is None:
        total = query.count()
        items = query.order_by(_sort_col).offset(offset).limit(limit).all()
        snapshot = [_to_dict(i) for i in items]
        # 显式 commit 释放 PG 连接（入参 db 是 Depends 给的，FastAPI finally 还会调 close，幂等）
        db.commit()
        # commit 后才做 Jellyfin lookup —— HTTP 慢操作期间不持连接
        return {
            "total": total,
            "items": [_enrich_with_jellyfin(d) for d in snapshot],
        }

    # in_jellyfin 过滤需要每条反查
    from web.backend.api.medialibraries import lookup_jellyfin_item, jellyfin_web_url
    all_items = query.order_by(_sort_col).all()
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


def _list_items_from_jellyfin(
    library_id: str,
    search: Optional[str],
    limit: int,
    offset: int,
    db: Session,  # 入参 db 不再用（保留签名兼容），内部短事务自管
):
    """
    Data source = 'jellyfin'：行从 Jellyfin /Items 拉，每行反查 AdultItem 做 cross-ref。
    返回的 dict 形状对齐 _to_dict / _to_dict_with_jellyfin（前端组件无感切换）：
      - id：linked AdultItem.id（无对应 AdultItem 时为 null —— 单行操作会 disabled）
      - jellyfin_id / jellyfin_url / jellyfin_name：Jellyfin 视角的标识
      - image_url：Jellyfin Primary 图直链（带 api_key），让 AdultPosterCell 直接用
      - title / release_date：Jellyfin Name / ProductionYear
      - file_path：Jellyfin Path 翻译后的本机路径
      - 其余刮削字段（actors / tags / studio / code 等）：从 cross-ref AdultItem 取
    """
    from common.jellyfin_client import JellyfinClient
    from web.backend.path_translator import translate_path_with_settings

    if not settings.jellyfin_api_key:
        raise HTTPException(status_code=400, detail="Jellyfin 未配置")

    try:
        client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
        from web.backend.diagnostics import timed
        with timed(f'jellyfin get_library_items({library_id[:8]})', slow_ms=2000):
            all_items = client.get_library_items(
                library_id,
                item_types='Movie',
                fields='Path,ImageTags,ProductionYear,Overview',
            )
    except Exception as e:
        logger.exception("从 Jellyfin 拉库内 items 失败")
        raise HTTPException(status_code=502, detail=f"Jellyfin 调用失败: {e}")

    # 搜索（Jellyfin 端过滤；这里在内存按 Name / Path 过滤）
    if search:
        s = search.lower()
        all_items = [
            it for it in all_items
            if (it.get('Name') and s in it['Name'].lower())
            or (it.get('Path') and s in it['Path'].lower())
        ]

    total = len(all_items)
    page = all_items[offset:offset + limit]

    # 批量反查 AdultItem：DB 约定存 Jellyfin view，直接按 jf_path 查；
    # 同时把本机变体也加进 IN 列表，兼容尚未重扫的历史数据
    candidate_paths = set()
    for it in page:
        jf_path = it.get('Path')
        if not jf_path:
            continue
        candidate_paths.add(jf_path)  # 主键（Jellyfin view）
        local = translate_path_with_settings(jf_path) or jf_path
        for v in (local, local.replace('/', '\\'), local.replace('\\', '/')):
            if v:
                candidate_paths.add(v)

    # cross-ref：在短事务里直接 snapshot 成 dict，session 关后再用更安全
    adult_by_path: Dict[str, dict] = {}
    if candidate_paths:
        with SessionLocal() as q_db:
            rows = q_db.query(AdultItem).filter(
                AdultItem.file_path.in_(list(candidate_paths))
            ).all()
            for r in rows:
                if not r.file_path:
                    continue
                adult_by_path[r.file_path] = {
                    'id': r.id,
                    'code': r.code,
                    'title': r.title,
                    'release_date': r.release_date,
                    'studio': r.studio,
                    'director': r.director,
                    'rating': r.rating,
                    'cover_url': r.cover_url,
                    'source': r.source,
                    'nfo_path': r.nfo_path,
                    'poster_path': r.poster_path,
                    'actors_json': r.actors,
                    'tags_json': r.tags,
                }

    host = (settings.jellyfin_host or '').rstrip('/')
    api_key = settings.jellyfin_api_key

    out = []
    for it in page:
        jf_id = it.get('Id')
        jf_path = it.get('Path')
        local = translate_path_with_settings(jf_path) if jf_path else None
        # 优先按 jf_path（DB 现在存的格式）找 cross-ref，再退到本机变体兼容历史
        ai = None
        if jf_path:
            ai = adult_by_path.get(jf_path)
        if not ai and local:
            ai = (adult_by_path.get(local)
                  or adult_by_path.get(local.replace('/', '\\'))
                  or adult_by_path.get(local.replace('\\', '/')))

        # 演员 / 标签解析（ai 是 dict，不是 ORM 对象）
        actors_list, tags_list = [], []
        if ai:
            try:
                actors_list = json.loads(ai['actors_json']) if ai.get('actors_json') else []
            except Exception:
                pass
            try:
                tags_list = json.loads(ai['tags_json']) if ai.get('tags_json') else []
            except Exception:
                pass

        image_url = None
        if jf_id and host and api_key:
            image_url = f"{host}/Items/{jf_id}/Images/Primary?quality=90&api_key={api_key}"

        # ai_poster / ai_nfo 来自 DB（Jellyfin view），磁盘存在性判定要本机视角
        ai_poster = ai['poster_path'] if ai else None
        ai_nfo = ai['nfo_path'] if ai else None
        ai_poster_local = translate_path_with_settings(ai_poster) if ai_poster else None
        ai_nfo_local = translate_path_with_settings(ai_nfo) if ai_nfo else None
        cover_local_ok = bool(ai_poster_local) and Path(ai_poster_local).exists()
        nfo_local_ok = bool(ai_nfo_local) and Path(ai_nfo_local).exists()

        row = {
            # 行 id：有 cross-ref 时给 AdultItem.id（让单行操作可用）；否则 None
            "id": ai['id'] if ai else None,
            "jellyfin_id": jf_id,
            "jellyfin_name": it.get('Name'),
            "jellyfin_url": jellyfin_web_url_helper(jf_id),
            "image_url": image_url,
            # 与 AdultItem dict 对齐的字段
            "code": ai['code'] if ai else None,
            "title": (ai['title'] if ai and ai.get('title') else it.get('Name')),
            "release_date": (ai['release_date'] if ai and ai.get('release_date')
                             else (str(it.get('ProductionYear')) if it.get('ProductionYear') else None)),
            "studio": ai['studio'] if ai else None,
            "director": ai['director'] if ai else None,
            "rating": ai['rating'] if ai else None,
            "cover_url": ai['cover_url'] if ai else None,
            "source": ai['source'] if ai else 'jellyfin',
            "has_metadata": bool(ai and ai.get('title')),
            "recognized": bool(ai and ai.get('code')),
            "file_name": Path(jf_path).name if jf_path else None,
            "file_path": local or jf_path,        # 本机视角给前端展示
            "nfo_path": ai_nfo_local,
            "poster_path": ai_poster_local,
            "cover_local_ok": cover_local_ok,
            "nfo_local_ok": nfo_local_ok,
            "actors": actors_list,
            "tags": tags_list,
            "_data_source": "jellyfin",
        }
        out.append(row)

    return {"total": total, "items": out}


def jellyfin_web_url_helper(item_id):
    """避免循环 import：用本地 helper 拼 URL（同 jellyfin.jellyfin_web_url）"""
    if not item_id or not settings.jellyfin_host:
        return None
    return f"{settings.jellyfin_host.rstrip('/')}/web/#/details?id={item_id}"


def _to_dict_with_jellyfin(item: AdultItem) -> dict:
    """生成列表 dict 并附带 Jellyfin 状态。**调用方必须确保 ORM 对象的属性已加载**
    （session 关后调用前先 _to_dict 或 expunge），否则 lookup 时 db 已释放可能触发懒加载。
    """
    return _enrich_with_jellyfin(_to_dict(item))


def _enrich_with_jellyfin(d: dict) -> dict:
    """给已 snapshot 的 dict 附加 Jellyfin cross-ref 字段。
    这里调 lookup_jellyfin_item 可能触发 Jellyfin HTTP（path index TTL 30s 过期时），
    所以本函数应当在 db session 关闭之后再调用，避免持连接跨 HTTP。
    """
    from web.backend.api.medialibraries import lookup_jellyfin_item, jellyfin_web_url
    fp = d.get('file_path')
    if not fp:
        return d
    jf = lookup_jellyfin_item(fp)
    if jf:
        d['jellyfin_id'] = jf['id']
        d['jellyfin_url'] = jellyfin_web_url(jf['id'])
        d['jellyfin_name'] = jf.get('name')
        # runtime_min 来自 jellyfin 的 RunTimeTicks（path-index 反查时记下的）
        if jf.get('runtime_min') is not None:
            d['runtime_min'] = jf['runtime_min']
    return d


@router.get("/items/{item_id}")
def get_item(item_id: int, db: Session = Depends(get_db)):
    item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    d = _to_dict(item, full=True)
    # 附加 Jellyfin 状态
    if item.file_path:
        from web.backend.api.medialibraries import lookup_jellyfin_item, jellyfin_web_url
        jf = lookup_jellyfin_item(item.file_path)
        if jf:
            d['jellyfin_id'] = jf['id']
            d['jellyfin_url'] = jellyfin_web_url(jf['id'])
            d['jellyfin_name'] = jf.get('name')
    return d


# 注意：清库 /items/_all 必须放在 /items/{item_id} 之前，否则 _all 会被当作 item_id 解析

@router.delete("/items/_all")
def clear_all_items(confirm: str = "", db: Session = Depends(get_db)):
    """
    清空番号库（仅 DB 记录，不动磁盘文件）。需要 confirm=YES 防误调。
    """
    if confirm != "YES":
        logger.warning(f"/items/_all 拒绝：缺少 confirm=YES (got {confirm!r})")
        raise HTTPException(status_code=400, detail="需要 confirm=YES 确认清空")
    n = db.query(AdultItem).delete()
    db.commit()
    logger.warning(f"/items/_all 清空番号库 DB 记录：{n} 行已删除（磁盘文件保留）")
    return {"ok": True, "deleted": n}


@router.post("/items/{item_id}/identify")
def identify_item(
    item_id: int,
    payload: IdentifyRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """给一个未识别（或识别错的）条目手动指定番号。指定后默认立即触发刮削。"""
    item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")

    code = (payload.code or "").strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="番号不能为空")

    # 番号已被其他记录占用 → 拒绝（避免 unique 冲突）
    clash = db.query(AdultItem).filter(
        AdultItem.code == code, AdultItem.id != item_id
    ).first()
    if clash:
        raise HTTPException(
            status_code=409,
            detail=f"番号 {code} 已被另一条目（id={clash.id}）占用",
        )

    item.code = code
    db.commit()

    task_id = None
    if payload.auto_scrape:
        task = create_task(db, "adult_scrape", f"刮削: {code}")
        background_tasks.add_task(run_adult_scrape_batch, task.id, [item.id], True, True)
        task_id = task.id

    return {"ok": True, "code": code, "task_id": task_id, "item": _to_dict(item, full=True)}


@router.put("/items/{item_id}")
def update_item(item_id: int, payload: ManualUpdate, db: Session = Depends(get_db)):
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

    # 封面：传新 URL 触发下载；传空字符串清除
    if payload.cover_url is not None:
        new_url = payload.cover_url.strip()
        if new_url:
            # 下载替换：DB 记 cover_url（外站 URL 索引）+ poster_path（本地文件）
            if item.file_path:
                try:
                    cover_path = _download_cover(new_url, _local_path(item.file_path))
                    if cover_path:
                        item.cover_url = new_url
                        # poster_path 也按 DB 约定存 Jellyfin view
                        from web.backend.path_translator import reverse_translate_path_with_settings
                        item.poster_path = reverse_translate_path_with_settings(str(cover_path)) or str(cover_path)
                    else:
                        # 下载失败：DB 仍然记 URL，但不更新 poster_path
                        item.cover_url = new_url
                        logger.warning(f"封面下载失败 item={item_id} url={new_url}")
                except Exception as e:
                    logger.warning(f"封面下载异常 item={item_id}: {e}")
                    raise HTTPException(status_code=502, detail=f"封面下载失败: {e}")
            else:
                # 没视频文件：只能记 URL（前端通过 cover_url 直接拉外站）
                item.cover_url = new_url
        else:
            # 空字符串 → 清除本地封面 + DB
            if item.poster_path:
                try:
                    p = _local_path(item.poster_path)
                    if p.exists():
                        p.unlink()
                except OSError as e:
                    logger.warning(f"清除本地封面文件失败 item={item_id}: {e}")
            item.cover_url = None
            item.poster_path = None

    db.commit()
    return _to_dict(item, full=True)


@router.post("/items/{item_id}/cover-upload")
def upload_cover(item_id: int, file: UploadFile = File(...)):
    """
    用户上传本地图片作为封面：保存到 <video>-poster.<ext>，写入 DB。
    支持 jpg / jpeg / png / webp，10MB 上限。
    """
    ALLOWED = {'.jpg', '.jpeg', '.png', '.webp'}
    MAX_BYTES = 10 * 1024 * 1024

    name = (file.filename or '').lower()
    ext = '.' + name.rsplit('.', 1)[-1] if '.' in name else ''
    if ext not in ALLOWED:
        raise HTTPException(status_code=400, detail=f"扩展名不支持，仅接受 {', '.join(ALLOWED)}")

    with SessionLocal() as db:
        item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
        if not item:
            raise HTTPException(status_code=404, detail="条目不存在")
        if not item.file_path:
            raise HTTPException(status_code=400, detail="条目没有关联视频文件，无法保存封面")
        video_path = _local_path(item.file_path)

        try:
            content = file.file.read()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"读上传文件失败: {e}")
        if len(content) > MAX_BYTES:
            raise HTTPException(status_code=400, detail=f"文件过大（>10MB）")
        if not content:
            raise HTTPException(status_code=400, detail="空文件")

        # 落地：跟 _download_cover 同款命名约定
        target = video_path.with_name(video_path.stem + '-poster' + ext)
        try:
            target.write_bytes(content)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"写入失败: {e}")

        # poster_path 也按 DB 约定存 Jellyfin view
        from web.backend.path_translator import reverse_translate_path_with_settings
        item.poster_path = reverse_translate_path_with_settings(str(target)) or str(target)
        # cover_url 不动（用户可能继续保留外站源链）
        db.commit()
        db.refresh(item)
        return _to_dict(item, full=True)


@router.delete("/items/{item_id}")
def delete_item(
    item_id: int,
    delete_files: bool = False,        # 同时删除硬盘上的视频 + nfo + 封面
    delete_in_jellyfin: bool = False,  # 同时从 Jellyfin 库中删除条目
    db: Session = Depends(get_db),
):
    item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")

    logger.warning(
        f"/adult/items/delete: id={item_id} code={item.code!r} "
        f"file_path={item.file_path!r} delete_files={delete_files} "
        f"delete_in_jellyfin={delete_in_jellyfin}"
    )

    deleted_files: List[str] = []
    failed_deletes: List[str] = []
    jellyfin_deleted = False

    # 1. 先从 Jellyfin 删（避免删了文件但 Jellyfin 还引用）
    if delete_in_jellyfin and settings.jellyfin_api_key and item.file_path:
        from web.backend.api.medialibraries import lookup_jellyfin_item, invalidate_path_index
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
            targets.append(_local_path(item.file_path))
        if item.nfo_path:
            targets.append(_local_path(item.nfo_path))
        if item.poster_path:
            targets.append(_local_path(item.poster_path))
        # 也尝试找 fanart / 同名 nfo
        if item.file_path:
            stem_path = _local_path(item.file_path).with_suffix('')
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

    logger.warning(
        f"/adult/items/delete 完成: id={item_id} jellyfin_deleted={jellyfin_deleted} "
        f"files_deleted={len(deleted_files)} failed={len(failed_deletes)}"
    )

    return {
        "ok": True,
        "jellyfin_deleted": jellyfin_deleted,
        "deleted_files": deleted_files,
        "failed_deletes": failed_deletes,
    }


# ---------- Watcher 状态 / 控制 ----------

@router.get("/watcher/status")
def watcher_status():
    """获取 watcher 当前状态"""
    from web.backend.services.adult_watcher import watcher
    return watcher.status()


@router.post("/reset-and-rescan")
def reset_and_rescan(library_id: str, dry_run: bool = False):
    """
    清空指定库的所有 AdultItem 元数据 + 重扫入库 + 刮削。

    流程：
      1. 删除该库下所有 AdultItem 行（识别 + 刮削数据全部清掉）
      2. 触发 watcher.trigger_libraries(force_scrape=True)：
         同一 task 内先扫描入库（识别番号），扫完后立即刮削（拉元数据 + 下封面 + 写 NFO）

    任务进度：5% 解析路径 → 15% 找到 N 视频 → 90% 识别完成 → 92% 刮削 → 100% 完成

    dry_run=True 时：只统计会被删的条数，不真删、不触发扫描。
    """
    logger.warning(f"/adult/reset-and-rescan: library_id={library_id!r} dry_run={dry_run}")
    if not library_id:
        raise HTTPException(status_code=400, detail="必须指定 library_id")

    cond = _library_path_filter(library_id)
    if cond is None:
        logger.warning(f"/adult/reset-and-rescan: 无法解析 library {library_id} 的路径")
        raise HTTPException(status_code=400, detail=f"无法解析 library {library_id} 的路径")

    if dry_run:
        with SessionLocal() as db:
            would_delete = db.query(AdultItem).filter(cond).count()
        return {
            "ok": True,
            "dry_run": True,
            "deleted": 0,
            "would_delete": would_delete,
            "library_id": library_id,
            "task_id": None,
            "message": f"测试模式：将清空 {would_delete} 条记录，未实际执行",
        }

    # 1. 清表（仅该库范围）
    with SessionLocal() as db:
        deleted = db.query(AdultItem).filter(cond).delete(synchronize_session=False)
        db.commit()
    logger.warning(f"/adult/reset-and-rescan: 清空 {deleted} 条 AdultItem (library {library_id})")

    # 2. 触发 watcher 扫描+刮削（同一任务，扫完衔接刮削）
    from web.backend.services.adult_watcher import watcher
    scheduled = watcher.trigger_libraries(
        [library_id],
        bypass_cooldown=True,
        force_scrape=True,
    )
    task_id = scheduled.get(library_id)
    if not task_id:
        logger.warning(
            f"/adult/reset-and-rescan: 已清空 {deleted} 条，"
            f"但库 {library_id} 已有任务在跑，未启动新任务"
        )
        raise HTTPException(
            status_code=409,
            detail=f"已清空 {deleted} 条，但库 {library_id} 已有任务在跑，未启动新任务",
        )

    logger.info(
        f"/adult/reset-and-rescan: 启动任务 #{task_id} (library {library_id}, deleted={deleted})"
    )
    return {
        "ok": True,
        "deleted": deleted,
        "library_id": library_id,
        "task_id": task_id,
        "message": f"已清空 {deleted} 条记录，扫描+刮削任务 #{task_id} 已启动",
    }


@router.post("/watcher/run-now")
def watcher_run_now(library_id: Optional[str] = None):
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
            f"(跳过 {len(target_ids) - len(scheduled)} 个已在跑中的库)"
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
def detect_adult_libraries():
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


# ---------- 单条刮削 / 批量刮削 ----------
# 注意：/scrape/batch 必须注册在 /scrape/{code} 之前，
# 否则 FastAPI 会把 "batch" 当作 code 参数去 DB 查（路由按注册顺序匹配）

@router.post("/scrape/batch")
def scrape_batch(
    payload: ScrapeRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """批量刮削（仅已识别 code、且未被用户排除的条目）"""
    query = (
        db.query(AdultItem)
        .filter(AdultItem.code != None)              # noqa: E711
        .filter(AdultItem.excluded == False)         # noqa: E712  excluded 不参与自动刮削
    )
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


@router.post("/scrape/{code}")
def scrape_one(
    code: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """刮削单个番号(按 code 查找)"""
    item = db.query(AdultItem).filter(AdultItem.code == code).first()
    if not item:
        raise HTTPException(status_code=404, detail=f"番号不存在: {code}")

    task = create_task(db, "adult_scrape", f"刮削: {code}")
    background_tasks.add_task(run_adult_scrape_batch, task.id, [item.id], True, True)
    return {"task_id": task.id, "status": "started"}


@cancellable_task
def run_adult_scrape_batch(task_id: int, item_ids: List[int], write_nfo: bool, download_cover: bool):
    """
    批量刮削。**关键约束**：每条 item 一个短事务，HTTP 慢请求期间不持有 DB 连接。
    旧实现整个批次共用一个 Session，会把 sqlalchemy 连接池吃满（pool=5+10），
    其他请求拿不到连接 → 整站不可用。
    """
    from web.backend.database import SessionLocal
    from tools.adult_manager.scrapers.manager import ScraperManager
    from tools.adult_manager.nfo_writer import write_nfo as do_write_nfo
    from web.backend.task_log_capture import attach as _log_attach
    from web.backend.shutdown import is_shutting_down

    manager = ScraperManager(
        delay=ADULT_SCRAPER_DELAY,
        sources=settings.adult_sources,
        batch=True,
    )
    if not manager.scrapers:
        with SessionLocal() as db:
            complete_task(db, task_id, {"error": "没有启用任何刮削源（检查 config.yaml.adult.sources）"}, success=False)
        return
    logger.info(f"刮削启用源: {manager.active_sources}")

    total = len(item_ids)
    success = failed = not_found = 0
    details = []

    def _patch():
        return {
            "total": total,
            "success": success,
            "failed": failed,
            "not_found": not_found,
            "details": details[-200:],
        }

    try:
        for i, item_id in enumerate(item_ids):
            # graceful shutdown: uvicorn reload / Ctrl+C 时让长任务尽快退出
            if is_shutting_down():
                logger.info(f"收到 shutdown 信号，批量刮削提前退出（已处理 {i}/{total}）")
                with SessionLocal() as db:
                    complete_task(db, task_id, {
                        "total": total,
                        "success": success,
                        "failed": failed,
                        "not_found": not_found,
                        "stopped_by_shutdown": True,
                        "details": details,
                    }, success=False, final_message=f"已处理 {i}/{total}，进程关闭信号触发提前退出")
                return

            # ---- 短事务 1：取出 item 元信息 ----
            with SessionLocal() as db:
                item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
                if not item:
                    continue
                code = item.code
                file_path = item.file_path
                existing_poster = item.poster_path
                existing_nfo = item.nfo_path

            _log_attach(task_id, sub_key=code)

            progress = 5 + int(90 * (i + 1) / total)
            with SessionLocal() as db:
                update_task_progress(
                    db, task_id, progress, f"[{i+1}/{total}] {code}",
                    result_patch=_patch(),
                )

            # ---- 慢操作（HTTP 刮削 / 下载 / 写文件）：不持有 DB 连接 ----
            try:
                result = manager.scrape(code)
                if not result:
                    not_found += 1
                    # 维护 scrape_attempts；达到阈值进 7 天 cooldown（不再让 watcher 反复打外站）
                    cooldown_set = False
                    with SessionLocal() as db:
                        it = db.query(AdultItem).filter(AdultItem.id == item_id).first()
                        if it:
                            it.scrape_attempts = (it.scrape_attempts or 0) + 1
                            it.last_scrape_at = datetime.utcnow()
                            it.source = 'not_found'
                            if it.scrape_attempts >= COOLDOWN_AFTER_FAILURES:
                                it.cooldown_until = datetime.utcnow() + timedelta(days=COOLDOWN_DAYS)
                                cooldown_set = True
                            db.commit()
                    details.append({
                        "code": code, "status": "not_found",
                        "auto_cooldown": cooldown_set,
                    })
                    continue

                d = result.to_dict()

                # 封面下载：DB 已记录 poster_path 且文件存在 → 跳过
                new_poster_path: Optional[str] = None
                if download_cover and d.get('cover_url') and file_path:
                    if existing_poster and Path(existing_poster).exists():
                        pass
                    else:
                        try:
                            cover_path = _download_cover(d['cover_url'], _local_path(file_path))
                            if cover_path:
                                # poster_path 按 DB 约定存 Jellyfin view
                                from web.backend.path_translator import reverse_translate_path_with_settings as _rev_tr
                                new_poster_path = _rev_tr(str(cover_path)) or str(cover_path)
                        except Exception as e:
                            logger.warning(f"封面下载失败 {code}: {e}")

                # NFO：DB 已记录 nfo_path 且文件存在 → 跳过
                new_nfo_path: Optional[str] = None
                if write_nfo and file_path:
                    if existing_nfo and Path(existing_nfo).exists():
                        pass
                    else:
                        try:
                            nfo_path = do_write_nfo(_local_path(file_path), d)
                            from web.backend.path_translator import reverse_translate_path_with_settings as _rev_tr
                            new_nfo_path = _rev_tr(str(nfo_path)) or str(nfo_path)
                        except Exception as e:
                            logger.warning(f"NFO 写入失败 {code}: {e}")

                # ---- 短事务 2：把抓到的元数据写回 ----
                with SessionLocal() as db:
                    item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
                    if not item:
                        continue
                    item.title = d.get('title')
                    item.release_date = d.get('release_date')
                    item.studio = d.get('studio')
                    item.director = d.get('director')
                    item.actors = json.dumps(d.get('actors') or [], ensure_ascii=False)
                    item.tags = json.dumps(d.get('tags') or [], ensure_ascii=False)
                    item.cover_url = d.get('cover_url')
                    item.rating = d.get('rating')
                    item.source = d.get('source')
                    if new_poster_path:
                        item.poster_path = new_poster_path
                    if new_nfo_path:
                        item.nfo_path = new_nfo_path
                    # 成功 → 重置失败计数 / 清除自动 cooldown（用户主动 excluded 不动）
                    item.scrape_attempts = 0
                    item.cooldown_until = None
                    item.last_scrape_at = datetime.utcnow()
                    db.commit()
                    title = item.title

                success += 1
                details.append({
                    "code": code,
                    "status": "success",
                    "title": title,
                    # 让前端能展示"刮到了哪些字段"，而不是只显示 title
                    "scraped_fields": {
                        "title": bool(d.get('title')),
                        "release_date": d.get('release_date'),
                        "studio": d.get('studio'),
                        "director": d.get('director'),
                        "actors_count": len(d.get('actors') or []),
                        "tags_count": len(d.get('tags') or []),
                        "cover": bool(d.get('cover_url')),
                        "rating": d.get('rating'),
                        "source": d.get('source'),  # "merged:javbus,javdb,..." 等
                        "saved_poster": bool(new_poster_path),
                        "saved_nfo": bool(new_nfo_path),
                    },
                })

            except Exception as e:
                logger.exception(f"刮削异常 {code}")
                failed += 1
                # 异常也算一次失败 —— 累加到 attempts，达阈值进 cooldown
                cooldown_set = False
                with SessionLocal() as db:
                    it = db.query(AdultItem).filter(AdultItem.id == item_id).first()
                    if it:
                        it.scrape_attempts = (it.scrape_attempts or 0) + 1
                        it.last_scrape_at = datetime.utcnow()
                        if it.scrape_attempts >= COOLDOWN_AFTER_FAILURES:
                            it.cooldown_until = datetime.utcnow() + timedelta(days=COOLDOWN_DAYS)
                            cooldown_set = True
                        db.commit()
                details.append({
                    "code": code,
                    "status": "failed",
                    "error": str(e),
                    "error_type": e.__class__.__name__,
                    "auto_cooldown": cooldown_set,
                })

        # 通知 Jellyfin（精准 path 通知，整库扫兜底）
        refreshed = False
        if (write_nfo or download_cover) and success > 0 and settings.jellyfin_api_key:
            try:
                from common.jellyfin_client import JellyfinClient
                with SessionLocal() as db:
                    update_task_progress(db, task_id, 99, "通知 Jellyfin 刷新...")

                # 收集成功刮削的 item 视频路径（jellyfin 视角）
                # DB 现在直接存 Jellyfin view，无需 reverse_translate
                paths_for_jf = []
                with SessionLocal() as db:
                    success_codes = [d['code'] for d in details if d.get('status') == 'success']
                    if success_codes:
                        items_in_db = (
                            db.query(AdultItem)
                            .filter(AdultItem.code.in_(success_codes))
                            .all()
                        )
                        for it in items_in_db:
                            if it.file_path:
                                paths_for_jf.append(it.file_path)

                jf = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
                if paths_for_jf and jf.notify_media_updated(paths_for_jf, update_type='Modified'):
                    refreshed = True
                else:
                    # 兜底：整库扫
                    jf.refresh_all_libraries()
                    refreshed = True
            except Exception as e:
                logger.warning(f"触发 Jellyfin 刷新失败: {e}")

        with SessionLocal() as db:
            complete_task(db, task_id, {
                "total": total,
                "success": success,
                "failed": failed,
                "not_found": not_found,
                "jellyfin_refreshed": refreshed,
                "details": details,
            })

    except Exception as e:
        logger.exception("批量刮削任务失败")
        with SessionLocal() as db:
            complete_task(db, task_id, {"error": str(e)}, success=False)
    finally:
        db.close()


@router.post("/items/{item_id}/sync-from-jellyfin")
def sync_from_jellyfin(item_id: int, db: Session = Depends(get_db)):
    """
    从 Jellyfin 同步元数据回番号库（防止重新刮削覆盖手动修改）。
    通过 file_path 反查 Jellyfin Item，拉取其 People/Genres/Tags/Overview 等。
    """
    item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    if not item.file_path:
        raise HTTPException(status_code=400, detail="番号没有关联视频文件")

    from web.backend.api.medialibraries import lookup_jellyfin_item
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


@router.get("/actors")
def list_actors(
    search: Optional[str] = None,
    limit: int = 30,
    db: Session = Depends(get_db),
):
    """
    返回库里所有女优名 + 出现次数（按出现次数倒序）。
    供前端"女优过滤"下拉提供建议用。
    """
    rows = db.query(AdultItem.actors).filter(AdultItem.actors != None).all()  # noqa: E711
    counter: dict = {}
    for (actors_json,) in rows:
        if not actors_json:
            continue
        try:
            arr = json.loads(actors_json)
        except Exception:
            continue
        if not isinstance(arr, list):
            continue
        for name in arr:
            if not name:
                continue
            counter[name] = counter.get(name, 0) + 1

    items = [{'name': n, 'count': c} for n, c in counter.items()]
    if search:
        s = search.lower()
        items = [x for x in items if s in x['name'].lower()]
    items.sort(key=lambda x: (-x['count'], x['name']))
    return {'actors': items[:limit]}


@router.get("/items/{item_id}/poster")
def get_item_poster(item_id: int, db: Session = Depends(get_db)):
    """返回番号海报图片 — 仅本地副本（DB.poster_path）。文件不存在就 404。"""
    from fastapi.responses import FileResponse
    item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    if not item.poster_path:
        raise HTTPException(status_code=404, detail="本地没有海报")
    p = _local_path(item.poster_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="海报文件不存在")
    return FileResponse(p, media_type='image/jpeg')


@router.post("/items/{item_id}/nfo")
def regenerate_nfo(item_id: int, db: Session = Depends(get_db)):
    """重新生成 NFO"""
    from tools.adult_manager.nfo_writer import write_nfo as do_write_nfo

    item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    if not item.file_path:
        raise HTTPException(status_code=400, detail="条目没有关联视频文件")

    data = _to_dict(item, full=True)
    try:
        nfo_path = do_write_nfo(_local_path(item.file_path), data)
        from web.backend.path_translator import reverse_translate_path_with_settings
        item.nfo_path = reverse_translate_path_with_settings(str(nfo_path)) or str(nfo_path)
        db.commit()
        return {"ok": True, "nfo_path": str(nfo_path)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入失败: {e}")


# ---------- 工具 ----------

def _to_dict(item: AdultItem, full: bool = False) -> dict:
    # DB 里 file_path / poster_path / nfo_path 都存 Jellyfin view，
    # 前端展示用本机视角；磁盘存在性判定也得用本机视角
    from web.backend.path_translator import translate_path_with_settings as _tr
    file_name = Path(item.file_path).name if item.file_path else None
    file_path_local = _tr(item.file_path) if item.file_path else None
    poster_path_local = _tr(item.poster_path) if item.poster_path else None
    nfo_path_local = _tr(item.nfo_path) if item.nfo_path else None

    # ⭐ 封面 / NFO 的真实存在性（跟 stats 计算 missing_cover/missing_nfo 同款逻辑）。
    # 之前前端只看 poster_path/cover_url 字段非空就判"有封面"，
    # 但字段有 URL 不等于本地下载成功 → 显示空白却被认为完整。
    # 这两个 bool 字段是权威判定，前端 HealthCell / 列表都应该用。
    cover_local_ok = bool(poster_path_local) and Path(poster_path_local).exists()
    nfo_local_ok = bool(nfo_path_local) and Path(nfo_path_local).exists()

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
        "recognized": item.code is not None,
        "file_name": file_name,
        "file_path": file_path_local,
        "nfo_path": nfo_path_local,
        "poster_path": poster_path_local,
        "cover_local_ok": cover_local_ok,   # 本地封面文件实存（权威）
        "nfo_local_ok": nfo_local_ok,       # 本地 NFO 文件实存
        "actors": json.loads(item.actors) if item.actors else [],
        "tags": json.loads(item.tags) if item.tags else [],
        # 有/无码：优先用户手动覆盖（is_uncensored_override），否则走关键字自动判定
        # is_uncensored=True 无码；False 有码；None 未识别
        "is_uncensored": _resolve_uncensored(item),
        # 是否被用户手动覆盖（前端用来给 badge 加 "M" 标记）
        "is_uncensored_manual": item.is_uncensored_override is not None,
        # 排除状态 + 失败计数 + 冷却到期（前端 HealthCell 用来判定 / 显示）
        "excluded": bool(getattr(item, 'excluded', False)),
        "scrape_attempts": int(getattr(item, 'scrape_attempts', 0) or 0),
        "cooldown_until": (
            # DB 是 naive UTC；显式 +Z 让浏览器按 UTC 解析
            item.cooldown_until.isoformat() + 'Z'
            if getattr(item, 'cooldown_until', None) else None
        ),
        # updated_at 给前端做图片 cache buster：重新识别后 onupdate 自动 bump，
        # 前端拼 ?v=<updated_at> 强制浏览器拉新封面（否则同 URL 命中缓存仍是老图）
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }
    if full:
        out["created_at"] = item.created_at.isoformat() if item.created_at else None
        # updated_at 已在默认字段里
    return out


def _download_cover(url: str, video_path: Path) -> Optional[Path]:
    """
    下载封面到 <video_stem>-poster.jpg。
    优先用 curl_cffi 绕过 Cloudflare（fourhoi.com / missav 等图床有 CF 防护）；
    失败再退回 requests。Referer 策略：先用同源（多数图床校验防盗链），
    若 403 再用无 Referer 重试（fourhoi 等同源 Referer 反而被拒的特例）。
    """
    from urllib.parse import urlparse
    p = urlparse(url)
    base_ua = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
    }
    same_origin_ref = f"{p.scheme}://{p.netloc}/" if p.scheme and p.netloc else None

    def _attempt_cffi(headers):
        try:
            from curl_cffi import requests as cffi_req
            r = cffi_req.get(url, timeout=30, headers=headers, impersonate='chrome120')
            if r.status_code < 400 and r.content:
                return r.content
            return None
        except ImportError:
            return None
        except Exception as e:
            logger.debug(f"封面下载 [cffi] 异常 {url}: {e}")
            return None

    def _attempt_requests(headers):
        try:
            r = requests.get(url, timeout=30, headers=headers)
            r.raise_for_status()
            return r.content
        except Exception as e:
            logger.debug(f"封面下载 [requests] {url}: {e}")
            return None

    # 4 个 attempt：cffi/requests × 同源 Referer/无 Referer
    attempts = []
    if same_origin_ref:
        attempts.append((_attempt_cffi,     {**base_ua, 'Referer': same_origin_ref}))
    attempts.append((_attempt_cffi,     dict(base_ua)))
    if same_origin_ref:
        attempts.append((_attempt_requests, {**base_ua, 'Referer': same_origin_ref}))
    attempts.append((_attempt_requests, dict(base_ua)))

    for fn, headers in attempts:
        content = fn(headers)
        if content:
            cover_path = video_path.with_name(video_path.stem + '-poster.jpg')
            cover_path.write_bytes(content)
            return cover_path

    logger.warning(f"封面下载失败 {url}（所有 attempt 都不通）")
    return None


# ============================================================================
# 女优库 lazy 构建（启动后台线程，慢慢爬 javdb 把名字归一化）
# ============================================================================

class ActressBuildStartReq(BaseModel):
    request_delay: float = 5.0  # 每次请求最小间隔（秒）。保守爬建议 ≥5s


@router.get("/actresses/build/status")
def actress_build_status():
    """前端轮询用：拿当前构建进度 + 总览数据。"""
    from web.backend.services.actress_builder import builder
    return builder.status()


@router.post("/actresses/build/start")
def actress_build_start(req: ActressBuildStartReq):
    """启动后台构建任务。已在跑 → 409。"""
    from web.backend.services.actress_builder import builder
    ok = builder.start(request_delay=req.request_delay)
    if not ok:
        raise HTTPException(status_code=409, detail="构建任务已在运行")
    return builder.status()


@router.post("/actresses/build/stop")
def actress_build_stop():
    """请求停止；线程会在当前 query 完成后退出。"""
    from web.backend.services.actress_builder import builder
    ok = builder.stop()
    if not ok:
        raise HTTPException(status_code=409, detail="没有正在运行的构建任务")
    return builder.status()


@router.get("/library-actors")
def list_library_actors(
    library_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    返回当前库（或全部）AdultItem.actors 字段中出现过的所有演员名字 + 作品数。
    用于"女优筛选"下拉 —— 不依赖 AdultActress 表是否解析过。

    Returns: { actors: [{name, count}, ...] } 按 count 降序
    """
    query = db.query(AdultItem)
    if library_id:
        cond = _library_path_filter(library_id)
        if cond is None:
            return {"actors": []}
        query = query.filter(cond)
    items = query.filter(AdultItem.actors != None).all()  # noqa: E711

    counter: Dict[str, int] = {}
    for it in items:
        if not it.actors:
            continue
        try:
            actors = json.loads(it.actors)
        except (ValueError, TypeError):
            continue
        if not isinstance(actors, list):
            continue
        for n in actors:
            if isinstance(n, str) and n.strip():
                counter[n] = counter.get(n, 0) + 1

    out = sorted(
        [{'name': k, 'count': v} for k, v in counter.items()],
        key=lambda x: (-x['count'], x['name']),
    )
    return {'actors': out}


@router.get("/actresses")
def list_actresses(
    q: Optional[str] = None,
    only_resolved: bool = False,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """女优列表。q 在 jp/zh/en/aliases 里全局模糊匹配（用于姓名→女优解析）。"""
    from web.backend.database import AdultActress
    query = db.query(AdultActress)
    if only_resolved:
        query = query.filter(
            AdultActress.source.notin_(['pending', 'not_found']),
            AdultActress.source.isnot(None),
        )
    if q:
        like = f'%{q}%'
        query = query.filter(
            (AdultActress.jp_name.ilike(like))
            | (AdultActress.zh_name.ilike(like))
            | (AdultActress.en_name.ilike(like))
            | (AdultActress.aliases.ilike(like))  # 别名 JSON 文本里 LIKE 匹配
        )
    total = query.count()
    rows = (query.order_by(AdultActress.id.desc())
                 .offset(offset).limit(limit).all())

    def _to_dict(a):
        try:
            aliases = json.loads(a.aliases) if a.aliases else []
        except Exception:
            aliases = []
        return {
            'id': a.id,
            'jp_name': a.jp_name,
            'zh_name': a.zh_name,
            'en_name': a.en_name,
            'aliases': aliases,
            'avatar_url': a.avatar_url,
            'birth_date': a.birth_date,
            'debut_date': a.debut_date,
            'age': a.age,
            'source': a.source,
            'javdb_id': a.javdb_id,
            'updated_at': a.updated_at.isoformat() if a.updated_at else None,
        }

    return {'total': total, 'items': [_to_dict(a) for a in rows]}


# ============================================================================
# 库统计（给"成人库详情页"的 stats 卡用，对齐普通库的统计卡结构）
# ============================================================================

@router.get("/stats")
def adult_library_stats(
    library_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    返回成人库各项指标。返回字段对齐前端「统计卡片」的指标网格：
      total / recognized / unrecognized / scraped / missing_cover / missing_nfo
      censored / uncensored / excluded / cooling
      total_size_bytes / total_size_gb / total_duration_seconds
    """
    query = db.query(AdultItem)
    if library_id:
        cond = _library_path_filter(library_id)
        if cond is None:
            return {
                "total": 0, "recognized": 0, "unrecognized": 0, "scraped": 0,
                "healthy": 0,
                "missing_cover": 0, "missing_nfo": 0,
                "censored": 0, "uncensored": 0,
                "excluded": 0, "cooling": 0,
                "total_size_bytes": 0, "total_size_gb": 0.0,
                "total_duration_seconds": 0,
            }
        query = query.filter(cond)

    items = query.all()
    total = len(items)
    recognized = 0
    scraped = 0
    healthy = 0  # 完全完整：scraped + 本地有封面 + 本地有 NFO（前端"健康度"用此值）
    # "缺封面" 跟 repair_covers 候选条件对齐：cover_url 有但本地 poster 缺（字段空或文件丢）
    # 这样 stats 数字 == 点"修复封面"按钮能处理的条目数；不会出现"显示缺 1 但没法修"的矛盾
    missing_cover = 0
    missing_nfo = 0
    censored = 0
    uncensored = 0
    excluded_count = 0
    cooling_count = 0

    now = datetime.utcnow()
    from web.backend.path_translator import translate_path_with_settings as _stat_tr
    for i in items:
        if i.code:
            recognized += 1
        if i.title and i.source not in (None, 'pending', 'not_found'):
            scraped += 1
            # poster_path / nfo_path 是 Jellyfin view，磁盘存在性判定要本机视角
            poster_local = _stat_tr(i.poster_path) if i.poster_path else None
            nfo_local = _stat_tr(i.nfo_path) if i.nfo_path else None
            cover_ok = bool(poster_local) and Path(poster_local).exists()
            nfo_ok = bool(nfo_local) and Path(nfo_local).exists()
            # 缺封面 = 有 cover_url（源给了图）但本地 poster 不可用
            if i.cover_url and not cover_ok:
                missing_cover += 1
            # 缺 NFO = nfo_path 字段空 或 文件不存在
            if not nfo_ok:
                missing_nfo += 1
            # 完整健康 = scraped + cover_ok + nfo_ok 三件齐全
            if cover_ok and nfo_ok:
                healthy += 1
        # 有码 / 无码：优先 is_uncensored_override，否则关键字自动判定
        flag = _resolve_uncensored(i)
        if flag is True:
            uncensored += 1
        elif flag is False:
            censored += 1
        # flag is None（未识别）→ 不计入

        # excluded：用户主动标记永久排除
        if i.excluded:
            excluded_count += 1
        # cooling：自动失败连续 N 次进入冷却中（且不是 excluded）
        elif i.cooldown_until and i.cooldown_until > now:
            cooling_count += 1

    # ---- 占用空间 + 总时长（仅当 library_id 提供时计算）----
    # 实施策略：
    #   total_size：rglob 库 location 累加视频文件 stat().st_size（毫秒级）
    #   duration：从 Jellyfin /Items 拿 RunTimeTicks 累加（一次 HTTP 调用）
    # 都失败 / 不可用时返回 0，不影响其它字段
    total_size_bytes = 0
    total_duration_seconds = 0

    if library_id:
        # 1) 文件系统大小
        try:
            from web.backend.api.medialibraries import get_library_by_id
            from web.backend.path_translator import translate_path_with_settings as _tr
            video_exts = {'.mp4', '.mkv', '.avi', '.wmv', '.mov', '.flv', '.webm', '.m4v', '.ts', '.rmvb'}
            lib_info = get_library_by_id(library_id)
            if lib_info:
                for loc in lib_info.get('locations') or []:
                    local = _tr(loc) or loc
                    p = Path(local)
                    if not p.exists():
                        continue
                    try:
                        for f in p.rglob('*'):
                            if not f.is_file():
                                continue
                            if f.suffix.lower() not in video_exts:
                                continue
                            try:
                                total_size_bytes += f.stat().st_size
                            except OSError:
                                continue
                    except (PermissionError, OSError):
                        continue
        except Exception as e:
            logger.warning(f"adult stats 拉文件大小失败: {e}")

        # 2) 总时长（从 Jellyfin 拿 RunTimeTicks）
        try:
            from common.jellyfin_client import JellyfinClient
            if settings.jellyfin_api_key:
                jc = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
                # Movie/Episode/Video 三种都拿（覆盖 Jellyfin 给成人库的几种识别结果）
                items = jc.get_library_items(
                    library_id,
                    item_types='Movie,Episode,Video',
                    fields='RunTimeTicks',
                )
                ticks = sum(int(it.get('RunTimeTicks') or 0) for it in items)
                # 1 RunTimeTick = 100ns → ÷ 10_000_000 转秒
                total_duration_seconds = ticks // 10_000_000
        except Exception as e:
            logger.warning(f"adult stats 拉时长失败: {e}")

    return {
        "total": total,
        "recognized": recognized,
        "unrecognized": total - recognized,
        "scraped": scraped,
        "healthy": healthy,           # scraped + 封面/NFO 都齐
        "missing_cover": missing_cover,
        "missing_nfo": missing_nfo,
        "censored": censored,
        "uncensored": uncensored,
        "excluded": excluded_count,
        "cooling": cooling_count,
        "total_size_bytes": total_size_bytes,
        "total_size_gb": round(total_size_bytes / (1024 ** 3), 2),
        "total_duration_seconds": total_duration_seconds,
    }


# ============================================================================
# 阶段 C：单条重刮 + 批量修复封面 / 修复识别错误
# ============================================================================

class AdultUncensoredReq(BaseModel):
    # 三态：True=手动设无码 / False=手动设有码 / None=清除手动覆盖（恢复自动判定）
    value: Optional[bool] = None


@router.post("/items/{item_id}/uncensored")
def adult_set_uncensored(item_id: int, req: AdultUncensoredReq):
    """
    手动设置 / 清除"有码 / 无码"标志。
      value=true  → 标为无码
      value=false → 标为有码
      value=null  → 清除手动覆盖，回归自动判定

    返回轻量 patch（不做 _enrich_with_jellyfin —— 那个会触发 path index 重建，
    跟"有/无码"修改完全无关，且能让单次响应慢到 1s+）。前端按字段 merge 即可。
    """
    with SessionLocal() as db:
        item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
        if not item:
            raise HTTPException(status_code=404, detail="条目不存在")
        item.is_uncensored_override = req.value
        db.commit()
        db.refresh(item)
        return {
            "id": item.id,
            "is_uncensored": _resolve_uncensored(item),
            "is_uncensored_manual": item.is_uncensored_override is not None,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        }


class AdultExcludeReq(BaseModel):
    excluded: bool = True


# 自动连续失败 N 次后进入冷却（不再永久排除）
# 区别:
#   - 用户主动"排除":  excluded=True,  永久跳过,只能用户手动取消
#   - 自动失败到阈值:   cooldown_until=now+COOLDOWN_DAYS, 到期自动失效
COOLDOWN_AFTER_FAILURES = 3
COOLDOWN_DAYS = 7


@router.post("/items/{item_id}/exclude")
def adult_exclude_item(item_id: int, req: AdultExcludeReq):
    """
    标记/取消"排除" —— 排除后所有自动流程都跳过此条，避免循环刮削被对方 ban。
    取消排除时同时 reset scrape_attempts，下一轮自动流程会重新尝试。
    """
    with SessionLocal() as db:
        item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
        if not item:
            raise HTTPException(status_code=404, detail="条目不存在")
        item.excluded = bool(req.excluded)
        if not req.excluded:
            # 用户主动取消排除 —— 同时清空自动 cooldown 让自动流程立即重试
            item.scrape_attempts = 0
            item.cooldown_until = None
        db.commit()
        db.refresh(item)
        return _to_dict_with_jellyfin(item)


@router.post("/items/{item_id}/clear-and-exclude")
def adult_clear_and_exclude(item_id: int):
    """清除条目所有元数据（含 code）+ 标 excluded。只保留 file_path。

    用于刮削错乱、或 code 本身识别错的场景。本地 poster / nfo 一并删。
    """
    from pathlib import Path as _P
    with SessionLocal() as db:
        item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
        if not item:
            raise HTTPException(status_code=404, detail="条目不存在")

        deleted_files = []
        # 删本地 poster
        if item.poster_path:
            try:
                p = _P(item.poster_path)
                if p.exists():
                    p.unlink()
                    deleted_files.append(str(p))
            except Exception as e:
                logger.warning(f"清除 poster 文件失败 {item.poster_path}: {e}")
        # 删本地 nfo
        if item.nfo_path:
            try:
                p = _P(item.nfo_path)
                if p.exists():
                    p.unlink()
                    deleted_files.append(str(p))
            except Exception as e:
                logger.warning(f"清除 nfo 文件失败 {item.nfo_path}: {e}")

        old_code = item.code
        # 清空所有元数据（含 code），只保留 file_path
        item.code = None
        item.title = None
        item.release_date = None
        item.studio = None
        item.director = None
        item.rating = None
        item.cover_url = None
        item.poster_path = None
        item.nfo_path = None
        item.actors = None
        item.tags = None
        item.source = None
        # 标记排除 + 重置失败计数（避免重新启用排除时立刻又进 cooldown）
        item.excluded = True
        item.scrape_attempts = 0
        item.cooldown_until = None
        db.commit()
        db.refresh(item)

        logger.info(
            f"clear-and-exclude: id={item_id} old_code={old_code!r} → cleared "
            f"deleted_files={len(deleted_files)}"
        )
        return {
            "ok": True,
            "deleted_files": deleted_files,
            "item": _to_dict_with_jellyfin(item),
        }


class AdultIdentifySearchReq(BaseModel):
    code: str


class AdultIdentifyApplyReq(BaseModel):
    code: str
    title: Optional[str] = None
    release_date: Optional[str] = None
    studio: Optional[str] = None
    director: Optional[str] = None
    actors: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    cover_url: Optional[str] = None
    rating: Optional[float] = None
    source: str


@router.post("/items/{item_id}/identify-search")
def adult_identify_search(
    item_id: int,
    req: AdultIdentifySearchReq,
):
    """
    用给定 番号 在每个启用的源跑一次 scrape()，返回所有命中的候选（每源最多 1 条）。
    **不持有 DB 连接跨 HTTP** —— 仅用一次短事务校验 item 存在；6 源 scrape 期间 db 已 release。
    """
    code = (req.code or '').strip()
    if not code:
        raise HTTPException(status_code=400, detail="请输入番号")

    # 短事务：仅校验 item 存在；之后释放 db
    with SessionLocal() as db:
        item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
        if not item:
            raise HTTPException(status_code=404, detail="条目不存在")

    from tools.adult_manager.scrapers.manager import ScraperManager
    manager = ScraperManager(
        delay=ADULT_SCRAPER_DELAY,
        sources=settings.adult_sources,
    )
    if not manager.scrapers:
        raise HTTPException(status_code=400, detail="没有启用任何刮削源")

    # 单源候选：每个源命中（title 非空）的都上候选；用户自己挑
    from common.label_cleaner import clean_label_list as _clean_label_list
    per_source_results = {}
    candidates = []
    for scraper in manager.scrapers:
        try:
            result = scraper.scrape(code)
        except Exception as e:
            logger.warning(f"[identify-search] [{scraper.name}] {code} 异常: {e}")
            continue
        if not result or not result.title:
            continue
        # 清洗 actors / tags 的标点 / 空白 / 重复（与合并路径一致）
        result.actors = _clean_label_list(result.actors)
        result.tags = _clean_label_list(result.tags)
        per_source_results[scraper.name] = result
        d = result.to_dict()
        candidates.append({
            'source': scraper.name,
            'code': code,
            'title': d.get('title'),
            'release_date': d.get('release_date'),
            'studio': d.get('studio'),
            'director': d.get('director'),
            'actors': d.get('actors') or [],
            'tags': d.get('tags') or [],
            'cover_url': d.get('cover_url'),
            'rating': d.get('rating'),
        })

    # 多源合并候选：把多源凑出来的最完整版作为推荐选项放最前
    if len(per_source_results) >= 2:
        merged = manager._merge_by_field_priority(code, per_source_results)
        d = merged.to_dict()
        merged_card = {
            'source': merged.source,  # "merged:javbus,javdb,..."
            'is_merged': True,
            'code': code,
            'title': d.get('title'),
            'release_date': d.get('release_date'),
            'studio': d.get('studio'),
            'director': d.get('director'),
            'actors': d.get('actors') or [],
            'tags': d.get('tags') or [],
            'cover_url': d.get('cover_url'),
            'rating': d.get('rating'),
        }
        candidates.insert(0, merged_card)  # 推到最前作为推荐

    return {'code': code, 'candidates': candidates}


@router.post("/items/{item_id}/identify-apply")
def adult_identify_apply(
    item_id: int,
    req: AdultIdentifyApplyReq,
):
    """
    把对话框里选中的 candidate 写到 AdultItem，重下封面 + 重写 NFO。
    **HTTP / 文件 I/O 期间不持 DB**：
      step1 短事务：读 item.file_path
      step2 不持 db：下封面 + 写 NFO（耗时操作）
      step3 短事务：把 candidate + 新 poster/nfo 路径写回
    """
    from tools.adult_manager.nfo_writer import write_nfo as do_write_nfo

    # ---- step1：取 file_path ----
    with SessionLocal() as db:
        item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
        if not item:
            raise HTTPException(status_code=404, detail="条目不存在")
        file_path = item.file_path

    # ---- step2：不持 db，做慢操作 ----
    new_poster_path = None
    if req.cover_url and file_path:
        try:
            cover_path = _download_cover(req.cover_url, _local_path(file_path))
            if cover_path:
                new_poster_path = str(cover_path)
        except Exception as e:
            logger.warning(f"封面下载失败 {req.code}: {e}")

    new_nfo_path = None
    if file_path:
        try:
            nfo_data = {
                'code': req.code, 'title': req.title,
                'release_date': req.release_date, 'studio': req.studio,
                'director': req.director,
                'actors': req.actors or [], 'tags': req.tags or [],
                'rating': req.rating, 'cover_url': req.cover_url,
                'source': req.source,
            }
            nfo_path = do_write_nfo(_local_path(file_path), nfo_data)
            new_nfo_path = str(nfo_path)
        except Exception as e:
            logger.warning(f"NFO 写入失败 {req.code}: {e}")

    # ---- step3：短事务回写 ----
    with SessionLocal() as db:
        item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
        if not item:
            raise HTTPException(status_code=404, detail="条目不存在（应用时已被删除）")
        item.code = req.code
        item.title = req.title
        item.release_date = req.release_date
        item.studio = req.studio
        item.director = req.director
        item.actors = json.dumps(req.actors or [], ensure_ascii=False)
        item.tags = json.dumps(req.tags or [], ensure_ascii=False)
        item.cover_url = req.cover_url
        item.rating = req.rating
        item.source = req.source
        if new_poster_path:
            item.poster_path = new_poster_path
        if new_nfo_path:
            item.nfo_path = new_nfo_path
        # 用户手动选了 candidate 应用 → 视为有效命中，reset attempts + 清自动 cooldown + 取消可能的自动排除
        item.scrape_attempts = 0
        item.excluded = False
        item.cooldown_until = None
        item.last_scrape_at = datetime.utcnow()
        db.commit()
        db.refresh(item)
        return _to_dict_with_jellyfin(item)


@router.post("/items/{item_id}/rescrape")
def rescrape_one_item(item_id: int):
    """
    单条「重新识别」—— 同步执行（不走任务系统）。
    **HTTP / 文件 I/O 期间不持 DB**（短事务读 → HTTP → 短事务写）。
    """
    from tools.adult_manager.scrapers.manager import ScraperManager
    from tools.adult_manager.nfo_writer import write_nfo as do_write_nfo

    # step1 短事务：读 code / file_path
    with SessionLocal() as db:
        item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
        if not item:
            raise HTTPException(status_code=404, detail="条目不存在")
        if not item.code:
            raise HTTPException(
                status_code=422,
                detail="该条目未识别番号，请使用「指定番号」功能手动绑定后再刮削",
            )
        code = item.code
        file_path = item.file_path

    manager = ScraperManager(
        delay=ADULT_SCRAPER_DELAY,
        sources=settings.adult_sources,
    )
    if not manager.scrapers:
        raise HTTPException(status_code=400, detail="没有启用任何刮削源")

    # step2 慢操作：不持 db
    try:
        result = manager.scrape(code)
    except Exception as e:
        logger.exception(f"刮削异常 {code}")
        raise HTTPException(status_code=502, detail=f"刮削异常: {e}")
    if not result:
        raise HTTPException(status_code=404, detail=f"所有源都没找到 {code}")

    d = result.to_dict()

    new_poster_path = None
    if d.get('cover_url') and file_path:
        try:
            cover_path = _download_cover(d['cover_url'], _local_path(file_path))
            if cover_path:
                new_poster_path = str(cover_path)
        except Exception as e:
            logger.warning(f"封面下载失败 {code}: {e}")

    new_nfo_path = None
    if file_path:
        try:
            nfo_path = do_write_nfo(_local_path(file_path), d)
            new_nfo_path = str(nfo_path)
        except Exception as e:
            logger.warning(f"NFO 写入失败 {code}: {e}")

    # step3 短事务：写回
    with SessionLocal() as db:
        item = db.query(AdultItem).filter(AdultItem.id == item_id).first()
        if not item:
            raise HTTPException(status_code=404, detail="条目不存在（应用时已被删除）")
        item.title = d.get('title')
        item.release_date = d.get('release_date')
        item.studio = d.get('studio')
        item.director = d.get('director')
        item.actors = json.dumps(d.get('actors') or [], ensure_ascii=False)
        item.tags = json.dumps(d.get('tags') or [], ensure_ascii=False)
        item.cover_url = d.get('cover_url')
        item.rating = d.get('rating')
        item.source = d.get('source')
        if new_poster_path:
            item.poster_path = new_poster_path
        if new_nfo_path:
            item.nfo_path = new_nfo_path
        # 成功 → reset 计数器，清自动 cooldown
        item.scrape_attempts = 0
        item.excluded = False
        item.cooldown_until = None
        item.last_scrape_at = datetime.utcnow()
        db.commit()
        db.refresh(item)
        return _to_dict_with_jellyfin(item)


@router.post("/repair/covers")
def repair_covers(
    background_tasks: BackgroundTasks,
    library_id: Optional[str] = None,
    dry_run: bool = False,
    db: Session = Depends(get_db),
):
    """
    扫描并修复封面图：
      条件：item.code IS NOT NULL 且 (poster_path 不存在 OR poster 文件实际不存在)
      仅对那些有 cover_url 的条目重新下载（cover_url 空的就跳过 —— 那是没刮过元数据）。

    dry_run=True 时：只算 candidates 数返回，不创建任务、不实际下载。
    """
    query = db.query(AdultItem).filter(AdultItem.code != None)  # noqa: E711
    if library_id:
        cond = _library_path_filter(library_id)
        if cond is None:
            raise HTTPException(status_code=400, detail="无法获取库路径")
        query = query.filter(cond)

    candidates = []
    for it in query.all():
        if not it.cover_url:
            continue  # 没刮过元数据，没 cover_url，跳过
        # 已记录 poster_path 且文件存在 → OK
        if it.poster_path and _local_path(it.poster_path).exists():
            continue
        candidates.append(it.id)

    if not candidates:
        # 没东西可修不算错误：返回 200 + count=0，让前端区分"启动了"和"没活儿干"
        return {"task_id": None, "status": "noop", "count": 0, "dry_run": dry_run}

    if dry_run:
        return {"task_id": None, "status": "dry_run", "count": len(candidates), "dry_run": True}

    task = create_task(db, "adult_repair_covers", f"修复封面 {len(candidates)} 条")
    background_tasks.add_task(_run_repair_covers, task.id, candidates)
    return {"task_id": task.id, "status": "started", "count": len(candidates), "dry_run": False}


@cancellable_task
def _run_repair_covers(task_id: int, item_ids: List[int]):
    """重新下载封面到本地（仅刷新 poster_path，不动其它元数据）。"""
    from web.backend.database import SessionLocal
    from web.backend.shutdown import is_shutting_down
    total = len(item_ids)
    success = failed = 0
    details = []

    for idx, item_id in enumerate(item_ids):
        if is_shutting_down():
            logger.info(f"收到 shutdown 信号，封面修复提前退出（已处理 {idx}/{total}）")
            with SessionLocal() as db:
                complete_task(db, task_id, {
                    "total": total, "success": success, "failed": failed,
                    "stopped_by_shutdown": True,
                    "details": details,
                }, success=False, final_message=f"已处理 {idx}/{total}，进程关闭信号触发提前退出")
            return

        # 短事务读
        with SessionLocal() as db:
            it = db.query(AdultItem).filter(AdultItem.id == item_id).first()
            if not it:
                continue
            code = it.code
            cover_url = it.cover_url
            file_path = it.file_path

        with SessionLocal() as db:
            update_task_progress(
                db, task_id, 5 + int(90 * (idx + 1) / total),
                f"[{idx+1}/{total}] {code}",
                result_patch={"total": total, "success": success, "failed": failed,
                              "details": details[-200:]},
            )

        if not cover_url or not file_path:
            failed += 1
            details.append({"code": code, "status": "skipped", "reason": "缺 cover_url 或 file_path"})
            continue

        try:
            cover_path = _download_cover(cover_url, _local_path(file_path))
            if cover_path:
                with SessionLocal() as db:
                    it = db.query(AdultItem).filter(AdultItem.id == item_id).first()
                    if it:
                        from web.backend.path_translator import reverse_translate_path_with_settings
                        it.poster_path = reverse_translate_path_with_settings(str(cover_path)) or str(cover_path)
                        db.commit()
                success += 1
                details.append({"code": code, "status": "success"})
            else:
                failed += 1
                details.append({"code": code, "status": "failed", "error": "下载返回空"})
        except Exception as e:
            logger.exception(f"修复封面失败 {code}")
            failed += 1
            details.append({"code": code, "status": "failed", "error": str(e)[:120]})

    with SessionLocal() as db:
        complete_task(db, task_id, {
            "total": total, "success": success, "failed": failed,
            "details": details,
        })


@router.post("/repair/metadata")
def repair_metadata(
    background_tasks: BackgroundTasks,
    library_id: Optional[str] = None,
    dry_run: bool = False,
    db: Session = Depends(get_db),
):
    """
    扫描并修复识别/刮削错误：
      包括所有 (recognized 但 source IN (NULL, 'pending', 'not_found')) 即未刮 / 刮失败的条目。
      未识别（code IS NULL）的不处理 —— 那需要用户手动指定番号。

    dry_run=True 时：只算 candidates 数返回，不创建任务、不实际刮削。
    """
    query = db.query(AdultItem).filter(AdultItem.code != None)  # noqa: E711
    if library_id:
        cond = _library_path_filter(library_id)
        if cond is None:
            raise HTTPException(status_code=400, detail="无法获取库路径")
        query = query.filter(cond)

    # 未刮成功的捞起来；跳过用户主动 excluded（永久）和自动 cooldown 未到期的
    from sqlalchemy import or_
    query = query.filter(
        AdultItem.excluded == False,  # noqa: E712
        or_(
            AdultItem.cooldown_until == None,  # noqa: E711
            AdultItem.cooldown_until < datetime.utcnow(),
        ),
        or_(
            AdultItem.title == None,  # noqa: E711
            AdultItem.source == None,  # noqa: E711
            AdultItem.source.in_(['pending', 'not_found']),
        ),
    )
    candidates = [i.id for i in query.all()]

    if not candidates:
        return {"task_id": None, "status": "noop", "count": 0, "dry_run": dry_run}

    if dry_run:
        return {"task_id": None, "status": "dry_run", "count": len(candidates), "dry_run": True}

    task = create_task(db, "adult_scrape_batch", f"修复识别 {len(candidates)} 条")
    background_tasks.add_task(run_adult_scrape_batch, task.id, candidates, True, True)
    return {"task_id": task.id, "status": "started", "count": len(candidates), "dry_run": False}


# ============================================================================
# 阶段 E：女优 toolbox 用的接口
# ============================================================================

class ResolveBatchReq(BaseModel):
    names: List[str]


@router.post("/actresses/resolve-batch")
def actress_resolve_batch(
    req: ResolveBatchReq,
    db: Session = Depends(get_db),
):
    """
    批量姓名解析：给 N 个姓名，返回每个姓名命中的女优概要（id/jp_name/zh_name/en_name/avatar_url）。
    匹配规则：jp_name / zh_name / en_name 精确，或 aliases JSON 包含。
    没命中的姓名不出现在结果里。
    """
    from web.backend.database import AdultActress
    out = {}
    if not req.names:
        return {'resolved': out}

    # 一把捞所有 actress（数据规模通常 <1k，比逐条查省）
    rows = db.query(AdultActress).all()
    by_jp = {r.jp_name: r for r in rows if r.jp_name}
    by_zh = {r.zh_name: r for r in rows if r.zh_name}
    by_en = {r.en_name: r for r in rows if r.en_name}

    # aliases 索引
    alias_to_actress = {}
    for r in rows:
        if not r.aliases:
            continue
        try:
            for n in json.loads(r.aliases) or []:
                alias_to_actress[n] = r
        except Exception:
            continue

    for name in req.names:
        n = (name or '').strip()
        if not n:
            continue
        a = by_jp.get(n) or by_zh.get(n) or by_en.get(n) or alias_to_actress.get(n)
        if not a:
            continue
        out[name] = {
            'id': a.id,
            'jp_name': a.jp_name,
            'zh_name': a.zh_name,
            'en_name': a.en_name,
            'avatar_url': a.avatar_url,
        }
    return {'resolved': out}


@router.get("/actresses/{actress_id}/works")
def actress_works(
    actress_id: int,
    db: Session = Depends(get_db),
):
    """
    某女优的所有作品。匹配规则：AdultItem.actors（JSON 字符串数组）包含
    actress 的 jp_name / zh_name / en_name / 任一 alias。
    """
    from web.backend.database import AdultActress
    a = db.query(AdultActress).filter(AdultActress.id == actress_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="女优不存在")

    # 收集所有可能匹配的姓名
    names = set(filter(None, [a.jp_name, a.zh_name, a.en_name]))
    if a.aliases:
        try:
            for n in json.loads(a.aliases) or []:
                if n: names.add(n)
        except Exception:
            pass

    if not names:
        return {
            'actress': {
                'id': a.id, 'jp_name': a.jp_name, 'zh_name': a.zh_name,
                'en_name': a.en_name, 'avatar_url': a.avatar_url,
                'birth_date': a.birth_date, 'debut_date': a.debut_date,
                'age': a.age, 'aliases': [],
            },
            'works': [],
        }

    from sqlalchemy import or_
    cond = or_(*[AdultItem.actors.contains(n) for n in names])
    items = (db.query(AdultItem)
               .filter(AdultItem.code != None, cond)  # noqa: E711
               .order_by(AdultItem.release_date.desc().nullslast())
               .all())

    aliases_list = []
    if a.aliases:
        try:
            aliases_list = list(json.loads(a.aliases) or [])
        except Exception:
            pass

    return {
        'actress': {
            'id': a.id,
            'jp_name': a.jp_name,
            'zh_name': a.zh_name,
            'en_name': a.en_name,
            'avatar_url': a.avatar_url,
            'birth_date': a.birth_date,
            'debut_date': a.debut_date,
            'age': a.age,
            'aliases': aliases_list,
            'source': a.source,
            'javdb_id': a.javdb_id,
        },
        'works': [_to_dict(i) for i in items],
    }
