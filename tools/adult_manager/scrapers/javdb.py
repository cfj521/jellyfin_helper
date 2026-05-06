"""
JavDB 刮削器
"""
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from .base import BaseScraper, ScrapeResult

logger = logging.getLogger(__name__)


class JavDBScraper(BaseScraper):
    """JavDB 刮削器（搜索 → 详情）"""

    name = "javdb"
    DEFAULT_BASE_URL = "https://javdb.com"

    def __init__(self, base_url: Optional[str] = None, **kwargs):
        super().__init__(**kwargs)
        self.BASE_URL = (base_url or self.DEFAULT_BASE_URL).rstrip('/')

    def scrape(self, code: str) -> Optional[ScrapeResult]:
        # 1. 搜索
        search_url = f"{self.BASE_URL}/search"
        resp = self._get(search_url, params={'q': code, 'f': 'all'})
        if not resp:
            return None

        soup = BeautifulSoup(resp.text, 'lxml')
        first = soup.select_one('.movie-list .item a')
        if not first:
            logger.info(f"[javdb] 未搜到 {code}")
            return None
        detail_path = first.get('href')
        if not detail_path:
            return None

        # 2. 详情
        detail_url = self.BASE_URL + detail_path
        resp2 = self._get(detail_url)
        if not resp2:
            return None
        return self._parse(code, resp2.text)

    def _parse(self, code: str, html: str) -> Optional[ScrapeResult]:
        soup = BeautifulSoup(html, 'lxml')

        title_tag = soup.select_one('h2.title strong.current-title')
        if not title_tag:
            return None

        result = ScrapeResult(code=code, source=self.name)
        result.title = title_tag.get_text(strip=True)

        original = soup.select_one('h2.title span.origin-title')
        if original:
            result.original_title = original.get_text(strip=True)

        # 封面
        cover = soup.select_one('.column-video-cover img, .video-cover img')
        if cover and cover.get('src'):
            result.cover_url = cover['src']

        # 评分
        rating_tag = soup.select_one('.score .value')
        if rating_tag:
            m = re.search(r'(\d+\.?\d*)', rating_tag.get_text())
            if m:
                try:
                    result.rating = float(m.group(1))
                except ValueError:
                    pass

        # info panel：每个 .panel-block 是一个属性
        for block in soup.select('.movie-panel-info .panel-block'):
            label = block.select_one('strong')
            value = block.select_one('.value')
            if not label or not value:
                continue
            key = label.get_text(' ', strip=True).lower()
            text = value.get_text(' ', strip=True)

            if 'released' in key or '日期' in key or '日期' in label.get_text():
                m = re.search(r'\d{4}-\d{2}-\d{2}', text)
                if m:
                    result.release_date = m.group(0)
            elif 'duration' in key or '時長' in key or '时长' in key:
                m = re.search(r'(\d+)', text)
                if m:
                    result.duration_minutes = int(m.group(1))
            elif 'director' in key or '導演' in key or '导演' in key:
                result.director = text
            elif 'maker' in key or 'studio' in key or '製作' in key or '制作' in key:
                result.studio = text
            elif 'tags' in key or '類別' in key or '类别' in key:
                result.tags = [a.get_text(strip=True) for a in value.select('a') if a.get_text(strip=True)]
            elif 'actor' in key or '演員' in key or '演员' in key:
                result.actors = [a.get_text(strip=True) for a in value.select('a') if a.get_text(strip=True)]

        return result
