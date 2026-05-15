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
import hashlib
import json
import logging
import re
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# 大搜索页（www.douban.com/search）—— 初始 HTML 不带结果（SPA），保留仅为兼容引用
SEARCH_URL = "https://www.douban.com/search"
# 电影专搜：返回 window.__DATA__ = {...} JSON，items[].id/title/cover_url/rating，
# 中英文均可命中。是 search_id 的正确端点。
SUBJECT_SEARCH_URL = "https://search.douban.com/movie/subject_search"
SUBJECT_DETAIL_URL = "https://movie.douban.com/subject/{id}/"
# 片单页：每页 25 条，?start=N 翻页
DOULIST_URL = "https://www.douban.com/doulist/{doulist_id}/"
# 排行榜（默认首屏 ~10 条）/ 正在上映（~19 条，含完整字段）
CHART_URL = "https://movie.douban.com/chart"
NOWPLAYING_URL = "https://movie.douban.com/cinema/nowplaying/"
# 即将上映：独立页 /coming，按上映日期排序的表格，~30+ 条；表格里只有标题/日期/类型/地区/想看数
COMING_URL = "https://movie.douban.com/coming"


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

    # ---- PoW 反爬挑战 ----
    # 2026 起豆瓣对未认证 UA 返回一个 ~3KB 的 "载入中..." shim 页：
    #   <form id="sec" method="POST" action="/c">
    #     <input id="tok" value="..." />   ← 服务端给的会话 token
    #     <input id="cha" value="..." />   ← 挑战字符串
    #     <input id="sol" value="" />      ← 客户端要算的解
    #     <input id="red" value="<原始 URL>" />
    #   </form>
    #   JS 里：sha512(cha + nonce) 前 4 位为 "0000" 时停下，把 nonce 写进 sol 后 submit
    #
    # 我们在 Python 端把 PoW 也算了 + POST 提交 → 拿到真实页面（同一 Session 内 cookie 复用，
    # 短期内不需要反复求解）
    _POW_PATTERN = re.compile(
        r'<form[^>]*id="sec"[^>]*action="([^"]+)"[\s\S]*?'
        r'id="tok"[^>]*value="([^"]+)"[\s\S]*?'
        r'id="cha"[^>]*value="([^"]+)"[\s\S]*?'
        r'id="red"[^>]*value="([^"]+)"',
        re.IGNORECASE
    )

    def _try_solve_pow(self, html: str, base_url: str) -> Optional[str]:
        """识别 PoW 挑战页 → 求解 → POST 提交 → 返回真正的页面 HTML。失败返回 None。"""
        m = self._POW_PATTERN.search(html)
        if not m:
            return None
        action, tok, cha, red = m.group(1), m.group(2), m.group(3), m.group(4)

        # PoW: 找 nonce 使 sha512(cha + str(nonce)) 前 difficulty 位为 '0'
        # 难度 4 → 期望 16^4=65K 次；Python sha512 ~1µs/次，期望 60-100ms
        difficulty = 4
        target = '0' * difficulty
        nonce = 0
        max_iter = 5_000_000
        t0 = time.time()
        while nonce < max_iter:
            nonce += 1
            h = hashlib.sha512((cha + str(nonce)).encode()).hexdigest()
            if h.startswith(target):
                break
        else:
            logger.warning(f"豆瓣 PoW 求解超过上限 {max_iter} 次未命中")
            return None
        solve_ms = (time.time() - t0) * 1000
        logger.info(f"豆瓣 PoW 求解成功 nonce={nonce} 耗时 {solve_ms:.0f}ms")

        post_url = urljoin(base_url, action)
        try:
            resp = self._session.post(
                post_url,
                data={'tok': tok, 'cha': cha, 'sol': str(nonce), 'red': red},
                timeout=self.timeout,
                allow_redirects=True,
            )
        except requests.exceptions.RequestException as e:
            logger.warning(f"豆瓣 PoW 提交失败: {e}")
            return None
        if resp.status_code != 200:
            logger.warning(f"豆瓣 PoW 提交返回 HTTP {resp.status_code}")
            return None
        # 提交后可能仍是 shim（被识破/Token 过期）→ 让上层走"全空"路径
        if self._POW_PATTERN.search(resp.text):
            logger.warning("豆瓣 PoW 提交后仍是挑战页（可能被识破）")
            return None
        return resp.text

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

            # PoW 反爬挑战页：~3KB 的"载入中..."shim，JS 算 SHA-512 PoW 再 submit
            # 在 Python 端求解 + 提交，然后用得到的真实页面继续后续解析
            if self._POW_PATTERN.search(resp.text):
                logger.info(f"豆瓣返回 PoW 挑战页 {url}，求解中…")
                solved = self._try_solve_pow(resp.text, resp.url or url)
                if solved:
                    return solved
                # PoW 失败 → 同一 attempt 内直接放弃（重试也大概率拿同一个挑战）
                return None

            return resp.text
        return None

    # ---------- 搜索 ID ----------

    def _search_items(self, query: str) -> Optional[list]:
        """
        调 search.douban.com/movie/subject_search?search_text={query}&cat=1002，
        从 window.__DATA__ 嵌入的 JSON 取 items[]。失败返回 None，空结果返回 []。

        query 可以是片名（中英文均可）或 IMDb tt 串。
        items[] 结构：{ id, title (末尾带 "(YYYY)"), rating: {value, count}, cover_url, ... }
        """
        if not query:
            return None
        html = self._get(SUBJECT_SEARCH_URL, params={'search_text': query, 'cat': '1002'})
        if not html:
            return None
        # window.__DATA__ = {...}; 后面跟其它 JS——用 raw_decode 切出第一个 JSON 对象
        m = re.search(r'window\.__DATA__\s*=\s*\{', html)
        if not m:
            return None
        try:
            data, _ = json.JSONDecoder().raw_decode(html[m.end() - 1:])
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning(f"豆瓣 subject_search JSON 解析失败 q={query!r}: {e}")
            return None
        return data.get('items') or []

    def search_id_by_imdb(self, imdb_id: str) -> Optional[str]:
        """
        按 IMDb tt 串在豆瓣电影搜索里命中条目。

        实测：豆瓣对**欧美电影**的条目页 IMDb 字段做了文本索引，
              search_text='tt0468569' 能直接命中唯一项（黑暗骑士 → 1851857）。
        中文电影 / 所有剧集的条目 IMDb 字段未被索引——这里搜不到时返回 None，
        让上层走 title 退化路径。
        """
        if not imdb_id or not imdb_id.startswith('tt'):
            return None
        items = self._search_items(imdb_id)
        if not items:
            return None
        sid = items[0].get('id')
        return str(sid) if sid else None

    @staticmethod
    def _item_rank(it: dict) -> tuple:
        """
        给候选 item 打分用于排序（大者优先）。
        排序键：(有评分 1/0, 票数, 评分)
          1. 有评分的（rating>0）压在无评分占位前——避开"未上映/预告片"条目
          2. 票数高的优先——同名片里票数最多的几乎一定是主版本
          3. 票数都一样时按评分降序（极端兜底，正常不会发生）
        """
        rt = it.get('rating') or {}
        try:
            val = float(rt.get('value') or 0)
        except (TypeError, ValueError):
            val = 0.0
        try:
            cnt = int(rt.get('count') or 0)
        except (TypeError, ValueError):
            cnt = 0
        return (1 if val > 0 else 0, cnt, val)

    def search_id(self, name: str, year: Optional[int] = None) -> Optional[str]:
        """
        按片名 + 可选年份在豆瓣搜索，返回最匹配的 subject_id。

        title 里末尾形如 "失控玩家 Free Guy‎ (2021)"——年份藏在文本里，不在独立 year 字段。

        策略：
          1. 有 year → 先取 year 匹配子集（同年同名时不会跨年错配）
          2. 子集（或全集，year 没传/没匹配时）按 (有评分, 票数, 评分) 降序排取最优
             这样多个同年同名条目（原版/复刻版/短片/预告占位）也会稳定选到主条目，
             不再像之前"取第一个 year 匹配"那样靠豆瓣返回顺序碰运气。
        """
        if not name:
            return None
        items = self._search_items(name)
        if not items:
            return None

        # year 消歧：title 末尾 "(2021)" 提年份；命中年份的进入候选子集
        candidates = items
        if year:
            matched = []
            for it in items:
                title = it.get('title') or ''
                ym = re.search(r'\((\d{4})\)\s*$', title)
                if ym and int(ym.group(1)) == year:
                    matched.append(it)
            if matched:
                candidates = matched
            # 没匹配项 → 退化到全集（年份偏差 ±1 时不至于完全没结果）

        # 排序选最优
        candidates_with_id = [it for it in candidates if it.get('id')]
        if not candidates_with_id:
            return None
        best = max(candidates_with_id, key=self._item_rank)
        sid = best.get('id')
        return str(sid) if sid else None

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

    def fetch_subject_summary(self, douban_id: str) -> Optional[Dict]:
        """
        豆瓣条目页爬取：返回剧情简介 + 演员表 + 国家 + 类型等。
        doulist 卡片 abstract 信息粒度太粗（只有导演/类型/年份），用户点开"简介"时再补这个。

        返回 dict 字段（缺失的为 None）：
            summary       剧情简介正文
            cast          主演列表（最多 10 位）
            countries     国家/地区
            genres        类型
            languages     语言
            release_date  首播日期
            duration      时长（电影分钟数 / 剧集每集分钟数）
            imdb_id       IMDb ID（如有）

        失败返回 None（HTTP 异常 / 反爬拦截页 / 关键字段全空）。
        """
        if not douban_id:
            return None
        url = SUBJECT_DETAIL_URL.format(id=douban_id)
        html = self._get(url)
        if not html:
            logger.warning(f"豆瓣条目 {douban_id}: _get 返回空（HTTP 错误或网络失败）")
            return None

        # 反爬侦测：登录墙、风控页、维护页 —— 这些 200 OK 但正文几乎全无
        # 命中任一关键字即视为拦截，免得走后续解析浪费时间
        antibot_markers = (
            '出于隐私保护', '登录跳转', '异常请求', '安全验证',
            'sec.douban.com', '请登录后查看', '正在跳转',
        )
        if any(k in html for k in antibot_markers):
            logger.warning(
                f"豆瓣条目 {douban_id}: 命中反爬/登录拦截页（HTML 长度 {len(html)}）"
            )
            return None

        try:
            soup = BeautifulSoup(html, 'lxml')
        except Exception:
            soup = BeautifulSoup(html, 'html.parser')

        # 剧情简介：<span property="v:summary"> 或 <span class="all hidden">
        summary: Optional[str] = None
        s_el = soup.select_one('span[property="v:summary"]') or soup.select_one('span.all.hidden')
        if not s_el:
            s_el = soup.select_one('#link-report span')
        if s_el:
            summary = s_el.get_text('\n', strip=True) or None

        # 海报：<a class="nbgnbg"><img src="..."></a> 或 <div id="mainpic"><img>
        poster_url: Optional[str] = None
        pic_el = soup.select_one('#mainpic img') or soup.select_one('a.nbgnbg img')
        if pic_el:
            poster_url = pic_el.get('src')

        # 标题 / 年份
        title: Optional[str] = None
        title_el = soup.select_one('span[property="v:itemreviewed"]') or soup.select_one('h1 span')
        if title_el:
            title = title_el.get_text(strip=True)
        year: Optional[int] = None
        year_el = soup.select_one('h1 span.year') or soup.select_one('span.year')
        if year_el:
            ytext = year_el.get_text(strip=True).strip('()')
            try:
                year = int(ytext)
            except ValueError:
                year = None

        # og: meta 兜底（豆瓣页面即便是精简版仍常带 OG 标签）
        if not title:
            og = soup.select_one('meta[property="og:title"]')
            if og:
                title = (og.get('content') or '').strip() or None
        if not title:
            # 最后的最后：用 <title>，去掉 "(豆瓣)" 后缀
            t_el = soup.select_one('title')
            if t_el:
                t = re.sub(r'\s*\(豆瓣\)\s*$', '', t_el.get_text(strip=True))
                title = t or None
        if not summary:
            og = soup.select_one('meta[property="og:description"]')
            if og:
                summary = (og.get('content') or '').strip() or None
        if not poster_url:
            og = soup.select_one('meta[property="og:image"]')
            if og:
                poster_url = og.get('content') or None

        # 评分 + 票数
        rating: Optional[float] = None
        votes: Optional[int] = None
        rating_el = soup.select_one('strong.rating_num')
        if rating_el:
            try:
                rating = float(rating_el.get_text(strip=True))
            except (ValueError, TypeError):
                pass
        votes_el = soup.select_one('span[property="v:votes"]')
        if votes_el:
            try:
                votes = int(votes_el.get_text(strip=True))
            except (ValueError, TypeError):
                pass

        # info 区域：<div id="info"> 内有"导演/编剧/主演/类型/制片国家/语言/上映日期/片长/IMDb"
        info_el = soup.select_one('#info')
        info_text = info_el.get_text('\n', strip=True) if info_el else ''

        def _grab(label: str) -> Optional[str]:
            m = re.search(rf'{label}[:：]\s*([^\n]+)', info_text)
            return m.group(1).strip() if m else None

        countries = _grab('制片国家/地区') or _grab('制片国家')
        genres_raw = _grab('类型')
        languages = _grab('语言')
        release_date = _grab('上映日期') or _grab('首播')
        duration = _grab('片长') or _grab('单集片长')
        imdb_id = _grab('IMDb')

        # 导演：豆瓣 info 区里是 <span class='pl'>导演</span>: <a rel="v:directedBy">...</a>
        # get_text('\n') 会把 "导演" / ":" / "陈凯歌" 拆成三行，regex 匹配不上 → 用专门的 rel 选择器
        directors = [a.get_text(strip=True) for a in soup.select('a[rel="v:directedBy"]')]
        director = ' / '.join(d for d in directors if d) or None

        # 演员（最多 10 位）
        cast: List[str] = []
        for a in soup.select('span.actor a.rl, a[rel="v:starring"]')[:10]:
            n = a.get_text(strip=True)
            if n and n not in cast:
                cast.append(n)

        # 关键字段（标题 + 简介）都为空 → 这页基本是没法用的（反爬页通过反爬侦测后，
        # 偶尔还能漏一些更隐蔽的精简页；这里再做一道兜底）
        if not title and not summary:
            head = html[:300].replace('\n', ' ').replace('\r', '')
            logger.warning(
                f"豆瓣条目 {douban_id}: 解析未拿到 title/summary（HTML 长度 {len(html)}），HTML 头: {head!r}"
            )
            return None

        # 命中：以 DEBUG 级别记录解析摘要，方便定位字段缺失
        logger.debug(
            f"豆瓣条目 {douban_id} 解析成功: title={title!r} year={year} "
            f"rating={rating} cast={len(cast)} imdb={imdb_id!r}"
        )

        return {
            'douban_id': douban_id,
            'title': title,
            'year': year,
            'poster_url': poster_url,
            'rating': rating,
            'votes': votes,
            'summary': summary,
            'director': director,
            'cast': cast,
            'countries': [c.strip() for c in re.split(r'[/,，、]', countries)] if countries else [],
            'genres': [g.strip() for g in re.split(r'[/,，、]', genres_raw)] if genres_raw else [],
            'languages': [l.strip() for l in re.split(r'[/,，、]', languages)] if languages else [],
            'release_date': release_date,
            'duration': duration,
            'imdb_id': imdb_id,
        }

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

    # ---------- 排行榜（/chart）----------

    def fetch_chart(self, start: int = 0, limit: int = 25) -> List[Dict]:
        """
        豆瓣电影排行榜 /chart。页面只有一屏（默认 section 的 ~10 条），所以 start>0 时返空。
        结构：<tr class="item"> 含 poster / title / rating / 信息段
        """
        if start > 0:
            return []
        html = self._get(CHART_URL)
        if not html:
            return []
        try:
            soup = BeautifulSoup(html, 'lxml')
        except Exception:
            soup = BeautifulSoup(html, 'html.parser')

        items: List[Dict] = []
        for tr in soup.select('tr.item')[:limit]:
            entry = self._parse_chart_row(tr)
            if entry:
                items.append(entry)
        return items

    def _parse_chart_row(self, tr) -> Optional[Dict]:
        # 标题 + douban_id
        title_a = tr.select_one('.pl2 > a')
        if not title_a:
            return None
        href = title_a.get('href') or ''
        m = re.search(r'subject/(\d+)/?', href)
        if not m:
            return None
        douban_id = m.group(1)
        # 标题文本里可能带 " / 别名"，第一个 / 之前才是主标题
        title_full = title_a.get_text(strip=True)
        title = re.split(r'\s*/\s*', title_full, maxsplit=1)[0].strip() or title_full

        # 海报
        poster_url = None
        poster_el = tr.select_one('a.nbg img')
        if poster_el:
            poster_url = poster_el.get('src')

        # 评分 + 人数
        rating: Optional[float] = None
        votes: Optional[int] = None
        rn = tr.select_one('.rating_nums')
        if rn:
            try:
                rating = float(rn.get_text(strip=True))
            except ValueError:
                pass
        # 人数 <span class="pl">(12345 人评价)</span>
        pl = tr.select_one('.star .pl')
        if pl:
            vm = re.search(r'(\d+)', pl.get_text())
            if vm:
                try:
                    votes = int(vm.group(1))
                except ValueError:
                    pass

        # info 段：日期 / 演员 / 国家 / 时长 / 类型 / ...
        p = tr.select_one('.pl2 p')
        info = p.get_text(' ', strip=True) if p else ''
        # 年份：从第一个 4 位数字捕获
        year = None
        ym = re.search(r'(19|20)\d{2}', info)
        if ym:
            try:
                year = int(ym.group(0))
            except ValueError:
                year = None

        return {
            'douban_id': douban_id,
            'title': title,
            'year': year,
            'rating': rating,
            'votes': votes,
            'poster_url': poster_url,
            # /chart 信息段比较杂；不分离 director（结构不固定），整段塞进 overview 截断 120 字
            'director': None,
            'overview': info[:120] if info else None,
            'genres': [],
        }

    # ---------- 正在上映（/cinema/nowplaying/ 的 #nowplaying section）----------

    def fetch_nowplaying(self, start: int = 0, limit: int = 25) -> List[Dict]:
        """
        /cinema/nowplaying/ #nowplaying section（~19 条）。
        每条 <li class="list-item"> 自带 data-* 属性（title/director/score/votes/duration 等），
        无需进 subject 页就能展示。
        即将上映另走 fetch_coming() —— 来自独立的 /coming 页，条目数多得多。
        """
        if start > 0:
            return []
        html = self._get(NOWPLAYING_URL)
        if not html:
            return []
        try:
            soup = BeautifulSoup(html, 'lxml')
        except Exception:
            soup = BeautifulSoup(html, 'html.parser')

        items: List[Dict] = []
        for li in soup.select('#nowplaying .list-item')[:limit]:
            entry = self._parse_nowplaying_item(li)
            if entry:
                items.append(entry)
        return items

    # ---------- 即将上映（/coming，~30+ 条；只有日期/标题/类型/地区/想看数）----------

    def fetch_coming(self, start: int = 0, limit: int = 50) -> List[Dict]:
        """
        /coming 即将上映电影表格。表格列：上映日期 / 片名 / 类型 / 制片国家/地区 / 想看人数
        没有海报 / 评分（未上映片正常情况）。
        """
        if start > 0:
            return []
        html = self._get(COMING_URL)
        if not html:
            return []
        try:
            soup = BeautifulSoup(html, 'lxml')
        except Exception:
            soup = BeautifulSoup(html, 'html.parser')

        items: List[Dict] = []
        for tr in soup.select('table.coming_list tbody tr')[:limit]:
            entry = self._parse_coming_row(tr)
            if entry:
                items.append(entry)
        return items

    def _parse_coming_row(self, tr) -> Optional[Dict]:
        tds = tr.select('td')
        if len(tds) < 5:
            return None
        date_str = tds[0].get_text(strip=True)
        title_a = tds[1].select_one('a')
        if not title_a:
            return None
        href = title_a.get('href') or ''
        m = re.search(r'subject/(\d+)/?', href)
        if not m:
            return None
        douban_id = m.group(1)
        title = title_a.get_text(strip=True)
        genres_text = tds[2].get_text(strip=True)
        region = tds[3].get_text(strip=True)
        want_text = tds[4].get_text(strip=True)
        want_m = re.search(r'(\d+)', want_text)
        want_count = int(want_m.group(1)) if want_m else None

        # 拼 overview：上映日期 / 类型 / 地区，方便卡片展示
        bits = []
        if date_str:
            bits.append(f'上映：{date_str}')
        if region:
            bits.append(f'地区：{region}')
        overview = ' / '.join(bits) if bits else None

        return {
            'douban_id': douban_id,
            'title': title,
            'year': None,         # /coming 表格只给月日，年份没拼出来；前端不显示 year 即可
            'rating': None,       # 未上映无评分
            'votes': want_count,  # 借用 votes 字段存"想看人数"
            'votes_label': '想看', # 前端 badge 文案：'14891 想看'（区别于其它源的 'XXX 评价'）
            'poster_url': None,   # 表格无海报；如要补图需进 subject 页 → 太贵不做
            'director': None,
            'overview': overview,
            'genres': [g.strip() for g in re.split(r'[/,，、]', genres_text) if g.strip()],
        }

    def _parse_nowplaying_item(self, li) -> Optional[Dict]:
        douban_id = li.get('data-subject')
        if not douban_id:
            return None

        def _i(name: str) -> Optional[int]:
            v = li.get(name) or ''
            return int(v) if v.isdigit() else None

        def _f(name: str) -> Optional[float]:
            v = (li.get(name) or '').strip()
            if not v or v == '0':
                return None
            try:
                return float(v)
            except ValueError:
                return None

        title = li.get('data-title') or '?'
        year = _i('data-release')
        rating = _f('data-score')
        votes = _i('data-votecount')
        director = (li.get('data-director') or '').strip() or None
        duration = (li.get('data-duration') or '').strip() or None
        region = (li.get('data-region') or '').strip() or None

        poster_url = None
        poster_el = li.select_one('.poster img')
        if poster_el:
            poster_url = poster_el.get('src')

        # 拼一段 overview 给卡片简介用（同 fetch_doulist 行为）
        bits = []
        if director:
            bits.append(f'导演：{director}')
        if region:
            bits.append(f'地区：{region}')
        if duration:
            bits.append(f'时长：{duration}')
        overview = ' / '.join(bits) if bits else None

        return {
            'douban_id': douban_id,
            'title': title,
            'year': year,
            'rating': rating,
            'votes': votes,
            'poster_url': poster_url,
            'director': director,
            'overview': overview,
            'genres': [],
        }

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

    def fetch_by_ids(
        self,
        imdb_id: Optional[str] = None,
        name: Optional[str] = None,
        year: Optional[int] = None,
        media_type: str = 'movie',
    ) -> Tuple[Optional[str], Optional[DoubanRating]]:
        """
        IMDb 优先 + 片名退化 的统一查询：

          1) 有 imdb_id 且 media_type='movie' → 用 imdb 串走 subject_search
             （欧美电影通常一击即中，零消歧成本）
          2) 失败 → 用 name + year 走 subject_search（中文片、剧集、imdb 缺失场景必走）
          3) 都失败 → (None, None)

        media_type='tv' 时跳过 imdb 搜（实测豆瓣对剧集 IMDb 字段未索引，省一次 HTTP）。
        """
        douban_id: Optional[str] = None
        if imdb_id and media_type != 'tv':
            douban_id = self.search_id_by_imdb(imdb_id)
            if douban_id:
                logger.debug(f"豆瓣 imdb 直命中 imdb={imdb_id} → {douban_id}")
        if not douban_id and name:
            douban_id = self.search_id(name, year)
        if not douban_id:
            return None, None
        rating = self.get_rating(douban_id)
        return douban_id, rating
