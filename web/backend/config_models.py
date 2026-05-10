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
    单个分类的目标库 + 路径模板。
    location_template 渲染成目录；file_template 渲染成目录内文件 stem（自动加扩展名）。
    move_mode 已迁移到 DispatchConfig.default_move_mode（统一全局）。
    """
    library_id: str = ''                   # jellyfin 库 ID（前端选）
    location_template: str = '{library_root}/{title}'
    file_template: str = '{title}'         # 目录内的文件 stem（不含扩展名）


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
        ),
        'tv': DispatchRule(
            location_template='{library_root}/{series_name}/Season {season:02d}',
            file_template='({series_name})S{season:02d}E{episode:02d}',
        ),
        'anime': DispatchRule(
            location_template='{library_root}/{anime_name}',
            file_template='({anime_name}){episode:03d}',
        ),
        'adult': DispatchRule(
            location_template='{library_root}/{code}',
            file_template='{code}({title})',
        ),
    })


# ============ LLM 兜底识别 ============

class LLMConfig(BaseModel):
    """
    LLM 类型识别配置。
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
    confidence_threshold: float = 0.85     # < 阈值落用户确认
