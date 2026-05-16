# 媒体元数据实体表 — 实施 PRD

> 生成日期：2026-05-15
> 修订日期：2026-05-15（增补：刷新策略与边界 §4.3、成人库处理边界 §1.4）
> 状态：方案待 review 后开工
> 触发：用户讨论 trending KV 缓存升级为"实体级长缓存"
> 关联：[2026-05-11 评分系统 review](2026-05-11-ratings-system-review.md)、`web/backend/api/discover.py`、`web/backend/api/ratings.py`、`web/backend/api/adult.py`

---

## 0. 背景与目标

### 现状
- `kv_cache` 按"查询参数"做 key，把 trending/list 响应整段 JSON 落表。
  - **同一部影视在多个查询里被重复存**（Trakt trending、TMDB popular、AniList trending 都缓存了完整 item）
  - 命中粒度是"整个查询"，单条 item 过期没法独立刷新
- 评分单独走 `media_ratings`，独立 TTL，工作良好（5/15 修复后真正生效）。
- 豆瓣 detail 走 `_DOUBAN_DETAIL_SCOPE` KV 30 天缓存（海报、简介、cast）。
- TMDB / AniList 详情走 30 分钟～几小时 KV，时间一过整个详情重抓。

### 痛点
1. **存储冗余**：同一部影视的元数据可能在 KV 里出现 5+ 份（trending / popular / top_rated / current_season / ...）。
2. **缓存粒度粗**：用户在详情页用了 30 天的版本，列表页 30 分钟 KV 过期重抓一次完全没必要。
3. **跨 source 不复用**：豆瓣源拿到的 imdb_id，下次 TMDB 列表再遇到同片不能秒出。
4. **每天都要再去一次 TMDB / 豆瓣**：API 配额 / 反爬压力没必要这么大。

### 目标
1. 建立 `media_metadata` 实体表，按 `(source, source_id)` 为单位长期存储。
2. 不替换 KV 缓存，而是**两层并存**：
   - L1 KV：缓存"查询 → ID 列表 + 排序信号"（trending 排名）
   - L2 实体表：缓存"ID → 元数据"（长 TTL）
3. 三个外部 ID（tmdb_id / imdb_id / anilist_id）建索引，跨 source 复用桥接。
4. 配置化：豆瓣字段范围 + LRU 周期。
5. 默认 365 天 LRU 清理：稳态 ~180 MB，永不增长。

### 非目标
- **不改 `media_ratings`**：评分继续独立走，它的 30 天 TTL + missing 重试逻辑已经够用。
- **不替换 KV 缓存**：trending 排名、Trakt 实时信号、片单顺序仍走 KV，因为这些跟"实体属性"无关。
- **不做演员维度的反向查询**（"某演员的所有片"）：现阶段无需求。
- **不存 backdrop/海报二进制**：所有图都存 URL，前端走 `/api/img-proxy` 代理。

---

## 1. 表结构

### 1.1 SQLAlchemy 模型（加在 `web/backend/database.py`）

```python
class MediaMetadata(Base):
    """
    媒体元数据实体表。一行 = 某个外部 source 的某个 ID 在一段时间内的元数据快照。

    主键策略：(source, source_id) 唯一。同一部影视可能存在多个 source 行
    （TMDB / AniList / 豆瓣），通过 imdb_id 等桥接 ID 互相关联。
    """
    __tablename__ = "media_metadata"

    id = Column(BigInteger, primary_key=True, index=True)

    # ----- 自家 ID（强约束）-----
    source = Column(String(16), nullable=False)      # 'tmdb' / 'anilist' / 'douban'
    source_id = Column(String(32), nullable=False)   # 该 source 内的唯一 ID

    # ----- 跨 source 桥接 ID（弱约束，建部分索引）-----
    tmdb_id = Column(BigInteger)
    imdb_id = Column(String(16))
    anilist_id = Column(BigInteger)

    # ----- 高频列表查询字段（前端卡片直接用）-----
    media_type = Column(String(16))           # 'movie' / 'tv' / 'anime'
    title = Column(String(512))               # 显示用（zh-CN 优先）
    original_title = Column(String(512))
    year = Column(Integer)                    # 冗余但便于排序/筛选
    release_date = Column(String(20))         # ISO 'YYYY-MM-DD' / 'YYYY-MM' / 'YYYY'
    poster_url = Column(String(512))

    # ----- 详情字段（JSONB 存可变结构，不上索引）-----
    ext = Column(JSONB)

    # ----- 生命周期 -----
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint('source', 'source_id', name='uq_media_metadata_source_id'),
        # 跨 source 桥接：用部分索引省空间
        Index('ix_media_metadata_tmdb_id', 'tmdb_id',
              postgresql_where=Column('tmdb_id').isnot(None)),
        Index('ix_media_metadata_imdb_id', 'imdb_id',
              postgresql_where=Column('imdb_id').isnot(None)),
        Index('ix_media_metadata_anilist_id', 'anilist_id',
              postgresql_where=Column('anilist_id').isnot(None)),
        # LRU 清理扫描用
        Index('ix_media_metadata_last_seen_at', 'last_seen_at'),
    )
```

