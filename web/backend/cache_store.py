"""
通用 DB 级 kv 缓存 helper —— 替代项目里散落的多个 in-memory dict + TTL 模式。

为啥不放内存：单 worker 部署也会因为 uvicorn --reload / 后端重启把内存缓存全清。
DB 持久化让 7 天 TTL 真的能撑 7 天，重启不丢、跨 worker 共享。

性能：一次 SELECT/INSERT ~5-15ms，对秒级响应的"库统计 / 剧集树 / TMDB 推荐"路径无感。
表结构：见 database.KvCache。

用法：
    from web.backend.cache_store import get_cached, set_cached, invalidate

    # 读
    cached = get_cached('lib_stats', f'{lib_id}|videos,size', ttl_seconds=7*86400)
    if cached:
        return cached  # 已含 _cached / _cached_at / _cache_age_seconds

    # 写
    set_cached('lib_stats', f'{lib_id}|videos,size', data_dict)

    # 失效
    invalidate('lib_stats', f'{lib_id}|*')   # 模糊（前缀匹配）
    invalidate('lib_stats')                   # 整个 scope 清空
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import and_

from web.backend.database import KvCache, SessionLocal

logger = logging.getLogger(__name__)


def get_cached(
    scope: str, key: str, ttl_seconds: int,
    allow_stale: bool = False,
) -> Optional[Dict[str, Any]]:
    """读缓存。
    命中且未过期 → 返回 dict（附带 _cached / _cached_at / _cache_age_seconds 三个元字段）
    未命中 → None
    过期：allow_stale=False（默认）返回 None；allow_stale=True 返回过期数据并附 _cache_stale=True
    """
    if not scope or not key:
        return None
    try:
        with SessionLocal() as db:
            row = (
                db.query(KvCache)
                .filter(KvCache.scope == scope, KvCache.key == key)
                .first()
            )
            if not row:
                return None
            age = datetime.utcnow() - row.cached_at
            is_stale = age > timedelta(seconds=max(0, ttl_seconds))
            if is_stale and not allow_stale:
                return None
            try:
                data = json.loads(row.data)
            except Exception as e:
                logger.warning(f"kv_cache scope={scope} key={key[:80]} JSON 反序列化失败: {e}")
                return None
            if not isinstance(data, dict):
                return data
            data['_cached'] = True
            data['_cached_at'] = row.cached_at.isoformat()
            data['_cache_age_seconds'] = int(age.total_seconds())
            if is_stale:
                data['_cache_stale'] = True
            return data
    except Exception as e:
        logger.warning(f"kv_cache get 异常 scope={scope} key={key[:80]}: {e}")
        return None


def set_cached(scope: str, key: str, data: Any) -> bool:
    """写缓存（upsert）。失败仅 warning 不抛。"""
    if not scope or not key:
        return False
    try:
        # 序列化前剥掉调用方可能误塞的元字段，避免循环写
        if isinstance(data, dict):
            payload = {k: v for k, v in data.items() if not k.startswith('_cached') and k != '_cache_age_seconds'}
        else:
            payload = data
        body = json.dumps(payload, ensure_ascii=False, default=_json_default)
        with SessionLocal() as db:
            row = (
                db.query(KvCache)
                .filter(KvCache.scope == scope, KvCache.key == key)
                .first()
            )
            if row:
                row.data = body
                row.cached_at = datetime.utcnow()
            else:
                db.add(KvCache(scope=scope, key=key, data=body, cached_at=datetime.utcnow()))
            db.commit()
            return True
    except Exception as e:
        logger.warning(f"kv_cache set 异常 scope={scope} key={key[:80]}: {e}")
        return False


def invalidate(scope: str, key_prefix: Optional[str] = None) -> int:
    """删除缓存。
    key_prefix=None → 删整个 scope
    key_prefix='abc'  → 删 scope 下 key=abc 或 key 以 'abc|' 开头的所有项
    返回删除条数。
    """
    if not scope:
        return 0
    try:
        with SessionLocal() as db:
            q = db.query(KvCache).filter(KvCache.scope == scope)
            if key_prefix:
                # 精确等于 OR 以 prefix| 开头（match _stats_cache_key 的拼接习惯）
                from sqlalchemy import or_
                q = q.filter(or_(
                    KvCache.key == key_prefix,
                    KvCache.key.like(key_prefix + '|%'),
                ))
            n = q.delete(synchronize_session=False)
            db.commit()
            return int(n or 0)
    except Exception as e:
        logger.warning(f"kv_cache invalidate 异常 scope={scope}: {e}")
        return 0


def _json_default(o):
    """让 datetime 等非 JSON 原生类型也能序列化。"""
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")
