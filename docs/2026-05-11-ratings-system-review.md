# 评分系统 Review 报告

**日期**：2026-05-11
**范围**：评分获取链路、各源融合、前端展示
**结论**：**架构合理，但有 4 个真实问题需修，2 个待观察项需测试验证。**

---

## 1. 当前架构梳理

### 1.1 数据来源

| 源 | 接口 | 取值 | 量级 | 同步/异步 |
|---|---|---|---|---|
| MDB List | `https://api.mdblist.com/` | IMDb / RT 影评 / RT 观众 / Metacritic / Trakt / Letterboxd / 综合分 | 1000 req/天（免费） | 单条同步 + 批量异步 |
| 豆瓣 | HTML 爬取 `/subject_search?cat=1002` | 豆瓣评分 + 评分人数 + douban_id | 5s/req（防封） | 始终异步 |

### 1.2 链路时序

```
GET /api/ratings?tmdb_id=550&media_type=movie
    ↓
1. 查 DB media_ratings 表
2. mdblist_status != 'fresh' → 同步打 MDB List → 更新 DB
3. douban_status != 'fresh'  → 排队后台 worker（不阻塞响应）
4. 返回当前 DB 状态 + 各源缓存状态字段（fresh/stale/missing）

POST /api/ratings/batch  (列表页用)
    ↓
1. tuple_(tmdb_id, media_type) IN (...) 一次查 DB
2. MDB List 也走异步队列（避免列表页打几十次同步外呼）
3. 豆瓣同样异步入队
4. 立即返回当前 DB 状态；首次访问会"先空后补"
```

### 1.3 缓存元信息

每行 `media_ratings` 单独跟踪 `mdblist_fetched_at` 和 `douban_fetched_at`，TTL 默认 30 天（`mdblist_cache_ttl_days` / `douban_cache_ttl_days`）。这避免了"一家拉过另一家就被强缓存"，设计合理。

### 1.4 前端展示

`RatingsBadges.vue` 组件支持两种密度：
- `compact`：列表卡片用，4 大主源（IMDb / RT 影评 / MC / 豆瓣）
- `full`：详情页用，7 个源 + 拉取中状态

---

## 2. 发现的问题

### 🔴 P0：title 被 MDB List 英文标题覆盖，导致豆瓣搜索失败

**文件**：`web/backend/api/ratings.py:217-218`

```python
if parsed.get('title'):
    rating.title = parsed['title']  # 无条件覆盖
```

**问题**：MDB List 返回的总是**英文 title**（甚至 IMDb 风格）。当中文/日文电影第一次走完 MDB List → 排队豆瓣时，`queue_douban_fetch` 用的是已被覆盖成英文的 title：

```python
# ratings.py:389-390
if _douban_status(rating) != "fresh" and rating is not None:
    queue_douban_fetch(tmdb_id, media_type, rating.title, rating.year)
```

**举例**：《让子弹飞》→ MDB List 返回 "Let the Bullets Fly" → 用英文搜豆瓣 → 大概率搜不到中文条目。

**影响**：所有非英语原产国的影视，豆瓣评分常年取不到（一直 missing）。

**修复方案**（推荐 B）：

A. 别覆盖已有的 title。但 MediaRating 没有"原始 title"字段，初次拉取就是 MDB List 的英文。
B. 给 douban worker 单独传"中文 title hint"参数。前端 batch 请求里本来就有 hint，只是被 DB 现存的覆盖了。改：

```python
# ratings.py:458-459，优先用 hint，rating.title 兜底
title = hint.get('title') or (rating.title if rating else None)
year = hint.get('year') or (rating.year if rating else None)
```

C. MediaRating 加个 `local_title` 字段专给豆瓣用。最干净但要改 schema。

### 🟠 P1：豆瓣源/AniList 源条目没有 tmdb_id，拿不到任何评分

**位置**：`web/frontend/src/views/discover/Trending.vue:577-582`

```js
const fetchRatings = async () => {
  const payload = displayItems.value
    .filter((x) => x.tmdb_id && x.media_type !== 'person' && x.media_type !== 'anime')
    ...
}
```

**问题**：MediaRating 表用 `(tmdb_id, media_type)` 做唯一键。豆瓣源条目大多没有 tmdb_id，AniList 番剧也常常没有。所以这两个 tab 下评分卡片全程显示"暂无评分"。

**影响**：用户在豆瓣/AniList tab 下完全看不到融合评分，体验跟 TMDB tab 落差大。

**修复方案**：
- AniList 番剧：后端 discover/anilist 端点可以尝试用 anilist→TMDB 的映射（AniList GraphQL 有 `idMal` / `externalLinks` 可桥接），拿到后写到 item.tmdb_id；或者评分系统加 anilist_id 主键支持。
- 豆瓣条目：豆瓣页有 IMDb ID 跳转链接，爬取时一并解析存下来；评分系统按 imdb_id 去查 MDB List 即可。

短期：加一句注释说明这是已知限制；长期：双 ID 索引。

### 🟠 P1：Letterboxd 评分单位混乱

