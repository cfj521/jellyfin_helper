# 2026-05-11 通宵任务进度

用户清单（共 7 项）：

| # | 标题 | 状态 |
|---|---|---|
| 1 | 搜种子按钮：附年份 + 非英文转英文 | ✅ 完成 |
| 2a | 海报左上角加 媒体类型徽标 | ✅ 完成 |
| 2b | 原 电影/剧集 位置改为 风格/类型标签 | ✅ 完成 |
| 2c | AniList/豆瓣 自建详情页（套用 TMDB 结构） | ⏭ 部分（豆瓣详情后端接口已加；自建 UI 留下次） |
| 3 | 简介改进：移除标题 + 贯通 anilist/豆瓣 介绍文 | ✅ 完成 |
| 4 | 豆瓣白名单：去重 + 替换失效片单 | ✅ 完成 |
| 5 | 媒体库改用无限滚动 | ❌ 未做（范围过大单次 session 难保证质量） |
| 6 | 评分系统 review + 报告 | ✅ 完成（落盘） |
| 7 | 流水线部分重复处理 + 报告 | ✅ 完成（落盘） |

---

## 已完成详情

### #1 搜种子按钮 — 英文优先 + 年份附带
- 后端新增 `GET /api/discover/title-en?tmdb_id=X&media_type=Y`：用 TMDB `/translations` 抽英文标题，30 天 kv_cache
- 前端 Trending.vue `searchTorrents` 改为 async：英文标题 → 后端查 → 兜底 original_title → title；query 末尾自动追加 `${year}`
- Detail.vue `searchTorrents` 同样追加年份（用 release_date 切前 4 位）

### #2a 媒体类型徽标
- Trending.vue 海报左上角新增 `.media-type-pill`：emoji + 文字组合，半透明深色底 + 按 media_type 着色的左侧色条
- 配色：电影→蓝、剧集→橙、番剧→粉、成人→红、人物→灰

### #2b 风格/类型标签
- 后端 `_normalize_tmdb` 引入 TMDB genre 映射缓存（`/genre/movie/list` + `/genre/tv/list`，30 天 TTL）→ 列表项带 `genres: [...]`
- Trakt 客户端 `TraktItem` 新增 `genres` 字段（extended=full 已返回）
- AniList 列表 query 已含 genres，豆瓣 doulist 解析也已含 genres
- 前端 .meta 里旧的"电影/剧集"el-tag 替换为 `el-tag` 风格标签数组（取前 2 个）

### #3 简介改进
- 移除 overview-overlay 中的 .overview-title（不再展示标题）
- AniList 列表 query 添加回 `description(asHtml: false)` —— 列表场景就能直接显示简介
- 豆瓣条目页详情新增后端 `GET /api/discover/douban-detail?douban_id=X`（30 天 kv_cache）
- 豆瓣 client `fetch_subject_summary` 爬条目页 → 抽 summary / cast / countries / genres / IMDb 等
- 前端 toggleOverview 改 async：豆瓣条目首次点开"简介"时按需 lazy-fetch 真正的剧情简介

### #4 豆瓣白名单
- 用户反馈：1（Top 250）和 2（高分华语电影）相似；3（高分日剧）和 4（高分韩剧）失效
- 改为：豆瓣 Top 250 / 必看高分剧情片 / 高分美剧 / 一生必看的100部电影
- 三处同步：config.yaml、config.yaml.example、config_models.py 默认值

### #6 评分系统 review
落盘：`docs/2026-05-11-ratings-system-review.md`
- 4 个真实问题（按优先级）：
  - **P0** title 被 MDB List 英文覆盖 → 中文片豆瓣搜索失败
  - **P1** AniList/豆瓣源条目无 tmdb_id → 评分一直 missing
  - **P1** Letterboxd 评分单位（5分制 vs 10分制）需采样验证
  - **P2** aggregate_score 字段存了但前端没展示
- 含 SQL 采样脚本 + 测试用例建议

### #7 流水线重复处理 review
落盘：`docs/2026-05-11-dispatch-partial-duplicate.md`
- 8 类重复场景分析（D1~D8）
- **关键风险**：D7（Repack/Proper）当前会**损坏文件**（copier.py 续传逻辑遇到目标更小但来源不同的文件会 append）
- 推荐方案：
  - 短期：copier 续传分支前加 mtime/source-hash 防御
  - 长期：DispatchRule.duplicate_policy 配置项 + 质量比较 + 被替换文件转 trash
- 含完整的测试用例清单

---

## 未完成 / 部分完成

### #2c AniList/豆瓣 自建详情页
**已做**：后端 `/api/discover/douban-detail` 接口完成，AniList 列表 query 已含 description（详情页前端可复用）

**未做**：
- 前端 `AniListDetail.vue` / `DoubanDetail.vue` 新增（套用 TMDB Detail.vue 的 hero / cast / similar 结构）
- 路由表加 `/discover/anilist/:id` `/discover/douban/:id`
- Trending.vue openDetail 把 AniList/豆瓣 条目从 `window.open` 切到内部路由

**为何延迟**：完整套用 TMDB 详情页结构（hero + 信息表 + 演员 + 视频 + 相似推荐）要 4-6 小时，且要测各类边界（演员图缺失 / 简介缺失 / 跨源 ID 桥接）。单次 autonomous session 内做完且不引入 regression 的概率低。
**建议下次**：先做 DoubanDetail.vue（数据更齐全），再做 AniListDetail.vue。

### #5 媒体库无限滚动
**完全未做**。

**为何延迟**：媒体库视图（LibraryDetail.vue）跟 Trending.vue 的数据流差异大：
- 媒体库要支持搜索 / 多维筛选（genre / year / 字幕 / 分辨率）
- 列表/网格双视图切换
- 选中态 + 批量操作（删除 / 维护工具栏）
- 现有分页逻辑深度耦合 jellyfinApi.libraryItems(start_index, limit, ...)

把现有分页改无限滚动等于重写视图层。需要至少 4-6 小时 + 充分手测。autonomous session 单次做完风险太高。
**建议下次**：先在 LibraryDetail.vue 加一个"无限滚动"开关 toggle（保留分页器作为 fallback），逐步迁移。

---

## 需要用户决策的事项

1. **P0 修复 (评分)**：要不要直接做掉？涉及 ratings.py:217-218 的 title 覆盖逻辑改造。
2. **下次优先级**：#2c vs #5 哪个先做？我倾向先 #2c（用户可见性高 + 范围相对收敛）。
3. **流水线 D7 隐患**：要不要做短期防御（copier mtime 检查）？这个不修，现有 PROPER/REPACK 种子可能损坏库内文件。
