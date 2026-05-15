"""
MDB List API 客户端。

聚合多家评分（IMDB / Rotten Tomatoes 影评人+观众 / Metacritic / Trakt / Letterboxd
等）一次拿全。免费额度 1000 req/天，登录后在 https://mdblist.com/api 生成 API Key。

主要查询路径：
    by_imdb('tt0111161')                - 按 IMDB ID
    by_tmdb(550, 'movie')               - 按 TMDB ID（必须带 media_type）

响应里有 ratings 数组，本客户端把它解析成扁平的 dict，调用方直接取即可。
"""
import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)


BASE_URL = "https://api.mdblist.com/"

# MDB List 的 source 名 -> 我们 ORM 字段名 + 解析方式
# 2026 改版后实测 ratings[] 各源 value 单位：
#   imdb            value=10分制       (例 8.8)
#   tomatoes        value=百分比        (影评人，0-100)
#   popcorn         value=百分比        (观众；旧名 tomatoesaudience，2026 改版)
#   metacritic      value=百分比
#   trakt           value=百分比，÷10 转 10分制
#   letterboxd      value=5分制（例 4.3），×2 转 10分制方便和 IMDB 比较
_RATING_PARSERS = {
    'imdb':       ('imdb_rating',       'float'),
    'metacritic': ('metacritic',        'int'),
    'tomatoes':   ('rt_critic',         'int'),
    'popcorn':    ('rt_audience',       'int'),
    'trakt':      ('trakt_rating',      'percent_to_ten'),
    'letterboxd': ('letterboxd_rating', 'five_to_ten'),
}


def parse_ratings(data: dict) -> dict:
    """
    把 MDB List 响应中的 ratings[] 拆成扁平字段，便于直接写入 DB。

    返回字段（不全的就缺）：
        title, year, imdb_id, tmdb_id, imdb_rating, imdb_votes, rt_critic,
        rt_audience, metacritic, trakt_rating, letterboxd_rating, aggregate_score
    """
    # 2026 改版后顶层不再有 imdbid/tmdbid，IDs 全部塞 ids 子对象
    ids = data.get('ids') or {}
    out: dict = {
        'title': data.get('title'),
        'year': data.get('year'),
        'imdb_id': ids.get('imdb') or data.get('imdbid'),
        'tmdb_id': ids.get('tmdb') or data.get('tmdbid'),
        # score / score_average 都可能在；优先用顶层 score（与原行为一致）
        'aggregate_score': data.get('score'),
    }

    for r in (data.get('ratings') or []):
        src = r.get('source')
        cfg = _RATING_PARSERS.get(src)
        if not cfg:
            continue
        field, kind = cfg
        v = r.get('value')
        if v is None:
            continue
        try:
            if kind == 'int':
                out[field] = int(v)
            elif kind == 'float':
                out[field] = float(v)
            elif kind == 'percent_to_ten':
                # 百分制 → 10 分制（Trakt）
                out[field] = round(float(v) / 10.0, 1)
            elif kind == 'five_to_ten':
                # 5 分制 → 10 分制（Letterboxd）
                out[field] = round(float(v) * 2.0, 1)
        except (TypeError, ValueError):
            continue

        # IMDB 的 votes 单独一字段
        if src == 'imdb':
            votes = r.get('votes')
            if votes is not None:
                try:
                    out['imdb_votes'] = int(votes)
                except (TypeError, ValueError):
                    pass

    return out


class MDBListClient:
    """MDB List 评分客户端。"""

    def __init__(
        self,
        api_key: str,
        delay: float = 1.0,
        max_retries: int = 2,
        timeout: float = 15.0,
    ):
        if not api_key:
            raise ValueError("MDB List 客户端需要 API Key")
        self.api_key = api_key
        self.delay = delay
        self.max_retries = max_retries
        self.timeout = timeout
        self._last_request_time = 0.0
        self._session = requests.Session()

    def _rate_limit(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request_time = time.time()

    def _request(self, path: str) -> Optional[dict]:
        """
        新版 MDB List 用 path 风格端点（2026 改版后）：
            /tmdb/{movie|show}/{id}
            /imdb/{movie|show}/{imdb_id}
        bare 根 URL 现在只返回网站链接 banner，旧 query-string 风格 (?tm=&m=) 已废弃。

        本方法返回 None 表示请求失败 / 没拿到数据。
        注意：banner 占位响应（无 ratings 字段）也算"没数据"，因为 MDB List 偶尔在错误路径上返回它。
        """
        url = f"{BASE_URL.rstrip('/')}{path}"
        params = {'apikey': self.api_key}
        for attempt in range(self.max_retries + 1):
            self._rate_limit()
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)
            except requests.exceptions.RequestException as e:
                logger.warning(f"MDB List 请求异常: {e}")
                return None

            # 429: 限流；5xx: 服务异常 → 退避重试
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= self.max_retries:
                    logger.error(
                        f"MDB List 失败 HTTP {resp.status_code}（已重试 {attempt} 次）"
                    )
                    return None
                backoff = (attempt + 1) * max(self.delay, 2.0)
                logger.warning(f"MDB List 限流/异常 ({resp.status_code})，{backoff}s 后重试")
                time.sleep(backoff)
                continue

            if resp.status_code == 404:
                # 找不到这个 ID，正常情况，不是错误
                return None

            if resp.status_code != 200:
                logger.error(
                    f"MDB List HTTP {resp.status_code} {path}: {resp.text[:200]}"
                )
                return None

            try:
                data = resp.json()
            except ValueError:
                logger.error(f"MDB List 返回非 JSON {path}: {resp.text[:200]}")
                return None

            # MDB List 错误响应也是 200，但带 error 字段
            if isinstance(data, dict) and data.get('error'):
                logger.warning(f"MDB List 业务错误 {path}: {data.get('error')}")
                return None

            # banner 占位检测：bare 根/错路径会返回只含 website/documentation 等链接的 dict
            # 没有 ratings 也没有 title——这种当成"无数据"处理，避免上游误把它写进缓存
            if isinstance(data, dict) and 'ratings' not in data and 'title' not in data \
                    and 'website' in data:
                logger.warning(f"MDB List 返回 banner 占位（路径错误？） {path}")
                return None

            return data
        return None

    def by_imdb(self, imdb_id: str, media_type: str = 'movie') -> Optional[dict]:
        """
        按 IMDB ID 查询（路径需带 media_type）：/imdb/{movie|show}/{imdb_id}
        """
        if not imdb_id:
            return None
        normalized = 'show' if media_type in ('tv', 'show') else 'movie'
        return self._request(f"/imdb/{normalized}/{imdb_id}")

    def by_tmdb(self, tmdb_id: int, media_type: str) -> Optional[dict]:
        """
        按 TMDB ID 查询：/tmdb/{movie|show}/{id}
        media_type 接受 'movie' / 'tv' / 'show'。
        """
        if not tmdb_id:
            return None
        normalized = 'show' if media_type in ('tv', 'show') else 'movie'
        return self._request(f"/tmdb/{normalized}/{tmdb_id}")
