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

from common.rate_limiter import quota_guard

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


# 详情查询：单条 anilist_id 一次性拿足够撑详情页的数据
_DETAIL_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    idMal
    title { romaji english native }
    coverImage { large extraLarge }
    bannerImage
    description(asHtml: false)
    season seasonYear
    startDate { year month day }
    endDate   { year month day }
    episodes duration format status source countryOfOrigin
    averageScore meanScore popularity favourites
    genres
    tags { name rank }
    studios { nodes { name } }
    trailer { id site }
    externalLinks { site url type }
    characters(perPage: 12, sort: ROLE) {
      edges {
        role
        node {
          id
          name { full }
          image { large }
        }
        voiceActors(language: JAPANESE) {
          name { full }
          image { large }
          language
        }
      }
    }
    relations {
      edges {
        relationType
        node {
          id
          type
          format
          title { romaji english native }
          coverImage { large }
        }
      }
    }
    recommendations(perPage: 12, sort: RATING_DESC) {
      nodes {
        mediaRecommendation {
          id
          title { romaji english native }
          coverImage { large }
          averageScore
        }
      }
    }
  }
}
"""


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
        timeout: int = 30,
        batch: bool = False,
    ):
        self.base_url = base_url.rstrip('/')
        self.request_delay = max(0.0, request_delay)
        self.timeout = timeout
        self.batch = batch
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
        # 保底层：先检查全局暂停 / 批量配额（anilist 当前未配 batch 配额，仅暂停保护）
        quota_guard.acquire('anilist', batch=self.batch)
        self._wait()
        try:
            r = self._session.post(
                self.base_url,
                json={'query': query, 'variables': variables or {}},
                timeout=self.timeout,
            )
            if r.status_code == 429:
                quota_guard.report_limited('anilist', 'HTTP 429')
                logger.warning("[anilist] 限流 (429)")
                return None
            r.raise_for_status()
            data = r.json()
            if 'errors' in data:
                # AniList 有时在 errors 中返回限流信息
                for err in data['errors']:
                    if err.get('status') == 429:
                        quota_guard.report_limited('anilist', 'GraphQL 429')
                        return None
                logger.warning(f"[anilist] GraphQL errors: {data['errors']}")
                return None
            quota_guard.report_success('anilist')
            return data.get('data')
        except requests.RequestException as e:
            logger.warning(f"[anilist] 请求失败: {e}")
            return None

    # ---- 详情查询 ----

    def detail(self, anilist_id: int) -> Optional[Dict]:
        """
        单条番剧详情。前端详情页用。
        返回 dict 比 _parse_item 更全：banner / description / studios / 声优 / characters /
        relations / external_links / trailer 等。
        """
        if not anilist_id:
            return None
        data = self._query(_DETAIL_QUERY, {'id': int(anilist_id)})
        if not data:
            return None
        m = (data.get('Media') or {})
        if not m:
            return None
        return self._parse_detail(m)

    @staticmethod
    def _parse_detail(m: Dict) -> Dict:
        """把 AniList Media 详情对象简化成前端需要的字段。"""
        title = m.get('title') or {}
        cover = m.get('coverImage') or {}
        start = m.get('startDate') or {}
        end = m.get('endDate') or {}

        cast = []
        for edge in (m.get('characters') or {}).get('edges') or []:
            ch = edge.get('node') or {}
            ch_name = (ch.get('name') or {}).get('full') or ''
            ch_image = (ch.get('image') or {}).get('large')
            # 主声优（取第一位日语 voice actor）
            va_name, va_image = None, None
            for va in edge.get('voiceActors') or []:
                if (va.get('language') or '').upper() == 'JAPANESE':
                    va_name = (va.get('name') or {}).get('full')
                    va_image = (va.get('image') or {}).get('large')
                    break
            cast.append({
                'character': ch_name,
                'image': ch_image,
                'voice_actor': va_name,
                'voice_actor_image': va_image,
            })

        # 关联作品（前/后传 / 衍生 / 改编）
        relations = []
        for edge in (m.get('relations') or {}).get('edges') or []:
            node = edge.get('node') or {}
            t = node.get('title') or {}
            c = node.get('coverImage') or {}
            relations.append({
                'anilist_id': node.get('id'),
                'relation': edge.get('relationType'),
                'title': t.get('english') or t.get('romaji') or t.get('native'),
                'media_type': (node.get('type') or '').lower() or 'anime',
                'format': node.get('format'),
                'cover_image': c.get('large'),
            })

        # 推荐
        recommendations = []
        for rec in ((m.get('recommendations') or {}).get('nodes') or [])[:12]:
            rm = rec.get('mediaRecommendation') or {}
            if not rm:
                continue
            t = rm.get('title') or {}
            c = rm.get('coverImage') or {}
            recommendations.append({
                'anilist_id': rm.get('id'),
                'title': t.get('english') or t.get('romaji') or t.get('native'),
                'cover_image': c.get('large'),
                'average_score': rm.get('averageScore'),
            })

        # external links → TMDB / MAL / 官网 等；顺手抽 tmdb_id
        ext_links = []
        tmdb_id = None
        mal_id = m.get('idMal')
        for link in m.get('externalLinks') or []:
            site = link.get('site') or ''
            url = link.get('url') or ''
            ext_links.append({'site': site, 'url': url, 'type': link.get('type')})
            if site.lower() == 'tmdb' and 'tv/' in url:
                try:
                    tmdb_id = int(url.rstrip('/').split('/')[-1])
                except (ValueError, IndexError):
                    pass

        trailer = m.get('trailer') or None
        if trailer:
            site = (trailer.get('site') or '').lower()
            trailer_id = trailer.get('id')
            if site == 'youtube' and trailer_id:
                trailer = {
                    'site': 'youtube',
                    'key': trailer_id,
                    'youtube_url': f'https://www.youtube.com/watch?v={trailer_id}',
                    'thumb_url': f'https://img.youtube.com/vi/{trailer_id}/hqdefault.jpg',
                }
            else:
                trailer = None

        # 主标签（前 6 个，按 rank 降序）
        tags = []
        for tag in sorted((m.get('tags') or []), key=lambda t: -(t.get('rank') or 0))[:6]:
            tags.append(tag.get('name'))

        return {
            'anilist_id': int(m.get('id') or 0),
            'mal_id': mal_id,
            'tmdb_id': tmdb_id,
            'media_type': 'anime',
            'title_romaji': title.get('romaji'),
            'title_english': title.get('english'),
            'title_native': title.get('native'),
            'cover_image': cover.get('extraLarge') or cover.get('large'),
            'banner_image': m.get('bannerImage'),
            'description': _strip_html(m.get('description')),
            'season': m.get('season'),
            'season_year': m.get('seasonYear'),
            'start_date': f"{start.get('year')}-{start.get('month') or 1:02d}-{start.get('day') or 1:02d}" if start.get('year') else None,
            'end_date': f"{end.get('year')}-{end.get('month') or 1:02d}-{end.get('day') or 1:02d}" if end.get('year') else None,
            'episodes': m.get('episodes'),
            'duration': m.get('duration'),
            'format': m.get('format'),
            'status': m.get('status'),
            'source': m.get('source'),
            'country_of_origin': m.get('countryOfOrigin'),
            'average_score': m.get('averageScore'),
            'mean_score': m.get('meanScore'),
            'popularity': m.get('popularity'),
            'favourites': m.get('favourites'),
            'genres': list(m.get('genres') or []),
            'tags': tags,
            'studios': [(s.get('name') or '') for s in ((m.get('studios') or {}).get('nodes') or []) if s.get('name')],
            'cast': cast,
            'relations': relations,
            'recommendations': recommendations,
            'external_links': ext_links,
            'trailer': trailer,
        }

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
