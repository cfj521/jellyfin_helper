"""
Jackett API 客户端
https://github.com/Jackett/Jackett/wiki/Web-API
"""
import logging
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


# Jackett 标准分类（Newznab 分类号）
JACKETT_CATEGORIES = {
    "movie": [2000, 2010, 2020, 2030, 2040, 2050, 2060, 2070, 2080],  # Movies/*
    "tv": [5000, 5020, 5030, 5040, 5045, 5050, 5060, 5070, 5080],     # TV/*
    "anime": [5070],
    "all": [],
}


class JackettClient:
    """Jackett 索引器聚合搜索客户端。"""

    def __init__(self, host: str, api_key: str, timeout: int = 60):
        self.host = host.rstrip('/')
        self.api_key = api_key
        self.timeout = timeout

    def _get(self, path: str, params: Optional[Dict] = None) -> Optional[Dict]:
        url = f"{self.host}{path}"
        params = dict(params or {})
        params['apikey'] = self.api_key
        try:
            r = requests.get(url, params=params, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Jackett 请求失败: {url} - {e}")
            return None

    def test_connection(self) -> bool:
        """测试连接：拉取已配置的 indexer 列表。"""
        data = self._get('/api/v2.0/indexers/all/results/torznab/api', {'t': 'caps'})
        return data is not None

    def search(
        self,
        query: str,
        categories: Optional[List[int]] = None,
        indexers: str = "all",
        limit: int = 50,
    ) -> List[Dict]:
        """
        搜索种子。

        Returns: 简化后的结果列表，每条包含
          title, size, seeders, peers, indexer, magnet, link, category, publish_date
        """
        params: Dict = {'Query': query}
        if categories:
            for c in categories:
                # Jackett 期望多个 cat 用同名 query 参数
                params.setdefault('Category[]', []).append(c)

        # 用 /results 而非 torznab，前者直接返回 JSON，结构更友好
        path = f'/api/v2.0/indexers/{indexers}/results'
        data = self._get(path, params)
        if not data:
            return []

        results = data.get('Results', [])[:limit]
        out = []
        for r in results:
            out.append({
                "title": r.get('Title'),
                "size": r.get('Size'),
                "seeders": r.get('Seeders'),
                "peers": r.get('Peers'),
                "indexer": r.get('Tracker'),
                "magnet": r.get('MagnetUri'),
                "link": r.get('Link'),  # .torrent 直链（部分 indexer）
                "category_desc": r.get('CategoryDesc'),
                "publish_date": r.get('PublishDate'),
                "guid": r.get('Guid'),
                "details": r.get('Details'),
            })
        return out