### 1.2 ext JSONB 字段约定（按 source 区分）

**TMDB 行**：
```jsonc
{
  "overview": "...",
  "tagline": "...",
  "original_language": "en",
  "english_title": "...",
  "runtime": 137,                  // 电影分钟
  "episode_runtime": [50, 55],      // 剧集每集分钟数（数组）
  "status": "Released",
  "backdrop_url": "...",
  "homepage": "...",
  "countries": ["United States"],
  "spoken_languages": ["English"],
  "studios": ["Studio A", "Studio B"],   // 最多 5
  "genres": ["Action", "Drama"],
  "directors": ["..."],
  "writers": ["..."],
  "cast": [                              // 最多 10
    {"name": "...", "character": "...", "profile_url": "..."}
  ],
  "videos": [                            // trailers
    {"key": "...", "type": "Trailer", "name": "...", "official": true}
  ],
  // 剧集独有
  "number_of_seasons": 8,
  "number_of_episodes": 73,
  "seasons": [
    {"season_number": 1, "name": "...", "air_date": "...",
     "episode_count": 10, "poster_url": "...", "overview": "..."}
  ]
}
```

**AniList 行**：
```jsonc
{
  "title_romaji": "...",
  "title_native": "...",
  "idMal": 11061,
  "description": "...",
  "banner_image": "...",
  "season": "SPRING",
  "season_year": 2013,
  "episodes": 25,
  "duration": 24,
  "format": "TV",
  "status": "FINISHED",
  "source": "MANGA",
  "country_of_origin": "JP",
  "average_score": 86,
  "popularity": 1234567,
  "favourites": 200000,
  "genres": ["Action", "Adventure"],
  "tags": [{"name": "Magic", "rank": 95}],   // 简化
  "studios": ["Wit Studio"],
  "trailer": {"id": "...", "site": "youtube"},
  "external_links": [{"site": "Crunchyroll", "url": "..."}],
  "characters": [                            // 最多 12
    {"name": "...", "image": "...", "role": "MAIN",
     "voice_actor": {"name": "...", "image": "..."}}
  ],
  "relations": [                             // 简化
    {"type": "SEQUEL", "anilist_id": ..., "title": "...", "cover": "..."}
  ]
}
```

**豆瓣行**（`store_douban_full=true` 时）：
```jsonc
{
  "summary": "...",                  // 剧情简介
  "countries": ["中国大陆"],
  "languages": ["汉语普通话"],
  "genres": ["剧情", "喜剧"],
  "duration": "132分钟",
  "director": "姜文",
  "cast": ["姜文", "葛优", "周润发"],    // 简化为字符串数组
  "rating": 9.0,                       // 副本：MediaRating 仍是 source of truth
  "votes": 1922179
}
```

**豆瓣行**（`store_douban_full=false` 时）只存事实字段，不存 summary/poster_url/cast/director：
```jsonc
{
  "countries": ["中国大陆"],
  "languages": ["汉语普通话"],
  "genres": ["剧情", "喜剧"],
  "duration": "132分钟"
}
```

> 注：豆瓣 `poster_url` 默认存在 `public.poster_url` 列里；`store_douban_full=false` 时不写该列。

### 1.3 字段长度说明
- `title` / `original_title` 512：覆盖中日韩多语言标题拼接。
- `poster_url` / `backdrop_url` 512：TMDB CDN 路径较长，留余量。
- `source_id` 32：豆瓣最长 ~9 位、TMDB ~8 位、AniList ~7 位。
- `imdb_id` 16：`tt` + 11 位数字 + 余量。

