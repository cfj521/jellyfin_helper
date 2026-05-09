"""
刮削器基类

所有 JAV 刮削站点（javbus / javdb / avbase / javlibrary 等）
现在都上 Cloudflare bot 防护，裸 requests 一律 403。
本基类默认改用 curl_cffi（模拟 Chrome 124 的 TLS/JA3 指纹）做 _get，
普通子类零改动即享受 CF 绕过。
curl_cffi 缺失时回退到 requests，保留可运行性。
"""
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# 懒加载 curl_cffi.requests；缺失时 _CFFI_REQ=None，落回普通 requests
try:
    from curl_cffi import requests as _CFFI_REQ  # type: ignore
except ImportError:
    _CFFI_REQ = None  # type: ignore
    logger.warning(
        "curl_cffi 未安装，刮削器将使用普通 requests；"
        "JAV 站点大多有 Cloudflare 防护，建议 pip install curl_cffi"
    )


_DEFAULT_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}


@dataclass
class ScrapeResult:
    """统一的刮削结果数据结构"""
    code: str
    title: Optional[str] = None
    original_title: Optional[str] = None
    release_date: Optional[str] = None
    studio: Optional[str] = None
    director: Optional[str] = None
    duration_minutes: Optional[int] = None
    actors: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    cover_url: Optional[str] = None
    rating: Optional[float] = None
    source: str = ""

    def to_dict(self) -> Dict:
        return {
            "code": self.code,
            "title": self.title,
            "original_title": self.original_title,
            "release_date": self.release_date,
            "studio": self.studio,
            "director": self.director,
            "duration_minutes": self.duration_minutes,
            "actors": self.actors,
            "tags": self.tags,
            "cover_url": self.cover_url,
            "rating": self.rating,
            "source": self.source,
        }


class BaseScraper(ABC):
    """刮削器抽象基类。默认走 curl_cffi 模拟 Chrome 指纹绕过 Cloudflare。"""

    name: str = ""

    # 子类可设为 False 强制走 requests（极少用；如对方站不友好 chrome 指纹）
    use_cffi: bool = True
    # impersonate 配置：curl_cffi 支持 chrome99 ~ chrome131 等
    cffi_impersonate: str = 'chrome124'

    def __init__(self, delay: float = 1.0, proxy: Optional[str] = None, timeout: int = 30):
        self.delay = delay
        self.timeout = timeout
        self._last_req_at = 0.0

        if self.use_cffi and _CFFI_REQ is not None:
            self._using_cffi = True
            self.session = _CFFI_REQ.Session(impersonate=self.cffi_impersonate)
        else:
            self._using_cffi = False
            self.session = requests.Session()

        self.session.headers.update(_DEFAULT_HEADERS)
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    def _rate_limit(self):
        elapsed = time.time() - self._last_req_at
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_req_at = time.time()

    def _get(self, url: str, **kwargs) -> Optional[Any]:
        """
        发起 GET。返回 response（成功，含 .text/.content/.url/.status_code），
        失败（网络 / 4xx / 5xx）返回 None。
        curl_cffi 的 raise_for_status 行为不完全等同 requests，统一手动判断。
        """
        self._rate_limit()
        t0 = time.time()
        try:
            r = self.session.get(url, timeout=self.timeout, **kwargs)
        except Exception as e:  # 含 requests / curl_cffi 各自的网络异常
            elapsed_ms = (time.time() - t0) * 1000
            logger.warning(f"[{self.name}] 请求异常 ({elapsed_ms:.0f}ms) {url} - {e}")
            return None
        elapsed_ms = (time.time() - t0) * 1000
        status = getattr(r, 'status_code', 0)
        if status >= 400:
            logger.warning(f"[{self.name}] 请求失败 ({elapsed_ms:.0f}ms) {url} - HTTP {status}")
            return None
        # 慢请求 → 升级到 WARNING 级别，便于在批量刮削日志里 grep
        if elapsed_ms > 5000:
            logger.warning(f"[{self.name}] SLOW GET ({elapsed_ms:.0f}ms) {url}")
        else:
            logger.debug(f"[{self.name}] GET ({elapsed_ms:.0f}ms) {url}")
        return r

    @abstractmethod
    def scrape(self, code: str) -> Optional[ScrapeResult]:
        """根据番号刮削元数据。失败返回 None。"""
        ...
