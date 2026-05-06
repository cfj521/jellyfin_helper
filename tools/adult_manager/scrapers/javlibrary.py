"""
JavLibrary 刮削器

注意：JavLibrary 启用了 Cloudflare 5s 验证盾，纯 requests 经常拿到 503。
失败时建议：
  - 配置 proxy（很多 IP 段被风控）
  - 或者把 Cookie 从浏览器里复制到 self.session.cookies
"""
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from .base import BaseScraper, ScrapeResult

logger = logging.getLogger(__name__)


class JavLibraryScraper(BaseScraper):
    """JavLibrary 刮削器"""

    name = "javlibrary"
    DEFAULT_BASE_URL = "https://www.javlibrary.com/cn"

    def __init__(self, base_url: Optional[str] = None, **kwargs):
        super().__init__(**kwargs)
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip('/')

    def _search(self, code: str) -> Optional[str]:
        """先搜索拿到详情页 URL"""
        search_url = f"{self.base_url}/vl_searchbyid.php"
        resp = self._get(search_url, params={"keyword": code})
        if not resp:
            return None

        # 搜索结果如果只有一个，会直接 302 到详情页
        # 否则页面会列出列表
        if 'video_title' in resp.text:
            # 已经是详情页（单一结果直接跳转）
            return resp.url
        # 列表页：取第一个匹配
        soup = BeautifulSoup(resp.text, 'lxml')
        first = soup.select_one('div.video > a')
        if first and first.get('href'):
            href = first['href']
            if href.startswith('./'):
                href = href[2:]
            return f"{self.base_url}/{href}"
        return None

    def scrape(self, code: str) -> Optional[ScrapeResult]:
        detail_url = self._search(code)
        if not detail_url:
            logger.info(f"[javlibrary] 未搜到 {code}")
            return None

        resp = self._get(detail_url)
        if not resp:
            return None
        return self._parse(code, resp.text)

    def _parse(self, code: str, html: str) -> Optional[ScrapeResult]:
        soup = BeautifulSoup(html, 'lxml')
        title_tag = soup.select_one('#video_title h3 a')
        if not title_tag:
            return None

        result = ScrapeResult(code=code, source=self.name)
        result.title = title_tag.get_text(strip=True)

        # 海报
        cover = soup.select_one('#video_jacket_img')
        if cover and cover.get('src'):
            href = cover['src']
            if href.startswith('//'):
                href = 'https:' + href
            result.cover_url = href

        # 评分
        score = soup.select_one('.score')
        if score:
            m = re.search(r'(\d+\.?\d*)', score.get_text())
            if m:
                try:
                    result.rating = float(m.group(1))
                except ValueError:
                    pass

        # 信息块（每个 div.item 一条）
        info = soup.select_one('#video_info')
        if not info:
            return result

        for item in info.select('.item'):
            label_el = item.select_one('.header')
            if not label_el:
                continue
            label = label_el.get_text(strip=True)
            value_el = item.select_one('td:not(.header), .text, .director, .maker, .label, .director a')

            if '日期' in label or 'Date' in label:
                t = item.get_text(' ', strip=True)
                m = re.search(r'(\d{4}-\d{2}-\d{2})', t)
                if m:
                    result.release_date = m.group(1)
            elif '长度' in label or 'Length' in label:
                t = item.get_text(' ', strip=True)
                m = re.search(r'(\d+)', t)
                if m:
                    result.duration_minutes = int(m.group(1))
            elif '导演' in label or 'Director' in label:
                a = item.select_one('a')
                if a:
                    result.director = a.get_text(strip=True)
            elif '制作' in label or 'Maker' in label or 'Studio' in label:
                a = item.select_one('a')
                if a:
                    result.studio = a.get_text(strip=True)
            elif '类别' in label or 'Genre' in label or 'Category' in label:
                tags = [a.get_text(strip=True) for a in item.select('a')]
                if tags:
                    result.tags = list(dict.fromkeys(tags))
            elif '演员' in label or 'Cast' in label or 'Star' in label:
                actors = [a.get_text(strip=True) for a in item.select('.star a, a')]
                if actors:
                    result.actors = list(dict.fromkeys(actors))

        return result