### 1.4 成人库的处理边界（adult_items / adult_actresses）

**结论：成人库不纳入 `media_metadata` 表，物理分离，逻辑哲学一致。**

#### 1.4.1 为什么不纳入

| 维度 | discover 类（TMDB/AniList/豆瓣） | 成人库（adult） |
|---|---|---|
| **触发模型** | 用户浏览发现 → 拉远端元数据 | 本地文件扫描 → 识别番号 → 刮远端元数据 |
| **生命周期** | 跟本地文件无关；30 天 TTL + 365 天 LRU | 跟本地文件强绑定；文件删 = 行删 |
| **写入字段** | 仅外部元数据 | 元数据 + `file_path` / `file_mtime` / `excluded` / `cooldown_until` / `scrape_attempts` 等本地状态 |
| **唯一标识** | `(source, source_id)` | `code`（番号，跨刮削源通用） |
| **多 source 关系** | TMDB / AniList / 豆瓣 行可并存 | 多刮削器（javbus/javdb）合并写**一行**，不分行存 |
| **用户主动 refresh** | ❌ 不允许（L3 哲学） | ✅ 允许（`POST /items/{id}/rescrape`，错番号必须能立刻重刮） |

成人库本质是"**本地媒体管理**"，不是"**内容发现**"。它有自己的实体表语义（`adult_items` 就是它的实体表），强行合并到 `media_metadata` 会让两边都被污染：
- `media_metadata` 不得不加 `file_path` / `excluded` 等本地状态列
- 或把本地状态留在 `adult_items`，元数据搬到 `media_metadata`，产生跨表事务复杂度
- 用户主动 rescrape 又违背了 L3 不暴露刷新的设计

#### 1.4.2 一致的哲学（成人库的隐式 L3 行为）

虽然物理上分离，但成人库的元数据缓存策略和本 PRD 的"L3 不暴露用户 refresh"哲学**部分一致**：

| 策略 | discover 类 L3 | 成人库 |
|---|---|---|
| 元数据来源 | 上游 API | 刮削器（javbus / javdb / javlibrary / missav / avbase） |
| 失败重试 | 后台 stale refresh worker | `cooldown_until` + `scrape_attempts`（连续失败 N 次进冷却） |
| 永久排除 | 不需要 | `excluded=true` 字段（用户主动标"非有效番号"） |
| 用户纠错入口 | L1 KV refresh 顺带 | **`POST /items/{id}/rescrape` 例外保留**（本地媒体管理刚需） |
| LRU 清理 | 365 天 last_seen_at | 文件删除时联动删行（`adult_watcher` 已实现） |

#### 1.4.3 不本次改造的范围

明确**不在本次 PRD 范围**：
- ❌ `adult_items` 表不改字段、不拆表
- ❌ `adult_actresses` 表保持不动
- ❌ `rescrape` / 刮削管理 API 全部保留行为
- ❌ 现有 `adult_watcher` 后台扫描逻辑不动

**唯一可考虑的小协同**（可选，工时 +1h）：
- 成人库的女优档案 `adult_actresses` 里如果有"跨作品复用"诉求（同一女优出现在 50 部片里只刮 1 次资料），现状已经是按 `jp_name` 唯一约束做的——本身就是个"实体表"，无需迁移
- 如果将来想做"演员维度搜索"，是单独的 PRD，不在本次

#### 1.4.4 跨库行为差异表（一目了然）

| 行为 | discover (L3) | adult |
|---|---|---|
| 数据表 | `media_metadata` | `adult_items` + `adult_actresses` |
| 主键 | `(source, source_id)` | `code` |
| 用户 refresh | ❌ | ✅ `rescrape` 端点 |
| 系统 stale TTL | 30 天 | 无明确 TTL（依赖文件 mtime + cooldown） |
| LRU | 365 天 last_seen_at | 文件删除时联动 |
| 错误重试 | 后台 sweep | cooldown_until + scrape_attempts |

---

## 2. 配置项

### 2.1 yaml + Settings 字段（落在 `web/backend/config.py`）

