"""
评分聚合 API。

数据来源：MDB List（IMDB / RT / Metacritic / Trakt / Letterboxd）+ 豆瓣（HTML 爬）
缓存策略：每家独立 TTL（mdblist_cache_ttl_days / douban_cache_ttl_days，默认各 30 天）

读取路径：
    GET  /api/ratings?tmdb_id=550&media_type=movie
        - 缓存内 → 直接返回
        - 缓存外 → 同步取 MDB List，异步排队豆瓣（豆瓣 5s/req 不能让用户等）
    POST /api/ratings/batch  body: {items: [{tmdb_id, media_type}, ...]}
        - 仅返回缓存命中部分；缺失/过期的异步排队，下次访问时已有
    POST /api/ratings/{tmdb_id}/refresh?media_type=movie
        - 强制重取（管理用）

豆瓣懒拉取：用单线程 worker 串行处理队列，避免反爬。
"""
import json
import logging
import threading
from datetime import datetime, timedelta
from queue import Queue, Empty
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_, tuple_

from backend.database import get_db, MediaRating, SessionLocal
from backend.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


# ============================================================
# 客户端工厂（按需创建，settings 改了重启即可）
# ============================================================

def _make_mdblist_client(batch: bool = False):
    """构造 MDB List 客户端；batch=True 时启用 batch 配额（20/min, 300/h, 1000/d）。"""
    if not settings.mdblist_enabled or not settings.mdblist_api_key:
        return None
    try:
        from common.mdblist_client import MDBListClient
        from common.rate_limiter import MDBLIST_DELAY
        return MDBListClient(
            api_key=settings.mdblist_api_key,
            delay=MDBLIST_DELAY,
            batch=batch,
        )
    except Exception as e:
        logger.warning(f"初始化 MDB List 客户端失败: {e}")
        return None


def _make_douban_client(batch: bool = False):
    """构造豆瓣客户端；batch=True 时启用 batch 配额（10/min, 300/h, 2000/d）。"""
    if not settings.douban_enabled:
        return None
    try:
        from common.douban_client import DoubanClient
        from common.rate_limiter import DOUBAN_DELAY
        return DoubanClient(
            user_agent=settings.douban_user_agent,
            delay=DOUBAN_DELAY,
            batch=batch,
        )
    except Exception as e:
        logger.warning(f"初始化豆瓣客户端失败: {e}")
        return None


# ============================================================
# Pydantic Schema
# ============================================================

class RatingResponse(BaseModel):
    """单条评分响应。NULL 字段表示该源没拿到或还没拉过。"""
    tmdb_id: int
    media_type: str
    imdb_id: Optional[str] = None
    douban_id: Optional[str] = None
    title: Optional[str] = None
    year: Optional[int] = None

    imdb_rating: Optional[float] = None
    imdb_votes: Optional[int] = None
    rt_critic: Optional[int] = None
    rt_audience: Optional[int] = None
    metacritic: Optional[int] = None
    trakt_rating: Optional[float] = None
    letterboxd_rating: Optional[float] = None
    douban_rating: Optional[float] = None
    douban_votes: Optional[int] = None
    aggregate_score: Optional[int] = None
    # TMDB 自家评分（从 media_metadata.ext.vote_average 反查；非 MDB List 数据，单独字段暴露）
    tmdb_rating: Optional[float] = None
    tmdb_vote_count: Optional[int] = None
    # 请求里携带的 douban_id（echo，便于前端按豆瓣 ID 反查到这条 rating）
    request_douban_id: Optional[str] = None

    mdblist_fetched_at: Optional[datetime] = None
    douban_fetched_at: Optional[datetime] = None
    # 缓存状态：fresh / stale / missing；用 stale 提示前端 UI 这条数据可能在更新
    mdblist_status: str = "missing"
    douban_status: str = "missing"

    class Config:
        from_attributes = True


class BatchRequest(BaseModel):
    items: List[dict]  # [{tmdb_id, media_type, imdb_id?}]


class BatchResponse(BaseModel):
    total: int
    ratings: List[RatingResponse]


# ============================================================
# 缓存判断辅助
# ============================================================

def _ttl(days: int) -> timedelta:
    return timedelta(days=days)


