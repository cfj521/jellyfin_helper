# 下载入库自动化流水线 — 实施计划

> 生成日期：2026-05-09
> 修订日期：2026-05-09（基于 review + 5 个高优 spike 结果）
> 状态：方案已确认 + 关键 spike 完成，待开工
> 替代/补充：之前的"sync_completed"简单回调机制将被本流水线取代

---

## Spike 实测结果（开工前预备）

5 项 spike 已完成，关键发现整合进本计划。已经落地的代码不再列入 Phase A 的工作量。

| Spike | 结论 | 已落地代码 |
|---|---|---|
| ① subtitle 同步抽离 | ✅ 可行。auto_fix_from_report 本身就是同步纯函数 | `backend/api/subtitle.py::run_subtitle_auto_fix_inline()`<br>原 `run_subtitle_auto_fix(task_id,...)` 改为 task wrapper 调 inline<br>额外加了"文件白名单"语义解决剧集精准问题 |
| ② audio 同步抽离 | ✅ 可行。mkvpropedit 仅改 default flag，不重打包 | `backend/api/audio.py::run_default_track_inline()`<br>apply=True + mkvpropedit 缺失自动降级为预览 |
| ③ qB metadata-only | ⚠️ **plan 原假设错** —— `paused=True` 不连 peer 永远拿不到 metadata。正解是 qB 4.6+ 的 `stopCondition='MetadataReceived'` 参数 | `common/qbittorrent_client.py::add_torrent(stop_condition=...)` + `get_files()` + `get_torrent_info()`<br>本机 DHT 不通无法实测 metadata 拉取耗时，生产用 PT tracker 应正常 |
| ④ DB 类型确认 | ✅ PostgreSQL（host=127.0.0.1）→ `JSONB` 直接用 | — |
| ⑤ LLM 识别质量 | ✅ **超预期**：qwen-plus 在 30 个真实样本上达 100%/100%（type/hint），平均 ¥0.0007/调用 | API key 已配置（aliyun.qwen-plus）<br>1000 个种子成本 ≈ ¥0.74<br>平均延迟 3.8s（pipeline 异步可接受） |

### 关键纠错（影响本 plan 的设计）

1. **`paused=True` ≠ metadata-only**（spike 3）：plan 原版描述错了。已修正为 `stopCondition='MetadataReceived'` + 监 `list_torrents` 的 `size > 0`。
2. **subtitle item_paths 的精度问题**（review）：底层 SubtitleScanner.scan() 只接目录。inline 函数引入"文件白名单"，传 `[E12.mkv]` 只处理 E12，**不动同目录已有的 E01-E11**。
3. **本机部署环境注意**：
   - mkvtoolnix 未装 → 音轨修改全部降级为预览（流水线 warn 不阻断）
   - qB 服务器 DHT 不通 → metadata-only 测试时 60s 拿不到。生产用私有 PT 种子（带 tracker）应正常。
4. **LLM 准确率超预期**：不需要 TMDB 反查兜底也能直接信 qwen-plus 的输出（confidence 全部 ≥ 0.95）。Phase E 的 "TMDB 反查验证" 简化为 confidence < 0.85 时才做。

---

## 0. 背景与目标

### 现状痛点

1. **下载和媒体库分盘**
   - qBittorrent 下载到 `/downloads`（NVMe SSD，500 GB，速度优先）
   - Jellyfin 媒体库在 `/library`（RAID5 HDD，容量优先）
   - 完成后需要按媒体类型分发到不同库（电影库 / 剧集库 / 动漫库 / 成人库 / ...）
   - 当前只有 `sync_completed` 一个简单端点，无自动转移、无清理、无后处理

2. **NVMe 容量瓶颈**
   - 物理 500 GB，需要保留 100 GB 给系统/缓存
   - **限制下载占用 ≤ 400 GB**，逼近时必须主动清理（无论分享率）
   - 跨盘无法硬链接，只能**复制**（保种）或**移动**（不保种）

3. **完成后的繁琐手工**
   - 元数据让 Jellyfin 自己刮（已有能力）
   - 字幕需要单独跑扫描+下载流程
   - 音轨需要单独跑检查流程
   - 所有这些都基于"视频文件路径"，不依赖 jellyfin item id

### 目标

构建一条端到端流水线，让用户操作只剩下"挑种子点下载"，剩下全自动：

```
用户挑种子 → metadata-only 预下载 → 类型识别（含 LLM 兜底）→ 重复检测
   → 用户确认（预填好分类、目标路径）→ 真下载 → 完成自动转移到库
   → Jellyfin 刮元数据 + 我们处理字幕 + 处理音轨 → 配额满自动清理
```

---

## 1. 设计原则

| 原则 | 说明 |
|---|---|
| **配额优先于分享率** | 400 GB 是硬约束，超阈值即使分享率没达到也强清 |
| **复制优先于移动** | 跨盘只能复制 + 留 NVMe 副本继续做种；分享率到了再清 |
| **类型识别在加种子时定** | 不在事后猜；优先级：用户主动选 > 元数据反查 > 启发式 > LLM 兜底 |
| **后处理基于文件路径** | 字幕 / 音轨 API 都接受 `item_paths`，不依赖 jellyfin item id |
| **流水线串行单 worker** | 简单可控，避免并发拖慢磁盘 IO 和触发外部 API 限频 |
| **失败分级** | copy/organize 失败阻断；jellyfin/subtitle/audio 失败仅警告 |
| **元数据交给 Jellyfin** | 我们只触发 refresh，不重复造刮削轮子 |

---

## 2. 用户场景

