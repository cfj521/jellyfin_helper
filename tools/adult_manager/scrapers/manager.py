"""
刮削器管理：多源回退
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple, Union

import requests

from common.label_cleaner import clean_label_list as _clean_label_list

from .base import BaseScraper, ScrapeResult
from .javbus import JavBusScraper
from .javdb import JavDBScraper
from .javlibrary import JavLibraryScraper
from .avbase import AvBaseScraper
from .missav import MissAvScraper

logger = logging.getLogger(__name__)


def _probe_cover_size(url: str, proxy: Optional[str], timeout: int = 6) -> int:
    """HEAD 一下 URL 拿 Content-Length，作为图片清晰度的 proxy 指标。
    探测失败返回 0（让上层用源优先级兜底）。"""
    if not url:
        return 0
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        # allow_redirects=True：很多图床会 302 到真正的图片 CDN
        r = requests.head(url, allow_redirects=True, timeout=timeout, proxies=proxies)
        if r.status_code != 200:
            return 0
        cl = r.headers.get('Content-Length')
        return int(cl) if cl and cl.isdigit() else 0
    except Exception as e:
        logger.debug(f"HEAD 探测失败 {url}: {e}")
        return 0


# 支持的源 → 实现类
SCRAPER_REGISTRY = {
    'javbus': JavBusScraper,
    'javdb': JavDBScraper,
    'javlibrary': JavLibraryScraper,
    'avbase': AvBaseScraper,
    'missav': MissAvScraper,
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

    def scrape(self, code: str, *, merge: bool = True) -> Optional[ScrapeResult]:
        """
        merge=True（默认，自动场景用）：跑所有源 → 字段级合并；
            命中要求 title + actors + cover_url 都齐。这样个别源解析半坏不影响最终数据。
        merge=False（手动 dialog 候选用）：第一个 title 非空即返回（旧行为）。
        """
        import time
        outer_t0 = time.time()

        if not merge:
            # 旧行为：第一个命中即返回
            for scraper in self.scrapers:
                t0 = time.time()
                try:
                    result = scraper.scrape(code)
                    elapsed_ms = (time.time() - t0) * 1000
                    if result and result.title:
                        # 清洗 actors / tags 标点空白等
                        result.actors = _clean_label_list(result.actors)
                        result.tags = _clean_label_list(result.tags)
                        total_ms = (time.time() - outer_t0) * 1000
                        logger.info(
                            f"[{scraper.name}] 命中 {code}: {result.title} "
                            f"(本源 {elapsed_ms:.0f}ms / 累计 {total_ms:.0f}ms)"
                        )
                        return result
                    logger.debug(f"[{scraper.name}] miss {code} ({elapsed_ms:.0f}ms)")
                except Exception as e:
                    elapsed_ms = (time.time() - t0) * 1000
                    logger.warning(f"[{scraper.name}] 异常 {code} ({elapsed_ms:.0f}ms) - {e}")
            total_ms = (time.time() - outer_t0) * 1000
            logger.info(f"所有源均未命中: {code} (累计 {total_ms:.0f}ms)")
            return None

        # 多源合并：跑所有源，按字段取并集
        per_source: Dict[str, ScrapeResult] = {}
        for scraper in self.scrapers:
            t0 = time.time()
            try:
                result = scraper.scrape(code)
                elapsed_ms = (time.time() - t0) * 1000
                if result and result.title:
                    per_source[scraper.name] = result
                    logger.info(
                        f"[{scraper.name}] 部分命中 {code}: {result.title!r} "
                        f"(actors={len(result.actors)} cover={'Y' if result.cover_url else 'N'} "
                        f"tags={len(result.tags)} {elapsed_ms:.0f}ms)"
                    )
                else:
                    logger.debug(f"[{scraper.name}] miss {code} ({elapsed_ms:.0f}ms)")
            except Exception as e:
                elapsed_ms = (time.time() - t0) * 1000
                logger.warning(f"[{scraper.name}] 异常 {code} ({elapsed_ms:.0f}ms) - {e}")

        total_ms = (time.time() - outer_t0) * 1000
        if not per_source:
            logger.info(f"所有源均未命中: {code} (累计 {total_ms:.0f}ms)")
            return None

        # 按 self.scrapers 顺序合并（前置源字段优先权高）
        merged = self._merge_by_field_priority(code, per_source)

        # 命中条件：title + cover_url 都齐即可（actors 不再强制）
        # 之前要求 actors 是想过滤"半坏的解析结果"，但实测发现：
        #   - FC2-PPV / HEYZO / 1pondo 等业余系列源站不留演员名
        #   - 部分商业番号源站演员字段也会缺（人员替换 / 数据库未更新）
        # 演员缺失但 title + cover 齐的情况下，认作命中比误判 not_found 更合理；
        # 用户事后可在"重新识别"对话框里挑别的源补
        if merged.title and merged.cover_url:
            sources_used = ','.join(per_source.keys())
            actors_note = '' if merged.actors else ' [actors 缺失]'
            logger.info(
                f"[merged: {sources_used}]{actors_note} 命中 {code}: {merged.title} "
                f"(actors={len(merged.actors)} tags={len(merged.tags)} {total_ms:.0f}ms)"
            )
            return merged

        # 字段没凑齐 —— 记录缺什么，仍然返回 None（让上层标记成 not_found）
        # 命中条件已不再要求 actors，所以这里也不把 actors 列入 missing
        missing = []
        if not merged.title: missing.append('title')
        if not merged.cover_url: missing.append('cover_url')
        sources_used = ','.join(per_source.keys())
        logger.warning(
            f"{code} 多源合并后仍缺 {missing}（已尝试 {sources_used}, 累计 {total_ms:.0f}ms）"
            f" — 视为未命中"
        )
        return None

    def _merge_by_field_priority(self, code: str, per_source: Dict[str, ScrapeResult]) -> ScrapeResult:
        """按 self.scrapers 注册顺序，每字段取第一个非空源。"""
        merged = ScrapeResult(code=code)
        ordered_results: List[Tuple[str, ScrapeResult]] = [
            (s.name, per_source[s.name]) for s in self.scrapers if s.name in per_source
        ]

        def first_nonempty(getter):
            for name, r in ordered_results:
                v = getter(r)
                if v:
                    return v, name
            return None, None

        title, _ = first_nonempty(lambda r: r.title)
        merged.title = title

        original_title, _ = first_nonempty(lambda r: r.original_title)
        merged.original_title = original_title

        # cover_url 选清晰度最高的：并行 HEAD 各源 cover_url，比 Content-Length 取最大。
        # 探测失败的源（HEAD 超时 / 4xx / 没返回 Content-Length）按下面的源优先级兜底：
        # avbase（DMM 高清）/ missav / javdb / javlibrary > javbus（防盗链 / 海外不稳）
        cover_preferred_order = ['avbase', 'missav', 'javdb', 'javlibrary', 'javbus']
        candidates: List[Tuple[str, str]] = []  # [(source_name, url), ...]
        for sname, r in per_source.items():
            if r.cover_url:
                candidates.append((sname, r.cover_url))

        cover = None
        if candidates:
            sizes: Dict[str, int] = {}
            with ThreadPoolExecutor(max_workers=min(5, len(candidates))) as ex:
                futures = {ex.submit(_probe_cover_size, url, self.proxy): (sname, url)
                           for sname, url in candidates}
                for fut in as_completed(futures):
                    sname, url = futures[fut]
                    try:
                        sizes[sname] = fut.result()
                    except Exception:
                        sizes[sname] = 0

            # 1) 有 size 数据的：按 size desc 排，size 一样按源优先级
            def _rank(item):
                sname, url = item
                size = sizes.get(sname, 0)
                pri = (cover_preferred_order.index(sname)
                       if sname in cover_preferred_order
                       else len(cover_preferred_order))
                return (-size, pri)  # size 大优先；同 size 走源优先级

            best = sorted(candidates, key=_rank)[0]
            cover = best[1]
            best_size = sizes.get(best[0], 0)
            logger.info(
                f"{code} cover 选 {best[0]} ({best_size/1024:.0f}KB) "
                f"候选={[(s, sizes.get(s, 0)//1024) for s, _ in candidates]}"
            )
        merged.cover_url = cover

        release, _ = first_nonempty(lambda r: r.release_date)
        merged.release_date = release

        studio, _ = first_nonempty(lambda r: r.studio)
        merged.studio = studio

        director, _ = first_nonempty(lambda r: r.director)
        merged.director = director

        duration, _ = first_nonempty(lambda r: r.duration_minutes)
        merged.duration_minutes = duration

        rating, _ = first_nonempty(lambda r: r.rating)
        merged.rating = rating

        actors, _ = first_nonempty(lambda r: r.actors)
        merged.actors = _clean_label_list(actors)

        tags, _ = first_nonempty(lambda r: r.tags)
        merged.tags = _clean_label_list(tags)

        # source 字段标记多源
        merged.source = 'merged:' + ','.join(per_source.keys())
        return merged
