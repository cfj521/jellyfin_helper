"""
豆瓣评分爬虫客户端。

豆瓣 2017 年关闭了所有公开 API，只能 HTML 爬虫。本客户端采用保守策略：
  - 严格按 request_delay 限速（建议 5-10s）
  - 单线程、不并发
  - User-Agent 模拟浏览器
  - 失败优雅退化（返回 None，不抛异常打断主流程）

主要查询路径：
    search_id(name, year)      - 按片名+年份搜索豆瓣详情页 ID
    get_rating(douban_id)      - 按已知 ID 拉评分
    fetch_by_name(name, year)  - 一站式：搜 ID 再拉评分

注意：豆瓣对国内/国外 IP 反爬强度不同；如部署在墙外可能需要走系统级代理（
本客户端不做代理逻辑，靠 requests 自动读 HTTP(S)_PROXY 环境变量）。
"""
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


SEARCH_URL = "https://www.douban.com/search"
SUBJECT_SEARCH_URL = "https://search.douban.com/movie/subject_search"
SUBJECT_DETAIL_URL = "https://movie.douban.com/subject/{id}/"
# 片单页：每页 25 条，?start=N 翻页
DOULIST_URL = "https://www.douban.com/doulist/{doulist_id}/"


class DoubanRating:
    """豆瓣评分简单数据载体。"""

    __slots__ = ('douban_id', 'rating', 'votes', 'title', 'year')

    def __init__(
        self,
        douban_id: str,
        rating: Optional[float] = None,
        votes: Optional[int] = None,
        title: Optional[str] = None,
        year: Optional[int] = None,
    ):
        self.douban_id = douban_id
        self.rating = rating
        self.votes = votes
        self.title = title
        self.year = year

    def to_dict(self) -> dict:
        return {
            'douban_id': self.douban_id,
            'rating': self.rating,
            'votes': self.votes,
            'title': self.title,
            'year': self.year,
        }


