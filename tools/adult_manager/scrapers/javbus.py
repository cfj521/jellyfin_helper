"""
JavBus 刮削器
"""
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from .base import BaseScraper, ScrapeResult

logger = logging.getLogger(__name__)


class JavBusScraper(BaseScraper):
    """JavBus 刮削器（解析公开页面 HTML）"""

    name = "javbus"
    DEFAULT_BASE_URL = "https://www.javbus.com"

    def __init__(self, base_url: Optional[str] = None, **kwargs):
        super().__init__(**kwargs)
        self.BASE_URL = (base_url or self.DEFAULT_BASE_URL).rstrip('/')

    def scrape(self, code: str) -> Optional[ScrapeResult]:
        url = f"{self.BASE_URL}/{code}"
        resp = self._get(url)
        if not resp:
            return None
        return self._parse(code, resp.text)

    def _parse(self, code: str, html: str) -> Optional[ScrapeResult]:
        soup = BeautifulSoup(html, 'lxml')

        title_tag = soup.find('h3')
        if not title_tag:
            logger.info(f"[javbus] 未找到 {code} 详情页（标题缺失）")
            return None

        result = ScrapeResult(code=code, source=self.name)
        result.title = title_tag.get_text(strip=True)

        # 封面
        cover = soup.select_one('a.bigImage img')
        if cover and cover.get('src'):
            href = cover['src']
            if href.startswith('//'):
                href = 'https:' + href
            elif href.startswith('/'):
                href = self.BASE_URL + href
            result.cover_url = href

        # 信息字段（散落在 .info 下的 <p>）
        info = soup.select_one('.info')
        if info:
            for p in info.find_all('p'):
                text = p.get_text(' ', strip=True)
                lower = text.lower()

                # 发行日期
                m = re.search(r'(?:发行日期|Release Date)[：:\s]*(\d{4}-\d{2}-\d{2})', text)
                if m:
                    result.release_date = m.group(1)

                # 时长
                m = re.search(r'(?:长度|Length)[：:\s]*(\d+)', text)
                if m:
                    result.duration_minutes = int(m.group(1))

                # 导演
                if '导演' in text or 'Director' in text:
                    a = p.find('a')
                    if a:
                        result.director = a.get_text(strip=True)

                # 制作商
                if '制作商' in text or 'Studio' in text:
                    a = p.find('a')
                    if a:
                        result.studio = a.get_text(strip=True)

            # 类别（tags）
            tags_block = info.find_all('span', class_='genre')
            if tags_block:
                tags = []
                for t in tags_block:
                    a = t.find('a')
                    if a:
                        tags.append(a.get_text(strip=True))
                result.tags = list(dict.fromkeys(tags))  # 去重保序

        # 演员
        actors = []
        for a in soup.select('a.avatar-box span'):
            name = a.get_text(strip=True)
            if name:
                actors.append(name)
        if actors:
            result.actors = list(dict.fromkeys(actors))

        return result
