"""
媒体元数据实体表读写门面（L3 长缓存）。

设计参考 docs/2026-05-15-media-metadata-store.md
所有 read 方法都会刷新 last_seen_at（LRU 续命）。
upsert 走 ON CONFLICT，原子写入，updated_at 推进。

线程安全：每次操作独立 SessionLocal；调用方不持有任何 db 状态。
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker

from web.backend.config import settings
from web.backend.database import MediaMetadata, engine

# 本地 session 工厂：expire_on_commit=False
# 默认 SessionLocal commit 后所有 ORM 字段会 expire，导致 session 关闭后再访问字段会
# DetachedInstanceError。本 store 的 read 方法在 commit (bump last_seen_at) 后还要把对象
# 返回给调用方在 session 外用，所以必须关掉 expire_on_commit。
_StoreSession = sessionmaker(
    bind=engine, autocommit=False, autoflush=False, expire_on_commit=False,
)

logger = logging.getLogger(__name__)


# 列表卡片直接展示的"公共字段"（read 时合成响应、write 时拍扁到 column）
# 语义约定（2026-05-15）：
#   title    —— **永远是英文标题**，用于种子搜索 / 跨源桥接锚点
#   title_zh —— 中文标题，前端展示优先用它，缺时 fallback 到 title
_PUBLIC_FIELDS = (
    'media_type', 'title', 'title_zh', 'original_title',
    'year', 'release_date', 'poster_url',
)

# 桥接 ID（read/write 时跟 ext 分开处理，对应表里独立列）
_BRIDGE_ID_FIELDS = ('tmdb_id', 'imdb_id', 'anilist_id')


def _is_stale_row(row: MediaMetadata) -> bool:
    """
    updated_at 超过 metadata_refresh_ttl_days → 应触发后台 refresh。
    数据仍可用，只是 SOT 那边可能有新版本了。
    """
    if row is None or row.updated_at is None:
        return False
    ttl = timedelta(days=max(1, settings.metadata_refresh_ttl_days))
    return datetime.utcnow() - row.updated_at > ttl


def _row_to_card_dict(row: MediaMetadata) -> dict:
    """实体表行 → 列表卡片用的精简 dict（不含 ext）。

    title 字段语义：永远是英文（种子搜索锚点）；title_zh 是中文（展示用）。
    前端通常用 title_zh || title 渲染卡片。
    """
    if row is None:
        return {}
    return {
        'source': row.source,
        'source_id': row.source_id,
        'tmdb_id': row.tmdb_id,
        'imdb_id': row.imdb_id,
        'anilist_id': row.anilist_id,
        'media_type': row.media_type,
        'title': row.title,            # 英文
        'title_zh': row.title_zh,      # 中文
        'original_title': row.original_title,
        'year': row.year,
        'release_date': row.release_date,
        'poster_url': row.poster_url,
    }


def _row_to_full_dict(row: MediaMetadata) -> dict:
    """实体表行 → 详情页用的完整 dict（含 ext 字段拍平）。"""
    base = _row_to_card_dict(row)
    if row is None:
        return base
    ext = row.ext or {}
    # ext 直接合到顶层（避免前端嵌套两层访问）
    out = {**base, **ext}
    out['_metadata_updated_at'] = row.updated_at.isoformat() if row.updated_at else None
    out['_metadata_stale'] = _is_stale_row(row)
    return out


# ============================================================
# 读
# ============================================================

def _bump_last_seen(db, keys: Iterable[Tuple[str, str]]) -> None:
    """批量刷新 last_seen_at。keys 是 [(source, source_id), ...]"""
    keys = list(keys)
    if not keys:
        return
    now = datetime.utcnow()
    # 用 OR 拼条件；如果将来 keys 太大可以分批。当前列表页一页 30，没问题
    conds = [
        and_(MediaMetadata.source == s, MediaMetadata.source_id == sid)
        for s, sid in keys
    ]
    if not conds:
        return
    try:
        db.query(MediaMetadata).filter(or_(*conds)).update(
            {MediaMetadata.last_seen_at: now}, synchronize_session=False,
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("bump last_seen_at failed (non-fatal)")


def get_by_source(source: str, source_id: str) -> Optional[MediaMetadata]:
    """精确按 (source, source_id) 查；命中刷 last_seen_at。"""
    if not source or not source_id:
        return None
    source_id = str(source_id)
    db = _StoreSession()
    try:
        row = db.query(MediaMetadata).filter(
            MediaMetadata.source == source,
            MediaMetadata.source_id == source_id,
        ).first()
        if row is not None:
            _bump_last_seen(db, [(source, source_id)])
        return row
    finally:
        db.close()


def get_by_tmdb(tmdb_id: int, media_type: str) -> Optional[MediaMetadata]:
    """
    按 tmdb_id 查 source='tmdb' 的行（媒体类型 movie/tv 是独立 namespace，必须连同 media_type）。
    """
    if not tmdb_id:
        return None
    db = _StoreSession()
    try:
        row = db.query(MediaMetadata).filter(
            MediaMetadata.source == 'tmdb',
            MediaMetadata.source_id == str(tmdb_id),
            MediaMetadata.media_type == media_type,
        ).first()
        if row is not None:
            _bump_last_seen(db, [('tmdb', str(tmdb_id))])
        return row
    finally:
        db.close()


def get_by_imdb(imdb_id: str) -> List[MediaMetadata]:
    """
    imdb_id 反查全部 source 行。同一部片可能 tmdb 和 douban 都有行。
    命中行不 bump last_seen_at（这是反查/桥接路径，不是直接展示路径）。
    """
    if not imdb_id:
        return []
    db = _StoreSession()
    try:
        return db.query(MediaMetadata).filter(MediaMetadata.imdb_id == imdb_id).all()
    finally:
        db.close()


def get_batch(
    keys: Iterable[Tuple[str, str]],
) -> Dict[Tuple[str, str], MediaMetadata]:
    """
    批量按 (source, source_id) 取实体行。list 端点用。
    返回 {(source, source_id): row}，未命中的 key 不在结果里。
    命中的行批量 bump last_seen_at。
    """
    keys = [(s, str(sid)) for s, sid in keys if s and sid]
    if not keys:
        return {}
    # 去重
    seen = set()
    deduped: List[Tuple[str, str]] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            deduped.append(k)

    db = _StoreSession()
    try:
        conds = [
            and_(MediaMetadata.source == s, MediaMetadata.source_id == sid)
            for s, sid in deduped
        ]
        rows = db.query(MediaMetadata).filter(or_(*conds)).all()
        out: Dict[Tuple[str, str], MediaMetadata] = {}
        for r in rows:
            out[(r.source, r.source_id)] = r
        if rows:
            _bump_last_seen(db, [(r.source, r.source_id) for r in rows])
        return out
    finally:
        db.close()


# ============================================================
# 写
# ============================================================

def upsert(
    source: str,
    source_id: str,
    public: Optional[dict] = None,
    ext: Optional[dict] = None,
    bridge_ids: Optional[dict] = None,
) -> None:
    """
    ON CONFLICT (source, source_id) DO UPDATE。

    参数：
      source      —— 'tmdb' / 'anilist' / 'douban'
      source_id   —— 该 source 内的唯一 ID（自动 str 化）
      public      —— 公共列字典，键限制在 _PUBLIC_FIELDS；None/缺键的列不动
      ext         —— JSONB 字段；整体替换（不做深合并）
      bridge_ids  —— {tmdb_id?, imdb_id?, anilist_id?}；None/缺键的不动

    updated_at / last_seen_at 每次都推到 now。
    """
    if not source or not source_id:
        return
    source_id = str(source_id)
    public = public or {}
    bridge_ids = bridge_ids or {}

    now = datetime.utcnow()
    # 组装行值
    row_values = {
        'source': source,
        'source_id': source_id,
        'updated_at': now,
        'last_seen_at': now,
    }
    # 公共列：仅写传入的（其它字段保留原值）
    for k in _PUBLIC_FIELDS:
        if k in public and public[k] is not None:
            row_values[k] = public[k]
    # 桥接 ID
    for k in _BRIDGE_ID_FIELDS:
        if k in bridge_ids and bridge_ids[k] is not None:
            row_values[k] = bridge_ids[k]
    # ext JSONB
    if ext is not None:
        row_values['ext'] = ext

    db = _StoreSession()
    try:
        stmt = pg_insert(MediaMetadata).values(**row_values)
        # 冲突时更新除 (source, source_id) 外的所有传入字段
        update_cols = {k: stmt.excluded[k] for k in row_values
                       if k not in ('source', 'source_id')}
        stmt = stmt.on_conflict_do_update(
            constraint='uq_media_metadata_source_id',
            set_=update_cols,
        )
        db.execute(stmt)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            f"upsert media_metadata failed: source={source} source_id={source_id}"
        )
    finally:
        db.close()


# ============================================================
# 状态查询
# ============================================================

def is_stale(row: MediaMetadata) -> bool:
    """对外暴露的 stale 判定（供 discover 等调用方决定是否 enqueue refresh）。"""
    return _is_stale_row(row)


def needs_refresh(source: str, source_id: str) -> Tuple[bool, Optional[MediaMetadata]]:
    """
    组合查询：(是否需要刷新, 现有行)
      - 不存在 → (True, None)
      - 存在 + fresh → (False, row)
      - 存在 + stale → (True, row)
    """
    row = get_by_source(source, source_id)
    if row is None:
        return True, None
    return _is_stale_row(row), row


# ============================================================
# 展示助手（read 路径用，外部调用方拼响应）
# ============================================================

def row_to_card_dict(row: MediaMetadata) -> dict:
    return _row_to_card_dict(row)


def row_to_full_dict(row: MediaMetadata) -> dict:
    return _row_to_full_dict(row)