### 场景 A：从"热门推荐"页发起下载（最优路径）

```
1. 用户在热门推荐页点"下载" Inception (2010, tmdb=27205)
2. 后端拿 tmdb_id 查 TMDB API → 得到 imdb_id, title, year
3. 用 imdb_id + title 双路调 Jackett 搜索
4. 用户选种子 → 预添加 metadata-only → 拿文件列表
5. 重复检测 → jellyfin 没这电影 → 通过
6. 推算分类 = movies、目标库 = 电影库、目标路径 = /library/movies/Inception (2010)/
7. 弹"确认下载"对话框，全部预填好
8. 用户点确认 → qB 真开始下 + 落 dispatch_map
9. 完成 → 复制到 /library/.../ → Jellyfin refresh → 字幕/音轨处理
```

### 场景 B：从"种子搜索"页（无 ID）

```
1. 用户搜 "葬送的芙莉莲"
2. Jackett 返回种子列表
3. 选种子 → 预添加 → 拿文件列表（含 [Sakurato] Sousou no Frieren - 12.mkv 等）
4. 启发式识别：番号正则不命中、SxxExx 不命中 (动漫常用 ' - 12' 格式)
5. → LLM 兜底：识别为 anime + 标题 "Frieren: Beyond Journey's End" + 集 12
6. → 用 LLM 给的搜索词反查 TMDB → 拿到 tv_id (209867)
7. 重复检测：剧集已有 S01E01-E11，本次新增 S01E12
8. 用户确认（默认勾"仅下新增" → qB filePrio 跳过 E01-E11）
9. 完成 → 仅 S01E12.mkv 转到 /library/anime/Frieren/Season 01/
10. 字幕扫描只针对 S01E12.mkv 单文件
```

### 场景 C：RSS 自动命中

```
1. RSS rule "Frieren S2 Sakurato" 命中新种子
2. 规则预设 category=anime + 复用上次搜出的 tmdb_id
3. 直接预添加 → 重复检测
4. 重复策略 download_only_new → 自动跳过已有集
5. 自动接受，无人值守完成全流程
```

### 场景 D：配额触发清理

```
1. 用户下了一堆 4K REMUX，/downloads 用量到 380 GB (95%)
2. 配额监控触发 cleanup
3. 按"代价排序"删除：先清已转移 + 已达分享率的种子
4. 不够 → 继续清已转移但未达分享率的（牺牲品）
5. 清到 280 GB (70%) 停止
6. 任务日志记录每条种子被清原因 + 释放空间
7. 前端状态条颜色由红变蓝，UI 提示完成
```

---

## 3. 数据流总览

```
[添加种子（场景 A/B/C 入口）]
   ↓
[metadata-only 预添加] ── 拿文件列表
   ↓
[类型识别] ── ① qB category 已标 → 用
              ② 番号正则 → adult
              ③ SxxExx + 系列名 jellyfin 模糊匹配 → tv（含动漫）
              ④ 电影名+年份 → TMDB 搜 → movie
              ⑤ LLM 兜底 → tmdb_search_hint → TMDB 验证
              ⑥ 仍不识别 → 用户确认界面留空
   ↓
[重复检测] ── 电影：按 tmdb_id 查 jellyfin
              剧集：按系列+SxxExx 列出已有 → 对比种子文件 → 拆"全部/部分/新增"
   ↓
[确认下载] ── 预填分类、目标路径、move_mode；剧集可选"仅下新增"
   ↓
[落 dispatch_map + 真开始下载]
   ↓
[完成轮询监工，30s 一次]
   ↓
[复制] ── /downloads → /library/<分类>/<结构化路径>/
[整理] ── 主视频、字幕、去 sample/nfo/RARBG.txt、按 SxxExx 拆 Season
[Jellyfin refresh] ── 让 Jellyfin 刮元数据
[字幕处理] ── 调 subtitle.auto_fix(item_paths=[本次新增文件])
[音轨处理] ── 调 audio.process(item_paths=[本次新增文件])
[标记 done] ── dispatch_map.status=done + qB tag library:<type>
   ↓
[配额监控（独立任务，每分钟）]
   ↓
[超阈值 → 配额清理（硬清，不看分享率）]
[每小时跑一次常规清理（软清，按分享率/做种天数）]
```

---

## 4. 数据模型

### `download_dispatch_map`（核心表）

```sql
CREATE TABLE download_dispatch_map (
    -- 主键
    torrent_hash         VARCHAR(64) PRIMARY KEY,

    -- 媒体识别
    media_type           VARCHAR(20),   -- movie / tv / anime / adult / unknown
    tmdb_id              VARCHAR(32),
    imdb_id              VARCHAR(32),
    series_tmdb_id       VARCHAR(32),   -- 剧集才有，用于跨季归属
    series_name          VARCHAR,
    title                VARCHAR,
    year                 INT,

    -- 目标
    target_library_id    VARCHAR(64),   -- jellyfin lib id
    target_root          VARCHAR,       -- 目标库根路径 e.g. /library/movies
    target_path          VARCHAR,       -- 完整目标目录 e.g. /library/movies/Inception (2010)
    move_mode            VARCHAR(10),   -- copy / move
    dispatched_files     JSONB,         -- 复制后实际产生的文件路径列表

    -- 状态机（review 建议拆 phase + phase_status，比单字段 17 个并列状态清晰）
    phase                VARCHAR(20),   -- copy / organize / jellyfin / subtitle / audio / done / cleaned
    phase_status         VARCHAR(20),   -- running / ok / failed / warned / skipped
    status_message       TEXT,
    error_log            TEXT,
    -- 各 phase 的耗时（秒）便于审计
    phase_timings        JSONB,         -- {copy: 234, organize: 5, jellyfin: 1, subtitle: 89, audio: 12}

    -- 复制 phase 的实时进度（大文件可能跑几十分钟，UI 实时显示）
    copy_bytes_done      BIGINT DEFAULT 0,
    copy_bytes_total     BIGINT DEFAULT 0,

    -- 配额清理决策用
    ratio_at_dispatch    DECIMAL(8,2),
    last_seen_ratio      DECIMAL(8,2),
    seeded_seconds       BIGINT DEFAULT 0,
    upload_bytes         BIGINT DEFAULT 0,
    cleanup_eligible_at  TIMESTAMP,

    -- 时间戳
    created_at           TIMESTAMP DEFAULT NOW(),
    dispatched_at        TIMESTAMP,
    cleaned_at           TIMESTAMP
);

CREATE INDEX idx_dispatch_phase ON download_dispatch_map(phase, phase_status);
CREATE INDEX idx_dispatch_seeded_until ON download_dispatch_map(cleanup_eligible_at);
```

