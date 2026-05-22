"""
数据库模块
PostgreSQL + SQLAlchemy
"""
from datetime import datetime
from typing import Optional
from pathlib import Path

from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Text, Boolean, Float,
    BigInteger, Numeric, UniqueConstraint, Index,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker, declarative_base

from backend.config import settings


# 创建引擎 (PostgreSQL)
engine = create_engine(
    settings.database_url,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    echo=settings.debug
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ==================== 模型定义 ====================

class User(Base):
    """用户"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(200), nullable=False)
    role = Column(String(20), nullable=False, default="guest")
    created_at = Column(DateTime, default=datetime.utcnow)


class Task(Base):
    """后台任务"""
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    task_type = Column(String(50), nullable=False)  # subtitle_scan, actor_fix, etc.
    status = Column(String(20), default="pending")  # pending, running, completed, failed
    progress = Column(Float, default=0.0)  # 0-100
    message = Column(Text)
    result = Column(Text)  # JSON 格式结果
    # 任务输入参数的 JSON 字符串。用于服务重启时按注册表恢复幂等任务（见 task_restart.py）
    params = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)


class ScanReport(Base):
    """扫描报告"""
    __tablename__ = "scan_reports"

    id = Column(Integer, primary_key=True, index=True)
    report_type = Column(String(50), nullable=False)  # subtitle, media, actor
    scan_path = Column(String(500), nullable=False)
    total_items = Column(Integer, default=0)
    issues_count = Column(Integer, default=0)
    report_data = Column(Text)  # JSON 格式
    created_at = Column(DateTime, default=datetime.utcnow)


class ActorInfo(Base):
    """演员信息缓存"""
    __tablename__ = "actors"

    id = Column(Integer, primary_key=True, index=True)
    jellyfin_id = Column(String(100), unique=True, nullable=False)
    name = Column(String(200), nullable=False)
    tmdb_id = Column(Integer)
    has_image = Column(Boolean, default=False)
    image_url = Column(String(500))
    # 图片来源：tmdb / wikidata / manual / none（两源都没找到）/ NULL（还没修过）
    image_source = Column(String(20))
    last_checked = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MediaItem(Base):
    """媒体项目（电影 / 剧集 / 集）"""
    __tablename__ = "media_items"

    id = Column(Integer, primary_key=True, index=True)
    jellyfin_id = Column(String(100), unique=True)
    title = Column(String(500), nullable=False)
    media_type = Column(String(20))  # movie, series, episode
    production_year = Column(Integer)
    file_path = Column(String(1000))
    file_size = Column(BigInteger)
    resolution = Column(String(20))
    codec = Column(String(50))
    has_subtitle = Column(Boolean, default=False)
    subtitle_langs = Column(String(100))  # 逗号分隔
    # 海报相关
    tmdb_id = Column(Integer)
    has_poster = Column(Boolean, default=False)
    has_backdrop = Column(Boolean, default=False)
    poster_url = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class VideoAnnotation(Base):
    """
    视频文件级用户标注。
    主要用途：标记硬字幕（hardcoded subtitles）—— 烧录在画面里的字幕，
    没有独立轨道，ffprobe 探测不到，但实际"覆盖"了某种语言。

    标注后，字幕扫描会把 hardcoded_subtitle_langs 计入"已覆盖"，
    避免该视频被误判为缺字幕。
    """
    __tablename__ = "video_annotations"

    id = Column(Integer, primary_key=True, index=True)
    file_path = Column(String(1000), unique=True, nullable=False, index=True)
    # JSON 数组字符串，例 ["chs"] / ["chs","eng"]
    hardcoded_subtitle_langs = Column(Text)
    note = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MediaRating(Base):
    """
    评分聚合缓存。一行 = 一部影视的多家评分快照。

    主键策略：(tmdb_id, media_type) 唯一。同一个数字 ID 在 TMDB 内 movie 和 tv
    是两个不同的命名空间（电影 550 和剧集 550 是两个东西），所以必须连同 media_type 唯一。
    """
    __tablename__ = "media_ratings"

    id = Column(Integer, primary_key=True, index=True)

    # ----- 关键 ID -----
    tmdb_id = Column(Integer, nullable=False, index=True)
    media_type = Column(String(10), nullable=False)  # 'movie' / 'tv'
    imdb_id = Column(String(20), index=True)
    douban_id = Column(String(20), index=True)

    # ----- 标题（冗余但实用：避免单纯按评分查询时还要 join media_items）-----
    title = Column(String(500))
    year = Column(Integer)

    # ----- 评分主体（NULL = 该源没拿到 / 未拉过）-----
    imdb_rating = Column(Float)
    imdb_votes = Column(Integer)
    rt_critic = Column(Integer)         # Rotten Tomatoes 影评人 (0-100)
    rt_audience = Column(Integer)       # Rotten Tomatoes 观众 (0-100)
    metacritic = Column(Integer)        # Metacritic (0-100)
    trakt_rating = Column(Float)        # Trakt (0-10)
    letterboxd_rating = Column(Float)   # Letterboxd (0-5)
    douban_rating = Column(Float)       # 豆瓣 (0-10)
    douban_votes = Column(Integer)
    aggregate_score = Column(Integer)   # MDB List 综合分 (0-100)
    # TMDB 自家评分：discover 拉详情/列表时镜像写入；
    # 不独立 fetcher（TMDB detail 抓取的副产品），保留独立 fetched_at 仅作可观测性
    tmdb_rating = Column(Float)         # TMDB (0-10)
    tmdb_vote_count = Column(Integer)

    # ----- 缓存元信息（每家独立 TTL，避免一家拉过另一家就被强缓存）-----
    mdblist_fetched_at = Column(DateTime)
    douban_fetched_at = Column(DateTime)
    tmdb_fetched_at = Column(DateTime)
    # 留个原始响应字段，将来想多展示一项不用重新爬
    raw_mdblist = Column(Text)
    raw_douban = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint('tmdb_id', 'media_type', name='uq_media_ratings_tmdb_id_type'),
    )


class MediaMetadata(Base):
    """
    媒体元数据实体表（L3 长缓存）。一行 = 某外部 source 的某个 ID 的元数据快照。

    设计原则：
      - 主键：(source, source_id) 唯一。同一部影视可能在多个 source 都有行
        （TMDB / AniList / 豆瓣），通过 tmdb_id / imdb_id / anilist_id 三个桥接 ID 关联
      - 用户**不**直接 refresh（详见 PRD §4.3 三层数据哲学）
      - 系统维护：cache miss / stale TTL / 每日 LRU 清理
      - 评分独立走 media_ratings 表，本表不存评分（豆瓣行 ext 里有副本，但不是 SOT）

    详见 docs/2026-05-15-media-metadata-store.md
    """
    __tablename__ = "media_metadata"

    id = Column(BigInteger, primary_key=True, index=True)

    # ----- 自家 ID（强约束）-----
    source = Column(String(16), nullable=False)       # 'tmdb' / 'anilist' / 'douban'
    source_id = Column(String(32), nullable=False)    # 该 source 内的唯一 ID

    # ----- 跨 source 桥接 ID（弱约束，部分索引在 migration 里建）-----
    tmdb_id = Column(BigInteger)
    imdb_id = Column(String(16))
    anilist_id = Column(BigInteger)

    # ----- 高频列表查询字段（前端卡片直接用）-----
    media_type = Column(String(16))                   # 'movie' / 'tv' / 'anime'
    # title 永远保存英文标题（用于种子站搜索/跨源桥接锚点；PT 站点几乎都用英文归档）
    title = Column(String(512))
    # 中文标题（前端列表卡片首选展示；为空时 fallback 到 title）
    title_zh = Column(String(512))
    original_title = Column(String(512))              # 原始制作语言的标题（日文/韩文/中文/英文）
    year = Column(Integer)
    release_date = Column(String(20))                 # 'YYYY-MM-DD' / 'YYYY-MM' / 'YYYY'
    poster_url = Column(String(512))

    # ----- 详情字段（JSONB，按 source 字段约定见 PRD §1.2）-----
    ext = Column(JSONB)

    # ----- 生命周期 -----
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint('source', 'source_id', name='uq_media_metadata_source_id'),
        Index('ix_media_metadata_last_seen_at', 'last_seen_at'),
        # 跨 source 桥接的部分索引（仅 NOT NULL 项进索引，省空间）在 _ONESHOT_MIGRATIONS 建
    )


class AdultItem(Base):
    """成人内容"""
    __tablename__ = "adult_items"

    id = Column(Integer, primary_key=True, index=True)
    # 番号；未识别的文件 code 为 NULL（PostgreSQL 的 UNIQUE 允许多个 NULL）
    code = Column(String(50), unique=True, nullable=True)
    title = Column(String(500))
    release_date = Column(String(20))
    studio = Column(String(200))
    director = Column(String(200))
    actors = Column(Text)  # JSON 数组（女优日文名，按出演顺序）
    tags = Column(Text)  # JSON 数组
    cover_url = Column(String(500))
    poster_path = Column(String(500))
    nfo_path = Column(String(500))
    file_path = Column(String(1000))
    file_mtime = Column(Float)  # 文件 mtime；用于增量扫描跳过已扫文件
    # 手动覆盖"有/无码"判定：NULL = 走自动判定（_UNCENSORED_KEYWORDS）；True/False = 用户手动指定
    is_uncensored_override = Column(Boolean, nullable=True)
    rating = Column(Float)
    source = Column(String(50))  # javbus, javdb
    # 永久排除（用户主动标"这不是有效番号"）：所有自动流程跳过；只能用户手动取消
    excluded = Column(Boolean, default=False, nullable=False, index=True)
    # 自动冷却到期时间戳：自动流程连续失败 N 次设为 now + COOLDOWN_DAYS
    # 自动流程过滤条件：cooldown_until IS NULL OR cooldown_until < NOW()
    # 时间到了自动失效，无需用户介入；用户也可以手动"立即重试"清掉
    cooldown_until = Column(DateTime, index=True)
    # 连续失败次数：每次刮削失败 +1，成功 / 用户介入时重置为 0。达 N 次进入 cooldown
    scrape_attempts = Column(Integer, default=0, nullable=False)
    last_scrape_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AdultActress(Base):
    """女优档案。
    日文名是规范主键（站点 / NFO 都以日文名为锚），中/英文为本地化展示。
    年龄是由 birth_date 计算的派生属性（age），不入库 —— 否则随日期变化得每天 update。
    """
    __tablename__ = "adult_actresses"

    id = Column(Integer, primary_key=True, index=True)
    # 日文艺名：唯一索引；用作和 AdultItem.actors[] 中字符串匹配的锚
    jp_name = Column(String(128), unique=True, nullable=False, index=True)
    # 中文译名（可手填或从中文站点抓）；可能多个译名共存，先存最常见的一个
    zh_name = Column(String(128), index=True)
    # 英文罗马字名（如 "Tsukasa Aoi"）
    en_name = Column(String(128))
    # 别名列表（曾用名 / 不同站点拼写）；JSON 字符串数组
    aliases = Column(Text)
    # 头像 URL（落地路径或外链；本地化下载后存相对路径）
    avatar_url = Column(String(500))
    # 出生日期 YYYY-MM-DD；前端用来算 age = today.year - year - (mm-dd 是否过)
    birth_date = Column(String(20))
    # 出道日期 YYYY-MM-DD
    debut_date = Column(String(20))
    # 数据来源 / 解析状态：
    #   pending    —— 已发现待解析（discover 阶段插入的占位）
    #   not_found  —— 解析过但站点没找到，跳过重试
    #   javdb / javbus / manual …  —— 已成功解析，标记数据来源
    source = Column(String(50), index=True)
    # javdb 的 actor 永久 id（同一人不同名查回都是同一 id），用作跨名归一化锚
    javdb_id = Column(String(40), unique=True, index=True)
    # 备注：身高/三围/事务所等自由字段，不规范化
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def age(self) -> Optional[int]:
        """根据 birth_date 计算当前年龄；缺数据或解析失败返回 None。"""
        if not self.birth_date:
            return None
        try:
            from datetime import date
            y, m, d = (int(x) for x in self.birth_date.split('-'))
            today = date.today()
            return today.year - y - ((today.month, today.day) < (m, d))
        except Exception:
            return None


class DownloadDispatchMap(Base):
    """
    下载入库流水线映射表（核心调度表）。

    生命周期（phase 流转）：
      analyzing → dispatch_queued → downloading → download_done →
      copying → organizing → jellyfin_recognizing → jellyfin_recognize_done →
      subtitle_fetching → audio_track_order_adjusting → all_jobs_done →
      (软清/硬清) → cleaned
    侧分支：analyzing(needs_review) → dismissed → 复活回 dispatch_queued
    所有 phase 常量见 tools/dispatch/phases.py
    """
    __tablename__ = "download_dispatch_map"

    torrent_hash = Column(String(64), primary_key=True)

    # ---- 媒体识别 ----
    media_type = Column(String(20))            # movie / tv / anime / adult / unknown
    tmdb_id = Column(String(32))
    imdb_id = Column(String(32))
    series_tmdb_id = Column(String(32))        # 剧集才有，跨季归属
    series_name = Column(String(500))
    title = Column(String(500))
    year = Column(Integer)

    # ---- 目标 ----
    target_library_id = Column(String(64))
    target_root = Column(String(500))
    target_path = Column(String(1000))
    move_mode = Column(String(10), default='copy')  # copy / move
    dispatched_files = Column(JSONB)                # ["/library/.../S01E12.mkv", ...]

    # ---- 状态机（拆 phase + phase_status，比单字段 N 个并列状态清晰）----
    # phase 取值见 tools/dispatch/phases.py PHASE_*
    phase = Column(String(40), default='analyzing', index=True)
    phase_status = Column(String(30), default='running')
    # status: running / succeeded / failed / warned / skipped / needs_review
    #         + downloading 阶段专属：metadata_pending / stalled / paused
    status_message = Column(Text)
    error_log = Column(Text)
    # 各 phase 耗时（秒），便于审计
    phase_timings = Column(JSONB, default=dict)
    # copy phase 实时进度（大文件可能跑几十分钟）
    copy_bytes_done = Column(BigInteger, default=0)
    copy_bytes_total = Column(BigInteger, default=0)

    # ---- 配额清理决策用 ----
    ratio_at_dispatch = Column(Numeric(8, 2))
    last_seen_ratio = Column(Numeric(8, 2))
    seeded_seconds = Column(BigInteger, default=0)
    upload_bytes = Column(BigInteger, default=0)
    cleanup_eligible_at = Column(DateTime, index=True)

    # ---- 时间戳 ----
    created_at = Column(DateTime, default=datetime.utcnow)
    # phase 或 phase_status 任一变化时更新；sweeper 据此判定 zombie（卡住 30min+ running）
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    dispatched_at = Column(DateTime)         # 完成转移到库的时刻
    cleaned_at = Column(DateTime)            # NVMe 副本清理时刻
    # qB 那边记录的"加入 qB 的时间"。逻辑唯一身份 = (torrent_hash, qb_added_on)：
    # 同 hash 但 qB 重新加种（删了重加），qb_added_on 变化 → adopt 视为新任务重置
    qb_added_on = Column(DateTime, index=True)


class KvCache(Base):
    """通用 key-value 缓存表。
    用来持久化原本只在内存里的 TTL 缓存（库统计 / 剧集树形子节点 / TMDB 推荐等），
    避免后端重启 / uvicorn reload 把缓存全清。

    scope: 缓存命名空间（'lib_stats' / 'tree_seasons' / 'tmdb_trending' 等）
    key  : scope 内的唯一 key（字符串，由调用方拼）
    data : 序列化后的 JSON 文本
    cached_at: 写入时刻；TTL 由调用方传入，过期与否在读取时算
    """
    __tablename__ = "kv_cache"

    scope = Column(String(40), primary_key=True)
    key = Column(String(500), primary_key=True)
    data = Column(Text, nullable=False)
    cached_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class LLMClassifyCache(Base):
    """LLM 类型识别结果缓存（按 torrent_name + sorted(files) 指纹）。"""
    __tablename__ = "llm_classify_cache"

    fingerprint_hash = Column(String(64), primary_key=True)
    media_type = Column(String(20))
    title_native = Column(String(500))
    title_en = Column(String(500))
    title_zh = Column(String(500))
    year = Column(Integer)
    season = Column(Integer)
    episode = Column(Integer)
    confidence = Column(Numeric(3, 2))
    tmdb_search_hint = Column(String(500))
    raw_response = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


# 已迭代加入的字段，需要兼容旧数据库（PostgreSQL ALTER TABLE IF NOT EXISTS）
# 格式：(table_name, column_name, column_def_sql)
_SCHEMA_PATCHES = [
    ("media_items", "production_year", "INTEGER"),
    ("media_items", "tmdb_id", "INTEGER"),
    ("media_items", "has_poster", "BOOLEAN DEFAULT FALSE"),
    ("media_items", "has_backdrop", "BOOLEAN DEFAULT FALSE"),
    ("media_items", "poster_url", "VARCHAR(500)"),
    ("adult_items", "file_mtime", "DOUBLE PRECISION"),
    ("adult_items", "excluded", "BOOLEAN DEFAULT FALSE NOT NULL"),
    ("adult_items", "scrape_attempts", "INTEGER DEFAULT 0 NOT NULL"),
    ("adult_items", "last_scrape_at", "TIMESTAMP"),
    ("adult_items", "cooldown_until", "TIMESTAMP"),
    ("adult_items", "is_uncensored_override", "BOOLEAN"),
    ("tasks", "params", "TEXT"),
    ("actors", "image_source", "VARCHAR(20)"),
    # dispatch 流水线 v2 重构（2026-05-10）：sweeper 判定 zombie 用的"最后变更"时间戳
    ("download_dispatch_map", "updated_at",
     "TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'UTC')"),
    # qb_added_on：qB 那边的加种时间戳，用于 adopt 识别"同 hash 重新加种"
    ("download_dispatch_map", "qb_added_on", "TIMESTAMP"),
    # 用户认证
    ("users", "role", "VARCHAR(20) DEFAULT 'guest' NOT NULL"),
    # TMDB 自家评分镜像进 media_ratings（统一评分聚合表，避免 metadata LRU 清后丢评分）
    ("media_ratings", "tmdb_rating", "DOUBLE PRECISION"),
    ("media_ratings", "tmdb_vote_count", "INTEGER"),
    ("media_ratings", "tmdb_fetched_at", "TIMESTAMP"),
]


_COLUMN_NULLABLE_PATCHES = [
    # 把已建表的 NOT NULL 约束改为允许 NULL（用于"未识别文件也入库"等场景）
    ("adult_items", "code"),
]


def _apply_schema_patches():
    """对已存在的表追加新增列、放宽 NOT NULL 约束（幂等）。"""
    from sqlalchemy import text
    with engine.begin() as conn:
        for table, column, col_def in _SCHEMA_PATCHES:
            sql = f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_def}'
            try:
                conn.execute(text(sql))
            except Exception:
                # 忽略：表还没创建（首次启动 create_all 已经把所有列建好了）
                pass
        for table, column in _COLUMN_NULLABLE_PATCHES:
            try:
                conn.execute(text(f'ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL'))
            except Exception:
                pass


# ============================================================================
# 一次性迁移（schema_migrations 标记，跑过就跳过）
# ============================================================================

# 每条：(migration_id, [SQL 列表])
# 跑成功 → INSERT 标记到 schema_migrations，重启不会重复跑
# 跑失败 → 不写标记，下次重启再试
_ONESHOT_MIGRATIONS = [
    (
        "2026-05-15__media_metadata_title_zh",
        # 加 title_zh 列；同时 TRUNCATE 现有数据（语义变更：title 必须是英文）
        # 开发期遵循"开发阶段不维护历史数据"原则，直接清表让代码按新语义重写
        [
            "ALTER TABLE media_metadata ADD COLUMN IF NOT EXISTS title_zh VARCHAR(512)",
            "TRUNCATE TABLE media_metadata RESTART IDENTITY",
        ],
    ),
    (
        "2026-05-15__media_metadata_partial_indexes",
        # 给 media_metadata 的 3 个跨源桥接 ID 建部分索引（仅 NOT NULL 行进索引，省空间）
        # 部分索引 SQLAlchemy DDL 表达不干净，单独 SQL 写更稳。
        [
            "CREATE INDEX IF NOT EXISTS ix_media_metadata_tmdb_id "
            "  ON media_metadata (tmdb_id) WHERE tmdb_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS ix_media_metadata_imdb_id "
            "  ON media_metadata (imdb_id) WHERE imdb_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS ix_media_metadata_anilist_id "
            "  ON media_metadata (anilist_id) WHERE anilist_id IS NOT NULL",
        ],
    ),
    (
        "2026-05-10__pipeline_v2_phase_rename",
        # dispatch 流水线 v2：phase/status 全部重命名 + 字段扩宽 + 清理 DownloadTask 旧表
        # 旧 phase 值（analyze/copy/organize/jellyfin/done/subtitle/audio）跟新代码的
        # phase 常量不一致，sweeper 看不懂会忽略 → 直接 TRUNCATE 让流水线从空表起步。
        [
            # ① 删旧的 DownloadTask 表（已被 DownloadDispatchMap 完全替代）
            "DROP TABLE IF EXISTS download_tasks",

            # ② phase / phase_status 列扩宽（新值最长 ~40 字符）
            "ALTER TABLE download_dispatch_map "
            "ALTER COLUMN phase TYPE VARCHAR(40)",
            "ALTER TABLE download_dispatch_map "
            "ALTER COLUMN phase_status TYPE VARCHAR(30)",

            # ③ 清空 dispatch_map 表（旧 phase 值跟新常量不兼容；qB 上的种子下次 adopt 会重认）
            "TRUNCATE TABLE download_dispatch_map",
        ],
    ),
    (
        "2026-05-22__backfill_tmdb_rating_to_media_ratings",
        # TMDB 自家评分从 media_metadata.ext 迁到 media_ratings（统一评分聚合表）
        # 只回填 media_ratings 已有的 (tmdb_id, media_type) 行；新条目靠 discover 路径自动镜像
        [
            "UPDATE media_ratings r "
            "SET tmdb_rating = (mm.ext->>'vote_average')::float, "
            "    tmdb_vote_count = NULLIF(mm.ext->>'vote_count', '')::int, "
            "    tmdb_fetched_at = mm.updated_at "
            "FROM media_metadata mm "
            "WHERE mm.source = 'tmdb' "
            "  AND mm.source_id = r.tmdb_id::text "
            "  AND mm.media_type = r.media_type "
            "  AND mm.ext ? 'vote_average' "
            "  AND r.tmdb_rating IS NULL",
        ],
    ),
]


def _apply_oneshot_migrations():
    """跑一次性迁移：跑过就在 schema_migrations 表里记一笔，下次跳过。"""
    import logging
    from sqlalchemy import text
    logger = logging.getLogger(__name__)

    with engine.begin() as conn:
        # 确保版本表存在
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id VARCHAR(200) PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'UTC')
            )
        """))

        # 拉已应用的迁移 id
        applied = {
            r[0] for r in conn.execute(text("SELECT id FROM schema_migrations")).fetchall()
        }

        pending = [(mid, sqls) for mid, sqls in _ONESHOT_MIGRATIONS if mid not in applied]
        if not pending:
            logger.info(f"schema migrations: 全部已应用（{len(applied)} 条），无待跑")
            return

        logger.info(f"schema migrations: {len(pending)} 条待跑，{len(applied)} 条已应用")
        for mig_id, sql_list in pending:
            try:
                for sql in sql_list:
                    logger.info(f"  [migration {mig_id}] EXEC: {sql[:120]}")
                    conn.execute(text(sql))
                conn.execute(
                    text("INSERT INTO schema_migrations (id) VALUES (:id)"),
                    {"id": mig_id},
                )
                logger.info(f"✓ [migration] applied: {mig_id}")
            except Exception as e:
                logger.error(f"✗ [migration] FAILED: {mig_id} → {e}")
                raise


def init_db():
    """初始化数据库：一次性迁移 → 建表 → 应用新增列（顺序敏感）。

    注意：一次性迁移先跑（含 TRUNCATE / DROP），再 create_all 让新增表/列补齐。
    """
    _apply_oneshot_migrations()
    Base.metadata.create_all(bind=engine)
    _apply_schema_patches()
