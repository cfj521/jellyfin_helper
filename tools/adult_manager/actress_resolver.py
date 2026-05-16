"""
女优姓名归一化解析器（多源 chain：javdb 主 → Minnano-AV 兜底）。

输入：任意已知名字（可能是日文 / 中文译名 / 英文罗马字）。
策略：
  1. javdb /search?q=<name>&f=actor → /actors/<id> 详情页（拿 jp/zh/en/aliases）
  2. javdb miss 时 fallback Minnano-AV（日本本土站，jp/kana/romaji 全；无中文）
  3. 返回 ResolvedActress.source ∈ {'javdb', 'minnano_av'}，由调用方写回 DB

输出的 jp_name 是该源的"日文名"权威字段，跟输入的 query 不一定相等
（比如 query="葵司" → javdb 返回 jp_name="葵つかさ", zh_name="葵司"）。
调用方应当用返回的 javdb_id（或 minnano_av source + jp_name）做去重 / 合并锚。
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
    # source 标记数据来源：'javdb' / 'minnano_av' / 未来扩源时新增
    # 写到 AdultActress.source 列，方便统计哪个源命中率高、出问题时定位
    source: str
    # javdb_id 仅 javdb 源有值，Minnano-AV 等其它源为 None；
    # 调用方做去重合并时：有 javdb_id 优先按它去重，否则按 jp_name 去重
    javdb_id: Optional[str]
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
        """主入口：给一个名字，按 chain 顺序查 → javdb 命中即返；miss 时 fallback Minnano-AV。"""
        if not query or not query.strip():
            return None
        query = query.strip()

        # ---- Source 1: javdb ----
        r1 = self._resolve_via_javdb(query)
        if r1:
            return r1

        # ---- Source 2: Minnano-AV（日本本土，jp/kana/romaji；无中文）----
        r2 = self._resolve_via_minnano_av(query)
        if r2:
            return r2

        return None

    def _resolve_via_javdb(self, query: str) -> Optional[ResolvedActress]:
        """javdb 源：/search?q=...&f=actor → /actors/<id>。"""
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
            return ResolvedActress(
                source='javdb', javdb_id=javdb_id, jp_name=query, avatar_url=thumb
            )

        jp, zh, en, aliases = self._parse_h1(r.text)
        # 详情页 h1 拿不到名字时（极少见）—— 至少把 query 当 jp_name 兜底
        if not (jp or zh or en):
            jp = query

        return ResolvedActress(
            source='javdb',
            javdb_id=javdb_id,
            jp_name=jp or query,
            zh_name=zh,
            en_name=en,
            aliases=aliases,
            avatar_url=thumb,
        )

    def _resolve_via_minnano_av(self, query: str) -> Optional[ResolvedActress]:
        """Minnano-AV 源（日本本土站，零反爬）。

        搜索行为：
          - 单命中 → 自动跳转到详情页（HTML 是详情结构）
          - "0 名" → 没结果（HTML 含「のAV女優検索結果 0 名」）
          - 多命中 → 列表页（少数情况）

        详情页 H1 模式：`<jp_name> <kana 读音> / <Romaji>`，一行拿三种名字。
        无中译名（Minnano-AV 是日文站）。
        """
        url = (
            'https://www.minnano-av.com/search_result.php'
            '?search_scope=actress&search_word=' + urllib.parse.quote(query)
        )
        r = self._get_minnano_av(url)
        if not r:
            return None

        html = r.text
        # 结果列表（多命中）→ 取第一个 actress_id 再 GET 详情
        if 'のAV女優検索結果' in html and '0 名' not in html:
            soup = BeautifulSoup(html, 'lxml')
            first = soup.find('a', href=re.compile(r'actress\.php\?actress_id=\d+'))
            if not first:
                return None
            href = first.get('href') or ''
            if href.startswith('/'):
                href = 'https://www.minnano-av.com' + href
            r2 = self._get_minnano_av(href)
            if not r2:
                return None
            html = r2.text
        elif 'のAV女優検索結果' in html and '0 名' in html:
            return None

        return self._parse_minnano_av_detail(html, fallback_query=query)

    def _get_minnano_av(self, url: str):
        """Minnano-AV 专用 GET：复用 session + 限流，但跳过 javdb 版权页判定。"""
        self._wait()
        try:
            r = self.session.get(url, timeout=self.timeout)
        except Exception as e:
            logger.warning(f"[actress.minnano_av] 请求异常 {url} - {e}")
            return None
        if getattr(r, 'status_code', 0) >= 400:
            logger.info(f"[actress.minnano_av] HTTP {r.status_code} {url}")
            return None
        return r

    @staticmethod
    def _parse_minnano_av_detail(html: str, fallback_query: str) -> Optional[ResolvedActress]:
        """从 Minnano-AV 详情页 HTML 抽 ResolvedActress。"""
        soup = BeautifulSoup(html, 'lxml')

        # H1 形如：'葵つかさ あおいつかさ / Aoi Tsukasa'
        h1 = soup.find('h1')
        if not h1:
            return None
        h1_text = h1.get_text(' ', strip=True)
        # 排除"0 名"列表的 H1（已被外层挡掉，但加双保险）
        if '検索結果' in h1_text:
            return None

        jp_name: Optional[str] = None
        kana: Optional[str] = None
        romaji: Optional[str] = None
        # 格式：<jp> <kana> / <romaji>  或  <jp> / <romaji>  或  <jp>
        if '/' in h1_text:
            left, right = h1_text.rsplit('/', 1)
            romaji = right.strip() or None
            parts = left.strip().split()
            if parts:
                jp_name = parts[0]
                if len(parts) >= 2:
                    kana = ' '.join(parts[1:])
        else:
            parts = h1_text.split()
            jp_name = parts[0] if parts else fallback_query

        if not jp_name:
            return None

        # actress_id：从 canonical / 内部分页 link 抠
        actress_id: Optional[str] = None
        link = soup.find('link', rel='canonical')
        canon = link.get('href') if link else ''
        m = re.search(r'actress[._]?id=(\d+)|actress(\d+)\.html', canon or '')
        if m:
            actress_id = m.group(1) or m.group(2)
        else:
            for a in soup.find_all('a', href=re.compile(r'actress\.php\?actress_id=\d+')):
                mm = re.search(r'actress_id=(\d+)', a.get('href') or '')
                if mm:
                    actress_id = mm.group(1)
                    break

        # 头像：找 src 含 actress 的图（非 gif 占位）
        avatar_url: Optional[str] = None
        for img in soup.find_all('img'):
            src = img.get('src') or ''
            if 'actress' in src and not src.lower().endswith('.gif'):
                if src.startswith('http'):
                    avatar_url = src
                elif src.startswith('/'):
                    avatar_url = 'https://www.minnano-av.com' + src
                else:
                    avatar_url = 'https://www.minnano-av.com/' + src
                break

        # aliases：kana 读音 + actress_id 形成的 deep link 标识，都塞 aliases 方便后续匹配
        aliases: List[str] = []
        if kana and kana != jp_name:
            aliases.append(kana)
        # 不主动抓"別名/旧芸名"——Minnano-AV 这块多数页面是空的 / UI 添加按钮
        # （实测会误抓"を追加"等按钮文本）；保守留空，未来确认有数据再加

        return ResolvedActress(
            source='minnano_av',
            javdb_id=None,                # Minnano-AV 不是 javdb，无 javdb_id
            jp_name=jp_name,
            zh_name=None,                 # 该源无中译名
            en_name=romaji,               # romaji 当 en_name
            aliases=aliases,
            avatar_url=avatar_url,
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
