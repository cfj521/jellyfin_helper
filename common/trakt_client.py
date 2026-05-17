"""
Trakt API 客户端 —— 公开 endpoint 只用 Client ID（无 OAuth 授权流程）。

注册流程（一次性）：
  1. https://trakt.tv/oauth/applications → New application
  2. Redirect URI 填 urn:ietf:wg:oauth:2.0:oob，权限默认全勾
  3. 保存后顶部 "Client ID" 即 64 位 hex；填到 settings.trakt.client_id

数据特征 vs TMDB：
  - TMDB popular = 元数据访问量加权（间接信号）
  - Trakt trending / watching = 真实用户当下"在看"的活动信号
  - 两者互补；Trakt 对老剧 / 长尾内容覆盖好，对小众片有惊喜
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

from common.rate_limiter import quota_guard

logger = logging.getLogger(__name__)


@dataclass
class TraktItem:
    trakt_id: int
    media_type: str           # 'movie' | 'tv'
    title: str
    year: Optional[int]
    overview: Optional[str]
    tmdb_id: Optional[int]    # 用于跳详情 / cross-ref
    imdb_id: Optional[str]
    runtime: Optional[int]    # 分钟
    watchers: Optional[int]   # trending 指标（同时在看的人数）
    play_count: Optional[int] # popular 指标
    rating: Optional[float]   # Trakt 用户评分 0-10
    poster_url: Optional[str] # 通过 TMDB 拼（Trakt 不直接给图）
    genres: List[str]         # extended=full 时返回；前端展示风格标签

    def to_dict(self) -> Dict:
        return {
            'trakt_id': self.trakt_id,
            'tmdb_id': self.tmdb_id,
            'imdb_id': self.imdb_id,
            'media_type': self.media_type,
            'title': self.title,
            'year': self.year,
            'overview': self.overview,
            'runtime': self.runtime,
            'watchers': self.watchers,
            'play_count': self.play_count,
            'rating': self.rating,
            'poster_url': self.poster_url,
            'genres': self.genres,
        }


class TraktClient:
    def __init__(
        self,
        client_id: str,
        base_url: str = 'https://api.trakt.tv',
        request_delay: float = 1.0,
        timeout: int = 15,
        batch: bool = False,
    ):
        if not client_id:
            raise ValueError("Trakt client_id 不能为空")
        self.client_id = client_id
        self.base_url = base_url.rstrip('/')
        self.request_delay = max(0.0, request_delay)
        self.timeout = timeout
        self.batch = batch
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._session = requests.Session()
        self._session.headers.update({
            'Content-Type': 'application/json',
            'trakt-api-version': '2',
            'trakt-api-key': client_id,
        })

    # ---- 限频 ----

    def _wait(self):
        with self._lock:
            elapsed = time.monotonic() - self._last_call
            if elapsed < self.request_delay:
                time.sleep(self.request_delay - elapsed)
            self._last_call = time.monotonic()

    def _get(self, path: str, params: Optional[Dict] = None) -> Optional[List]:
        # 保底层：先检查全局暂停 / 批量配额（trakt 当前未配 batch 配额）
        quota_guard.acquire('trakt', batch=self.batch)
        self._wait()
        try:
            url = f"{self.base_url}{path}"
            r = self._session.get(url, params=params, timeout=self.timeout)
            if r.status_code == 429:
                quota_guard.report_limited('trakt', f'HTTP 429 {path}')
                logger.warning(f"[trakt] {path} 限流 (429)")
                return None
            r.raise_for_status()
            quota_guard.report_success('trakt')
            return r.json()
        except requests.RequestException as e:
            logger.warning(f"[trakt] {path} 请求失败: {e}")
            return None

    # ---- 业务接口 ----

    def trending(self, media_type: str, page: int = 1, limit: int = 30) -> List[TraktItem]:
        """实时观看趋势：return 中含 watchers（同时在看的人数）。

        media_type: 'movie' | 'tv'（trakt 用 'shows' 表 TV）
        """
        path_seg = 'shows' if media_type == 'tv' else 'movies'
        data = self._get(f'/{path_seg}/trending',
                         params={'page': page, 'limit': limit, 'extended': 'full'})
        if not data:
            return []
        return [self._parse_item(it, media_type) for it in data]

    def popular(self, media_type: str, page: int = 1, limit: int = 30) -> List[TraktItem]:
        """流行（trakt 算法综合 watch + favorite + rating）。
        没有 watchers 字段；这接口直接返回 [movie/show, ...]，外层解构跟 trending 不一样。
        """
        path_seg = 'shows' if media_type == 'tv' else 'movies'
        data = self._get(f'/{path_seg}/popular',
                         params={'page': page, 'limit': limit, 'extended': 'full'})
        if not data:
            return []
        # popular 的元素直接是 movie/show 对象，没有 wrapper
        return [
            self._parse_item({'movie' if media_type == 'movie' else 'show': it}, media_type)
            for it in data
        ]

    def watched(self, media_type: str, period: str = 'weekly', page: int = 1, limit: int = 30) -> List[TraktItem]:
        """周期内累计观看排行。period: daily | weekly | monthly | yearly | all
        指标：play_count（累计播放次数）。
        """
        path_seg = 'shows' if media_type == 'tv' else 'movies'
        data = self._get(f'/{path_seg}/watched/{period}',
                         params={'page': page, 'limit': limit, 'extended': 'full'})
        if not data:
            return []
        return [self._parse_item(it, media_type) for it in data]

    def anticipated(self, media_type: str, page: int = 1, limit: int = 30) -> List[TraktItem]:
        """期待榜：未上映 / 即将上映按"添加到 watchlist"人数排序。
        wrapper 里的指标字段是 list_count（多少人加了 watchlist）。
        """
        path_seg = 'shows' if media_type == 'tv' else 'movies'
        data = self._get(f'/{path_seg}/anticipated',
                         params={'page': page, 'limit': limit, 'extended': 'full'})
        if not data:
            return []
        # anticipated wrapper 是 {list_count, movie/show: {...}}
        out = []
        for it in data:
            parsed = self._parse_item(it, media_type)
            # play_count / watchers 在 anticipated 里没有；用 list_count 顶到 watchers 字段方便前端展示
            if it.get('list_count') is not None:
                parsed.watchers = it.get('list_count')
            out.append(parsed)
        return out

    # ---- 解析辅助 ----

    @staticmethod
    def _parse_item(wrapper: Dict, media_type: str) -> TraktItem:
        """trending / watched / collected 的 wrapper：{watchers/play_count, movie/show: {...}}
        popular 已被外层包成同款形状。
        """
        inner_key = 'show' if media_type == 'tv' else 'movie'
        inner = wrapper.get(inner_key) or {}
        ids = inner.get('ids') or {}
        return TraktItem(
            trakt_id=int(ids.get('trakt') or 0),
            media_type=media_type,
            title=inner.get('title') or '',
            year=inner.get('year'),
            overview=inner.get('overview'),
            tmdb_id=int(ids.get('tmdb')) if ids.get('tmdb') else None,
            imdb_id=ids.get('imdb'),
            runtime=inner.get('runtime'),
            watchers=wrapper.get('watchers'),
            play_count=wrapper.get('play_count'),
            rating=inner.get('rating'),
            poster_url=None,    # Trakt 不给图，前端按 tmdb_id 自行从 TMDB 拼
            genres=list(inner.get('genres') or []),
        )
