"""
Wikidata 客户端：演员图片兜底来源。

经过 SPARQL endpoint https://query.wikidata.org/sparql 直接查 entity + P18 (image)。
图片实际存储在 Wikimedia Commons，通过 Special:FilePath 路径取并自动按 width 缩放。

不需要 API Key。但 Wikimedia 基金会要求每次请求带可识别的 User-Agent
（参见 https://meta.wikimedia.org/wiki/User-Agent_policy）；少了或写成默认值会被 403。
"""
import logging
import time
from typing import List, Optional, Tuple

import requests

from common.rate_limiter import quota_guard

logger = logging.getLogger(__name__)


SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"


def _escape_sparql_literal(s: str) -> str:
    """SPARQL 字符串字面量转义：反斜杠和双引号。"""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _build_query(name: str, lang: str) -> str:
    """
    按指定语言精确匹配 label 或 altLabel，限定为人类（Q5），可选拿到 P18 图片。

    LIMIT 5：避免常见名（"John Smith"）拉回上千条；同时给客户端"优先选有图者"的余地。
    """
    escaped = _escape_sparql_literal(name)
    return f"""
SELECT ?item ?image WHERE {{
  {{
    ?item rdfs:label "{escaped}"@{lang} .
  }} UNION {{
    ?item skos:altLabel "{escaped}"@{lang} .
  }}
  ?item wdt:P31 wd:Q5 .
  OPTIONAL {{ ?item wdt:P18 ?image . }}
}}
LIMIT 5
""".strip()


class WikidataClient:
    """SPARQL 客户端，专做"按演员名找头像"。"""

    def __init__(
        self,
        user_agent: str,
        language_order: Optional[List[str]] = None,
        delay: float = 1.0,
        max_retries: int = 2,
        timeout: float = 30.0,
        batch: bool = False,
    ):
        if not user_agent:
            raise ValueError("Wikidata 客户端需要 User-Agent（基金会强制要求）")
        self.user_agent = user_agent
        self.language_order = language_order or ['zh', 'en']
        self.delay = delay
        self.max_retries = max_retries
        self.timeout = timeout
        self.batch = batch
        self._last_request_time = 0.0
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': user_agent,
            'Accept': 'application/sparql-results+json',
        })

    def _rate_limit(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request_time = time.time()

    def _query(self, sparql: str) -> Optional[dict]:
        # 保底层：先检查全局暂停 / 批量配额
        quota_guard.acquire('wikidata', batch=self.batch)

        for attempt in range(self.max_retries + 1):
            self._rate_limit()
            try:
                resp = self._session.get(
                    SPARQL_ENDPOINT,
                    params={'query': sparql, 'format': 'json'},
                    timeout=self.timeout,
                )
            except requests.exceptions.RequestException as e:
                logger.warning(f"Wikidata 查询异常: {e}")
                return None

            if resp.status_code == 429 or resp.status_code >= 500:
                if resp.status_code == 429:
                    quota_guard.report_limited('wikidata', 'HTTP 429')
                if attempt >= self.max_retries:
                    logger.error(
                        f"Wikidata 查询失败 HTTP {resp.status_code}（已重试 {attempt} 次）"
                    )
                    return None
                backoff = (attempt + 1) * max(self.delay, 2.0)
                logger.warning(
                    f"Wikidata 限流/服务异常 ({resp.status_code})，{backoff}s 后重试"
                )
                time.sleep(backoff)
                continue

            if resp.status_code != 200:
                logger.error(
                    f"Wikidata 查询失败 HTTP {resp.status_code}: {resp.text[:200]}"
                )
                return None

            try:
                data = resp.json()
                quota_guard.report_success('wikidata')
                return data
            except ValueError:
                logger.error(f"Wikidata 返回非 JSON: {resp.text[:200]}")
                return None
        return None

    def search_person_image(self, name: str) -> Tuple[Optional[str], Optional[str]]:
        """
        按 language_order 依次查找演员，返回 (image_url, qid)。

        - (image_url, qid)        ：找到 entity 且有 P18 图片
        - (None, qid)             ：找到 entity 但无图（"已收录但 Wikidata 也没图"）
        - (None, None)            ：所有语言都没匹配到任何 entity
        """
        last_qid: Optional[str] = None
        for lang in self.language_order:
            data = self._query(_build_query(name, lang))
            if not data:
                continue
            bindings = data.get('results', {}).get('bindings', [])
            if not bindings:
                continue

            # 优先选有 P18 的；否则选第一条作为 last_qid 候选
            with_image = next((b for b in bindings if 'image' in b), None)
            chosen = with_image or bindings[0]

            qid = chosen['item']['value'].rsplit('/', 1)[-1]
            last_qid = qid

            if 'image' in chosen:
                return chosen['image']['value'], qid

        return None, last_qid

    def download_image(self, image_url: str, width: int = 632) -> Optional[bytes]:
        """
        从 Wikimedia Commons 取图。image_url 形如：
        http://commons.wikimedia.org/wiki/Special:FilePath/Foo.jpg

        Special:FilePath 自动重定向到实际文件，附加 ?width=N 让服务端按目标
        宽度返回缩放版（节省带宽，与 TMDB 的 h632 量级接近）。
        """
        if not image_url:
            return None
        sized_url = f"{image_url}?width={width}"
        try:
            resp = self._session.get(
                sized_url, timeout=self.timeout, allow_redirects=True
            )
            resp.raise_for_status()
            return resp.content
        except requests.exceptions.RequestException as e:
            logger.warning(f"Wikidata 图片下载失败: {sized_url} - {e}")
            return None

    def find_person_image(
        self, name: str
    ) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
        """
        一站式：搜索 + 下载。返回 (image_bytes, qid, image_url)。

        - (bytes, qid, url) ：成功
        - (None, qid, url)  ：元数据齐全但下载失败
        - (None, qid, None) ：找到 entity 但 P18 没值
        - (None, None, None)：完全没匹配
        """
        image_url, qid = self.search_person_image(name)
        if not image_url:
            return None, qid, None
        image_data = self.download_image(image_url)
        return image_data, qid, image_url
