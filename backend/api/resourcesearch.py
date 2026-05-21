"""
资源搜索 API（拆自 discover.py）
- 通过 Jackett 搜索种子
"""
import sys
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from backend.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


class SearchRequest(BaseModel):
    query: str
    category: str = "all"  # all | movie | tv | anime
    indexers: str = "all"
    limit: int = 50


@router.post("/search")
def search_jackett(request: SearchRequest):
    """通过 Jackett 搜索种子。"""
    if not settings.jackett_api_key:
        raise HTTPException(status_code=400, detail="未配置 Jackett API Key")

    from common.jackett_client import JackettClient, JACKETT_CATEGORIES
    client = JackettClient(settings.jackett_host, settings.jackett_api_key)

    cats = JACKETT_CATEGORIES.get(request.category, [])
    try:
        results = client.search(
            query=request.query,
            categories=cats or None,
            indexers=request.indexers,
            limit=request.limit,
        )
        return {"count": len(results), "results": results}
    except Exception as e:
        logger.exception("Jackett 搜索失败")
        raise HTTPException(status_code=500, detail=f"搜索失败: {e}")
