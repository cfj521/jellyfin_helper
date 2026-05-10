"""
女优姓名归一化解析器（javdb 主源）。

输入：任意已知名字（可能是日文 / 中文译名 / 英文罗马字）。
策略：
  1. /search?q=<name>&f=actor —— 找出第一张女优卡，拿 javdb_id + 头像缩略图 URL
  2. /actors/<javdb_id> —— 取详情页 h1 ("中文, 日文N movie(s)" 这种格式)
  3. 拆 h1 文本 → jp_name / zh_name / en_name / aliases

返回 {javdb_id, jp_name, zh_name, en_name, aliases, avatar_url}（None 表示没找到）。

输出的 jp_name 是站点的"日文名"权威字段，跟输入的 query 不一定相等
（比如 query="葵司" → 返回 jp_name="葵つかさ", zh_name="葵司"）。
调用方应当用返回的 javdb_id 做去重 / 合并锚。
"""
import logging
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import List, Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# javdb 路径里这些 slug 是分类页，不是女优；要黑名单掉
_CATEGORY_SLUGS = frozenset({'censored', 'uncensored', 'western', 'fc2', 'all'})

# 用于判断字符串语种
_RE_KANA = re.compile(r'[぀-ゟ゠-ヿ]')               # 平假/片假
_RE_LATIN = re.compile(r'^[A-Za-z][A-Za-z\s\.\'\-]*$')  # 纯英文（含空格 / 点 / 撇）
_RE_ACTOR_HREF = re.compile(r'^/actors/([a-zA-Z0-9_-]+)/?$')


@dataclass
class ResolvedActress:
    javdb_id: str
    jp_name: str
    zh_name: Optional[str] = None
    en_name: Optional[str] = None
    aliases: List[str] = field(default_factory=list)
    avatar_url: Optional[str] = None


class ActressResolver:
    """女优姓名解析器（javdb）。"""

    BASE = 'https://javdb.com'

    def __init__(self, request_delay: float = 5.0, timeout: int = 20):
        self.request_delay = max(0.0, float(request_delay))
        self.timeout = timeout
        self._lock = threading.Lock()
        self._last_call = 0.0

        # 复用刮削器的 cffi 入口（绕 Cloudflare）
        from .scrapers.base import _CFFI_REQ
        if _CFFI_REQ is None:
            raise RuntimeError("curl_cffi 未安装；resolver 需要它来过 Cloudflare")
        self.session = _CFFI_REQ.Session(impersonate='chrome124')
        self.session.headers.update({
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
            ),
            'Accept-Language': 'zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7',
        })

    # ---- 限频 + GET ----

    def _wait(self):
        with self._lock:
            elapsed = time.monotonic() - self._last_call
            if elapsed < self.request_delay:
                time.sleep(self.request_delay - elapsed)
            self._last_call = time.monotonic()

    def _get(self, url: str):
        self._wait()
        try:
            r = self.session.get(url, timeout=self.timeout)
        except Exception as e:
            logger.warning(f"[actress] 请求异常 {url} - {e}")
            return None
        if getattr(r, 'status_code', 0) >= 400:
            logger.info(f"[actress] HTTP {r.status_code} {url}")
            return None
        # javdb 在被屏蔽地区会返回 200 + 版权拦截页（HTML 很短，含 copyright restrictions）
        # 这种情况要识别出来，否则上层会误以为"找不到"，把全部条目标 not_found
        body = (r.text or '')
        if len(body) < 4000 and (
            'copyright restrictions' in body.lower()
            or '版权限制' in body
            or '版權限制' in body
        ):
            logger.error(
                f"[actress] javdb 地理屏蔽（200 但返回版权拦截页）: {url}\n"
                f"  → 出口 IP 被 javdb 屏蔽，请更换代理节点（日本/美国线路通常可用）"
            )
            return None
        return r

    # ---- 业务逻辑 ----

    def resolve(self, query: str) -> Optional[ResolvedActress]:
        """主入口：给一个名字，返回归一化结果（含 javdb_id）。"""
        if not query or not query.strip():
            return None
        query = query.strip()

        # Step 1: search → 拿 actor href + thumbnail
        search_url = f'{self.BASE}/search?q={urllib.parse.quote(query)}&f=actor'
        r = self._get(search_url)
        if not r:
            return None
        href, javdb_id, thumb = self._find_first_actor(r.text)
        if not javdb_id:
            return None

        # Step 2: 详情页 → 解析 h1 拿别名对
        detail_url = self.BASE + href
        r = self._get(detail_url)
        if not r:
            # 详情挂了仍可保底返回 search 的简短结果
            return ResolvedActress(javdb_id=javdb_id, jp_name=query, avatar_url=thumb)

        jp, zh, en, aliases = self._parse_h1(r.text)
        # 详情页 h1 拿不到名字时（极少见）—— 至少把 query 当 jp_name 兜底
        if not (jp or zh or en):
            jp = query

        return ResolvedActress(
            javdb_id=javdb_id,
            jp_name=jp or query,
            zh_name=zh,
            en_name=en,
            aliases=aliases,
            avatar_url=thumb,
        )

    # ---- 解析辅助 ----

    @staticmethod
    def _find_first_actor(html: str):
        """搜索结果页里找第一张真正的女优卡。"""
        soup = BeautifulSoup(html, 'lxml')
        for a in soup.select('a[href^="/actors/"]'):
            href = a.get('href') or ''
            m = _RE_ACTOR_HREF.match(href)
            if not m or m.group(1) in _CATEGORY_SLUGS:
                continue
            img = a.find('img')
            avatar = img.get('src') if img else None
            return href, m.group(1), avatar
        return None, None, None

    @staticmethod
    def _parse_h1(html: str):
        """从详情页 h1 / actor-section-name 抠出 (jp, zh, en, aliases)。
        h1 形如 '葵司, 葵つかさ476 movie(s)' / '小倉由菜184 movie(s)'。
        """
        soup = BeautifulSoup(html, 'lxml')
        h1 = soup.select_one('span.actor-section-name, h1.title, h1')
        if not h1:
            return None, None, None, []
        text = h1.get_text(' ', strip=True)
        # 干掉尾部 "N movie(s)"
        text = re.sub(r'\s*\d+\s*movie\(s\)\s*$', '', text).strip()
        if not text:
            return None, None, None, []

        parts = [p.strip() for p in re.split(r'[,，、/／]', text) if p.strip()]
        if not parts:
            return None, None, None, []

        kana_parts: List[str] = []
        latin_parts: List[str] = []
        kanji_parts: List[str] = []  # 含汉字、不含假名、非纯英文
        for p in parts:
            if _RE_KANA.search(p):
                kana_parts.append(p)
            elif _RE_LATIN.match(p):
                latin_parts.append(p)
            else:
                kanji_parts.append(p)

        jp = zh = en = None
        aliases: List[str] = []

        # 日文名：优先有假名的；没有的话首个全汉字也是日本艺名（如"小倉由菜"）
        if kana_parts:
            jp = kana_parts[0]
            aliases.extend(kana_parts[1:])
            # 既有假名 → 全汉字 part 是中文译名
            if kanji_parts:
                zh = kanji_parts[0]
                aliases.extend(kanji_parts[1:])
        elif kanji_parts:
            jp = kanji_parts[0]
            aliases.extend(kanji_parts[1:])

        if latin_parts:
            en = latin_parts[0]
            aliases.extend(latin_parts[1:])

        return jp, zh, en, aliases
