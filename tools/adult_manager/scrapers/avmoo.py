"""
AvMoo 刮削器

AvMoo 是 JavBus 的克隆站，HTML 结构几乎一致，所以解析复用 JavBus。
仅需要换 BASE_URL 和走"搜索 → 详情"的路径（AvMoo 的 URL 用内部 slug 不是直接番号）。
"""
import logging
from typing import Optional

from bs4 import BeautifulSoup

from .base import BaseScraper, ScrapeResult
from .javbus import JavBusScraper

logger = logging.getLogger(__name__)


class AvMooScraper(JavBusScraper):
    """AvMoo 刮削器（继承 JavBus，仅换 base url + 增加搜索一跳）"""

    name = "avmoo"
    DEFAULT_BASE_URL = "https://avmoo.cyou/cn"

    def __init__(self, base_url: Optional[str] = None, **kwargs):
        # 跳过 JavBusScraper 的 __init__（避免它读自身常量）
        BaseScraper.__init__(self, **kwargs)
        self.BASE_URL = (base_url or self.DEFAULT_BASE_URL).rstrip('/')

    def _search(self, code: str) -> Optional[str]:
        """AvMoo 详情页 URL 用 slug，需要先搜索"""
        url = f"{self.BASE_URL}/search/{code}"
        resp = self._get(url)
        if not resp:
            return None
        soup = BeautifulSoup(resp.text, 'lxml')
        # 第一个搜索结果
        first = soup.select_one('.movie-box, a.movie-box')
        if first:
            href = first.get('href') or ''
            if href.startswith('http'):
                return href
            if href.startswith('/'):
                # 拼成完整 URL
                from urllib.parse import urlparse
                parsed = urlparse(self.BASE_URL)
                return f"{parsed.scheme}://{parsed.netloc}{href}"
        return None

    def scrape(self, code: str) -> Optional[ScrapeResult]:
        detail_url = self._search(code)
        if not detail_url:
            # 兜底：直接尝试 /{code}
            detail_url = f"{self.BASE_URL}/{code}"

        resp = self._get(detail_url)
        if not resp:
            return None
        return self._parse(code, resp.text)