```yaml
# config.yaml
metadata:
  # 是否存豆瓣的非事实字段（简介 / 海报 / 演员）
  # true:  跟 TMDB/AniList 等价（默认）
  # false: 只存事实字段，简介/海报/演员仍留在 30 天 KV 缓存
  # 改 false 的场景：项目要开源 / 担心豆瓣 ToS / 想省存储
  store_douban_full: true

  # LRU 清理阈值（天）：last_seen_at 超过该天数的行会被定时任务清掉
  # 365 = 一年没访问过的条目才会清；0 = 永不清理
  lru_keep_days: 365

  # 元数据 refresh TTL（天）：updated_at 超过该天数会标记 stale → 后台异步 refresh
  # 这是"长期保留 + 偶尔纠错"机制：行还在，去上游拿新数据覆盖
  refresh_ttl_days: 30
```

```python
# config.py 增加（用现有 _yaml_config.get 风格）
class MetadataSettings:
    store_douban_full: bool = _yaml_config.get('metadata', {}).get('store_douban_full', True)
    lru_keep_days: int = _yaml_config.get('metadata', {}).get('lru_keep_days', 365)
    refresh_ttl_days: int = _yaml_config.get('metadata', {}).get('refresh_ttl_days', 30)

settings.metadata = MetadataSettings()
```

### 2.2 配置项语义
- `lru_keep_days = 0` → 永不清理（库会持续增长，需自行 monitor）
- `lru_keep_days < refresh_ttl_days` 没意义但允许：等价于"过期了就直接删，从不 refresh"
- 切换 `store_douban_full` 不会回填已有行；新写入 / refresh 时按当前配置写

---

## 3. 模块设计

### 3.1 新增 `web/backend/services/metadata_store.py`

统一封装"读 → 命中返回 / 未命中下游 → 写入"逻辑：

```python
class MetadataStore:
    """实体元数据读写门面。

    使用方：discover.py、details endpoint、ratings.py 中需要 imdb_id 桥接的地方。
    线程安全：每次操作独立 SessionLocal。
    """

    # ---- 读 ----

    def get_by_source(
        self, source: str, source_id: str,
    ) -> Optional[MediaMetadata]:
        """精确按 (source, source_id) 查；命中刷新 last_seen_at。"""

    def get_by_tmdb(
        self, tmdb_id: int, media_type: str,
    ) -> Optional[MediaMetadata]:
        """按 tmdb_id 找 source='tmdb' 行（带 media_type 区分电影/剧集 namespace）。"""

    def get_by_imdb(self, imdb_id: str) -> List[MediaMetadata]:
        """imdb_id 反查全部 source 行（同一部片可能 tmdb 和 douban 都有行）。"""

    def get_batch(
        self, keys: List[Tuple[str, str]],
    ) -> Dict[Tuple[str, str], MediaMetadata]:
        """批量读：list 端点专用。一次 SQL，去重，返回 dict。
        命中的行 last_seen_at 一并 UPDATE，避免多次 round-trip。"""

    # ---- 写 ----

    def upsert(
        self, source: str, source_id: str, data: dict,
        bridge_ids: Optional[dict] = None,    # {imdb_id, tmdb_id, anilist_id}
    ) -> MediaMetadata:
        """ON CONFLICT (source, source_id) DO UPDATE。
        每次写都刷新 updated_at + last_seen_at。
        data 里区分公共字段（写公共列）和 ext 字段（写 JSONB）。"""

    # ---- 状态 ----

    def is_stale(self, row: MediaMetadata) -> bool:
        """updated_at 超过 refresh_ttl_days 天 → 应触发后台 refresh。"""

    def needs_refresh(
        self, source: str, source_id: str,
    ) -> Tuple[bool, Optional[MediaMetadata]]:
        """组合查询：(行不存在 OR 行 stale, 现有行)"""
```

### 3.2 改造点列表

| # | 文件 | 函数 | 改造 |
|---|---|---|---|
| 1 | `discover.py` | `_normalize_tmdb()` | list item → upsert 到 `media_metadata` |
| 2 | `discover.py` | `get_trending` / `get_list` / `get_trakt` | 调上游前先 `get_batch`，命中跳过补全；不命中的进上游+upsert |
| 3 | `discover.py` | `get_anilist` | item.to_dict() → upsert |
| 4 | `discover.py` | `get_detail` (TMDB) | 命中直接返回 ext 合成响应；stale 时返回旧数据 + 后台异步 refresh |
| 5 | `discover.py` | `get_anilist_detail` | 同上 |
| 6 | `discover.py` | `get_douban_lists` / 豆瓣 detail enrich | 命中直接返回；豆瓣 worker 写入时按 `store_douban_full` 配置裁剪字段 |
| 7 | `ratings.py` | `_fetch_mdblist_sync` | 同步取到的 imdb_id 一并 upsert（让评分系统也喂养实体表） |
| 8 | `ratings.py` | 豆瓣 worker `_process_one` | 命中后写入豆瓣行（imdb_id 桥接给跨源复用） |

