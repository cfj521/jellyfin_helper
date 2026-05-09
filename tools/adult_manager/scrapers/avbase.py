"""
AvBase 刮削器（avbase.net）

特点：
  - 公开稳定，DMM 图床 cover 更高清
  - 商业番号收录全（SSIS / PFES / STARS / 等）
  - 不收 FC2-PPV（FC2 番号会 404）
  - 用作 javbus/javdb 失败时的兜底备用源
"""
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from .base import BaseScraper, ScrapeResult

logger = logging.getLogger(__name__)


class AvBaseScraper(BaseScraper):
    """AvBase 刮削器（解析公开页面 HTML）"""

    name = "avbase"
    DEFAULT_BASE_URL = "https://www.avbase.net"

    def __init__(self, base_url: Optional[str] = None, **kwargs):
        super().__init__(**kwargs)
        self.BASE_URL = (base_url or self.DEFAULT_BASE_URL).rstrip('/')

    def scrape(self, code: str) -> Optional[ScrapeResult]:
        # 1) 标准番号直链：/works/SSIS-001 这种能直接命中
        direct_url = f"{self.BASE_URL}/works/{code}"
        resp = self._get(direct_url)
        if resp:
            r = self._parse(code, resp.text)
            if r and r.title:
                return r

        # 2) 直链 404 → 走搜索回退（EBOD/MIDV 等老系列详情页是 /works/<maker>:<code>）
        return self._scrape_via_search(code)

    def _scrape_via_search(self, code: str) -> Optional[ScrapeResult]:
        search_url = f"{self.BASE_URL}/works?q={code}"
        resp = self._get(search_url)
        if not resp:
            return None

        soup = BeautifulSoup(resp.text, 'lxml')
        target = self._find_detail_link(soup, code)
        if not target:
            logger.info(f"[{self.name}] 搜索 {code} 无匹配详情页")
            return None

        if target.startswith('/'):
            target = self.BASE_URL + target
        detail = self._get(target)
        if not detail:
            return None
        return self._parse(code, detail.text)

    def _find_detail_link(self, soup: BeautifulSoup, code: str) -> Optional[str]:
        """在搜索结果页里找命中 <code> 的详情链接。
        avbase 详情页 URL 形态：
          - /works/{code}              （新番号）
          - /works/{maker}:{code}      （老系列，如 ebody:EBOD-205）
        策略：首选完全匹配 /works/{code}；其次匹配 :CODE 后缀（不区分大小写）。
        """
        code_upper = code.upper()
        exact = f"/works/{code}"
        suffix_candidates = []
        for a in soup.select('a[href*="/works/"]'):
            href = a.get('href') or ''
            if '?' in href:
                continue
            # 完全匹配优先
            if href == exact or href == self.BASE_URL + exact:
                return href
            # 后缀匹配 :CODE（大小写不敏感）
            if href.upper().endswith(':' + code_upper):
                suffix_candidates.append(href)
        return suffix_candidates[0] if suffix_candidates else None

    def _parse(self, code: str, html: str) -> Optional[ScrapeResult]:
        soup = BeautifulSoup(html, 'lxml')

        h1 = soup.find('h1')
        if not h1:
            logger.info(f"[{self.name}] 未找到 {code} 详情页（标题缺失）")
            return None

        result = ScrapeResult(code=code, source=self.name)
        result.title = h1.get_text(strip=True)

        # 封面：从 DMM 图床取，优先 pl 大图
        # avbase 页面里通常先列 awsimgsrc 再列 pics.dmm 多张
        cover_img = (
            soup.select_one('img[src*="awsimgsrc.dmm.co.jp"]')
            or soup.select_one('img[src*="pics.dmm.co.jp"]')
            or soup.select_one('meta[property="og:image"]')
        )
        if cover_img:
            url = cover_img.get('src') or cover_img.get('content')
            if url:
                result.cover_url = url

        # 字段提取：avbase 用统一 <div class="text-xs">标签</div><div class="text-sm">值</div>
        for label_div in soup.select('div.text-xs'):
            label = label_div.get_text(strip=True)
            value_div = label_div.find_next_sibling('div')
            if not value_div:
                continue
            value = value_div.get_text(' ', strip=True)
            if not value:
                continue

            if label == '発売日':
                # 2021/02/18 → 2021-02-18
                m = re.search(r'(\d{4})[/.-](\d{2})[/.-](\d{2})', value)
                if m:
                    result.release_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            elif label == 'メーカー':
                result.studio = value
            elif label == '監督':
                result.director = value
            elif label == '収録分数':
                m = re.search(r'(\d+)', value)
                if m:
                    result.duration_minutes = int(m.group(1))

        # 标签：链接到 /tags/...
        tags = []
        for a in soup.select('a[href*="/tags/"]'):
            t = a.get_text(strip=True)
            if t and t not in tags:
                tags.append(t)
        if tags:
            result.tags = tags

        # 演员：从 <title>/h1 中提取。avbase 没有结构化的 actresses 链接，
        # title 格式 "<番号>: <作品标题> <演员1> <演员2>（<演员1>／<演员2>）| AV検索データベース"
        # 括号内的"／"分隔形式比较稳定
        title_tag = soup.find('title')
        if title_tag:
            t = title_tag.get_text()
            # 抓取（）或()内最后那段含／的
            m = re.search(r'[（(]([^（）()]+?[／/][^（）()]+?)[）)]', t)
            if m:
                actors = [x.strip() for x in re.split(r'[／/]', m.group(1)) if x.strip()]
                if actors:
                    result.actors = actors

        return result
