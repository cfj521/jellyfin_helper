"""
MissAV 刮削器（missav.ai）

特点：
  - 收录范围广：商业番号 + FC2-PPV + 各种系列
  - 中文化标题（h1 是中文翻译标题）
  - 元数据齐全：番号 / 标题 / 发行日期 / 时长 / 演员 / 类型 / 制作商 / 标签 / 系列 / 导演 / 封面
  - **站点用 Cloudflare 防护**，需要 curl_cffi 模拟浏览器 TLS 指纹绕过
"""
import logging
import re
from typing import Optional
from urllib.parse import unquote

from bs4 import BeautifulSoup

from .base import BaseScraper, ScrapeResult

logger = logging.getLogger(__name__)


class MissAvScraper(BaseScraper):
    """MissAV 刮削器（用 curl_cffi 绕过 Cloudflare）"""

    name = "missav"
    DEFAULT_BASE_URL = "https://missav.ai/cn"

    def __init__(self, base_url: Optional[str] = None, **kwargs):
        super().__init__(**kwargs)
        self.BASE_URL = (base_url or self.DEFAULT_BASE_URL).rstrip('/')

        # MissAV 用 Cloudflare bot 防护，普通 requests 直接 403
        # 用 curl_cffi 模拟 Chrome 浏览器的 TLS/JA3 指纹
        try:
            from curl_cffi import requests as cffi_req
            self._cffi = cffi_req.Session(impersonate='chrome120')
            # 透传 BaseScraper 已配置的代理（如有）
            if self.session.proxies:
                self._cffi.proxies = dict(self.session.proxies)
        except ImportError as e:
            logger.error(f"[{self.name}] 需要安装 curl_cffi: pip install curl_cffi")
            self._cffi = None

    def _get(self, url: str, **kwargs):
        """覆盖父类 _get，改用 curl_cffi"""
        if self._cffi is None:
            return None
        self._rate_limit()
        try:
            r = self._cffi.get(url, timeout=self.timeout, **kwargs)
            if r.status_code >= 400:
                logger.warning(f"[{self.name}] 请求失败 {url} - HTTP {r.status_code}")
                return None
            return r
        except Exception as e:
            logger.warning(f"[{self.name}] 请求异常 {url} - {e}")
            return None

    def scrape(self, code: str) -> Optional[ScrapeResult]:
        # 第一步：直接 GET 详情页（最快路径）
        url = f"{self.BASE_URL}/{code}"
        resp = self._get(url)
        if resp:
            r = self._parse(code, resp.text)
            if r and r.title:
                return r

        # 第二步：直接 URL 失败 / 解析失败 → 搜索回退
        # missav 详情页 URL 路径有时不规整（带版本前缀 /dmXX/、不同语言、有/无 -uncensored-leak 等）
        # 搜索结果里能找到精确的小写 code 链接，进而拿到正确详情页
        return self._scrape_via_search(code)

    def _scrape_via_search(self, code: str) -> Optional[ScrapeResult]:
        # 搜索 URL：/search/{code} 直接走根 host，不要 /cn/ 前缀（前缀会跳到列表页）
        host_root = re.sub(r'/cn/?$|/en/?$|/ja/?$|/[a-z]{2}/?$', '', self.BASE_URL)
        if not host_root.startswith('http'):
            host_root = self.BASE_URL.rstrip('/')
        search_url = f"{host_root}/search/{code}"
        resp = self._get(search_url)
        if not resp:
            return None

        # 在搜索结果里找精确匹配的视频详情 URL（小写 code）
        # 例：https://missav.ai/fc2-ppv-4857234  /  https://missav.ai/cn/SSIS-001
        target = re.sub(r'[^A-Za-z0-9]', '', code).lower()
        # 抓所有看起来像 detail URL 的 href
        # 详情页通常 path 末段就是 code 本身（可能含中划线但去除非字母数字应等于 target）
        candidates = []
        for m in re.finditer(r'href="(https?://[^"]+|/[^"]+)"', resp.text):
            href = m.group(1)
            # 过滤明显不是详情的
            if any(x in href.lower() for x in ['/search/', '/genres/', '/labels/',
                                                '/makers/', '/actresses/', '/directors/',
                                                '/login', '/register', '/saved/',
                                                'twitter.com', 'telegram']):
                continue
            # 取最后一段 path
            tail = href.rstrip('/').split('/')[-1].split('?')[0]
            tail_norm = re.sub(r'[^A-Za-z0-9]', '', tail).lower()
            if tail_norm == target:
                # 拼成完整 URL
                full = href if href.startswith('http') else f"{host_root}{href}"
                if full not in candidates:
                    candidates.append(full)

        if not candidates:
            logger.info(f"[{self.name}] 搜索回退也未找到精确匹配 {code}")
            return None

        # 取第一条详情页
        detail_url = candidates[0]
        logger.info(f"[{self.name}] 搜索回退命中 {code} → {detail_url}")
        resp = self._get(detail_url)
        if not resp:
            return None
        return self._parse(code, resp.text)

    def _parse(self, code: str, html: str) -> Optional[ScrapeResult]:
        soup = BeautifulSoup(html, 'lxml')

        # 没收录的页面会是 SPA "Page Not Found" 或者 404 fallback
        # 用 h1 + og:title 双重判断
        h1 = soup.find('h1')
        og_title = soup.find('meta', property='og:title')
        if not h1 and not og_title:
            logger.info(f"[{self.name}] 未找到 {code} 详情页")
            return None

        result = ScrapeResult(code=code, source=self.name)
        # h1 是中文化标题（更友好），优先用
        if h1:
            result.title = h1.get_text(strip=True)
        elif og_title:
            result.title = og_title.get('content', '').strip()

        # 封面：og:image 最稳；twitter:image 备用
        og_img = soup.find('meta', property='og:image')
        tw_img = soup.find('meta', attrs={'name': 'twitter:image'})
        if og_img:
            result.cover_url = og_img.get('content', '').strip() or None
        elif tw_img:
            result.cover_url = tw_img.get('content', '').strip() or None

        # 字段：每条形如 <div class="text-secondary"><span>label:</span><value/></div>
        # 我们扫描所有 .text-secondary 块，按 label 分发
        for block in soup.select('div.text-secondary'):
            label_span = block.find('span')
            if not label_span:
                continue
            label = label_span.get_text(strip=True).rstrip(':：').strip()

            if label == '发行日期':
                t = block.find('time')
                if t and t.get('datetime'):
                    # 2021-02-18T11:00:54+08:00 → 2021-02-18
                    result.release_date = t['datetime'][:10]
                else:
                    text = block.get_text(' ', strip=True)
                    m = re.search(r'(\d{4}-\d{2}-\d{2})', text)
                    if m:
                        result.release_date = m.group(1)

            elif label == '时长':
                text = block.get_text(' ', strip=True)
                m = re.search(r'(\d+):(\d+):(\d+)', text)
                if m:
                    result.duration_minutes = int(m.group(1)) * 60 + int(m.group(2))
                else:
                    m = re.search(r'(\d+)\s*分', text)
                    if m:
                        result.duration_minutes = int(m.group(1))

            elif label == '导演':
                a = block.find('a')
                if a:
                    result.director = a.get_text(strip=True)

            elif label == '发行商':
                a = block.find('a')
                if a:
                    result.studio = a.get_text(strip=True)

            elif label in ('女优', '演员'):
                actors = []
                for a in block.find_all('a'):
                    name = a.get_text(strip=True)
                    # 去掉括号注释（"葵司 (葵つかさ)" → 取主名）
                    name = re.sub(r'\s*[（(].*?[）)]\s*', '', name).strip()
                    if name and name not in actors:
                        actors.append(name)
                if actors:
                    result.actors = actors

            elif label in ('类型', '类别', '标签'):
                tags = list(result.tags)
                for a in block.find_all('a'):
                    t = a.get_text(strip=True)
                    if t and t not in tags:
                        tags.append(t)
                result.tags = tags

        # 兜底：若上面没拿到 director/studio 等，从全局 a[href] 抓一次
        if not result.studio:
            a = soup.select_one('a[href*="/makers/"][class*="text-nord"]')
            if a:
                result.studio = a.get_text(strip=True)
        if not result.director:
            a = soup.select_one('a[href*="/directors/"]')
            if a:
                result.director = a.get_text(strip=True)

        return result