### 3.3 LRU 清理 job

新增 `web/backend/services/metadata_lru.py`，注册到现有的 dispatch scheduler（已有每日跑的位置）：

```python
def cleanup_metadata_lru():
    """每天跑一次：删除 last_seen_at < now - lru_keep_days 天的行。"""
    if settings.metadata.lru_keep_days <= 0:
        return   # 0 表示永不清理
    cutoff = datetime.utcnow() - timedelta(days=settings.metadata.lru_keep_days)
    with SessionLocal() as db:
        deleted = db.query(MediaMetadata).filter(
            MediaMetadata.last_seen_at < cutoff
        ).delete(synchronize_session=False)
        db.commit()
        logger.info(f"metadata LRU: 清理 {deleted} 行（cutoff={cutoff.isoformat()}）")
```

挂到 dispatch scheduler 的 daily slot（参考 `tools/dispatch/scheduler.py` 现有的 sweeper 位置）。

### 3.4 后台 refresh 机制

stale 行不在用户请求路径上直接 refresh（会增延迟）。改用：

1. 用户读时：命中 stale 行 → 立即返回旧数据 → 入队后台 refresh
2. 后台 worker：单线程，按 source 串行（TMDB / AniList 各一个 worker，豆瓣继续用现有的）
3. refresh 完写回，下次读就是 fresh

worker 复用现有 `_DoubanWorker` / `_MDBListWorker` 模式（在 `ratings.py` 里），新增对等的 `_MetadataRefreshWorker`。

---

## 4. 查询流程示例

### 4.1 trending 列表（hot path）

```
GET /api/discover/trending?media_type=all&time_window=week&page=1
  ↓
1. _kv_get(_TMDB_TRENDING_SCOPE, "all:week:p1", ttl=30min)
   - 命中 → 拿到 item 列表（含 tmdb_id 排序信号）
   - 未命中 → 调 TMDB API → 落 KV
  ↓
2. tmdb_ids = [it.tmdb_id for it in items]
  ↓
3. metadata = store.get_batch([('tmdb', str(tid)) for tid in tmdb_ids])
  ↓
4. for it in items:
     md = metadata.get(('tmdb', str(it['tmdb_id'])))
     if md and not store.is_stale(md):
         it.update(md.to_card_dict())   # 实体表的标题/海报/genres 覆盖 KV 的
     else:
         store.upsert('tmdb', str(it['tmdb_id']), it, bridge_ids={'tmdb_id': it['tmdb_id']})
         if md and store.is_stale(md):
             enqueue_refresh('tmdb', tmdb_id=it['tmdb_id'], media_type=it['media_type'])
  ↓
5. 返回 items
```

### 4.2 详情页（TMDB）

```
GET /api/discover/detail?tmdb_id=550&media_type=movie
  ↓
1. md = store.get_by_tmdb(550, 'movie')
   - 命中 fresh → 直接合成响应返回（包含 ext 里的 cast/seasons/videos）
   - 命中 stale → 返回旧数据 + enqueue_refresh
   - 不命中 → 调 TMDB detail API → upsert → 返回
```

### 4.3 刷新策略与边界（核心设计哲学）

**三层数据 + 三种刷新**：

```
L1: KV cache（短缓存）
    存什么：trending 排名、列表分页、Trakt 实时信号
    特性：易变，TTL 分钟～小时
    用户可刷新：✅  GET /api/discover/...?refresh=true

L2: media_ratings（动态指标）
    存什么：IMDb / RT / Metacritic / 豆瓣评分票数
    特性：现实中真的变（票数天天涨）
    用户可刷新：✅  POST /api/ratings/{tmdb_id}/refresh

L3: media_metadata（事实库，本次新增）
    存什么：标题、原文标题、海报、演员、简介、类型
    特性：几乎不变，偶尔修订
    用户可刷新：❌  完全由系统策略管理
```

**L3 不暴露用户主动 refresh 端点的理由**：