> **DB 迁移方式**：项目用 `backend/database.py::_SCHEMA_PATCHES`（`ALTER TABLE IF NOT EXISTS`）
> 而非 alembic。新表加到 `Base.metadata.create_all()` 自动创建，新列追加到
> `_SCHEMA_PATCHES` 列表手动 ALTER。简单稳定，参考既有的 adult_items 等表的演化方式。

### `llm_classify_cache`（LLM 结果缓存）

```sql
CREATE TABLE llm_classify_cache (
    fingerprint_hash     VARCHAR(64) PRIMARY KEY,  -- hash(torrent_name + sorted(files))
    media_type           VARCHAR(20),
    title_native         VARCHAR,
    title_en             VARCHAR,
    title_zh             VARCHAR,
    year                 INT,
    season               INT,
    episode              INT,
    confidence           DECIMAL(3,2),
    tmdb_search_hint     VARCHAR,
    raw_response         TEXT,
    created_at           TIMESTAMP DEFAULT NOW()
);
```

---

## 5. 配置项

### 新增到 `config.yaml.example` 的字段

```yaml
qbittorrent:
  # （已有）host / username / password / download_path
  download_path: /downloads

  # 新增：配额管理
  quota:
    enabled: true
    max_bytes: 429496729600       # 400 GB 上限（用户可改）
    warn_threshold: 0.85          # 85%(340G) 起 UI 状态条变橙
    cleanup_threshold: 0.95       # 95%(380G) 触发硬清
    target_after_cleanup: 0.70    # 清理后回到 70%(280G)
    check_interval_seconds: 60    # 配额监控轮询周期

  # 新增：做种 / 清理策略
  seeding:
    target_ratio: 1.0             # 默认分享率 1.0（私有种用户可调高）
    min_seed_days: 7              # 至少做种 7 天
    min_seed_hours_per_torrent: 24
    cleanup_interval_seconds: 3600  # 常规软清周期 1h

# 新增：下载入库流水线
dispatch:
  enabled: true
  poll_interval_seconds: 30       # 完成轮询周期
  worker_concurrency: 1           # **单 worker 串行**（用户确认）
  copy_buffer_mb: 8               # rsync / shutil.copy 缓冲
  default_move_mode: copy         # 整体默认（每条规则可覆盖）
  trash_dir: /downloads/.trash    # sample/nfo/RARBG.txt 丢这里
  trash_keep_days: 7              # trash 保留天数

  # 各分类的目标库 + 目录模板（用户可改）
  rules:
    movie:
      library_id: ""              # jellyfin 库 id（前端选）
      location_template: "{library_root}/{title} ({year})"
      file_template: "{title} ({year})"
      move_mode: copy

    tv:
      library_id: ""
      location_template: "{library_root}/{series_name}/Season {season:02d}"
      file_template: "{series_name} S{season:02d}E{episode:02d}"
      move_mode: copy

    anime:
      library_id: ""
      location_template: "{library_root}/{anime_name}/Season {season:02d}"
      file_template: "{anime_name} S{season:02d}E{episode:02d}"
      move_mode: copy

    adult:
      library_id: ""
      location_template: "{library_root}/{code}"
      file_template: "{code}"
      move_mode: move             # 番号通常不保种

# 新增：LLM 兜底识别
llm:
  enabled: true
  # Spike 实测：qwen-plus 30/30 准确率 100%，¥0.74 / 1000 调用
  provider: qwen                  # qwen / deepseek / glm / openai / claude
  api_key: ""                     # aliyun dashscope key（OpenAI 兼容接口）
  model: qwen-plus
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  timeout_seconds: 15             # qwen-plus 实测 ~3.8s，预留余量
  max_retries: 1
  cache_ttl_days: 30              # llm_classify_cache 有效期
  confidence_threshold: 0.85      # 实测 confidence 全部 ≥ 0.95；阈值设 0.85 让低置信度落用户确认
  tmdb_verify_below: 0.85         # confidence < 此值才调 TMDB 反查验证（高置信度直接信）
  daily_call_limit: 500           # 每日调用上限（防失控）

# 配置嵌套化建议：当前 settings 是扁平字段（30+ 个 qbittorrent_*），新增 4 段会让
# Settings 类膨胀。建议借此机会小重构 — 改用 nested pydantic BaseModel：
#   class QBittorrentConfig(BaseModel): host, username, password, quota: QuotaConfig, ...
#   class DispatchConfig(BaseModel): worker_concurrency, rules: Dict[str, RuleConfig], ...
# 一次性切换，后续新加字段不再翻倍声明。

---

## 6. 状态机

```
                       +---------+
                       | pending |
                       +----+----+
                            | 完成轮询命中（progress 100%）
                            v
                       +---------+
                       | copying |
                       +----+----+
                            |
              fail──────────+──────success
                ↓                       ↓
        +----------------+      +-------------+
        | failed_copy    |      | organizing  |
        | (阻断，需介入) |      +------+------+
        +----------------+             |
              ↓                  fail──+──success
        手动重试               ↓                 ↓
                       +-----------------+   +---------------------+
                       | failed_organize |   | notifying_jellyfin  |
                       | (阻断，需介入)  |   +----------+----------+
                       +-----------------+              |
                              ↓                  fail───+───success
                          手动重试                ↓               ↓
                                          +------------------+   +----------------------+
                                          |    warn_jellyfin |   | processing_subtitle  |
                                          | (继续走，仅警告) |   +-----------+----------+
                                          +------------------+               |
                                                ↓                     warn───+───success
                                            继续走                     ↓             ↓
                                                                +----------------+  +----------------+
                                                                | warn_subtitle  |  | processing_audio
                                                                | (继续，仅警告) |  +-------+--------+
                                                                +-------+--------+          |
                                                                        |             warn──+──success
                                                                        ↓                   ↓
                                                                  继续走 audio        +------+
                                                                                      | done |
                                                                                      +------+
                                                                                          |
                                                                                  分享率/天数到
                                                                                  或配额清理
                                                                                          ↓
                                                                                    +---------+
                                                                                    | cleaned |
                                                                                    +---------+
