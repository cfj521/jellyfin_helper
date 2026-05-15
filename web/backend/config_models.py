"""
新增配置段的嵌套 pydantic models。
旧配置仍是扁平字段（config.py 中），新增的 dispatch / quota / seeding / llm
用嵌套结构以避免 30+ 扁平字段膨胀。
"""
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


# ============ qBittorrent 配额管理 ============

class QuotaConfig(BaseModel):
    """/downloads 容量配额管理。

    limit 用 shutil.disk_usage(download_path).total 动态读取真实磁盘容量。
    后端 stat 不到下载盘（容器没挂载、路径不存在）→ 整个配额功能禁用，
    不监视、不强清，UI 显示灰态。
    """
    enabled: bool = True
    warn_threshold: float = 0.85           # 85% UI 状态条变橙
    cleanup_threshold: float = 0.95        # 95% 触发硬清
    target_after_cleanup: float = 0.70     # 清理后回到 70% 留余量
    check_interval_seconds: int = 300      # 5 分钟


class SeedingConfig(BaseModel):
    """做种 / 软清策略。"""
    target_ratio: float = 1.0              # 默认分享率（私有种用户可调高）
    min_seed_days: int = 7
    min_seed_hours_per_torrent: int = 24
    cleanup_interval_seconds: int = 86400  # 软清周期 1 天


# ============ 入库流水线 ============

class DispatchRule(BaseModel):
    """
    单个分类的目标库 + 路径模板 + 同源去重策略。
    location_template 渲染成目录；file_template 渲染成目录内文件 stem（自动加扩展名）。
    move_mode 已迁移到 DispatchConfig.default_move_mode（统一全局）。

    duplicate_policy：目标位置已被其他 dispatch_map 行占用时怎么办：
      - 'higher_quality_wins'：用质量 tier 比较，新种子更高 → 旧文件入 trash 再覆盖；
                              同等或更低 → 跳过（保留旧的）
      - 'always_skip'：永远跳过新种子（保守，番号默认）
      - 'always_replace'：永远用新的覆盖（旧文件先入 trash 兜底）
      - 'needs_review'：标 phase_status=needs_review，等用户点 UI 决策
    """
    library_id: str = ''                   # jellyfin 库 ID（前端选）
    location_template: str = '{library_root}/{title}'
    file_template: str = '{title}'         # 目录内的文件 stem（不含扩展名）
    duplicate_policy: str = 'higher_quality_wins'


class DispatchConfig(BaseModel):
    """下载完成 → 入库流水线 配置。"""
    enabled: bool = True
    # poll_interval_seconds：DispatchPipeline 兜底轮询（事件驱动后只在闲时生效）
    poll_interval_seconds: int = 30
    # adopt_interval_seconds：孤儿种子认领（兜底处理 qB Web 直接加 / Jackett RSS 推过来的种子）
    adopt_interval_seconds: int = 300      # 5 分钟
    # trash_interval_seconds：trash 目录过期清理周期
    trash_interval_seconds: int = 86400    # 1 天
    worker_concurrency: int = 1            # 单 worker 串行（用户已确认）
    copy_buffer_mb: int = 8
    default_move_mode: str = 'copy'        # 所有 media_type 共享：copy / move
    trash_dir: str = '/downloads/.trash'   # sample/nfo/RARBG.txt 丢这里
    # 各分类的规则；用户在前端配置具体 library_id + location_template
    rules: Dict[str, DispatchRule] = Field(default_factory=lambda: {
        'movie': DispatchRule(
            location_template='{library_root}/{title} ({year})',
            file_template='{title} ({year})',
            duplicate_policy='higher_quality_wins',
        ),
        'tv': DispatchRule(
            location_template='{library_root}/{series_name}/Season {season:02d}',
            file_template='({series_name})S{season:02d}E{episode:02d}',
            duplicate_policy='higher_quality_wins',
        ),
        'anime': DispatchRule(
            location_template='{library_root}/{anime_name}',
            file_template='({anime_name}){episode:03d}',
            duplicate_policy='higher_quality_wins',
        ),
        'adult': DispatchRule(
            location_template='{library_root}/{code}',
            file_template='{code}({title})',
            duplicate_policy='always_skip',  # 番号同 code 不轻易覆盖
        ),
    })


# ============ LLM 兜底识别 ============

class LLMConfig(BaseModel):
    """
    LLM 媒体识别配置。
    Spike 实测：qwen-plus 30/30 准确率 100%，¥0.74 / 1000 调用。
    """
    enabled: bool = True
    provider: str = 'qwen'                 # qwen / deepseek / openai / lmstudio
    api_key: str = ''
    model: str = 'qwen-plus'
    base_url: str = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
    timeout_seconds: int = 180
    max_retries: int = 1
    cache_ttl_days: int = 30
    # 优先级：默认 False = LLM 兜底；True = LLM 排在识别链首位，规则正则作为兜底
    prefer_first: bool = False


# ============ 第三方推荐源（Trakt / AniList / 豆瓣 doulist）============

class TraktConfig(BaseModel):
    """Trakt API 配置。
    https://trakt.tv/oauth/applications 创建 application 后，把"Client ID"贴到 client_id。
    公开 endpoint（trending / popular）不需要 OAuth；header 里带 trakt-api-key 即可。
    """
    enabled: bool = False
    client_id: str = ''
    base_url: str = 'https://api.trakt.tv'
    request_delay: float = 1.0
    timeout_seconds: int = 15
    # 缓存 TTL（分钟）：Trakt 是实时观看活动信号，刷新频率应该比 TMDB 高一点
    cache_minutes: int = 60


class AniListConfig(BaseModel):
    """AniList GraphQL 配置（公开 API，**完全不需要账号和 key**）。"""
    enabled: bool = True
    base_url: str = 'https://graphql.anilist.co'
    request_delay: float = 1.0
    timeout_seconds: int = 15
    cache_minutes: int = 240        # 4 小时；番剧排行变化慢


class DoubanListsConfig(BaseModel):
    """豆瓣 doulist（精选片单）爬取配置。
    跟评分爬取共用 douban.request_delay；这里只放片单 ID 白名单。

    豆瓣对未登录访问反爬强，触发限速会拉黑半小时；而片单（如 Top 250）本身
    变化极慢（一周改不了几条），所以 TTL 默认拉到 3 天。
    """
    enabled: bool = True
    cache_days: int = 3              # 默认 3 天
    # 默认列表：豆瓣 Top 250 + 豆瓣官方排行/上映两个动态页。用户可在 config.yaml 改 / 增。
    # 格式：[{name: "中文显示名", doulist_id: "<ID 或保留字>", media_type: "movie"|"tv"}, ...]
    #   doulist_id 取值：
    #     纯数字       → 经典 /doulist/<id>/ 片单
    #     "chart"      → 豆瓣电影排行榜（/chart 默认 section）
    #     "nowplaying" → 正在上映（/cinema/nowplaying/ 的 nowplaying section）
    #     "upcoming"   → 即将上映（/cinema/nowplaying/ 的 upcoming section）
    # 失效时打开 douban.com 找新 doulist URL 中的 ID 替换即可。
    lists: List[Dict] = Field(default_factory=lambda: [
        {'name': '豆瓣 Top 250', 'doulist_id': '240962',     'media_type': 'movie'},
        {'name': '豆瓣排行榜',   'doulist_id': 'chart',      'media_type': 'movie'},
        {'name': '正在上映',     'doulist_id': 'nowplaying', 'media_type': 'movie'},
        {'name': '即将上映',     'doulist_id': 'upcoming',   'media_type': 'movie'},
    ])