def _mdblist_status(rating: Optional[MediaRating]) -> str:
    if rating is None or rating.mdblist_fetched_at is None:
        return "missing"
    if datetime.utcnow() - rating.mdblist_fetched_at > _ttl(settings.mdblist_cache_ttl_days):
        return "stale"
    return "fresh"


def _douban_status(rating: Optional[MediaRating]) -> str:
    if rating is None or rating.douban_fetched_at is None:
        return "missing"
    if datetime.utcnow() - rating.douban_fetched_at > _ttl(settings.douban_cache_ttl_days):
        return "stale"
    return "fresh"


def _fetch_tmdb_rating_map(keys: List[tuple]) -> dict:
    """
    批量从 media_metadata.ext 取 TMDB 评分。返回 {(tmdb_id, media_type): (tmdb_rating, tmdb_vote_count)}。
    keys 是 [(tmdb_id, media_type), ...]。
    """
    if not keys:
        return {}
    from backend.services.metadata_store import get_batch
    rows = get_batch([('tmdb', str(tid)) for tid, _ in keys])
    # rows 是 {(source, source_id): MediaMetadata}
    by_tmdb: dict = {}
    for (source, sid), row in rows.items():
        ext = row.ext or {}
        v = ext.get('vote_average')
        c = ext.get('vote_count')
        if v is None:
            continue
        try:
            by_tmdb[(int(sid), row.media_type or '')] = (
                float(v),
                int(c) if c is not None else None,
            )
        except (TypeError, ValueError):
            continue
    return by_tmdb


def _to_response(
    rating: Optional[MediaRating],
    *,
    tmdb_id: int,
    media_type: str,
    tmdb_rating_pair: Optional[tuple] = None,
) -> RatingResponse:
    """ORM → Pydantic，附带缓存状态字段 + TMDB 评分（来自 media_metadata）。"""
    base = {
        "tmdb_id": rating.tmdb_id if rating else tmdb_id,
        "media_type": rating.media_type if rating else media_type,
        "mdblist_status": _mdblist_status(rating),
        "douban_status": _douban_status(rating),
    }
    if tmdb_rating_pair is not None:
        base["tmdb_rating"], base["tmdb_vote_count"] = tmdb_rating_pair
    if rating is None:
        return RatingResponse(**base)
    return RatingResponse(
        **base,
        imdb_id=rating.imdb_id,
        douban_id=rating.douban_id,
        title=rating.title,
        year=rating.year,
        imdb_rating=rating.imdb_rating,
        imdb_votes=rating.imdb_votes,
        rt_critic=rating.rt_critic,
        rt_audience=rating.rt_audience,
        metacritic=rating.metacritic,
        trakt_rating=rating.trakt_rating,
        letterboxd_rating=rating.letterboxd_rating,
        douban_rating=rating.douban_rating,
        douban_votes=rating.douban_votes,
        aggregate_score=rating.aggregate_score,
        mdblist_fetched_at=rating.mdblist_fetched_at,
        douban_fetched_at=rating.douban_fetched_at,
    )


# ============================================================
# MDB List 同步取（用户等得起，秒级返回）
# ============================================================