```

`failed_*` 阻断点需要人工介入；`warn_*` 不阻断流水线继续走。

---

## 7. 关键模块设计

### 7.1 配额监控与清理 `tools/dispatch/quota.py`

```python
class QuotaManager:
    def __init__(self, settings):
        self.cfg = settings.qbittorrent.quota
        self.seeding_cfg = settings.qbittorrent.seeding

    def check(self) -> dict:
        """读 /downloads 目录磁盘用量 + qB 任务统计"""
        usage_bytes = shutil.disk_usage(self.cfg.download_path).used  # 或更精确：sum 所有 torrent.size
        return {
            'used': usage_bytes,
            'limit': self.cfg.max_bytes,
            'ratio': usage_bytes / self.cfg.max_bytes,
            'state': self._state(usage_bytes),
        }

    def needs_cleanup(self) -> bool:
        return self.check()['ratio'] >= self.cfg.cleanup_threshold

    def quota_cleanup(self) -> List[CleanupResult]:
        """硬清：按代价排序删除直到回到 target_after_cleanup"""
        target = self.cfg.target_after_cleanup * self.cfg.max_bytes
        candidates = self._list_cleanup_candidates()  # 已转移的 dispatch_map 行
        candidates.sort(key=self._cost_score)         # 代价从小到大

        freed = 0
        results = []
        for c in candidates:
            if self._current_used() <= target:
                break
            results.append(self._delete_torrent(c))
            freed += c.size
        return results

    def regular_cleanup(self) -> List[CleanupResult]:
        """软清：按 ratio/天数策略"""
        eligible = (
            db.query(DispatchMap)
              .filter(status='done')
              .filter(or_(
                  last_seen_ratio >= self.seeding_cfg.target_ratio,
                  seeded_seconds >= self.seeding_cfg.min_seed_days * 86400,
              ))
              .filter(seeded_seconds >= 24 * 3600)
              .all()
        )
        return [self._delete_torrent(c) for c in eligible]

    def _cost_score(self, c) -> float:
        """代价越小越优先清。综合：是否私有种、已上传量、文件大小、距离 ratio 多远"""
        ...
```

### 7.2 流水线编排 `tools/dispatch/pipeline.py`

```python
class DispatchPipeline:
    """单 worker 串行处理。从 dispatch_map 取 status=pending 的，跑完整 pipeline。"""

    def __init__(self, settings):
        self.cfg = settings.dispatch
        self.qb = QBittorrentClient(...)

    def run_forever(self):
        while True:
            row = self._claim_next_pending()
            if not row:
                time.sleep(self.cfg.poll_interval_seconds)
                continue
            try:
                self._run_one(row)
            except Exception as e:
                logger.exception(f"pipeline 异常 {row.torrent_hash}")
                self._set_status(row, 'failed_unknown', str(e))

    def _run_one(self, row):
        # 阻断 phases（失败需介入）
        self._step_copy(row)        # phase=copy → ok / failed
        self._step_organize(row)    # phase=organize → ok / failed
        # 主 pipeline 走到这里就标 'done' 释放下一个种子
        # 后处理（jellyfin/subtitle/audio）派给独立 worker 队列，避免阻塞主流水线
        self._step_jellyfin(row)    # phase=jellyfin → ok / warned
        self._enqueue_post_process(row)
        self._set_phase(row, 'done', 'ok')
        self.qb.add_tag(row.torrent_hash, f'library:{row.media_type}')


class PostProcessWorker:
    """独立单 worker：处理 dispatch_map 中已 done 但未做字幕/音轨的种子。
    主 pipeline 不等它，所以 subtitle 找几分钟字幕也不影响下个种子开始转移。"""

    def run_forever(self):
        while True:
            row = self._claim_next_post_pending()
            if not row:
                time.sleep(30)
                continue
            self._step_subtitle(row)   # phase=subtitle → ok / warned / skipped
            self._step_audio(row)      # phase=audio → ok / warned / skipped
