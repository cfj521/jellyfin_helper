"""
刮削器管理：多源回退
"""
import logging
from typing import Dict, List, Optional, Union

from .base import BaseScraper, ScrapeResult
from .javbus import JavBusScraper
from .javdb import JavDBScraper
from .javlibrary import JavLibraryScraper
from .avmoo import AvMooScraper

logger = logging.getLogger(__name__)


# 支持的源 → 实现类
SCRAPER_REGISTRY = {
    'javbus': JavBusScraper,
    'javdb': JavDBScraper,
    'javlibrary': JavLibraryScraper,
    'avmoo': AvMooScraper,
}


class ScraperManager:
    """按顺序尝试多个刮削器，第一个成功且字段非空的即返回。"""

    def __init__(
        self,
        delay: float = 1.0,
        proxy: Optional[str] = None,
        sources: Optional[List[Union[str, Dict]]] = None,
    ):
        """
        sources 支持两种形式：
          1) ['javbus', 'javdb']  —— 简单列表，全部用默认 base_url
          2) [{'name': 'javbus', 'base_url': 'https://...', 'enabled': true},
              {'name': 'javdb',  'enabled': false}]  —— 完整配置

        proxy 是全局代理，每个源也可以在配置里覆盖（item['proxy']）。
        """
        self.delay = delay
        self.proxy = proxy
        self.scrapers: List[BaseScraper] = []

        sources = sources or ['javbus', 'javdb']

        for src in sources:
            if isinstance(src, str):
                cfg = {'name': src}
            elif isinstance(src, dict):
                if not src.get('enabled', True):
                    continue
                cfg = src
            else:
                logger.warning(f"未知的源配置格式: {src!r}")
                continue

            name = cfg.get('name')
            cls = SCRAPER_REGISTRY.get(name)
            if cls is None:
                logger.warning(f"未知的刮削源: {name}")
                continue

            kwargs = {
                'delay': delay,
                'proxy': cfg.get('proxy', proxy),
            }
            if cfg.get('base_url'):
                kwargs['base_url'] = cfg['base_url']
            if cfg.get('timeout'):
                kwargs['timeout'] = cfg['timeout']

            try:
                self.scrapers.append(cls(**kwargs))
            except TypeError:
                # 老的子类不接受 base_url，回退到不传
                kwargs.pop('base_url', None)
                self.scrapers.append(cls(**kwargs))

        if not self.scrapers:
            logger.warning("ScraperManager 没有可用的刮削源")

    @property
    def active_sources(self) -> List[str]:
        return [s.name for s in self.scrapers]

    def scrape(self, code: str) -> Optional[ScrapeResult]:
        """逐个尝试，第一个返回非空 title 的视为命中。"""
        for scraper in self.scrapers:
            try:
                result = scraper.scrape(code)
                if result and result.title:
                    logger.info(f"[{scraper.name}] 命中 {code}: {result.title}")
                    return result
            except Exception as e:
                logger.warning(f"[{scraper.name}] 异常 {code} - {e}")
        logger.info(f"所有源均未命中: {code}")
        return None
