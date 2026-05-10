"""
AniList GraphQL 客户端 —— 公开 API，无需 key。

vs TMDB 在 anime 上的优势：
  - 季度划分准（season 字段）；TMDB 把多季度连作放一个 series
  - 原生日文标题（title.native）；TMDB 经常缺
  - 类型 tag 颗粒细（如「邪念」「治愈」「萌系」），用户筛选友好
  - trending 信号是真实社区活动，更新及时

文档：https://docs.anilist.co/
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class AniListItem:
    anilist_id: int
    media_type: str           # 'anime'（本 client 只查 ANIME）
    title_romaji: Optional[str]
    title_english: Optional[str]
    title_native: Optional[str]
    cover_image: Optional[str]
    banner_image: Optional[str]
    description: Optional[str]
    season: Optional[str]     # WINTER / SPRING / SUMMER / FALL
    season_year: Optional[int]
    episodes: Optional[int]
    duration: Optional[int]   # 单集分钟
    format: Optional[str]     # TV / MOVIE / OVA / ONA / SPECIAL ...
    status: Optional[str]     # FINISHED / RELEASING / NOT_YET_RELEASED ...
    average_score: Optional[int]   # 0-100
    popularity: Optional[int]      # 全站收藏人数
    trending: Optional[int]        # 实时热度（仅 trending 排序时有）
    genres: List[str]
    studios: List[str]
    tmdb_id: Optional[int] = None  # 通过 external links 反查（可选）

    def to_dict(self) -> Dict:
        return self.__dict__.copy()


# 列表查询：包含 description（前端"简介"按钮要展示）；继续不拉 bannerImage / externalLinks 减体积
# 详情页用更完整的 query（暂未引入；想看 banner 等需要再加）
_LIST_QUERY = """
query ($page: Int, $perPage: Int, $sort: [MediaSort], $season: MediaSeason, $seasonYear: Int) {
  Page(page: $page, perPage: $perPage) {
    media(type: ANIME, sort: $sort, season: $season, seasonYear: $seasonYear, isAdult: false) {
      id
      title { romaji english native }
      coverImage { large extraLarge }
      description(asHtml: false)
      season
      seasonYear
      episodes
      duration
      format
      status
      averageScore
      popularity
      trending
      genres
    }
  }
}
"""


class AniListClient:
    def __init__(
        self,
        base_url: str = 'https://graphql.anilist.co',
        request_delay: float = 1.0,
        timeout: int = 15,
    ):
        self.base_url = base_url.rstrip('/')
        self.request_delay = max(0.0, request_delay)
        self.timeout = timeout
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._session = requests.Session()
        self._session.headers.update({
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        })

    def _wait(self):
        with self._lock:
            elapsed = time.monotonic() - self._last_call
            if elapsed < self.request_delay:
                time.sleep(self.request_delay - elapsed)
            self._last_call = time.monotonic()

    def _query(self, query: str, variables: Optional[Dict] = None) -> Optional[Dict]:
        self._wait()
        try:
            r = self._session.post(
                self.base_url,
                json={'query': query, 'variables': variables or {}},
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
            if 'errors' in data:
                logger.warning(f"[anilist] GraphQL errors: {data['errors']}")
                return None
            return data.get('data')
        except requests.RequestException as e:
            logger.warning(f"[anilist] 请求失败: {e}")
            return None

    # ---- 业务接口 ----

    def trending(self, page: int = 1, limit: int = 30) -> List[AniListItem]:
        """实时趋势（trending 字段降序）。"""
        data = self._query(_LIST_QUERY, {
            'page': page, 'perPage': min(50, limit),
            'sort': ['TRENDING_DESC', 'POPULARITY_DESC'],
        })
        return self._parse_list(data)

    def popular(self, page: int = 1, limit: int = 30) -> List[AniListItem]:
        """全站收藏量降序（"最受欢迎"）。"""
        data = self._query(_LIST_QUERY, {
            'page': page, 'perPage': min(50, limit),
            'sort': ['POPULARITY_DESC'],
        })
        return self._parse_list(data)

    def top_rated(self, page: int = 1, limit: int = 30) -> List[AniListItem]:
        """评分降序。"""
        data = self._query(_LIST_QUERY, {
            'page': page, 'perPage': min(50, limit),
            'sort': ['SCORE_DESC'],
        })
        return self._parse_list(data)

    def current_season(self, season: str, year: int, page: int = 1, limit: int = 30) -> List[AniListItem]:
        """当季番剧（按 popularity 排序）。
        season: WINTER | SPRING | SUMMER | FALL
        """
        data = self._query(_LIST_QUERY, {
            'page': page, 'perPage': min(50, limit),
            'sort': ['POPULARITY_DESC'],
            'season': season,
            'seasonYear': year,
        })
        return self._parse_list(data)

    # ---- 解析辅助 ----

    @staticmethod
    def _parse_list(data: Optional[Dict]) -> List[AniListItem]:
        if not data:
            return []
        media_list = (data.get('Page') or {}).get('media') or []
        return [AniListClient._parse_item(m) for m in media_list]

    @staticmethod
    def _parse_item(m: Dict) -> AniListItem:
        title = m.get('title') or {}
        cover = m.get('coverImage') or {}
        # 列表场景没有 studios / banner / description / externalLinks（query 里被剔了）
        studios_raw = (m.get('studios') or {}).get('nodes') or []
        ext_links = m.get('externalLinks') or []

        # external links 里有 TMDB 链接的话顺手抓 tmdb_id（用户跳详情用）
        # 列表 query 没拉这字段；详情 query 才有
        tmdb_id = None
        for link in ext_links:
            site = (link.get('site') or '').lower()
            url = link.get('url') or ''
            if site == 'tmdb' and 'tv/' in url:
                try:
                    # 形如 https://www.themoviedb.org/tv/12345
                    tmdb_id = int(url.rstrip('/').split('/')[-1])
                except (ValueError, IndexError):
                    pass

        return AniListItem(
            anilist_id=int(m.get('id') or 0),
            media_type='anime',
            title_romaji=title.get('romaji'),
            title_english=title.get('english'),
            title_native=title.get('native'),
            cover_image=cover.get('extraLarge') or cover.get('large'),
            banner_image=m.get('bannerImage'),
            description=_strip_html(m.get('description')),
            season=m.get('season'),
            season_year=m.get('seasonYear'),
            episodes=m.get('episodes'),
            duration=m.get('duration'),
            format=m.get('format'),
            status=m.get('status'),
            average_score=m.get('averageScore'),
            popularity=m.get('popularity'),
            trending=m.get('trending'),
            genres=list(m.get('genres') or []),
            studios=[(s.get('name') or '') for s in studios_raw if s.get('name')],
            tmdb_id=tmdb_id,
        )


def _strip_html(s: Optional[str]) -> Optional[str]:
    """AniList description 含 <i><br> 等简易 HTML，前端不渲染就剥掉。"""
    if not s:
        return s
    import re
    s = re.sub(r'<br\s*/?>', '\n', s)
    s = re.sub(r'<[^>]+>', '', s)
    return s.strip()