def _fetch_mdblist_sync(
    db: Session,
    tmdb_id: Optional[int],
    media_type: str,
    imdb_id: Optional[str] = None,
) -> Optional[MediaRating]:
    """
    同步从 MDB List 取数据并写入 DB。失败返回 None；成功返回更新后的 MediaRating。

    tmdb_id 可为 None（豆瓣条目桥接路径）—— 此时必须传 imdb_id：
      内部用 imdb_id 拿数据，从响应里的 ids.tmdb 派生 tmdb_id 作为 MediaRating 主键。
    """
    # 这函数被 mdblist 后台 worker 调用 → batch=True
    client = _make_mdblist_client(batch=True)
    if client is None:
        return None

    try:
        if imdb_id:
            data = client.by_imdb(imdb_id, media_type)
        elif tmdb_id:
            data = client.by_tmdb(tmdb_id, media_type)
        else:
            logger.warning("_fetch_mdblist_sync 没收到 tmdb_id 也没 imdb_id，跳过")
            return None
    except Exception as e:
        logger.warning(f"MDB List 查询异常 tmdb={tmdb_id} imdb={imdb_id}: {e}")
        return None

    if not data:
        return None

    from common.mdblist_client import parse_ratings
    parsed = parse_ratings(data)

    # 空响应防误缓存
    rating_fields = (
        'imdb_rating', 'rt_critic', 'rt_audience', 'metacritic',
        'trakt_rating', 'letterboxd_rating', 'aggregate_score',
    )
    has_any_rating = any(parsed.get(f) is not None for f in rating_fields)
    if not has_any_rating:
        logger.info(
            f"MDB List 无评分数据，跳过缓存写入 tmdb={tmdb_id} imdb={imdb_id} "
            f"title={parsed.get('title')!r}"
        )
        return None

    # tmdb_id 为空时，从响应派生（response.ids.tmdb 是真实 tmdb_id）
    if not tmdb_id:
        parsed_tmdb = parsed.get('tmdb_id')
        if not parsed_tmdb:
            logger.warning(
                f"MDB List 响应缺 tmdb_id，无法写 MediaRating（imdb={imdb_id}）"
            )
            return None
        tmdb_id = int(parsed_tmdb)

    # 找到既有行就更新，没有就插入
    rating = (
        db.query(MediaRating)
        .filter(MediaRating.tmdb_id == tmdb_id, MediaRating.media_type == media_type)
        .first()
    )
    if rating is None:
        rating = MediaRating(tmdb_id=tmdb_id, media_type=media_type)
        db.add(rating)

    # title/year/imdb_id 也一并更新（MDB List 有就用它的）
    # 例外：MDB List 几乎只返回英文 title，但豆瓣 worker 拿这个去搜中文/日文条目会失败。
    # 所以"DB 已有 CJK 字符"时优先保留 —— 中文片豆瓣搜索靠这一保护。
    new_title = parsed.get('title')
    if new_title:
        existing = rating.title or ''
        existing_has_cjk = any(
            '一' <= c <= '鿿'  # 中日韩统一表意文字
            or '぀' <= c <= 'ヿ'  # 平假名/片假名
            or '가' <= c <= '힯'  # 韩文
            for c in existing
        )
        if not existing_has_cjk:
            rating.title = new_title
        # 否则 keep existing 中文/日文/韩文 title，避免被英文覆盖
    if parsed.get('year'):
        rating.year = parsed['year']
    if parsed.get('imdb_id'):
        rating.imdb_id = parsed['imdb_id']

    rating.imdb_rating = parsed.get('imdb_rating', rating.imdb_rating)
    rating.imdb_votes = parsed.get('imdb_votes', rating.imdb_votes)
    rating.rt_critic = parsed.get('rt_critic', rating.rt_critic)
    rating.rt_audience = parsed.get('rt_audience', rating.rt_audience)
    rating.metacritic = parsed.get('metacritic', rating.metacritic)
    rating.trakt_rating = parsed.get('trakt_rating', rating.trakt_rating)
    rating.letterboxd_rating = parsed.get('letterboxd_rating', rating.letterboxd_rating)
    rating.aggregate_score = parsed.get('aggregate_score', rating.aggregate_score)

    rating.mdblist_fetched_at = datetime.utcnow()
    rating.raw_mdblist = json.dumps(data, ensure_ascii=False)
    db.commit()
    db.refresh(rating)

    # 顺带把 imdb_id 桥接写入 L3 实体表（不动 title/ext，避免英文覆盖中文）
    # 实体表已存在 tmdb 行时只补 imdb_id；不存在则不插（让 discover detail 路径来填）
    try:
        if rating.imdb_id:
            from backend.services import metadata_store
            existing = metadata_store.get_by_tmdb(tmdb_id, media_type)
            if existing is not None and not existing.imdb_id:
                metadata_store.upsert(
                    source='tmdb', source_id=str(tmdb_id),
                    bridge_ids={'imdb_id': rating.imdb_id},
                )
    except Exception:
        logger.exception(f"L3 imdb_id 桥接写入失败 tmdb={tmdb_id}")

    return rating


# ============================================================
# 豆瓣懒拉取队列（单线程后台 worker）
# ============================================================