```

### 7.3 文件整理 `tools/dispatch/organizer.py`

```python
class TorrentOrganizer:
    """把种子包内文件结构化复制到目标路径。"""

    GARBAGE_PATTERNS = [
        r'^sample/',
        r'\.nfo$',
        r'^RARBG\.txt$',
        r'/proof\.png$',
        r'^Screens?/',
    ]

    def organize(self, source: Path, target: Path, media_type: str,
                 metadata: dict) -> List[Path]:
        """
        返回 dispatched_files：实际复制到目标位置的文件路径列表。
        sample / nfo 等丢到 trash_dir。
        """
        videos = []
        subs = []
        garbage = []
        for f in walk(source):
            if self._is_garbage(f):
                garbage.append(f)
            elif f.suffix.lower() in VIDEO_EXTS:
                videos.append(f)
            elif f.suffix.lower() in SUB_EXTS:
                subs.append(f)

        if media_type in ('tv', 'anime'):
            return self._organize_tv(videos, subs, target, metadata)
        else:
            return self._organize_single(videos, subs, target, metadata)
```

### 7.4 字幕/音轨调用 `tools/dispatch/post_process.py`

> **Spike 1+2 已落地**：subtitle/audio 同步 inline 函数已抽离到 backend/api/{subtitle,audio}.py，
> 直接 import 调用即可；不再需要轮询 Task 状态。

```python
def post_process_subtitle(row, settings):
    """字幕处理：仅警告，不阻断主流程。"""
    from backend.api.subtitle import run_subtitle_auto_fix_inline
    try:
        # 关键：传 dispatched_files（具体的视频文件路径）
        # inline 函数会自动"父目录扫描 + 文件白名单过滤"，剧集场景不动同目录的旧集
        result = run_subtitle_auto_fix_inline(
            paths=row.dispatched_files,
            recursive=False,
            expected_langs=settings.preferred_langs,
            dry_run=False,
            do_rename=True,
            refresh_library_ids=[],   # 主 pipeline 已 refresh，不重复
            progress_cb=lambda pct, msg: row.update_phase('subtitle', pct, msg),
        )
        if result.get('error'):
            return ('warned', result['error'])
        if result['download']['failed'] > 0:
            return ('warned', f"{result['download']['failed']} 失败")
        return ('ok', result)
    except Exception as e:
        logger.warning(f"字幕处理失败（不阻断）: {e}")
        return ('warned', str(e))


def post_process_audio(row, settings):
    """音轨处理：检查 default 音轨是否符合 preferred，能改则改（仅 default flag，不重打包）。"""
    from backend.api.audio import run_default_track_inline
    try:
        result = run_default_track_inline(
            item_paths=row.dispatched_files,
            preferred_langs=settings.preferred_audio_langs,
            skip_single_track=True,
            apply=True,                  # 真改
            refresh_library_ids=[],
            progress_cb=lambda pct, msg: row.update_phase('audio', pct, msg),
        )
        # apply=True 但 mkvpropedit 缺失会自动降级为预览，error 字段会有提示
        if result.get('error'):
            return ('skipped', result['error'])  # mkvtoolnix 未装
        return ('ok', result)
    except Exception as e:
        logger.warning(f"音轨处理失败（不阻断）: {e}")
        return ('warned', str(e))
```

### 7.5 LLM 类型识别 `common/llm_client.py`

```python
class LLMClient:
    """支持 deepseek / qwen / glm / openai / claude，统一 OpenAI-compatible 接口"""

    def classify_torrent(self, torrent_name: str, files: List[dict]) -> dict:
        """
        files: [{path, size}]
        返回 dict: {media_type, title_*, year, season, episode, confidence,
                   tmdb_search_hint, reasoning}
        命中缓存直接返回；否则调 LLM + 落库。
        """
        fp = self._fingerprint(torrent_name, files)
        cached = db.query(LLMClassifyCache).filter_by(fingerprint_hash=fp).first()
        if cached and not self._expired(cached):
            return self._to_dict(cached)

        if self._daily_count() >= self.cfg.daily_call_limit:
            raise QuotaExceeded("LLM 每日上限已达")

        prompt = self._build_prompt(torrent_name, files)
        response = self._call_with_retry(prompt)  # JSON mode 强制结构化
        result = self._parse_response(response)

        db.add(LLMClassifyCache(fingerprint_hash=fp, **result))
        db.commit()
        return result