**文件**：`common/mdblist_client.py:32-39`、`web/backend/database.py:162`

DB 字段注释 `# Letterboxd (0-5)`，但 parser 用 `'float'` 直接存（不归一）：

```python
'letterboxd': ('letterboxd_rating', 'float'),
```

而 `frontend/RatingsBadges.vue:36` 显示 `{{ rating.letterboxd_rating.toFixed(1) }}` —— 不会自动加 "/5"。

**实测验证**：MDB List Letterboxd 返回值我没有实测，可能是 5 分制（如 3.4），也可能跟其他源一样标准化过。**建议加日志先采样几条数据看实际值范围**，再决定是否改 parser 或前端 label。

```python
# 临时加在 mdblist_client.py parse_ratings 里
if src == 'letterboxd' and v is not None:
    logger.info(f"[letterboxd-debug] tmdb={data.get('tmdbid')} raw_value={v}")
```

### 🟡 P2：豆瓣 worker 单线程串行，列表页冷启动很慢

**文件**：`web/backend/api/ratings.py:244-345`

豆瓣 worker 一次只处理一条，限速 5s/req（默认 douban_request_delay）。列表页一次进来 30 条豆瓣首次拉取，需要 **150 秒** 才能补齐。期间用户已经滚走或换 tab。

**现状是合理设计**（防豆瓣封 IP），但 UX 上只能观察"分批补齐"。

**改进方向**（可选）：
- 提示更直观：现在 RatingsBadges 在 full 模式才显示"拉取中"。compact 模式（列表卡片）完全没标识，用户看到一直空白会困惑。建议 compact 也加一个最小 dot 表示 pending。
- 队列优先级：用户当前正在看的卡片优先入队（按视口可见排）。

### 🟡 P2：aggregate_score 字段存了但没在前端展示

`MDB List` 返回的 `score` 是它自家的综合分（0-100）。我们存进 `aggregate_score` 字段，但 RatingsBadges 完全没显示。

**建议**：在 full 模式右边加一颗 `MDB 综合 87`，给用户一眼总览。或者直接当排序键暴露给用户（"按综合分排序"）。

### 🟢 P3：refresh API 可能引发无限刷新循环

**文件**：`ratings.py:469-492`

`POST /api/ratings/{tmdb_id}/refresh` 同步重取 MDB List，再排队豆瓣。如果前端在 RatingsBadges 上加自动重试（看到 stale 就 refresh），可能误打满配额。**目前前端没有自动 refresh 调用**，但建议在路由里加个简单的"同 IP 60s 内同 tmdb_id 不允许 refresh"防护。

---

## 3. 测试建议

### 3.1 必跑测试用例

| 场景 | 验证点 | 期望 |
|---|---|---|
| 中文电影《让子弹飞》| 首次访问后等 30s 看豆瓣评分 | 应显示豆瓣 8.7 分（修 P0 后） |
| 日剧《孤独的美食家》| AniList tab 看条目 | 当前会"暂无评分"——确认是 P1 已知限制 |
| 韩剧豆瓣榜 | 切到豆瓣 tab | 当前全是空——P1 已知 |
| Top Rated 1990 | 同步 MDB List 是否打回完整 7 项 | IMDB/MC/RT 都有 |
| Letterboxd 值 | DB 直接 select letterboxd_rating | 看实际数值范围（修 P1 必备） |

### 3.2 自动化测试现状

`tests/test_session_regressions.py` 已有评分相关，建议补：
- 验证 title 不被 MDB List 英文覆盖（针对 P0 修复后）
- batch 端点 hint 优先级（针对 P0）
- Letterboxd 归一逻辑（针对 P1）

### 3.3 手动数据采样

```sql
-- 看看哪些条目豆瓣一直 missing
SELECT tmdb_id, media_type, title, mdblist_fetched_at, douban_fetched_at
FROM media_ratings
WHERE douban_fetched_at IS NULL AND mdblist_fetched_at IS NOT NULL
LIMIT 50;

-- 看看 letterboxd 实际值范围
SELECT MIN(letterboxd_rating), MAX(letterboxd_rating), AVG(letterboxd_rating),
       COUNT(*) FILTER (WHERE letterboxd_rating > 5) AS over_5
FROM media_ratings WHERE letterboxd_rating IS NOT NULL;
```

---

## 4. 总结优先级

| 优先级 | 问题 | 工时估计 |
|---|---|---|
| P0 | title 覆盖导致豆瓣搜失败 | 30 分钟 |
| P1 | AniList/豆瓣条目无评分 | 4-6 小时（需补 ID 桥接） |
| P1 | Letterboxd 单位 | 1 小时（采样 + 决策 + 改） |
| P2 | aggregate_score 不显示 | 30 分钟 |
| P2 | 豆瓣队列冷启动慢 UX | 2 小时（compact pending hint + 优先级） |
| P3 | refresh 防滥用 | 30 分钟 |

**建议下一步**：先修 P0（中文影视豆瓣评分），其次跑 P1 Letterboxd 采样，最后做 P1 ID 桥接。