class DoubanClient:
    """豆瓣评分爬虫，懒拉取场景使用。"""

    def __init__(
        self,
        user_agent: str,
        delay: float = 5.0,
        max_retries: int = 1,
        timeout: float = 15.0,
    ):
        self.user_agent = user_agent
        self.delay = delay
        self.max_retries = max_retries
        self.timeout = timeout
        self._last_request_time = 0.0
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        })

    def _rate_limit(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request_time = time.time()

    def _get(self, url: str, params: Optional[dict] = None) -> Optional[str]:
        for attempt in range(self.max_retries + 1):
            self._rate_limit()
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)
            except requests.exceptions.RequestException as e:
                logger.warning(f"豆瓣请求异常: {url} - {e}")
                return None

            if resp.status_code == 429 or resp.status_code in (403, 503):
                # 反爬触发：豆瓣经常返回 403/503
                if attempt >= self.max_retries:
                    logger.warning(f"豆瓣访问受限 HTTP {resp.status_code} {url}")
                    return None
                backoff = (attempt + 1) * max(self.delay * 2, 10.0)
                logger.warning(f"豆瓣 {resp.status_code}，{backoff}s 后重试")
                time.sleep(backoff)
                continue

            if resp.status_code != 200:
                logger.warning(f"豆瓣 HTTP {resp.status_code}: {url}")
                return None

            return resp.text
        return None

    # ---------- 搜索 ID ----------

    def search_id(self, name: str, year: Optional[int] = None) -> Optional[str]:
        """
        按片名 + 可选年份在豆瓣搜索，返回最匹配的 subject_id。

        豆瓣搜索结果页 HTML 里电影条目链接形如：
            https://movie.douban.com/subject/30176393/?...
        我们匹配第一条命中。年份用于消歧（同名片）。
        """
        if not name:
            return None
        query = f"{name} {year}" if year else name
        html = self._get(SEARCH_URL, params={'cat': '1002', 'q': query})
        if not html:
            return None

        # subject 链接里 ID 是连续数字
        # 正则比 BeautifulSoup 更鲁棒（豆瓣搜索结果页结构经常微调）
        match = re.search(r'movie\.douban\.com/subject/(\d+)/?', html)
        return match.group(1) if match else None

    # ---------- 取评分 ----------

    def get_rating(self, douban_id: str) -> Optional[DoubanRating]:
        """按豆瓣 subject_id 取评分页。"""
        if not douban_id:
            return None
        url = SUBJECT_DETAIL_URL.format(id=douban_id)
        html = self._get(url)
        if not html:
            return None

        try:
            soup = BeautifulSoup(html, 'lxml')
        except Exception:
            soup = BeautifulSoup(html, 'html.parser')

        # 评分：<strong class="ll rating_num" property="v:average">8.5</strong>
        rating: Optional[float] = None
        rating_el = soup.select_one('strong.rating_num')
        if rating_el:
            text = rating_el.get_text(strip=True)
            try:
                rating = float(text) if text else None
            except ValueError:
                rating = None

        # 票数：<span property="v:votes">123456</span>
        votes: Optional[int] = None
        votes_el = soup.select_one('span[property="v:votes"]')
        if votes_el:
            text = votes_el.get_text(strip=True)
            try:
                votes = int(text)
            except ValueError:
                votes = None

        # 标题 + 年份（万一上层没传，从详情页带回）
        title: Optional[str] = None
        title_el = soup.select_one('span[property="v:itemreviewed"]')
        if title_el:
            title = title_el.get_text(strip=True)

        year: Optional[int] = None
        year_el = soup.select_one('span.year')
        if year_el:
            ytext = year_el.get_text(strip=True).strip('()')
            try:
                year = int(ytext)
            except ValueError:
                year = None

        return DoubanRating(
            douban_id=douban_id,
            rating=rating,
            votes=votes,
            title=title,
            year=year,
        )

    # ---------- 片单（doulist）爬取 ----------

    def fetch_doulist(self, doulist_id: str, start: int = 0, limit: int = 25) -> List[Dict]:
        """
        拉一个豆瓣片单的一页（25 条/页）。返回简要 dict 列表：
          [{douban_id, title, year, rating, votes, poster_url, overview, director, genres}, ...]

        start: 偏移（0/25/50/...），分页用
        limit: 上限（豆瓣每页 25 条；穿页要多次调用）

        豆瓣 doulist 页面结构：
          <div class="doulist-item"> 每项
            <div class="title"><a href=".../subject/12345/">标题</a></div>
            <div class="rating"><span class="rating_nums">8.5</span><span>(1234人评价)</span></div>
            <div class="abstract">导演: ... 主演: ... 类型: ... 年份: ...</div>
            <a class="post" href="..."><img src="..." /></a>

        豆瓣对未登录访问限速严，建议 delay >= 5s + cache_ttl_days 设大点。
        """
        if not doulist_id:
            return []
        url = DOULIST_URL.format(doulist_id=doulist_id)
        html = self._get(url, params={'start': max(0, int(start))})
        if not html:
            return []
        try:
            soup = BeautifulSoup(html, 'lxml')
        except Exception:
            soup = BeautifulSoup(html, 'html.parser')

        items: List[Dict] = []
        for card in soup.select('div.doulist-item')[:limit]:
            entry = self._parse_doulist_card(card)
            if entry:
                items.append(entry)
        return items

    def _parse_doulist_card(self, card) -> Optional[Dict]:
        # 标题 + douban_id
        title_el = card.select_one('.title a')
        if not title_el:
            return None
        title = title_el.get_text(strip=True)
        href = title_el.get('href') or ''
        m = re.search(r'subject/(\d+)/?', href)
        if not m:
            return None
        douban_id = m.group(1)

        # 海报
        poster_url = None
        poster_el = card.select_one('.post img')
        if poster_el:
            poster_url = poster_el.get('src')

        # 评分 + 人数
        rating: Optional[float] = None
        votes: Optional[int] = None
        rating_el = card.select_one('.rating .rating_nums')
        if rating_el:
            try:
                rating = float(rating_el.get_text(strip=True))
            except ValueError:
                pass
        votes_el = card.select_one('.rating span:not(.allstar):not(.rating_nums)')
        if votes_el:
            vt = votes_el.get_text(strip=True)
            vm = re.search(r'(\d+)', vt)
            if vm:
                try:
                    votes = int(vm.group(1))
                except ValueError:
                    pass

        # abstract 段：导演 / 主演 / 类型 / 年份 揉一起
        abstract = card.select_one('.abstract')
        director = None
        genres: List[str] = []
        year: Optional[int] = None
        if abstract:
            text = abstract.get_text('\n', strip=True)
            # 导演: xxx
            md = re.search(r'导演[:：]\s*([^\n]+)', text)
            if md:
                director = md.group(1).strip()
            # 类型: xx / yy
            mg = re.search(r'类型[:：]\s*([^\n]+)', text)
            if mg:
                genres = [g.strip() for g in re.split(r'[/,，、]', mg.group(1)) if g.strip()]
            # 年份: 2024 (中国)
            my = re.search(r'年份[:：]\s*(\d{4})', text)
            if my:
                try:
                    year = int(my.group(1))
                except ValueError:
                    pass

        return {
            'douban_id': douban_id,
            'title': title,
            'year': year,
            'rating': rating,
            'votes': votes,
            'poster_url': poster_url,
            'director': director,
            'genres': genres,
        }

    # ---------- 一站式 ----------

    def fetch_by_name(
        self, name: str, year: Optional[int] = None
    ) -> Tuple[Optional[str], Optional[DoubanRating]]:
        """
        按片名+年份搜出 ID，再取评分。返回 (douban_id, rating)。

        - (id, rating) ：成功
        - (id, None)   ：找到 ID 但评分页拉失败
        - (None, None) ：搜不到 ID
        """
        douban_id = self.search_id(name, year)
        if not douban_id:
            return None, None
        rating = self.get_rating(douban_id)
        return douban_id, rating
