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

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_, tuple_

from web.backend.database import get_db, MediaRating, SessionLocal
from web.backend.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


# ============================================================
# 客户端工厂（按需创建，settings 改了重启即可）
# ============================================================

def _make_mdblist_client():
    if not settings.mdblist_enabled or not settings.mdblist_api_key:
        return None
    try:
        from common.mdblist_client import MDBListClient
        return MDBListClient(
            api_key=settings.mdblist_api_key,
            delay=settings.mdblist_request_delay,
        )
    except Exception as e:
        logger.warning(f"初始化 MDB List 客户端失败: {e}")
        return None


def _make_douban_client():
    if not settings.douban_enabled:
        return None
    try:
        from common.douban_client import DoubanClient
        return DoubanClient(
            user_agent=settings.douban_user_agent,
            delay=settings.douban_request_delay,
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


def _to_response(
    rating: Optional[MediaRating],
    *,
    tmdb_id: int,
    media_type: str,
) -> RatingResponse:
    """ORM → Pydantic，附带缓存状态字段。"""
    base = {
        "tmdb_id": rating.tmdb_id if rating else tmdb_id,
        "media_type": rating.media_type if rating else media_type,
        "mdblist_status": _mdblist_status(rating),
        "douban_status": _douban_status(rating),
    }
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
    tmdb_id: int,
    media_type: str,
    imdb_id: Optional[str] = None,
) -> Optional[MediaRating]:
    """
    同步从 MDB List 取数据并写入 DB。失败返回 None；成功返回更新后的 MediaRating。

    优先按 imdb_id 查（覆盖更全），没传 imdb_id 才走 tmdb_id+media_type。
    """
    client = _make_mdblist_client()
    if client is None:
        return None

    try:
        if imdb_id:
            data = client.by_imdb(imdb_id)
        else:
            data = client.by_tmdb(tmdb_id, media_type)
    except Exception as e:
        logger.warning(f"MDB List 查询异常 tmdb={tmdb_id}: {e}")
        return None

    if not data:
        return None

    from common.mdblist_client import parse_ratings
    parsed = parse_ratings(data)

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

    def enqueue(self, tmdb_id: int, media_type: str, title: str, year: Optional[int]):
        if not settings.douban_enabled:
            return
        key = (tmdb_id, media_type)
        with self._lock:
            if key in self._enqueued:
                return
            self._enqueued.add(key)
            self._queue.put((tmdb_id, media_type, title, year))
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
        # 客户端在 worker 内创建一次：保留 session 复用，且严格按 delay 限速
        client = _make_douban_client()
        if client is None:
            return

        while True:
            try:
                tmdb_id, media_type, title, year = self._queue.get(timeout=10.0)
            except Empty:
                # 队列空了 10 秒就退出，下次有任务再启线程
                logger.debug("豆瓣 worker 空闲退出")
                return

            try:
                self._process_one(client, tmdb_id, media_type, title, year)
            except Exception:
                logger.exception(
                    f"豆瓣处理异常 tmdb={tmdb_id} ({media_type}) {title}"
                )
            finally:
                with self._lock:
                    self._enqueued.discard((tmdb_id, media_type))

    def _process_one(self, client, tmdb_id, media_type, title, year):
        if not title:
            logger.debug(f"跳过豆瓣（无标题）tmdb={tmdb_id}")
            return
        douban_id, dr = client.fetch_by_name(title, year)

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


_douban_worker = _DoubanWorker()


def queue_douban_fetch(tmdb_id: int, media_type: str, title: str, year: Optional[int]):
    """对外暴露的入队函数。"""
    _douban_worker.enqueue(tmdb_id, media_type, title, year)


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

    # 豆瓣：缓存外排队。hint 优先：调用方传的 title 比 DB 里被 MDB List 覆盖的英文版本更利于豆瓣命中
    if _douban_status(rating) != "fresh":
        douban_title = title or (rating.title if rating else None)
        douban_year = year or (rating.year if rating else None)
        if douban_title:
            queue_douban_fetch(tmdb_id, media_type, douban_title, douban_year)

    return _to_response(rating, tmdb_id=tmdb_id, media_type=media_type)


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

    # 提取 (tmdb_id, media_type) 列表并去重
    keys: List[tuple] = []
    seen = set()
    for it in items:
        try:
            tid = int(it.get('tmdb_id'))
            mt = it.get('media_type', 'movie')
            if mt not in ('movie', 'tv'):
                continue
            k = (tid, mt)
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
        if _douban_status(rating) != "fresh" and title:
            queue_douban_fetch(tmdb_id, media_type, title, year)

    responses = [
        _to_response(by_key.get(k), tmdb_id=k[0], media_type=k[1]) for k in keys
    ]
    return BatchResponse(total=len(responses), ratings=responses)


@router.post("/{tmdb_id}/refresh", response_model=RatingResponse)
def refresh_rating(
    tmdb_id: int,
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

    rating = _fetch_mdblist_sync(db, tmdb_id, media_type, imdb_id=imdb_id)
    if rating is None:
        # 没现成行也没 MDB List 数据：手工建一行占位，便于豆瓣写入
        rating = (
            db.query(MediaRating)
            .filter(MediaRating.tmdb_id == tmdb_id, MediaRating.media_type == media_type)
            .first()
        )

    if rating is not None:
        queue_douban_fetch(tmdb_id, media_type, rating.title, rating.year)

    return _to_response(rating, tmdb_id=tmdb_id, media_type=media_type)


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

    def enqueue(self, tmdb_id: int, media_type: str, imdb_id: Optional[str]):
        if not (settings.mdblist_enabled and settings.mdblist_api_key):
            return
        key = (tmdb_id, media_type)
        with self._lock:
            if key in self._enqueued:
                return
            self._enqueued.add(key)
            self._queue.put((tmdb_id, media_type, imdb_id))
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
                tmdb_id, media_type, imdb_id = self._queue.get(timeout=10.0)
            except Empty:
                return
            db = SessionLocal()
            try:
                _fetch_mdblist_sync(db, tmdb_id, media_type, imdb_id=imdb_id)
            except Exception:
                logger.exception(f"MDB List 后台处理异常 tmdb={tmdb_id}")
            finally:
                db.close()
                with self._lock:
                    self._enqueued.discard((tmdb_id, media_type))


_mdblist_worker = _MDBListWorker()


def _enqueue_mdblist_fetch(tmdb_id: int, media_type: str, imdb_id: Optional[str]):
    _mdblist_worker.enqueue(tmdb_id, media_type, imdb_id)