1. **共享性**：实体表是全用户共享的事实库。单个用户点击不应允许"用错误数据覆盖正确数据"（例如豆瓣临时返 PoW，正确条目被错条目覆盖）。
2. **source of truth 在上游**：错就是错，靠 30 天 TTL 自然纠正；用户力气没有用武之地。
3. **配额保护**：详情页按钮不应每次都打 TMDB / 豆瓣 / MDB List。
4. **UX 完整性不丢**：用户点"刷新"调 L1 KV refresh，**同时顺带触发 L3 stale check 异步入队**——用户感知"刷新生效了"，新评分 / 新排名秒出，metadata 后台默默 refresh。

**端点收敛**：

| 端点 | 层 | 行为 | 状态 |
|---|---|---|---|
| `GET /api/discover/...?refresh=true` | L1 | 清 KV + 走上游 + 顺带 L3 stale check 入队 | ✅ 现有，保留 |
| `POST /api/ratings/{tmdb_id}/refresh` | L2 | 同步刷评分，绕 TTL，走节流 | ✅ 现有，保留 |
| ~~`POST /api/discover/detail/{id}/refresh`~~ | L3 | metadata 主动 refresh | ❌ **不实现** |
| ~~`POST /api/metadata/{source}/{id}/refresh`~~ | L3 | metadata 主动 refresh | ❌ **不实现** |

**L3 的实际更新路径（仅 3 条）**：

1. **被动 cache miss**：用户读到不存在的 (source, source_id) → 上游 → upsert（同步）
2. **被动 stale refresh**：用户读到 stale 行 → 立刻返回旧数据 → enqueue 后台刷新（异步）
3. **每日后台 sweep**：扫 `updated_at < now - 30天 AND last_seen_at > now - 7天` 的"近期被访问但已 stale"行，批量进刷新队列

**L3 的错误纠正路径**：

| 错误来源 | 纠正方式 |
|---|---|
| 上游临时返错 | 30 天 TTL 自然过期 → 后台 refresh 拿到正确数据 |
| 用户在 L1 触发 refresh | 顺带把对应行的 stale check 推早（如果命中 stale → 立即入队） |
| 库里看到明显错误（开发者视角） | 直接 `DELETE FROM media_metadata WHERE source=... AND source_id=...`，下次自然重抓 |

**L1 refresh 流程示例**（用户感知唯一入口）：

```
GET /api/discover/detail?tmdb_id=550&media_type=movie&refresh=true
  ↓
1. _kv_del(_TMDB_DETAIL_SCOPE, key)            — 清 L1
2. 调 TMDB detail API                            — 走上游
3. _kv_set(...)                                 — 回填 L1
4. store.upsert(...)                            — 顺带更新 L3 metadata
5. 检查 ratings stale → 入队（不绕 ratings 节流） — L2 异步
6. 返回响应
```

L1 refresh 是 L3 实体表唯一的"用户感知触发"路径。

---

### 4.4 豆瓣 worker 命中后写入

```
DoubanWorker._process_one():
    ...
    if douban_id and dr:
        # 写 MediaRating（现状不变）
        ...
        # 新增：upsert media_metadata
        ext = {
            'countries': ..., 'languages': ..., 'genres': ...,
            'duration': ...,
        }
        if settings.metadata.store_douban_full:
            ext.update({
                'summary': summary,
                'director': director,
                'cast': cast,            # 字符串数组
                'rating': dr.rating, 'votes': dr.votes,
            })
        public = {
            'media_type': media_type, 'title': title, 'year': year,
            'release_date': release_date,
        }
        if settings.metadata.store_douban_full:
            public['poster_url'] = poster_url
        store.upsert('douban', douban_id,
                     data={**public, 'ext': ext},
                     bridge_ids={'imdb_id': imdb_id_from_douban_page})
```

---

## 5. 迁移步骤

### 5.1 schema 落表（一次性迁移）

在 `_ONESHOT_MIGRATIONS` 加：

```python
(
    "2026-05-15__media_metadata_init",
    [
        # 表结构由 SQLAlchemy create_all 创建；这里只补"额外的运行时索引/约束"
        # （create_all 不会建 partial index，所以放这里）
        "CREATE INDEX IF NOT EXISTS ix_media_metadata_tmdb_id "
        "  ON media_metadata (tmdb_id) WHERE tmdb_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS ix_media_metadata_imdb_id "
        "  ON media_metadata (imdb_id) WHERE imdb_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS ix_media_metadata_anilist_id "
        "  ON media_metadata (anilist_id) WHERE anilist_id IS NOT NULL",
    ],
),
```

