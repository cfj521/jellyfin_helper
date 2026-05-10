"""
TMDB API 客户端
用于搜索演员并获取图片
"""
import requests
import logging
import time
from typing import Optional, Dict, List, Tuple

logger = logging.getLogger(__name__)


class TMDBClient:
    """TMDB API客户端"""

    BASE_URL = "https://api.themoviedb.org/3"
    IMAGE_BASE_URL = "https://image.tmdb.org/t/p"

    def __init__(
        self,
        api_key: str,
        delay: float = 30.0,
        max_retries: int = 3,
        language: str = "zh-CN",
    ):
        """
        初始化TMDB客户端

        Args:
            api_key: TMDB API密钥 (v3 auth)
            delay: 两次请求之间的最小间隔（秒）。默认 30 偏保守，避免风控
            max_retries: 收到 429/5xx 时的最大重试次数（累加退避）
            language: 默认显示语言（zh-CN / en-US 等），所有请求自动注入；
                      调用方在 params 里传 language 可临时覆盖
        """
        self.api_key = api_key
        self.delay = delay
        self.max_retries = max_retries
        self.language = language
        self.last_request_time = 0.0

    def _rate_limit(self):
        """限流控制"""
        elapsed = time.time() - self.last_request_time
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self.last_request_time = time.time()

    def _request(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """发送API请求；遇到 429 / 5xx 自动按 Retry-After 或累加退避重试"""
        url = f"{self.BASE_URL}{endpoint}"
        default_params = {'api_key': self.api_key}
        # 自动注入语言（调用方传 language 可覆盖）
        if self.language:
            default_params['language'] = self.language
        if params:
            default_params.update(params)

        for attempt in range(self.max_retries + 1):
            self._rate_limit()
            try:
                response = requests.get(url, params=default_params, timeout=30)
            except requests.exceptions.RequestException as e:
                logger.error(f"TMDB请求异常: {endpoint} - {e}")
                return None

            # 命中 429 或 5xx：退避后重试
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self.max_retries:
                    logger.error(
                        f"TMDB请求失败（已重试 {attempt} 次）: {endpoint} HTTP {response.status_code}"
                    )
                    return None
                # 优先用 Retry-After 头，否则做累加退避
                retry_after = response.headers.get('Retry-After')
                if retry_after and retry_after.isdigit():
                    backoff = int(retry_after)
                else:
                    # 退避：(attempt+1) × max(delay, 5)，给 TMDB 一点恢复时间
                    # 当 delay 设得很小时不会被牵连到几乎不退避
                    backoff = int((attempt + 1) * max(self.delay, 5.0))
                logger.warning(
                    f"TMDB限流/服务异常 ({response.status_code})，{backoff}s 后重试 "
                    f"({attempt+1}/{self.max_retries}): {endpoint}"
                )
                time.sleep(backoff)
                continue

            try:
                response.raise_for_status()
                return response.json()
            except requests.exceptions.HTTPError as e:
                logger.error(f"TMDB请求失败: {endpoint} - {e}")
                return None
        return None

    def test_connection(self) -> bool:
        """测试API连接"""
        result = self._request('/configuration')
        if result:
            logger.info("TMDB API连接成功")
            return True
        return False

    def search_person(self, name: str) -> Optional[Dict]:
        """
        搜索演员

        Args:
            name: 演员名称

        Returns:
            匹配的演员信息，如果找不到返回None
        """
        # 先用原始名称搜索
        result = self._request('/search/person', {'query': name})

        if result and result.get('results'):
            # 返回第一个匹配结果
            return result['results'][0]

        # 如果是中文名，尝试搜索失败，记录日志
        if any('\u4e00' <= char <= '\u9fff' for char in name):
            logger.debug(f"中文演员 '{name}' 在TMDB中未找到")

        return None

    def get_person_by_id(self, tmdb_id: str) -> Optional[Dict]:
        """
        通过TMDB ID获取演员信息

        Args:
            tmdb_id: TMDB演员ID

        Returns:
            演员详细信息
        """
        return self._request(f'/person/{tmdb_id}')

    def get_person_image_url(self, profile_path: str, size: str = "h632") -> str:
        """
        获取演员图片完整URL

        Args:
            profile_path: 图片路径（从搜索结果获取）
            size: 图片尺寸，可选值: w45, w185, h632, original

        Returns:
            完整的图片URL
        """
        return f"{self.IMAGE_BASE_URL}/{size}{profile_path}"

    def download_image(self, profile_path: str, size: str = "h632") -> Optional[bytes]:
        """
        下载演员图片

        Args:
            profile_path: 图片路径
            size: 图片尺寸

        Returns:
            图片二进制数据
        """
        if not profile_path:
            return None

        url = self.get_person_image_url(profile_path, size)
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return response.content
        except requests.exceptions.RequestException as e:
            logger.error(f"图片下载失败: {url} - {e}")
            return None

    # ---------- 热门内容 ----------

    def trending(self, media_type: str = 'all', time_window: str = 'week',
                 page: int = 1, region: Optional[str] = None) -> List[Dict]:
        """
        获取热门内容。

        media_type: all | movie | tv | person
        time_window: day | week
        page: 分页（TMDB 一页 20 条）
        """
        params = {'page': page}
        if region:
            params['region'] = region
        data = self._request(f'/trending/{media_type}/{time_window}', params)
        return data.get('results', []) if data else []

    def popular_movies(self, page: int = 1, region: Optional[str] = None) -> List[Dict]:
        params = {'page': page}
        if region:
            params['region'] = region
        data = self._request('/movie/popular', params)
        return data.get('results', []) if data else []

    def popular_tv(self, page: int = 1) -> List[Dict]:
        data = self._request('/tv/popular', {'page': page})
        return data.get('results', []) if data else []

    # 各类别端点统一映射（与 TMDB 官网导航一致）
    _CATEGORY_ENDPOINTS = {
        'movie': {
            'popular':      '/movie/popular',
            'now_playing':  '/movie/now_playing',
            'upcoming':     '/movie/upcoming',
            'top_rated':    '/movie/top_rated',
        },
        'tv': {
            'popular':      '/tv/popular',
            'airing_today': '/tv/airing_today',
            'on_the_air':   '/tv/on_the_air',
            'top_rated':    '/tv/top_rated',
        },
    }

    def list_items(self, media_type: str, category: str, page: int = 1, region: Optional[str] = None) -> List[Dict]:
        """
        统一的列表查询：
          media_type ∈ {movie, tv}
          category   ∈ {popular, now_playing, upcoming, top_rated, airing_today, on_the_air}
        """
        endpoint = self._CATEGORY_ENDPOINTS.get(media_type, {}).get(category)
        if not endpoint:
            logger.warning(f"未知分类: {media_type}/{category}")
            return []
        params: Dict = {'page': page}
        if region:
            params['region'] = region
        data = self._request(endpoint, params)
        return data.get('results', []) if data else []

    # ---------- 影视搜索与海报 ----------

    def search_movie(self, title: str, year: Optional[int] = None) -> Optional[Dict]:
        """搜索电影。year 可选用于消歧。"""
        params = {'query': title, 'include_adult': 'false'}
        if year:
            params['year'] = year
        result = self._request('/search/movie', params)
        if result and result.get('results'):
            return result['results'][0]
        return None

    def search_tv(self, title: str, year: Optional[int] = None) -> Optional[Dict]:
        """搜索剧集。year 对应 first_air_date_year。"""
        params = {'query': title, 'include_adult': 'false'}
        if year:
            params['first_air_date_year'] = year
        result = self._request('/search/tv', params)
        if result and result.get('results'):
            return result['results'][0]
        return None

    def get_movie_poster_path(self, tmdb_id: int) -> Optional[str]:
        """获取电影海报路径"""
        data = self._request(f'/movie/{tmdb_id}')
        return data.get('poster_path') if data else None

    def get_tv_poster_path(self, tmdb_id: int) -> Optional[str]:
        """获取剧集海报路径"""
        data = self._request(f'/tv/{tmdb_id}')
        return data.get('poster_path') if data else None

    def get_detail(
        self,
        media_type: str,
        tmdb_id: int,
        language: str = "zh-CN",
        append: str = "credits,videos,recommendations,similar,external_ids,translations",
    ) -> Optional[Dict]:
        """
        获取电影 / 剧集详情，一次性带回演员表、视频、相似推荐等关联数据。

        Args:
            media_type: 'movie' 或 'tv'
            tmdb_id: TMDB ID
            language: 主语言（zh-CN 优先返回中文标题/简介）
            append: 用 TMDB 的 append_to_response 一次性拉关联数据
        """
        if media_type not in ('movie', 'tv'):
            return None
        data = self._request(
            f'/{media_type}/{tmdb_id}',
            {
                'language': language,
                'append_to_response': append,
            },
        )
        return data

    def get_image_url(self, image_path: str, size: str = "original") -> str:
        """通用图片 URL 拼接（海报/背景图/演员图都用同一套 CDN）。"""
        return f"{self.IMAGE_BASE_URL}/{size}{image_path}"

    def download_poster(self, image_path: str, size: str = "original") -> Optional[bytes]:
        """下载海报或背景图原始数据。"""
        if not image_path:
            return None
        url = self.get_image_url(image_path, size)
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return response.content
        except requests.exceptions.RequestException as e:
            logger.error(f"海报下载失败: {url} - {e}")
            return None

    # ---------- Episode 缩略图 ----------

    def get_episode_still_path(
        self,
        tv_id: int,
        season_number: int,
        episode_number: int,
    ) -> Optional[str]:
        """
        取某集的 still_path（剧集场景图，对应 Jellyfin Episode 的 Primary 缩略图）。

        端点：/tv/{tv_id}/season/{season}/episode/{episode}
        """
        if not (tv_id and season_number is not None and episode_number is not None):
            return None
        data = self._request(
            f'/tv/{tv_id}/season/{season_number}/episode/{episode_number}'
        )
        return data.get('still_path') if data else None

    def download_episode_still(
        self,
        still_path: str,
        size: str = "original",
    ) -> Optional[bytes]:
        """下载 still 图。语义上和 download_image 一样，但用 16:9 尺寸更合适。"""
        if not still_path:
            return None
        url = self.get_image_url(still_path, size)
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return response.content
        except requests.exceptions.RequestException as e:
            logger.error(f"Episode still 下载失败: {url} - {e}")
            return None

    def find_person_image(self, name: str, tmdb_id: Optional[str] = None) -> Tuple[Optional[bytes], Optional[str]]:
        """
        查找并下载演员图片

        Args:
            name: 演员名称
            tmdb_id: TMDB ID（如果已知）

        Returns:
            (图片数据, TMDB ID) 元组
        """
        person = None

        # 如果有TMDB ID，直接获取
        if tmdb_id:
            person = self.get_person_by_id(tmdb_id)

        # 否则通过名称搜索
        if not person:
            person = self.search_person(name)

        if not person:
            logger.debug(f"未找到演员: {name}")
            return None, None

        profile_path = person.get('profile_path')
        if not profile_path:
            logger.debug(f"演员 '{name}' 没有图片")
            return None, str(person.get('id'))

        image_data = self.download_image(profile_path)
        return image_data, str(person.get('id'))
