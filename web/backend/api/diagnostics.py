"""
可用性检测 API。

设计：
  - GET /api/diagnostics/system      → 本地零成本检查（DB + 系统命令行工具）
    页面打开自动跑，安全便宜
  - GET /api/diagnostics/services    → 列出所有当前已启用的网络类服务（前端用来生成按钮列表）
  - POST /api/diagnostics/check/{group}/{name}  → 单项手动测试
    所有网络类检测一律手动按钮触发，避免打开配置页就发一波请求

统一返回结构：
{
  "name": "tmdb",
  "label": "TMDB",
  "group": "metadata",
  "status": "ok" | "fail" | "not_configured",
  "message": "configuration ok",
  "elapsed_ms": 234,
  "checked_at": "2026-05-17T22:48:43"
}
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from datetime import datetime
from typing import Callable, Dict, List, Tuple

import requests
from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from web.backend.config import settings
from web.backend.database import SessionLocal

logger = logging.getLogger(__name__)
router = APIRouter()


# ============================================================================
# 通用工具
# ============================================================================

def _make_result(name: str, label: str, group: str, status: str,
                 message: str = '', elapsed_ms: int = 0) -> Dict:
    return {
        'name': name,
        'label': label,
        'group': group,
        'status': status,
        'message': message,
        'elapsed_ms': elapsed_ms,
        'checked_at': datetime.now().isoformat(timespec='seconds'),
    }


# ============================================================================
# 系统级（本地、零成本，进入页面自动跑）
# ============================================================================

def _check_binary(name: str, label: str, args=('--version',)) -> Dict:
    """检查命令行工具是否在 PATH 上且可执行。"""
    path = shutil.which(name)
    if not path:
        return _make_result(name, label, 'system', 'not_configured',
                            f'未找到 {name}（PATH 中缺）')
    t0 = time.time()
    try:
        r = subprocess.run(
            [name, *args],
            capture_output=True, text=True, timeout=3, errors='replace',
        )
        elapsed = int((time.time() - t0) * 1000)
        out = (r.stdout or r.stderr or '').strip().splitlines()
        first = (out[0] if out else f'exit={r.returncode}').strip()
        return _make_result(name, label, 'system', 'ok', first[:100], elapsed)
    except subprocess.TimeoutExpired:
        return _make_result(name, label, 'system', 'fail', '--version 超时 3s')
    except Exception as e:
        return _make_result(name, label, 'system', 'fail',
                            f'{type(e).__name__}: {e}')


def _check_database() -> Dict:
    t0 = time.time()
    try:
        with SessionLocal() as db:
            db.execute(text('SELECT 1'))
        elapsed = int((time.time() - t0) * 1000)
        return _make_result('database', 'PostgreSQL', 'system', 'ok',
                            'SELECT 1 ok', elapsed)
    except Exception as e:
        return _make_result('database', 'PostgreSQL', 'system', 'fail',
                            f'{type(e).__name__}: {e}')


@router.get("/system")
def diagnostics_system():
    """本地零成本检查：DB + 系统命令行工具。打开配置页就自动跑。"""
    return {
        'items': [
            _check_database(),
            _check_binary('ffmpeg', 'FFmpeg'),
            _check_binary('ffprobe', 'FFprobe'),
            _check_binary('mkvpropedit', 'MKVPropEdit (mkvtoolnix)'),
            # unrar 不接受 --version；不带参数会打印 banner 和退出码 0/10
            _check_binary('unrar', 'unrar', args=()),
            _check_binary('bsdtar', 'bsdtar'),
        ]
    }


# ============================================================================
# 网络类（手动触发）—— 每个返回 (ok, message)
# ============================================================================

# ---- 核心 ----

def _check_jellyfin() -> Tuple[bool, str]:
    if not settings.jellyfin_host or not settings.jellyfin_api_key:
        return False, '未配置 host / api_key'
    from common.jellyfin_client import JellyfinClient
    client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
    try:
        info = client._request('GET', '/System/Info')
        if info:
            return True, f"Jellyfin {info.get('Version', 'unknown')}"
        return False, '/System/Info 返回空'
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


# ---- 下载链路 ----

def _check_qbittorrent() -> Tuple[bool, str]:
    if not settings.qbittorrent_host or not settings.qbittorrent_username:
        return False, '未配置 host / username'
    from common.qbittorrent_client import QBittorrentClient
    client = QBittorrentClient(
        host=settings.qbittorrent_host,
        username=settings.qbittorrent_username,
        password=settings.qbittorrent_password,
    )
    try:
        return (True, '登录 ok') if client.login() else (False, '登录失败（账号/密码？）')
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def _check_jackett() -> Tuple[bool, str]:
    if not settings.jackett_host or not settings.jackett_api_key:
        return False, '未配置 host / api_key'
    from common.jackett_client import JackettClient
    client = JackettClient(settings.jackett_host, settings.jackett_api_key)
    try:
        return (True, 'caps 拉取 ok') if client.test_connection() else (False, '请求失败')
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


# ---- 元数据 / 评分 / 推荐 ----

def _check_tmdb() -> Tuple[bool, str]:
    if not settings.tmdb_api_key:
        return False, '未配置 api_key'
    from common.tmdb_client import TMDBClient
    client = TMDBClient(settings.tmdb_api_key)
    try:
        cfg = client._request('/configuration')
        if cfg and cfg.get('images'):
            return True, f"配置 ok (images cdn: {cfg['images'].get('secure_base_url', '?')})"
        return False, '/configuration 返回空'
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def _check_douban() -> Tuple[bool, str]:
    if not settings.douban_enabled:
        return False, '豆瓣已禁用'
    try:
        r = requests.get(
            'https://movie.douban.com/',
            headers={'User-Agent': settings.douban_user_agent or 'Mozilla/5.0'},
            timeout=10,
        )
        if r.status_code == 200:
            return True, '首页可达'
        if r.status_code in (403, 429, 503):
            return False, f'HTTP {r.status_code}（疑似反爬 / 限流）'
        return False, f'HTTP {r.status_code}'
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def _check_mdblist() -> Tuple[bool, str]:
    if not settings.mdblist_enabled or not settings.mdblist_api_key:
        return False, '未启用 / 缺 api_key'
    from common.mdblist_client import MDBListClient
    client = MDBListClient(settings.mdblist_api_key)
    try:
        # 拿《肖申克的救赎》评分做 health check（固定 IMDB ID）
        data = client.by_imdb('tt0111161', 'movie')
        if data and (data.get('ratings') or data.get('title')):
            return True, '示例查询 ok'
        return False, '返回空 / 配额耗尽？'
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def _check_trakt() -> Tuple[bool, str]:
    if not settings.trakt.enabled:
        return False, '未启用'
    if not settings.trakt.client_id:
        return False, '缺 client_id'
    from common.trakt_client import TraktClient
    from common.rate_limiter import TRAKT_DELAY
    client = TraktClient(
        client_id=settings.trakt.client_id,
        base_url=settings.trakt.base_url,
        request_delay=TRAKT_DELAY,
    )
    try:
        # /genres/movies 是公开 GET，最轻量
        data = client._get('/genres/movies')
        if data:
            return True, f'{len(data)} 个 genre'
        return False, '返回空 / client_id 错？'
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def _check_anilist() -> Tuple[bool, str]:
    if not settings.anilist.enabled:
        return False, '未启用'
    from common.anilist_client import AniListClient
    from common.rate_limiter import ANILIST_DELAY
    client = AniListClient(
        base_url=settings.anilist.base_url,
        request_delay=ANILIST_DELAY,
    )
    try:
        # 最轻量 GraphQL：站点统计
        data = client._query('query{SiteStatistics{anime{nodes{date count}}}}')
        return (True, 'GraphQL ok') if data else (False, 'GraphQL 返回空')
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def _check_wikidata() -> Tuple[bool, str]:
    if not settings.wikidata_enabled:
        return False, '未启用'
    if not settings.wikidata_user_agent:
        return False, '缺 user_agent（基金会强制要求）'
    from common.wikidata_client import WikidataClient
    from common.rate_limiter import WIKIDATA_DELAY
    client = WikidataClient(
        user_agent=settings.wikidata_user_agent,
        language_order=settings.wikidata_language_order,
        delay=WIKIDATA_DELAY,
    )
    try:
        data = client._query('SELECT ?item WHERE {?item wdt:P31 wd:Q5} LIMIT 1')
        return (True, 'SPARQL ok') if data else (False, 'SPARQL 返回空')
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def _check_llm() -> Tuple[bool, str]:
    if not settings.llm.enabled:
        return False, '未启用'
    if not settings.llm.api_key:
        return False, '缺 api_key'
    # 不实际跑 chat（费钱），只 GET /models 看 401/200
    try:
        url = settings.llm.base_url.rstrip('/') + '/models'
        r = requests.get(
            url,
            headers={'Authorization': f'Bearer {settings.llm.api_key}'},
            timeout=10,
        )
        if r.status_code == 200:
            return True, '/models ok（api_key 有效）'
        if r.status_code in (401, 403):
            return False, f'/models HTTP {r.status_code}（api_key 无效？）'
        return False, f'/models HTTP {r.status_code}'
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


# ---- 字幕源 ----

def _check_assrt() -> Tuple[bool, str]:
    if not settings.assrt_api_token:
        return False, '缺 api_token'
    from tools.subtitle_downloader.assrt import AssrtClient
    from common.rate_limiter import ASSRT_DELAY
    client = AssrtClient(token=settings.assrt_api_token, request_delay=ASSRT_DELAY)
    try:
        q = client.quota() or {}
        avail = (q.get('result') or {}).get('avail_count')
        if avail is not None:
            return True, f'剩余配额 {avail} 次/天'
        return True, 'quota 端点 ok'
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def _check_opensubtitles() -> Tuple[bool, str]:
    if not settings.opensubtitles_api_key:
        return False, '缺 api_key'
    try:
        # /infos/formats 公开端点，只验 api_key 有效
        r = requests.get(
            'https://api.opensubtitles.com/api/v1/infos/formats',
            headers={
                'Api-Key': settings.opensubtitles_api_key,
                'User-Agent': 'JellyfinHelper v1.0',
            },
            timeout=15,
        )
        if r.status_code == 200:
            return True, 'API ok（api_key 有效）'
        if r.status_code == 401:
            return False, 'HTTP 401（api_key 无效）'
        return False, f'HTTP {r.status_code}'
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def _check_shooter() -> Tuple[bool, str]:
    # shooter 没公开 status 端点，HEAD 主页判可达性
    try:
        r = requests.head('https://www.shooter.cn/', timeout=10, allow_redirects=True)
        if r.status_code < 500:
            return True, f'shooter.cn HTTP {r.status_code}'
        return False, f'HTTP {r.status_code}'
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


# ---- 成人刮削源（按 settings.adult_sources 启用项） ----

def _check_adult_source(name: str) -> Tuple[bool, str]:
    """对单个 adult scraper 做可达性检测：访问 base_url 看状态码。"""
    from tools.adult_manager.scrapers.manager import ScraperManager
    mgr = ScraperManager(sources=[{'name': name, 'enabled': True}])
    if not mgr.scrapers:
        return False, f'未知源 {name!r}'
    scraper = mgr.scrapers[0]
    # 找 base_url：子类可能用 BASE_URL 类常量 / base_url 实例属性
    url = (getattr(scraper, 'base_url', None)
           or getattr(scraper, 'BASE_URL', None)
           or getattr(type(scraper), 'BASE_URL', None))
    if not url:
        return False, '该 scraper 未暴露 base_url'
    try:
        r = scraper.session.get(url, timeout=getattr(scraper, 'timeout', 15))
        status = getattr(r, 'status_code', 0)
        if status == 200:
            return True, f'{url} HTTP 200'
        if status == 403:
            return False, f'HTTP 403（疑似 Cloudflare 拦截）'
        if status < 500:
            return True, f'HTTP {status}（可达但非 200，可能正常）'
        return False, f'HTTP {status}'
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


# ============================================================================
# 注册表 & 单项手动测试
# ============================================================================

# (group, name) → (label, check_fn)
_REGISTRY: Dict[Tuple[str, str], Tuple[str, Callable[[], Tuple[bool, str]]]] = {
    ('core',     'jellyfin'):      ('Jellyfin',       _check_jellyfin),
    ('download', 'qbittorrent'):   ('qBittorrent',    _check_qbittorrent),
    ('download', 'jackett'):       ('Jackett',        _check_jackett),
    ('metadata', 'tmdb'):          ('TMDB',           _check_tmdb),
    ('metadata', 'douban'):        ('豆瓣',           _check_douban),
    ('metadata', 'mdblist'):       ('MDB List',       _check_mdblist),
    ('metadata', 'trakt'):         ('Trakt',          _check_trakt),
    ('metadata', 'anilist'):       ('AniList',        _check_anilist),
    ('metadata', 'wikidata'):      ('Wikidata',       _check_wikidata),
    ('metadata', 'llm'):           ('LLM',            _check_llm),
    ('subtitle', 'assrt'):         ('assrt.net',      _check_assrt),
    ('subtitle', 'opensubtitles'): ('OpenSubtitles',  _check_opensubtitles),
    ('subtitle', 'shooter'):       ('Shooter',        _check_shooter),
}


@router.get("/services")
def diagnostics_list_services():
    """
    返回所有可手动测试的网络服务清单 + 当前 enabled 状态。
    前端用来生成测试按钮列表。
    """
    items: List[Dict] = []
    for (group, name), (label, _) in _REGISTRY.items():
        items.append({
            'group': group,
            'name': name,
            'label': label,
            'enabled': _is_enabled(group, name),
        })
    # 成人刮削源：按 settings.adult_sources 动态生成
    for entry in (settings.adult_sources or []):
        if isinstance(entry, dict):
            n = entry.get('name')
            enabled = bool(entry.get('enabled', True))
        else:
            n, enabled = str(entry), True
        if n:
            items.append({
                'group': 'adult',
                'name': n,
                'label': f'刮削源 · {n}',
                'enabled': enabled,
            })
    return {'items': items}


def _is_enabled(group: str, name: str) -> bool:
    """根据 settings 判断该服务是否处于"已启用 + 已配置凭据"状态。"""
    try:
        if group == 'core' and name == 'jellyfin':
            return bool(settings.jellyfin_host and settings.jellyfin_api_key)
        if group == 'download' and name == 'qbittorrent':
            return bool(settings.qbittorrent_host and settings.qbittorrent_username)
        if group == 'download' and name == 'jackett':
            return bool(settings.jackett_host and settings.jackett_api_key)
        if group == 'metadata':
            if name == 'tmdb':     return bool(settings.tmdb_api_key)
            if name == 'douban':   return bool(settings.douban_enabled)
            if name == 'mdblist':  return bool(settings.mdblist_enabled and settings.mdblist_api_key)
            if name == 'trakt':    return bool(settings.trakt.enabled and settings.trakt.client_id)
            if name == 'anilist':  return bool(settings.anilist.enabled)
            if name == 'wikidata': return bool(settings.wikidata_enabled and settings.wikidata_user_agent)
            if name == 'llm':      return bool(settings.llm.enabled and settings.llm.api_key)
        if group == 'subtitle':
            if name == 'assrt':         return bool(settings.assrt_api_token)
            if name == 'opensubtitles': return bool(settings.opensubtitles_api_key)
            if name == 'shooter':       return True   # 无 key，永远可测
    except Exception:
        return False
    return False


@router.post("/check/{group}/{name}")
def diagnostics_check(group: str, name: str):
    """对单个服务做一次实际检测。"""
    if group == 'adult':
        label = f'刮削源 · {name}'
        fn = lambda: _check_adult_source(name)
    else:
        entry = _REGISTRY.get((group, name))
        if not entry:
            raise HTTPException(404, f'未知服务: {group}/{name}')
        label, fn = entry

    t0 = time.time()
    try:
        ok, message = fn()
    except Exception as e:
        ok, message = False, f'{type(e).__name__}: {e}'
    elapsed = int((time.time() - t0) * 1000)
    return _make_result(name, label, group,
                        'ok' if ok else 'fail', message, elapsed)