class _DoubanWorker:
    """
    单线程豆瓣爬虫 worker。

    设计要点：
      - 全局唯一线程，串行处理；多个 API 调用塞进同一队列
      - 去重：(tmdb_id, media_type) 在队列里只保留一份
      - 启动惰性：第一次有任务排进来才创建线程
    """

    def __init__(self):
        self._queue: Queue = Queue()
        self._enqueued: set = set()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def enqueue(
        self,
        tmdb_id: int,
        media_type: str,
        title: str,
        year: Optional[int],
        imdb_id: Optional[str] = None,
    ):
        if not settings.douban_enabled:
            return
        key = (tmdb_id, media_type)
        with self._lock:
            if key in self._enqueued:
                return
            self._enqueued.add(key)
            self._queue.put((tmdb_id, media_type, title, year, imdb_id))
            self._ensure_thread()

    def _ensure_thread(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="douban-rating-worker", daemon=True
        )
        self._thread.start()
        logger.info("豆瓣爬虫 worker 已启动")

    def _run(self):
        # 客户端在 worker 内创建一次：保留 session 复用 + batch 配额（worker 算批量）
        client = _make_douban_client(batch=True)
        if client is None:
            return

        while True:
            try:
                tmdb_id, media_type, title, year, imdb_id = self._queue.get(timeout=10.0)
            except Empty:
                # 队列空了 10 秒就退出，下次有任务再启线程
                logger.debug("豆瓣 worker 空闲退出")
                return

            try:
                self._process_one(client, tmdb_id, media_type, title, year, imdb_id)
            except Exception:
                logger.exception(
                    f"豆瓣处理异常 tmdb={tmdb_id} ({media_type}) {title}"
                )
            finally:
                with self._lock:
                    self._enqueued.discard((tmdb_id, media_type))

    def _process_one(self, client, tmdb_id, media_type, title, year, imdb_id):
        # 没 title 也没 imdb_id：完全无法搜索，跳过（且不写 fetched_at，留待下次）
        if not title and not imdb_id:
            logger.debug(f"跳过豆瓣（无标题且无 imdb）tmdb={tmdb_id}")
            return
        # IMDb 优先 + title 退化：欧美电影通常 imdb 一击即中，中文/剧集走 title
        douban_id, dr = client.fetch_by_ids(
            imdb_id=imdb_id, name=title, year=year, media_type=media_type,
        )

        # 空命中防误缓存：search 找不到 douban_id 也没拿到 rating，就当本次失败，
        # 不写 douban_fetched_at，让下次访问能再试。否则 30 天 TTL 把空记录锁死。
        got_anything = bool(douban_id) or (dr is not None and dr.rating is not None)
        if not got_anything:
            # 日志带 imdb_id 才能定位是"真没命中"还是"根本没传 imdb_id"
            logger.info(
                f"豆瓣未命中，跳过缓存写入 imdb={imdb_id or '无'} ({media_type}) title={title!r}"
            )
            return

        # 写 DB
        db = SessionLocal()
        try:
            rating = (
                db.query(MediaRating)
                .filter(
                    MediaRating.tmdb_id == tmdb_id,
                    MediaRating.media_type == media_type,
                )
                .first()
            )
            if rating is None:
                rating = MediaRating(
                    tmdb_id=tmdb_id, media_type=media_type, title=title, year=year
                )
                db.add(rating)

            if douban_id:
                rating.douban_id = douban_id
            if dr is not None:
                if dr.rating is not None:
                    rating.douban_rating = dr.rating
                if dr.votes is not None:
                    rating.douban_votes = dr.votes
                rating.raw_douban = json.dumps(dr.to_dict(), ensure_ascii=False)
            rating.douban_fetched_at = datetime.utcnow()
            db.commit()
            logger.info(
                f"豆瓣更新 tmdb={tmdb_id} {title} → "
                f"id={douban_id} rating={dr.rating if dr else None}"
            )
        finally:
            db.close()

        # 顺带写 L3 实体表：minimal douban 行，imdb_id 桥接给后续跨源复用
        # 注意：完整 douban 字段（summary/cast/poster）由 discover.get_douban_detail 路径
        # 写入；本 worker 只覆盖 title/year/imdb_id 桥接，避免 ext 误清空
        if douban_id:
            try:
                from backend.services import metadata_store
                bridge_ids = {}
                if imdb_id:
                    bridge_ids['imdb_id'] = imdb_id
                metadata_store.upsert(
                    source='douban', source_id=str(douban_id),
                    public={
                        'media_type': media_type if media_type in ('movie', 'tv') else 'movie',
                        'title': title, 'year': year,
                    },
                    bridge_ids=bridge_ids,
                )
            except Exception:
                logger.exception(f"L3 douban 桥接写入失败 douban_id={douban_id}")


