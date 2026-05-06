"""
刮削器基类
"""
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


_DEFAULT_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}


@dataclass
class ScrapeResult:
    """统一的刮削结果数据结构"""
    code: str
    title: Optional[str] = None
    original_title: Optional[str] = None
    release_date: Optional[str] = None
    studio: Optional[str] = None
    director: Optional[str] = None
    duration_minutes: Optional[int] = None
    actors: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    cover_url: Optional[str] = None
    rating: Optional[float] = None
    source: str = ""

    def to_dict(self) -> Dict:
        return {
            "code": self.code,
            "title": self.title,
            "original_title": self.original_title,
            "release_date": self.release_date,
            "studio": self.studio,
            "director": self.director,
            "duration_minutes": self.duration_minutes,
            "actors": self.actors,
            "tags": self.tags,
            "cover_url": self.cover_url,
            "rating": self.rating,
            "source": self.source,
        }


class BaseScraper(ABC):
    """刮削器抽象基类"""

    name: str = ""

    def __init__(self, delay: float = 1.0, proxy: Optional[str] = None, timeout: int = 30):
        self.delay = delay
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(_DEFAULT_HEADERS)
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        self._last_req_at = 0.0

    def _rate_limit(self):
        elapsed = time.time() - self._last_req_at
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_req_at = time.time()

    def _get(self, url: str, **kwargs) -> Optional[requests.Response]:
        self._rate_limit()
        try:
            r = self.session.get(url, timeout=self.timeout, **kwargs)
            r.raise_for_status()
            return r
        except requests.exceptions.RequestException as e:
            logger.warning(f"[{self.name}] 请求失败 {url} - {e}")
            return None

    @abstractmethod
    def scrape(self, code: str) -> Optional[ScrapeResult]:
        """根据番号刮削元数据。失败返回 None。"""
        ...
