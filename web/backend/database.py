"""
数据库模块
PostgreSQL + SQLAlchemy
"""
from datetime import datetime
from typing import Optional
from pathlib import Path

from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Text, Boolean, Float,
    BigInteger, UniqueConstraint, Index,
)
from sqlalchemy.orm import sessionmaker, declarative_base

from web.backend.config import settings


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


class DownloadTask(Base):
    """下载任务"""
    __tablename__ = "download_tasks"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(500), nullable=False)
    source = Column(String(50))  # jackett, manual
    magnet_link = Column(Text)
    torrent_hash = Column(String(100))
    status = Column(String(20), default="pending")  # pending, downloading, completed, failed
    progress = Column(Float, default=0.0)
    download_path = Column(String(1000))
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)


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

    # ----- 缓存元信息（每家独立 TTL，避免一家拉过另一家就被强缓存）-----
    mdblist_fetched_at = Column(DateTime)
    douban_fetched_at = Column(DateTime)
    # 留个原始响应字段，将来想多展示一项不用重新爬
    raw_mdblist = Column(Text)
    raw_douban = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint('tmdb_id', 'media_type', name='uq_media_ratings_tmdb_id_type'),
    )


class AdultItem(Base):
    """成人内容"""
    __tablename__ = "adult_items"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, nullable=False)  # 番号
    title = Column(String(500))
    release_date = Column(String(20))
    studio = Column(String(200))
    director = Column(String(200))
    actors = Column(Text)  # JSON 数组
    tags = Column(Text)  # JSON 数组
    cover_url = Column(String(500))
    poster_path = Column(String(500))
    nfo_path = Column(String(500))
    file_path = Column(String(1000))
    file_mtime = Column(Float)  # 文件 mtime；用于增量扫描跳过已扫文件
    rating = Column(Float)
    source = Column(String(50))  # javbus, javdb
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# 已迭代加入的字段，需要兼容旧数据库（PostgreSQL ALTER TABLE IF NOT EXISTS）
# 格式：(table_name, column_name, column_def_sql)
_SCHEMA_PATCHES = [
    ("media_items", "production_year", "INTEGER"),
    ("media_items", "tmdb_id", "INTEGER"),
    ("media_items", "has_poster", "BOOLEAN DEFAULT FALSE"),
    ("media_items", "has_backdrop", "BOOLEAN DEFAULT FALSE"),
    ("media_items", "poster_url", "VARCHAR(500)"),
    ("adult_items", "file_mtime", "DOUBLE PRECISION"),
    ("tasks", "params", "TEXT"),
    ("actors", "image_source", "VARCHAR(20)"),
]


def _apply_schema_patches():
    """对已存在的表追加新增列（幂等）。"""
    from sqlalchemy import text
    with engine.begin() as conn:
        for table, column, col_def in _SCHEMA_PATCHES:
            sql = f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_def}'
            try:
                conn.execute(text(sql))
            except Exception:
                # 忽略：表还没创建（首次启动 create_all 已经把所有列建好了）
                pass


def init_db():
    """初始化数据库：建表 + 应用新增列。"""
    Base.metadata.create_all(bind=engine)
    _apply_schema_patches()
