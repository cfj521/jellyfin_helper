"""
配置管理模块
从 config.yaml 或环境变量读取配置
"""
import os
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic_settings import BaseSettings


ROOT_DIR = Path(__file__).parent.parent.parent


def load_yaml_config() -> dict:
    """加载 YAML 配置文件"""
    config_paths = [
        ROOT_DIR / "config.yaml",
        ROOT_DIR / "config.yml",
        Path.home() / ".jellyfin-helper" / "config.yaml"
    ]

    for path in config_paths:
        if path.exists():
            with open(path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}

    return {}


_yaml_config = load_yaml_config()


# ---- 外部工具路径注入 ----
# 把 yaml 里 tools.ffmpeg_dir 配置的目录加到当前进程 PATH 最前面，
# 这样 shutil.which('ffprobe') / which('ffmpeg') 立即能找到，
# 不必动系统 PATH（特别是 winget 装的 ffmpeg 经常不在系统 PATH 里）
_tools_cfg = _yaml_config.get('tools') or {}
_ffmpeg_dir = _tools_cfg.get('ffmpeg_dir') or ''
_mkvtoolnix_dir = _tools_cfg.get('mkvtoolnix_dir') or ''
for _extra in (_ffmpeg_dir, _mkvtoolnix_dir):
    if _extra and Path(_extra).is_dir():
        _cur = os.environ.get('PATH', '')
        # 避免重复加
        if _extra not in _cur.split(os.pathsep):
            os.environ['PATH'] = _extra + os.pathsep + _cur