_douban_worker = _DoubanWorker()


def queue_douban_fetch(
    tmdb_id: int,
    media_type: str,
    title: str,
    year: Optional[int],
    imdb_id: Optional[str] = None,
):
    """对外暴露的入队函数。imdb_id 可选——传了 worker 会优先用它直命中欧美电影。"""
    _douban_worker.enqueue(tmdb_id, media_type, title, year, imdb_id=imdb_id)


# ============================================================
# refresh 节流（防滥用）
# ============================================================
# 防止前端在 RatingsBadges 上加自动重试逻辑或用户连点导致 MDB List 配额被打满。
# 设计：(ip, tmdb_id, media_type) 三元组 60s 内只允许一次。
# 内存里维护即可——单进程、不持久；过期项随访问惰性清理。

_REFRESH_COOLDOWN_SECONDS = 60
_refresh_last_call: dict = {}  # (ip, tmdb_id, media_type) -> datetime
_refresh_lock = threading.Lock()


def _check_refresh_throttle(ip: str, tmdb_id: int, media_type: str) -> Optional[int]:
    """返回 None 表示放行；返回剩余秒数表示需要拒绝。"""
    key = (ip, tmdb_id, media_type)
    now = datetime.utcnow()
    with _refresh_lock:
        last = _refresh_last_call.get(key)
        if last is not None:
            elapsed = (now - last).total_seconds()
            if elapsed < _REFRESH_COOLDOWN_SECONDS:
                return int(_REFRESH_COOLDOWN_SECONDS - elapsed)
        _refresh_last_call[key] = now
        # 惰性清理过期项（避免无限增长）
        if len(_refresh_last_call) > 256:
            cutoff = now - timedelta(seconds=_REFRESH_COOLDOWN_SECONDS)
            for k in [k for k, v in _refresh_last_call.items() if v < cutoff]:
                _refresh_last_call.pop(k, None)
    return None


# ============================================================
# REST 路由
# ============================================================