> SQLAlchemy `create_all` 会先建表 + UNIQUE + 普通 Index，但部分索引（带 WHERE）SA 表达不太干净，单独 SQL 写更稳。

### 5.2 不需要 backfill
- 不从 KV 缓存反向迁移现有 item 到实体表——让用户访问时自然填充
- KV 缓存现状原封不动，所以即便实体表为空也不影响功能

### 5.3 分阶段上线

按依赖关系切：

**Phase 1：基础设施（半天）**
- 加表 + 迁移
- 写 `MetadataStore` + 单元测试（mock SessionLocal）
- 加配置项 + Settings

**Phase 2：写入路径（半天）**
- discover list 5 个端点改成"上游响应 → upsert"（写入但还不读）
- ratings.py 的 MDB List / 豆瓣 worker 也加 upsert
- 上线观察：DB 行数增长正常？字段没乱进 ext？

**Phase 3：读取路径（半天）**
- discover list 5 个端点改成"先 get_batch → 命中跳过上游"
- detail 端点改成"先查实体表"
- 监控：上游 API 调用频率应明显下降

**Phase 4：LRU + refresh worker（半天）**
- 加 `cleanup_metadata_lru` + 注册到 scheduler
- 加 `_MetadataRefreshWorker` + stale 后台刷新
- 跑一次手动 cleanup 验证不误删

---

## 6. 空间预算

### 6.1 单行字节

| 部分 | TMDB 行 | AniList 行 | 豆瓣行(full) | 豆瓣行(facts only) |
|---|---|---|---|---|
| 公共列 | ~450 B | ~400 B | ~400 B | ~250 B |
| ext JSONB 原始 | 4-6 KB | 4-7 KB | 1.5-2 KB | ~150 B |
| ext 压缩(TOAST) | 2-3 KB | 2.5-3.5 KB | ~800 B | 不 TOAST |
| **行总占用** | **~3 KB** | **~3.5 KB** | **~1.2 KB** | **~400 B** |

### 6.2 索引

主键 + 3 个部分索引 + last_seen_at 索引 ≈ 4 × 80 B/行 = **~320 B/行**

### 6.3 库规模（按当前使用量）

假设：日活 ~100 TMDB + 50 AniList + 30 豆瓣，去重后净新增 ~100 行 / 日。

| 时间窗口 | 行数 | 总占用（full）| 总占用（facts only）|
|---|---|---|---|
| 1 个月 | 3,000 | ~12 MB | ~10 MB |
| 6 个月（无清理）| 18,000 | ~70 MB | ~55 MB |
| 1 年（无清理）| 36,000 | **~180 MB** | ~150 MB |
| 3 年（无清理）| 108,000 | ~540 MB | ~450 MB |
| **3 年 + 365 天 LRU（稳态）** | 36,000 | **~180 MB** | ~150 MB |

PostgreSQL 完全在舒适区。

### 6.4 与现状对比
- 当前 `kv_cache` 表中 trending/list 重复存了大量同一 item 的完整字段
- 新方案可能让**总 DB 占用反而下降**（因为去重）

---

## 7. 风险与回退

### 7.1 风险点

| 风险 | 影响 | 缓解 |
|---|---|---|
| upsert 写并发冲突 | 极少（同 (source, source_id) 并发请求很少） | ON CONFLICT DO UPDATE 原子，PG 会序列化 |
| TMDB 字段改名 | ext 里 schema 漂移 | ext 用 JSONB，字段缺失不影响其它行；写入侧对未知字段透传 |
| LRU 清理跑歪误删 | 用户收藏被清 | 收藏功能（如有）单独存 `media_items`，不依赖 `media_metadata`；LRU 只查 last_seen_at |
| 实体表读慢于 KV | unlikely（PG B-tree 索引 50µs） | 实测，超过 2ms 回 KV 优先 |
| stale 后台 refresh worker 卡住 | refresh 不进行，实体永远 stale 但还可用 | worker 自带 timeout + 异常退出重启逻辑（复用 douban worker 模式） |
| 配置 `store_douban_full=false` 后已存的 full 行还在 | 已存的不变（已经存了的就存了），新写按配置 | 文档说明；用户要彻底切就手动 UPDATE |