class Settings(BaseSettings):
    """应用配置"""

    # 应用配置
    app_name: str = "Jellyfin Tools"
    debug: bool = False
    # 默认允许所有来源（开发模式下 vite dev server 也能访问）
    # 可通过环境变量 JF_CORS_ORIGINS='["http://x","http://y"]' 收紧
    cors_origins: List[str] = ["*"]

    # 认证
    auth_secret_key: str = (_yaml_config.get('auth') or {}).get('secret_key', 'change-me-to-a-random-string')
    auth_token_expire_hours: int = (_yaml_config.get('auth') or {}).get('token_expire_hours', 72)
    auth_users: List[dict] = (_yaml_config.get('auth') or {}).get('users') or []

    # 服务端口（环境变量 JF_BACKEND_PORT / JF_FRONTEND_PORT 优先于 yaml）
    backend_port: int = _yaml_config.get('server', {}).get('backend_port', 8000)
    frontend_port: int = _yaml_config.get('server', {}).get('frontend_port', 5173)

    # 数据库 (PostgreSQL)
    db_host: str = _yaml_config.get('database', {}).get('host', '192.168.89.11')
    db_port: int = _yaml_config.get('database', {}).get('port', 5432)
    db_name: str = _yaml_config.get('database', {}).get('name', 'jellyfin_helper')
    db_user: str = _yaml_config.get('database', {}).get('user', 'jellyfin_helper')
    db_password: str = _yaml_config.get('database', {}).get('password', 'JfTools@2026')

    @property
    def database_url(self) -> str:
        # 用户密码可能含 @ : / # 等特殊字符，必须 URL 编码
        from urllib.parse import quote_plus
        user = quote_plus(self.db_user)
        pwd = quote_plus(self.db_password)
        return f"postgresql://{user}:{pwd}@{self.db_host}:{self.db_port}/{self.db_name}"

    # Jellyfin 配置
    jellyfin_host: str = _yaml_config.get('jellyfin', {}).get('host', 'http://localhost:8096')
    jellyfin_external_url: str = _yaml_config.get('jellyfin', {}).get('external_url', '')
    jellyfin_api_key: str = _yaml_config.get('jellyfin', {}).get('api_key', '')
    # jellyfin SQLite 数据库文件本地可达路径（Ubuntu 部署默认 /var/lib/jellyfin/data/jellyfin.db）。
    # 跨主机部署时填 SMB 挂载的本地路径（如 W:/jellyfin/jellyfin.db）。
    # 留空则禁用 DB 直读，所有反查走 REST。
    jellyfin_db_path: str = _yaml_config.get('jellyfin', {}).get('db_path', '')

    # 路径映射：把 Jellyfin 报上来的路径转换为本后端可访问的路径
    # 例：Jellyfin 在 Linux 上看到 /library/videos/...，本工具在 Windows 上挂载为 Z:/videos/...
    # 配置形如：
    #   path_mappings:
    #     enabled: true
    #     rules:
    #       - from: /library/videos
    #         to: Z:/videos
    #       - from: /mnt/media
    #         to: \\\\NAS\\media
    # 按顺序尝试匹配；没命中的路径原样返回；enabled=false 时所有规则被旁路
    path_mappings_enabled: bool = (_yaml_config.get('path_mappings') or {}).get('enabled', False)
    path_mappings_rules: List[dict] = (_yaml_config.get('path_mappings') or {}).get('rules') or []

    # TMDB 配置
    tmdb_api_key: str = _yaml_config.get('tmdb', {}).get('api_key', '')
    # TMDB 请求延迟（秒）：每次 TMDB 调用之间的最小间隔
    # 实际生效以 config.yaml 为准；这里只是 yaml 没配置时的兜底
    tmdb_request_delay: float = _yaml_config.get('tmdb', {}).get('request_delay', 1.0)
    # TMDB 显示语言（影响标题、简介、类型名等本地化字段）
    # 常见：zh-CN / zh-TW / en-US / ja-JP / ko-KR
    tmdb_language: str = _yaml_config.get('tmdb', {}).get('language', 'zh-CN')

    # 缓存 TTL —— 除路径索引（硬编码 30 秒）外，全部可配
    # 注意：改值后需要重启后端才生效（settings 模块级一次性加载）
    cache_tmdb_minutes: int = _yaml_config.get('cache', {}).get('tmdb_minutes', 120)
    # 库信息缓存（天）：合并了原本的 library_stats / tree_children / subtitle_scan 三项 TTL，
    # 一律按"库内容低频变动"语义同源管理。默认 7 天，整库手动刷新或重启后端可强制失效。
    cache_library_days: int = _yaml_config.get('cache', {}).get('library_days', 7)

    # Wikidata 配置（演员图兜底来源；公共知识库无需 API Key，但要求礼貌的 User-Agent）
    wikidata_enabled: bool = _yaml_config.get('wikidata', {}).get('enabled', True)
    wikidata_user_agent: str = _yaml_config.get('wikidata', {}).get(
        'user_agent', 'JellyfinHelper/1.0 (cfj521@gmail.com)'
    )
    # 搜索语言顺序：按列表顺序依次尝试，命中第一个就停
    wikidata_language_order: List[str] = _yaml_config.get('wikidata', {}).get(
        'language_order', ['zh', 'en']
    )
    wikidata_request_delay: float = _yaml_config.get('wikidata', {}).get('request_delay', 1.0)

    # MDB List 配置（评分聚合 API：IMDB / RT / Metacritic / Trakt / Letterboxd 一次拿全）
    mdblist_enabled: bool = _yaml_config.get('mdblist', {}).get('enabled', True)
    mdblist_api_key: str = _yaml_config.get('mdblist', {}).get('api_key', '')
    mdblist_request_delay: float = _yaml_config.get('mdblist', {}).get('request_delay', 1.0)
    mdblist_cache_ttl_days: int = _yaml_config.get('mdblist', {}).get('cache_ttl_days', 30)

    # 豆瓣评分（无官方 API，HTML 爬虫；爬取较慢，作为懒拉取的后台任务）
    douban_enabled: bool = _yaml_config.get('douban', {}).get('enabled', True)
    douban_request_delay: float = _yaml_config.get('douban', {}).get('request_delay', 5.0)
    douban_cache_ttl_days: int = _yaml_config.get('douban', {}).get('cache_ttl_days', 30)
    douban_user_agent: str = _yaml_config.get('douban', {}).get(
        'user_agent',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    )
    # 豆瓣 detail 后台预取 worker（保守策略，避免触发反爬封禁）
    # worker_delay: 后台 prefetch worker 的请求间隔（前台请求另走 request_delay）
    # worker_max_failures / worker_cooldown_seconds: 全局熔断阈值
    #   连续 N 次失败 → 进 cooldown，期间 **所有路径**（前台/后台/ratings/maintenance）
    #   的豆瓣请求都直接返回 None，不发出 HTTP 请求
    douban_worker_delay: float = _yaml_config.get('douban', {}).get('worker_delay', 30.0)
    douban_worker_max_failures: int = _yaml_config.get('douban', {}).get('worker_max_failures', 5)
    douban_worker_cooldown_seconds: int = _yaml_config.get('douban', {}).get('worker_cooldown_seconds', 3600)

    # ---- 媒体元数据实体表（media_metadata）----
    # 详见 docs/2026-05-15-media-metadata-store.md
    # L3 长缓存：跨 discover 类 source 共享，独立 TTL + LRU
    metadata_store_douban_full: bool = _yaml_config.get('metadata', {}).get('store_douban_full', True)
    metadata_lru_keep_days: int = _yaml_config.get('metadata', {}).get('lru_keep_days', 365)
    metadata_refresh_ttl_days: int = _yaml_config.get('metadata', {}).get('refresh_ttl_days', 30)
    # 语言配置：刮削（上游拉数据语言）+ 显示（前端展示语言）
    # 默认 'en'。豆瓣无独立配置项——爬虫源永远只能拿中文，按全局配置走时也会
    # 自动 fallback 到 title_zh（豆瓣行 title_zh 永远有值）
    metadata_scrape_language: str = _yaml_config.get('metadata', {}).get('scrape_language', 'en')
    metadata_display_language: str = _yaml_config.get('metadata', {}).get('display_language', 'en')

    # OpenSubtitles 配置
    opensubtitles_api_key: str = _yaml_config.get('subtitle', {}).get('opensubtitles_api_key', '')
    opensubtitles_username: Optional[str] = _yaml_config.get('subtitle', {}).get('opensubtitles_username')
    opensubtitles_password: Optional[str] = _yaml_config.get('subtitle', {}).get('opensubtitles_password')
    opensubtitles_request_delay: float = _yaml_config.get('subtitle', {}).get('opensubtitles_request_delay', 2.0)
    # ── 字幕语言：两套配置，职责分明 ──
    #
    # required_langs：**缺字幕判定**的依据。原子单语言，任一缺即视为缺字幕。
    #   UI 上以 checkbox 形式勾选。值域：chs / cht / eng / jpn / kor。
    #   例：['chs', 'eng'] = "必须同时有简体中文 AND 英文，缺哪个都报警"。
    #   特殊：通用中文 'zh'（未指定简繁的字幕）在判定时同时覆盖 chs 和 cht。
    required_langs: List[str] = _yaml_config.get('subtitle', {}).get(
        'required_langs', ['chs', 'eng']
    )
    #
    # downloading_langs：**自动下载字幕**时的语言优先级（排序池，前优先）。
    #   UI 上以可拖动排序池形式调整。支持双语复合（chs.eng / cht.eng）。
    #   例：['chs.eng', 'chs', 'eng'] = "优先抓简英双语包，没有就单简，再没有就单英"。
    downloading_langs: List[str] = _yaml_config.get('subtitle', {}).get(
        'downloading_langs', ['chs.eng', 'chs', 'eng']
    )

    # 射手字幕 assrt.net（中文字幕主力源；API 限频 20/min）
    assrt_api_token: str = _yaml_config.get('subtitle', {}).get('assrt_api_token', '')
    # default 4.0：assrt 服务端硬限 20 req/min。3.0s 间隔正好踩边，常被
    # 并发的"前端手动搜字幕"/配额查询击穿。4.0s 留 33% buffer 给其它并发。
    assrt_request_delay: float = _yaml_config.get('subtitle', {}).get('assrt_request_delay', 4.0)

    # 字幕格式偏好（按顺序优先；同一语言下只下载排名最高的那一种）
    # 'ass' / 'ssa' 视为同一类（ASS 是 SSA v4+），'srt' 是兜底通用格式，'sup' 是蓝光图形字幕
    # 用户在包里同时拿到 chs.ass + chs.srt 时，按此顺序只保留 chs.ass
    preferred_subtitle_formats: List[str] = _yaml_config.get('subtitle', {}).get(
        'preferred_formats', ['ass', 'srt', 'sup']
    )

    # 字幕下载源优先级：按顺序尝试，第一个命中即返回
    # List[Dict]: [{name: 'assrt'|'opensubtitles', enabled: bool}, ...]
    subtitle_sources: List[dict] = _yaml_config.get('subtitle', {}).get('sources') or [
        {'name': 'assrt', 'enabled': True},
        {'name': 'opensubtitles', 'enabled': True},
    ]

    # 期望的默认音轨语言（按优先级排序，命中第一个就会被选为默认音轨）
    # 归一化代码：chs / cht / eng / jpn / kor
    preferred_audio_langs: List[str] = _yaml_config.get('audio', {}).get(
        'preferred_langs', ['chs', 'eng']
    )

    # Jackett 配置
    jackett_host: str = _yaml_config.get('jackett', {}).get('host', 'http://192.168.89.12:9117')
    jackett_api_key: str = _yaml_config.get('jackett', {}).get('api_key', '0w69x0scwq346shr2iddhszlak90znbq')
    # 搜索关键字 chip 列表（单个词，可在 UI 中点击 toggle 进/出搜索框）
    jackett_search_keywords: List[str] = _yaml_config.get('jackett', {}).get('search_keywords') or [
        '2160p', '1080p', '720p',
        'x265', 'x264', 'HEVC',
        'HDR', 'SDR', 'Dolby Vision',
        'BluRay', 'WEB-DL', 'REMUX',
        '中字',
    ]
    # 默认搜索附加字符串：进入搜索页面时自动拼到 query 后面（可空）
    jackett_default_keywords: str = _yaml_config.get('jackett', {}).get('default_keywords', '') or ''

    # ── debug 段：开发/调试用的开关 ──
    # show_dry_run_in_toolbar：在媒体库 / 成人库的操作按钮 confirm 弹窗里显示"测试模式"勾选
    # 默认 False —— 生产环境用户不需要看到这个；开发时勾上方便预演影响范围
    debug_show_dry_run_in_toolbar: bool = _yaml_config.get('debug', {}).get(
        'show_dry_run_in_toolbar', False
    )

    # qBittorrent 配置
    qbittorrent_host: str = _yaml_config.get('qbittorrent', {}).get('host', 'http://192.168.89.6')
    qbittorrent_username: str = _yaml_config.get('qbittorrent', {}).get('username', 'admin')
    qbittorrent_password: str = _yaml_config.get('qbittorrent', {}).get('password', 'Colex!51')
    qbittorrent_download_path: str = _yaml_config.get('qbittorrent', {}).get('download_path', '/downloads')

    # qBittorrent 配额 + 做种策略（嵌套 pydantic models，见 web/backend/config_models.py）
    # yaml 段：qbittorrent.quota / qbittorrent.seeding
    @property
    def qbittorrent_quota(self):
        from web.backend.config_models import QuotaConfig
        return QuotaConfig(**(_yaml_config.get('qbittorrent', {}).get('quota') or {}))

    @property
    def qbittorrent_seeding(self):
        from web.backend.config_models import SeedingConfig
        return SeedingConfig(**(_yaml_config.get('qbittorrent', {}).get('seeding') or {}))

    # 入库流水线（dispatch 段）
    @property
    def dispatch(self):
        from web.backend.config_models import DispatchConfig
        return DispatchConfig(**(_yaml_config.get('dispatch') or {}))

    # LLM 兜底识别（llm 段）
    @property
    def llm(self):
        from web.backend.config_models import LLMConfig
        return LLMConfig(**(_yaml_config.get('llm') or {}))

    # 第三方推荐源（trakt / anilist / douban_lists 段）
    @property
    def trakt(self):
        from web.backend.config_models import TraktConfig
        return TraktConfig(**(_yaml_config.get('trakt') or {}))

    @property
    def anilist(self):
        from web.backend.config_models import AniListConfig
        return AniListConfig(**(_yaml_config.get('anilist') or {}))

    @property
    def douban_lists(self):
        from web.backend.config_models import DoubanListsConfig
        return DoubanListsConfig(**(_yaml_config.get('douban_lists') or {}))

    # 成人内容配置
    adult_enabled: bool = _yaml_config.get('adult', {}).get('enabled', False)
    # 旧字段：单一目录（向后兼容，不推荐用）
    adult_media_path: str = _yaml_config.get('adult', {}).get('media_path', '')
    # 新字段：多个 Jellyfin 库的 ID 列表（推荐用法，自动识别 + 用户可手选）
    adult_library_ids: List[str] = _yaml_config.get('adult', {}).get('library_ids') or []
    # 新字段：除了 Jellyfin 库外的额外目录（用户自定义路径，留口子）
    adult_extra_paths: List[str] = _yaml_config.get('adult', {}).get('extra_paths') or []
    # 自动识别开关：首次启动时探测哪个 Jellyfin 库是成人库
    adult_auto_detect: bool = _yaml_config.get('adult', {}).get('auto_detect', True)
    # 自动监视：开启后通过 Jellyfin WebSocket 实时监听库变化，发现新视频自动入库 + 刮削
    adult_auto_scrape: bool = _yaml_config.get('adult', {}).get('auto_scrape', False)
    adult_scraper_delay: float = _yaml_config.get('adult', {}).get('scraper_delay', 3.0)
    # 刮削源配置（List[Dict]）。按顺序回退，第一个命中即返回。
    # missav 放最后：覆盖广（含 FC2 + 中文标题），但需 CF 绕过较慢，作为最终兜底
    # 过滤掉已下线源（如 avmoo）—— 老 config.yaml 可能还残留，避免 ScraperManager 每次启动都 warn
    adult_sources: List[dict] = [
        s for s in (
            _yaml_config.get('adult', {}).get('sources') or [
                {'name': 'javbus', 'enabled': True},
                {'name': 'javdb', 'enabled': True},
                {'name': 'javlibrary', 'enabled': False},
                {'name': 'avbase', 'enabled': True},
                {'name': 'missav', 'enabled': True},
            ]
        )
        if isinstance(s, dict) and s.get('name') in {
            'javbus', 'javdb', 'javlibrary', 'avbase', 'missav',
        }
    ]

    class Config:
        env_prefix = "JF_"
        case_sensitive = False

    def to_dict(self) -> dict:
        """转换为嵌套 dict，供 CLI 工具复用（CLI 期望的是 yaml 原始结构）。"""
        return {
            "server": {
                "backend_port": self.backend_port,
                "frontend_port": self.frontend_port,
            },
            "database": {
                "host": self.db_host,
                "port": self.db_port,
                "name": self.db_name,
                "user": self.db_user,
                "password": self.db_password,
            },
            "jellyfin": {
                "host": self.jellyfin_host,
                "api_key": self.jellyfin_api_key,
                "db_path": self.jellyfin_db_path,
            },
            "tmdb": {
                "api_key": self.tmdb_api_key,
                "request_delay": self.tmdb_request_delay,
                "language": self.tmdb_language,
            },
            "cache": {
                "tmdb_minutes": self.cache_tmdb_minutes,
                "library_days": self.cache_library_days,
            },
            "wikidata": {
                "enabled": self.wikidata_enabled,
                "user_agent": self.wikidata_user_agent,
                "language_order": self.wikidata_language_order,
                "request_delay": self.wikidata_request_delay,
            },
            "mdblist": {
                "enabled": self.mdblist_enabled,
                "api_key": self.mdblist_api_key,
                "request_delay": self.mdblist_request_delay,
                "cache_ttl_days": self.mdblist_cache_ttl_days,
            },
            "douban": {
                "enabled": self.douban_enabled,
                "request_delay": self.douban_request_delay,
                "cache_ttl_days": self.douban_cache_ttl_days,
                "user_agent": self.douban_user_agent,
            },
            "subtitle": {
                "opensubtitles_api_key": self.opensubtitles_api_key,
                "opensubtitles_username": self.opensubtitles_username,
                "opensubtitles_password": self.opensubtitles_password,
                "opensubtitles_request_delay": self.opensubtitles_request_delay,
                "required_langs": self.required_langs,         # 缺字幕判定
                "downloading_langs": self.downloading_langs,   # 下载优先级（排序池）
                "preferred_formats": self.preferred_subtitle_formats,
                "assrt_api_token": self.assrt_api_token,
                "assrt_request_delay": self.assrt_request_delay,
                "sources": self.subtitle_sources,
            },
            "audio": {
                "preferred_langs": self.preferred_audio_langs,
            },
            "path_mappings": {
                "enabled": self.path_mappings_enabled,
                "rules": self.path_mappings_rules,
            },
            "jackett": {
                "host": self.jackett_host,
                "api_key": self.jackett_api_key,
                "search_keywords": self.jackett_search_keywords,
                "default_keywords": self.jackett_default_keywords,
            },
            "qbittorrent": {
                "host": self.qbittorrent_host,
                "username": self.qbittorrent_username,
                "password": self.qbittorrent_password,
                "download_path": self.qbittorrent_download_path,
            },
            "adult": {
                "enabled": self.adult_enabled,
                "media_path": self.adult_media_path,
                "library_ids": self.adult_library_ids,
                "extra_paths": self.adult_extra_paths,
                "auto_detect": self.adult_auto_detect,
                "auto_scrape": self.adult_auto_scrape,
                "scraper_delay": self.adult_scraper_delay,
                "sources": self.adult_sources,
            },
            "debug": {
                "show_dry_run_in_toolbar": self.debug_show_dry_run_in_toolbar,
            },
        }


settings = Settings()


# 确保数据目录存在
data_dir = ROOT_DIR / "data"
data_dir.mkdir(exist_ok=True)