```

### 7.6 加种子时的识别链 `tools/dispatch/identify.py`

```python
def identify_media(torrent_name: str, files: List[dict],
                   user_hint: Optional[dict] = None) -> dict:
    """
    返回 {media_type, tmdb_id?, imdb_id?, title, year?, season?, episode?,
          confidence, source}

    source: user_hint / category / regex_avcode / regex_episode / tmdb_search /
            llm_with_tmdb_verify / llm_only / unknown
    """
    # ① 用户主动选（热门页传入）
    if user_hint and user_hint.get('media_type'):
        return {**user_hint, 'source': 'user_hint', 'confidence': 1.0}

    # ② qB category 已标
    # （场景：RSS 规则命中时已经写了 category）

    # ③ 番号正则
    av_match = match_av_code(torrent_name, files)
    if av_match:
        return {'media_type': 'adult', 'code': av_match, 'source': 'regex_avcode', 'confidence': 0.95}

    # ④ SxxExx + 系列名 jellyfin 模糊匹配
    ep_info = extract_episode_info(torrent_name, files)
    if ep_info:
        series = jellyfin_match_series(ep_info['series_name'])
        if series:
            return {
                'media_type': series.get('type', 'tv'),  # tv / anime
                'tmdb_id': series['tmdb_id'],
                'series_name': series['name'],
                'season': ep_info['season'],
                'episode': ep_info['episode'],
                'source': 'regex_episode',
                'confidence': 0.9,
            }

    # ⑤ 电影名 + 年份 → TMDB
    movie_info = extract_movie_info(torrent_name)
    if movie_info:
        tmdb_result = tmdb_search_movie(movie_info['title'], year=movie_info['year'])
        if tmdb_result:
            return {**tmdb_result, 'source': 'tmdb_search', 'confidence': 0.85}

    # ⑥ LLM 兜底（spike 实测 qwen-plus 30/30 = 100% 准确率）
    if settings.llm.enabled:
        try:
            llm_result = llm_client.classify_torrent(torrent_name, files)
            conf = llm_result['confidence']
            # 实测 confidence 全部 ≥ 0.95 → 高置信度直接信，不必每次都 TMDB 反查
            if conf >= settings.llm.confidence_threshold:
                # 仅在 confidence 较低时 TMDB 反查验证
                if conf < settings.llm.tmdb_verify_below and llm_result.get('tmdb_search_hint'):
                    verified = verify_via_tmdb(llm_result)
                    if verified:
                        return {**verified, 'source': 'llm_with_tmdb_verify', 'confidence': conf}
                # 直接采用 LLM 结果（含 tmdb_search_hint，调用方可异步反查 TMDB 拿 ID）
                return {**llm_result, 'source': 'llm_only', 'confidence': conf}
        except Exception as e:
            logger.warning(f"LLM 识别失败: {e}")

    # ⑦ 都没识别
    return {'media_type': 'unknown', 'source': 'unknown', 'confidence': 0.0}