### 7.2 回退方案

如果 Phase 3（读路径）上线后发现回归：
1. 把读路径 feature flag 关掉（不读实体表，回 KV 原路径）
2. Phase 2 的写路径保留运行（不影响功能，仅消耗少量写 IO）
3. 用户透明，无数据丢失

完全回退：把 `metadata` 配置项整组关闭 + DROP TABLE media_metadata。无残留依赖。

---

## 8. 测试要点

### 8.1 单元测试

- `MetadataStore.upsert`：首次插入 / 二次更新（updated_at 推进）/ ON CONFLICT 路径
- `MetadataStore.get_batch`：去重、命中刷新 last_seen_at、部分命中
- `is_stale`：边界（恰好 30 天，30 天 + 1µs）
- `cleanup_metadata_lru`：边界 + 0 配置时 no-op

### 8.2 集成测试

- 跑一遍 trending → 检查 `media_metadata` 有写入
- 二次跑同 trending → 检查上游未被调用（统计 `_request` 次数 mock）
- detail 命中 stale → 返回旧数据 + 后台 refresh 入队
- 豆瓣 worker 完成后 → `media_metadata` 有 douban 行 + imdb_id 桥接

### 8.3 手动验收

- 起服务后浏览 5 分钟 → `media_metadata` 行数 ~100-200
- 关服务再起 → 同 trending 加载耗时应明显变短（命中实体表，不出网）
- `SELECT count(*) FROM media_metadata GROUP BY source` 三个 source 都有
- `SELECT count(*) FROM media_metadata WHERE imdb_id IS NOT NULL` 应占总数 ~60%+

---

## 9. 工时估算

| Phase | 内容 | 工时 |
|---|---|---|
| 1 | 表 / Migration / Store / 配置 / 单测 | 4 h |
| 2 | discover.py 5 个端点 + ratings.py 写入 | 3 h |
| 3 | discover.py 5 个端点读取改造 | 3 h |
| 4 | LRU job + refresh worker | 2 h |
| - | 集成测试 + 联调 | 2 h |
| **合计** | | **~14 h** |

---

## 10. 已决事项

以下原 "开放问题" 已确认，明列以备实施时不跑偏：

1. **AniList → TMDB 反查**：✅ **启用**。AniList 行写入时，如果 `externalLinks` 反查到 tmdb_id，同时填到 `media_metadata.tmdb_id` 列。这样下次 TMDB 列表加载到同 ID 时可直接命中（跨 source 复用）。

2. **豆瓣海报 URL fallback / 自动重抓**：❌ **不实现**。现有 `/api/img-proxy` 能 fail-soft，足够。首次失败的图永远失败的小问题留待用户反馈再说，避免增加无收益的后台扫图复杂度。

3. **新片短 TTL 特例**：❌ **不实现**。统一 30 天 TTL。奥斯卡颁奖期 / 圣诞档 metadata 偶尔不新鲜可接受，用户主动 KV refresh 会顺带 enqueue L3 stale check。

4. **L3 是否暴露用户主动 refresh 端点**：❌ **不暴露**。详见 §4.3。

5. **成人库是否纳入 media_metadata**：❌ **不纳入**。物理分离 + 哲学一致。详见 §1.4。

6. **store_douban_full 默认值**：✅ **true**（豆瓣存全字段，跟 TMDB/AniList 等价）。

7. **LRU 默认值**：✅ **365 天**（一年没访问的清掉），稳态库大小 ~180 MB。

---

## 11. 后续可能 PRD（不在本次范围）

如果本次落地后用户有更高需求，下面是几个可能的后续方向：

1. **演员维度搜索**：从 `media_metadata.ext.cast` 反查"某演员的所有片"。需要 GIN 索引 + 单独检索端点。
2. **跨库统一搜索**：把 `media_metadata` + `adult_items` + `adult_actresses` 接入全局搜索。
3. **Admin 后门**：管理员鉴权下的 metadata 强制 refresh 端点（生产排错用）。当前用 `DELETE FROM ... WHERE source=... AND source_id=...` 已经够。
4. **元数据多版本 / diff**：每次 refresh 保留旧版本，提供"何时换的海报 / 简介"审计。空间代价 2-3 倍。