@router.get("", response_model=RatingResponse)
def get_rating(
    tmdb_id: int,
    media_type: str = "movie",
    imdb_id: Optional[str] = None,
    title: Optional[str] = None,
    year: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """
    取单条评分。

    流程：
      1. 查 DB
      2. MDB List 不在 fresh 缓存内 → 同步重取
      3. 豆瓣不在 fresh 缓存内 → 排队后台爬（不阻塞响应）
      4. 返回当前 DB 状态 + 各源缓存状态

    title / year（可选）：调用方提供的本地化标题/年份，优先用于豆瓣搜索。
    用途：Detail 页 TMDB 详情拿到的 zh-CN title 比 MDB List 英文 title 更利于豆瓣命中。
    """
    if media_type not in ('movie', 'tv'):
        raise HTTPException(status_code=400, detail="media_type 必须是 movie 或 tv")

    rating = (
        db.query(MediaRating)
        .filter(MediaRating.tmdb_id == tmdb_id, MediaRating.media_type == media_type)
        .first()
    )

    # MDB List：缓存外同步取
    if _mdblist_status(rating) != "fresh":
        new_rating = _fetch_mdblist_sync(db, tmdb_id, media_type, imdb_id=imdb_id)
        if new_rating is not None:
            rating = new_rating

    # 豆瓣：缓存外排队。
    #   - hint 优先：调用方传的 title 比 DB 里被 MDB List 覆盖的英文版本更利于豆瓣命中
    #   - imdb_id：worker 会用它直命中欧美电影（豆瓣对欧美电影 imdb 字段做了文本索引）
    if _douban_status(rating) != "fresh":
        douban_title = title or (rating.title if rating else None)
        douban_year = year or (rating.year if rating else None)
        douban_imdb = imdb_id or (rating.imdb_id if rating else None)
        if douban_title or douban_imdb:
            queue_douban_fetch(
                tmdb_id, media_type, douban_title, douban_year, imdb_id=douban_imdb,
            )

    tmdb_map = _fetch_tmdb_rating_map([(tmdb_id, media_type)])
    return _to_response(
        rating, tmdb_id=tmdb_id, media_type=media_type,
        tmdb_rating_pair=tmdb_map.get((tmdb_id, media_type)),
    )


@router.post("/batch", response_model=BatchResponse)
def get_ratings_batch(
    request: BatchRequest,
    db: Session = Depends(get_db),
):
    """
    批量取评分（用于列表页一次拿多条）。

    与单条不同：MDB List 也走异步（避免一次列表加载触发上百次外部请求阻塞响应）。
    返回当前 DB 状态；缺失/过期的项各家会被排进后台队列，后续访问会逐步变成 fresh。
    """
    items = request.items or []
    if not items:
        return BatchResponse(total=0, ratings=[])

    # 桥接：豆瓣条目（无 tmdb_id 有 douban_id）→ 找到 (tmdb_id, media_type) 走主流程
    from backend.services import metadata_store as _ms
    # 桥接失败的豆瓣 id（用于响应 echo，让前端展开时看到 pending dot 而不是空空）
    failed_douban_ids: List[str] = []
    # 完全没抓过 detail 的 id（用于触发预取建立 douban→imdb_id 映射）
    unmapped_douban_ids: List[str] = []
    for it in items:
        if it.get('tmdb_id'):
            continue
        douban_id = it.get('douban_id')
        if not douban_id:
            continue
        try:
            douban_row = _ms.get_by_source('douban', str(douban_id))
            # 倒灌：如果 media_metadata 没豆瓣行，但 KV 缓存里有 detail（旧路径预取过的）
            # → 立即调 _upsert_douban_detail 把 detail 灌进实体表，免去再次抓豆瓣（反爬严）
            if douban_row is None:
                try:
                    from backend.cache_store import get_cached as _kv_get_local
                    cached_detail = _kv_get_local(
                        'douban_detail', str(douban_id),
                        ttl_seconds=30 * 86400,
                    )
                    if isinstance(cached_detail, dict) and cached_detail.get('summary'):
                        from backend.api.discover import _upsert_douban_detail
                        _upsert_douban_detail(cached_detail)
                        # 若拿到 imdb_id，同时触发 MDB List 抓取
                        if cached_detail.get('imdb_id'):
                            enqueue_mdblist_by_imdb(cached_detail['imdb_id'], 'movie')
                        # 重新查一遍刚 upsert 的行
                        douban_row = _ms.get_by_source('douban', str(douban_id))
                except Exception:
                    logger.exception(f"KV → media_metadata 倒灌失败 douban_id={douban_id}")
            if douban_row is None:
                # 路径 2：还没抓过 detail，触发预取
                unmapped_douban_ids.append(str(douban_id))
                failed_douban_ids.append(str(douban_id))
                continue
            imdb_id = douban_row.imdb_id
            if not imdb_id:
                # detail 已抓但条目本身无 IMDb（中文片常见）→ 桥接不可能，但前端要 echo
                failed_douban_ids.append(str(douban_id))
                continue
            # 路径 1：用 imdb_id 直查 MediaRating（已有 imdb_id 索引）
            rating_by_imdb = db.query(MediaRating).filter(
                MediaRating.imdb_id == imdb_id
            ).first()
            if rating_by_imdb:
                # 桥接成功 → 走主流程
                it['tmdb_id'] = rating_by_imdb.tmdb_id
                it['media_type'] = rating_by_imdb.media_type
                if not it.get('imdb_id'):
                    it['imdb_id'] = imdb_id
                continue
            # 桥接到 imdb 但 MediaRating 还没数据
            it['imdb_id'] = imdb_id
            failed_douban_ids.append(str(douban_id))
        except Exception:
            logger.exception(f"batch douban→tmdb 桥接异常 douban_id={douban_id}")
            failed_douban_ids.append(str(douban_id))

    # 异步建立 douban → imdb 桥接：入队豆瓣 detail 预取
    # 复用 discover.py 的 _kick_douban_prefetch（单 worker + 5s/req 限速 + 去重）
    # worker 抓 detail 后会 _upsert_douban_detail 写入 imdb_id，下次访问该卡片就能桥接
    if unmapped_douban_ids:
        try:
            from backend.api.discover import _kick_douban_prefetch
            _kick_douban_prefetch(unmapped_douban_ids)
        except Exception:
            logger.exception("kick douban prefetch (ratings batch) 失败")

    # 提取 (tmdb_id, media_type) 列表并去重
    keys: List[tuple] = []
    seen = set()
    # 同时维护一个 douban_id → key 的反向映射（用于响应里 echo douban_id）
    douban_to_key: dict = {}
    for it in items:
        try:
            tid = int(it.get('tmdb_id'))
            mt = it.get('media_type', 'movie')
            if mt not in ('movie', 'tv'):
                continue
            k = (tid, mt)
            if it.get('douban_id'):
                douban_to_key[str(it['douban_id'])] = k
            if k in seen:
                continue
            seen.add(k)
            keys.append(k)
        except (TypeError, ValueError):
            continue

    if not keys:
        return BatchResponse(total=0, ratings=[])

    rows = (
        db.query(MediaRating)
        .filter(tuple_(MediaRating.tmdb_id, MediaRating.media_type).in_(keys))
        .all()
    )
    by_key = {(r.tmdb_id, r.media_type): r for r in rows}

    # 反查请求里的 imdb_id 和 title hint（豆瓣需要 title）
    hints: dict = {}
    for it in items:
        try:
            tid = int(it.get('tmdb_id'))
            mt = it.get('media_type', 'movie')
            hints[(tid, mt)] = it
        except (TypeError, ValueError):
            continue

    # 排队缺失/过期项
    for key in keys:
        rating = by_key.get(key)
        hint = hints.get(key, {})
        tmdb_id, media_type = key

        if _mdblist_status(rating) != "fresh":
            # MDB List 也用后台获取（首批批量响应会缺，下次轮询补齐）
            _enqueue_mdblist_fetch(tmdb_id, media_type, imdb_id=hint.get('imdb_id'))

        # 豆瓣：优先用 hint 里的 title/year（前端展示的本地化标题），DB 兜底
        # 不能反着来：MDB List 一刷 DB title 就被英文覆盖，再去搜豆瓣中文条目会查不到
        title = hint.get('title') or (rating.title if rating else None)
        year = hint.get('year') or (rating.year if rating else None)
        # imdb_id：hint 没传就用 DB 里 MDB List 写入的（worker 优先按 imdb 直命中欧美电影）
        douban_imdb = hint.get('imdb_id') or (rating.imdb_id if rating else None)
        if _douban_status(rating) != "fresh" and (title or douban_imdb):
            queue_douban_fetch(tmdb_id, media_type, title, year, imdb_id=douban_imdb)

    tmdb_map = _fetch_tmdb_rating_map(keys)
    # 反向：key → 请求里的 douban_id，用于 echo
    key_to_douban: dict = {}
    for d_id, k in douban_to_key.items():
        key_to_douban.setdefault(k, d_id)
    responses = []
    for k in keys:
        resp = _to_response(
            by_key.get(k), tmdb_id=k[0], media_type=k[1],
            tmdb_rating_pair=tmdb_map.get(k),
        )
        if k in key_to_douban:
            resp.request_douban_id = key_to_douban[k]
        responses.append(resp)

    # 桥接失败的豆瓣条目：返回空 rating + douban_status='missing'，
    # 让前端展开时看到 pending dot 而不是"空空"，避免用户误以为"点了没反应"
    # echo 所有失败 id（不只 unmapped），保证前端 ratingsByKey['douban-xxx'] 总能命中
    for d_id in failed_douban_ids:
        responses.append(RatingResponse(
            tmdb_id=0,                # 占位（前端不按 tmdb 索引）
            media_type='movie',
            request_douban_id=d_id,
            douban_status='missing',
            mdblist_status='missing',
        ))
    return BatchResponse(total=len(responses), ratings=responses)


@router.post("/{tmdb_id}/refresh", response_model=RatingResponse)
def refresh_rating(
    tmdb_id: int,
    request: Request,
    media_type: str = "movie",
    imdb_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """强制重取（绕过 TTL，用于管理或用户主动刷新）。"""
    logger.info(
        f"/ratings/{tmdb_id}/refresh: media_type={media_type!r} imdb_id={imdb_id!r}"
    )
    if media_type not in ('movie', 'tv'):
        raise HTTPException(status_code=400, detail="media_type 必须是 movie 或 tv")

    # 同 IP + 同 (tmdb_id, media_type) 60s 内只允许一次，防 MDB List 配额被打满
    client_ip = request.client.host if request.client else "unknown"
    cooldown = _check_refresh_throttle(client_ip, tmdb_id, media_type)
    if cooldown is not None:
        raise HTTPException(
            status_code=429,
            detail=f"刷新过于频繁，请 {cooldown}s 后再试",
        )

    rating = _fetch_mdblist_sync(db, tmdb_id, media_type, imdb_id=imdb_id)
    if rating is None:
        # 没现成行也没 MDB List 数据：手工建一行占位，便于豆瓣写入
        rating = (
            db.query(MediaRating)
            .filter(MediaRating.tmdb_id == tmdb_id, MediaRating.media_type == media_type)
            .first()
        )

    if rating is not None:
        # 豆瓣：用 DB 里的 imdb_id（_fetch_mdblist_sync 刚写入）直命中欧美电影
        queue_douban_fetch(
            tmdb_id, media_type, rating.title, rating.year,
            imdb_id=rating.imdb_id,
        )

    tmdb_map = _fetch_tmdb_rating_map([(tmdb_id, media_type)])
    return _to_response(
        rating, tmdb_id=tmdb_id, media_type=media_type,
        tmdb_rating_pair=tmdb_map.get((tmdb_id, media_type)),
    )


# ============================================================
# MDB List 异步队列（批量场景用，避免阻塞）
# ============================================================

class _MDBListWorker:
    """单线程 MDB List worker。MDB List 本身快，但批量时也不让用户等。"""

    def __init__(self):
        self._queue: Queue = Queue()
        self._enqueued: set = set()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def enqueue(
        self,
        tmdb_id: Optional[int],
        media_type: str,
        imdb_id: Optional[str],
    ):
        """tmdb_id 可空（豆瓣条目桥接路径，仅有 imdb_id 时）。
        无 tmdb_id 时按 imdb_id 去重，避免反复抓同一 imdb。"""
        if not (settings.mdblist_enabled and settings.mdblist_api_key):
            return
        if not tmdb_id and not imdb_id:
            return
        key = (tmdb_id, media_type) if tmdb_id else ('imdb', imdb_id, media_type)
        with self._lock:
            if key in self._enqueued:
                return
            self._enqueued.add(key)
            self._queue.put((tmdb_id, media_type, imdb_id, key))
            self._ensure_thread()

    def _ensure_thread(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="mdblist-rating-worker", daemon=True
        )
        self._thread.start()

    def _run(self):
        while True:
            try:
                tmdb_id, media_type, imdb_id, dedup_key = self._queue.get(timeout=10.0)
            except Empty:
                return
            db = SessionLocal()
            try:
                _fetch_mdblist_sync(db, tmdb_id, media_type, imdb_id=imdb_id)
            except Exception:
                logger.exception(f"MDB List 后台处理异常 tmdb={tmdb_id} imdb={imdb_id}")
            finally:
                db.close()
                with self._lock:
                    self._enqueued.discard(dedup_key)


_mdblist_worker = _MDBListWorker()


def _enqueue_mdblist_fetch(
    tmdb_id: Optional[int], media_type: str, imdb_id: Optional[str],
):
    _mdblist_worker.enqueue(tmdb_id, media_type, imdb_id)


def enqueue_mdblist_by_imdb(imdb_id: str, media_type: str = 'movie'):
    """对外暴露：仅有 imdb_id 时的入队入口（豆瓣 detail worker 用）。"""
    _mdblist_worker.enqueue(None, media_type, imdb_id)