```

---

## 8. 分阶段实施计划

> **顺序原则（review 调整）**：A1 单独优先（DB 是后面所有 phase 依赖）；A3 紧跟（qB client 是流水线工具）；
> H 提前到 A 后做（A3 已铺好 client，H 只是 UI 增强；做完 MVP 期间用户就有可用界面）；E 因 spike 已验证 LLM 100% 准确率，工作量减半；G 简化为透传 qB 自带 RSS rule。

### Phase A — 基础设施（P0，2 天）

**目标**：建立流水线的"骨架"，先跑通"完成 → 复制 → Jellyfin 刷新"。

| # | 任务 | 文件 | 备注 |
|---|---|---|---|
| **A1** | DB schema：`download_dispatch_map` + `llm_classify_cache` 模型类，**用 `_SCHEMA_PATCHES` ALTER**（不用 alembic） | `backend/database.py` | 拆出独立 0.5 天交付 |
| **A2** | 配置项：`qbittorrent.quota` / `seeding` / `dispatch` / `llm` 加到 `Settings`，**借此重构为嵌套 pydantic models** | `backend/config.py` + `config.yaml.example` | 0.5 天 |
| **A3** | `QBittorrentClient` **几乎重写**（现 6 方法 → ~20 方法，spike 已加 3 个）：剩余 add_tag / set_tags / set_category / set_file_priority / recheck / reannounce / set_force_start / set_save_path / transfer_info / 限速接口 / RSS 一组 | `common/qbittorrent_client.py` | **1 天**（spike 后修正） |
| A4 | 完成轮询任务（替换现有 sync_completed）：扫 qB → 写 dispatch_map(phase=copy, phase_status=running) | `tools/dispatch/poll.py`（新） | 行级锁 |
| A5 | 单 worker 串行编排骨架（DispatchPipeline）+ 状态机（phase + phase_status）+ PostProcessWorker（独立队列） | `tools/dispatch/pipeline.py`（新） | — |
| A6 | `_step_copy`：shutil.copy2 + 实时 bytes_done 写库进度回调 | `tools/dispatch/copier.py`（新） | — |
| A7 | `_step_jellyfin`：调 `jellyfin.trigger_refresh(library_id, mode)` | 复用现有 ✅ | — |
| A8 | 后端 main.py 启动时拉起 DispatchPipeline + PostProcessWorker 后台线程 | `backend/main.py` | 套现有 lifespan 范式 |

**验收**：手动加一个种子，落 dispatch_map 后能自动复制到目标库 + Jellyfin 触发刷新。phase 推进可观察，copy 进度实时更新。

### Phase B — 配额管理（P0，0.5 天）

**目标**：NVMe 容量受控。

| # | 任务 | 文件 |
|---|---|---|
| B1 | `QuotaManager` 实现：check / quota_cleanup / regular_cleanup（**配额清理只清安全档**：已转移 + 已达 ratio；不够再升级到牺牲档，落 task 警告） | `tools/dispatch/quota.py`（新） |
| B2 | 后端定时任务：每分钟跑 `check + needs_cleanup → quota_cleanup` | `tools/dispatch/scheduler.py`（新） |
| B3 | 后端定时任务：每小时跑 `regular_cleanup` | 同上 |
| B4 | 前端下载管理页顶部状态条：用量进度 + 颜色阈值 + "立即清理" 按钮 | `frontend/src/views/discover/Downloads.vue` |
| B5 | 配额清理日志：写入任务系统（Task / TaskLog） | 复用现有任务系统 |

**验收**：往 /downloads 灌满到 380 GB，配额自动清理回 280 GB；UI 颜色和数字实时更新。

### Phase H — qB UX 增强（P1，可与 B 并行，0.5 天）

> 提前到 C 之前 —— A3 已铺好 client，H 只是 UI 增强；做完 MVP 期间就有可用界面。

| # | 任务 |
|---|---|
| H1 | 多选批量暂停 / 恢复 / 删除 / 强制启动 |
| H2 | 列表加 ETA / ratio / added_on / tracker 状态 / category / tags |
| H3 | 顶部全局速度统计（与配额状态条整合） |
| H4 | 限速控件 + 备用限速一键切换 |
| H5 | 单条操作下拉菜单：强制启动 / 重新校验 / 重新 announce / 修改保存路径 / 修改分类 |

**验收**：下载管理页 UX 接近 qBittorrent Web UI 水准。

### Phase C — 文件整理 + 后处理（P0，0.5 天 ↓）

> **Spike 1+2 已完成**：subtitle / audio inline 函数已落地。本 phase 只剩 organizer + 调用胶水，工作量减半。

| # | 任务 | 文件 |
|---|---|---|
| C1 | `TorrentOrganizer`：去 sample/nfo/RARBG.txt（→ trash_dir）；按 SxxExx 拆 Season；记录 dispatched_files | `tools/dispatch/organizer.py`（新） |
| C2 | `post_process_subtitle`：调已有的 `run_subtitle_auto_fix_inline(paths=dispatched_files, ...)` ✅ —— 文件白名单语义已 spike 验证 | `tools/dispatch/post_process.py`（新） |
| C3 | `post_process_audio`：调已有的 `run_default_track_inline(item_paths=dispatched_files, apply=True, ...)` ✅ | 同上 |
| C4 | trash_dir 定时清理：保留 7 天（按配置） | `tools/dispatch/scheduler.py` |

**验收**：剧集种子完成后，sample/nfo 入 trash，主视频按 SxxExx 落到目标 Season 目录，字幕/音轨流水线日志清晰可见。剧集只对**新增的集**跑字幕（白名单已 spike 验证）。

### Phase D — 入库前预处理（P1，1.5 天）

**目标**：从添加种子到点确认，预填好分类和路径，识别重复。

| # | 任务 | 文件 |
|---|---|---|
| D1 | metadata-only 预添加 helper：用 spike 3 已加的 `add_torrent(stop_condition='MetadataReceived', download_limit=1)` ✅ + 轮 `list_torrents` 监 size > 0 | `common/qbittorrent_client.py` ✅ 已加基础 | 主要写轮询 + 超时回退 |
| D2 | 启发式识别：番号正则 / SxxExx 提取 / 电影名+年份提取 | `tools/dispatch/identify.py`（新） |
| D3 | TMDB 反查：imdb_id → /find；query → /search/movie /search/tv | 复用 `common/tmdb_client.py` 或扩展 |
| D4 | jellyfin 重复检测：电影按 tmdb_id 查；剧集按 series + SxxExx 列对比 | `backend/api/jellyfin.py` 新加 helper |
| D5 | 前端"添加种子确认对话框"：预填分类/路径，重复时显示选择（全下/仅新增/跳过/替换） | `frontend/src/components/AddTorrentDialog.vue`（新） |
| D6 | 后端 `/api/dispatch/preview`：接 magnet 或 .torrent → 预添加 → 识别 → 返回 dispatch 预览 | `backend/api/dispatch.py`（新） |
| D7 | "仅下新增"实现：qB filePrio 跳过已存在的集 | `set_file_priority`（A3 待补） |

**验收**：从种子搜索页选种子点下载，10 秒内弹"添加确认"对话框，能看到文件列表 / 推断分类 / 重复警告。剧集 partial 重复能正确高亮已有集。

### Phase E — LLM 兜底（P1，0.3 天 ↓）

> **Spike 5 已完成**：qwen-plus 30/30 准确率 100%，平均 ¥0.0007/调用。
> 实测 confidence 全部 ≥ 0.95，**Phase D 的 identify 链可直接信高置信度结果**，工作量减半。

| # | 任务 | 文件 |
|---|---|---|
| E1 | `LLMClient` 抽象：OpenAI-compatible 接口，支持 qwen / deepseek / openai / claude / glm | `common/llm_client.py`（新） |
| E2 | `llm_classify_cache` 表 ORM helper（A1 已建表） | `backend/database.py` |
| E3 | Prompt 模板（直接复用 spike 用过的 v1 版本） | `tools/dispatch/llm_prompts.py`（新） |
| E4 | identify.py 接入 LLM 兜底分支：confidence ≥ tmdb_verify_below 直接信；< 阈值才 TMDB 反查 | `tools/dispatch/identify.py` |
| E5 | 前端设置页加 LLM 配置面板（provider / api_key / 测试按钮 / 每日剩余配额） | `frontend/src/views/Settings.vue` |
| E6 | 每日配额限制 + LLM 调用计数 | `tools/dispatch/llm_quota.py`（新） |

**验收**：动漫种子（无 ID 元数据）能被 LLM 直接识别 + 拿到可用 tmdb_search_hint，触发 TMDB 反查拿 tmdb_id。

### Phase F — 入口整合（P2，0.5 天）

**目标**：热门页 → Jackett → qB 全链路打通。

| # | 任务 |
|---|---|
| F1 | 热门推荐页"下载"按钮：拿 tmdb_id → 后端用 imdb + title 双路调 Jackett |
| F2 | 种子搜索页结果点"下载"：走 D6 的 preview 流程 |
| F3 | 添加种子手动入口（粘贴 magnet / 上传 .torrent） |

**验收**：从热门页一气呵成下到完成入库；种子搜索页同样路径；手动入口也能走预览。

### Phase G — RSS 集成（P2，0.5 天 ↓）

> **review 简化**：直接透传 qB 自带 RSS rule，不在我们这边做规则编辑器。
> qB 4.6 自带的 RSS rule 编辑器功能完整，不重复造。

| # | 任务 |
|---|---|
| G1 | `QBittorrentClient` RSS API 封装（A3 已规划：read 接口为主） |
| G2 | 后端 `/api/rss/*` 端点：仅 read-only 透传（feeds 列表 / 命中历史 / 状态） |
| G3 | 前端"RSS"tab：展示当前 qB 配置的 feeds + 命中历史，规则编辑跳到 qB Web UI |
| G4 | RSS 命中处理：通过 qB rule 的 savePath 路径前缀做 category 推断；命中后走 dispatch_map 默认流程 |

**验收**：在 qB 里配一条字幕组动漫 RSS rule，新一集发布 30 分钟内自动加种 + 重复检测 + 完成入库 + 字幕处理，全程无人值守。我们的 UI 透传展示。

---

## 9. 路线图（修订后）

```
Day 1     Phase A1     DB schema (拆出独立交付)
Day 1.5   Phase A2     配置项嵌套化
Day 2-3   Phase A3     QBittorrentClient 几乎重写（spike 已加 3 个，剩余 ~10 个）
Day 3.5   Phase A4-A8  完成轮询 + pipeline 骨架 + copy + jellyfin refresh + main.py 拉起
Day 4     Phase B      配额管理（quota 监控 + 硬清/软清）
Day 4.5   Phase H      qB UX 增强（A3 已铺好 client，UI 顺手做）  ← 提前
Day 5     Phase C      文件整理 + post_process 调用胶水（subtitle/audio inline 已完成）
                              ↑ 至此 MVP 闭环：手动加种 → 全自动入库 → 配额管理 + 可用 UI
Day 5.5-6.5  Phase D    预处理 + 重复检测（D5 AddTorrentDialog 是 UI 大头）
Day 6.5-6.8  Phase E    LLM 兜底（spike 验证后简化）
Day 7        Phase F    入口整合（热门/搜索/手动）
Day 7.5      Phase G    RSS 透传集成（review 简化）
```

**总计**：约 7.5 个工作日（review 估 8.5 → spike 揭示部分工作量被高估，回到 7.5）。
**MVP（A+B+H+C）**：5 天可交付，含可用 UI。

---

## 10. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 跨盘复制慢（用户实测 ~1GB/s）但仍可能阻塞 | 单 worker 串行 + 实时 bytes_done 写库 + UI 进度条；后处理派给独立 worker 不阻塞主线 |
| qB 重启 → seeding 时间清零 | 用 dispatch_map 自累计 seeded_seconds（不依赖 qB 进程内状态） |
| LLM 幻觉 / 误判 | spike 已验证 qwen-plus 100% 准确，confidence 低于阈值才 TMDB 反查；调用前查 cache 防重复消耗 |
| 番号正则 false positive | 严格匹配格式 + 长度限制 + 要求主视频文件 ≥ 100MB |
| 字幕 / 音轨 API 失败 | 隔离到 PostProcessWorker 独立队列 + warn 不阻断主流水线 |
| 配额清理误删私有种 | 安全档（已转移 + 已达 ratio）优先；不够升级到牺牲档时落 task 警告 + 推送通知 |
| jellyfin refresh 失败 | 超时 + 仅 warn 不阻断；UI 显示警告状态 |
| ⚠️ **mkvpropedit 未装** | spike 已验证：apply=True 自动降级为预览，warn 不阻断。**生产部署需 `apt install mkvtoolnix`** 才能真改默认音轨 |
| ⚠️ **qB 服务器 DHT 不通**（spike 本机测时 60s 拿不到 metadata） | 生产用私有 PT 种子（带 tracker URL）应正常；公开 magnet 失败时给"超时，请检查 qB DHT 设置"提示 |
| ⚠️ **subtitle dry_run 仍消耗 assrt 配额**（spike 1 副发现） | dispatch pipeline 始终用 dry_run=False；UI 测试模式提供 dry_run 时给配额消耗提示 |
| 数据库迁移用 _SCHEMA_PATCHES（与 alembic 不兼容） | 新表加到 Base.metadata + 新列追加到 _SCHEMA_PATCHES。简单，参考 adult_items 演化路径 |

---

## 11. 验收标准（端到端）

实施完成后，用户应该能完成以下场景，全程无手工介入：

1. **场景 A**：从热门推荐点 Inception 下载 → 选种子 → 自动识别为电影 → 自动复制到电影库 → Jellyfin 刮元数据 → 字幕/音轨自动处理。
2. **场景 B**：从种子搜索找一部新动漫 → LLM 识别 → TMDB 验证 → 加入动漫库。
3. **场景 C**：配 RSS rule 追新番 → 30 分钟内自动加种、入库、字幕处理。
4. **场景 D**：连续下载到 NVMe 380G → 自动清理回 280G，所有日志可查。
5. **场景 E**：剧集季内更新（已有 E01-E11，发布 E12 整季合集）→ 自动 filePrio 跳过 E01-E11，只下 E12 → 字幕只对 E12 处理。

每个场景都通过 = 整个流水线可交付。

---

## 附：未来扩展（不在本计划内）

- 种子文件树 UI 预览（加种子前看包内有什么，按规则跳过 sample 等）
- Tag 多维管理（按分辨率 / 字幕组 / 来源站点打标签）
- 流量曲线图表（每日上传/下载 / 配额历史）
- 智能推荐（基于已有库 + 热门 → 推荐"你可能想下"）
- 跨设备 / 多 qB 实例支持
