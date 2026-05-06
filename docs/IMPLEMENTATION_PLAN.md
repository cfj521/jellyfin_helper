# 下一步实施计划

> 生成日期：2026-05-03
> 基于现状盘点：项目整体完成度约 **40-50%**，文档存在夸大。本计划按"先修复 → 再补全 → 最后扩展"的顺序排列。

---

## 0. 现状速览

| 模块 | 实际完成度 | 主要问题 |
|------|----------|---------|
| CLI 工具 - actor_fix | 95% | 可用 |
| CLI 工具 - subtitle_scanner | 100% | 可用 |
| CLI 工具 - subtitle_renamer | 95% | 返回值类型与 Web API 不匹配 |
| CLI 工具 - subtitle_downloader | 85% | 缺 OpenSubtitles 凭证 |
| Web 后端 - tasks/stats | 100% | 可用 |
| Web 后端 - subtitle | 70% | **3 处 bug 导致崩溃** |
| Web 后端 - metadata | 90% | 缺海报相关端点 |
| Web 后端 - media | 85% | 重复检测仅按文件大小 |
| Web 前端 - Subtitle Download | **5%** | 仅占位符 |
| Web 前端 - Metadata Posters | **5%** | 仅占位符 |
| Web 前端 - Settings | 50% | 只读，无编辑 |
| Jackett / qBittorrent 集成 | **0%** | 配置已写但无代码 |
| 成人内容管理 | **0%** | 仅数据库表 `adult_items` |

---

## Phase 1 — 关键修复（P0，1-2 天）

> 目标：让现有声称"已完成"的功能真正能跑通。

### 1.1 修复 `web/backend/api/subtitle.py` 的崩溃 bug

| 位置 | 错误 | 修复 |
|------|------|------|
| Line 95 | `scanner.scan_directory(...)` 方法不存在 | 改为 `scanner.scan(...)` |
| Line 104-107 | 属性名 `with_subtitles` / `without_subtitles` 不存在 | 改为 `videos_with_sub` / `videos_without_sub` |
| Line 202-213 | `renamer.process_directory()` 返回 `int`，下游按 `List[dict]` 处理 | 二选一：① 改 renamer 返回 `List[dict]`；② 改 API 按 int 处理。**推荐 ①**，便于前端展示明细 |

**验收**：在 Web 界面发起一次字幕扫描和一次重命名，任务状态能从 `running` 推进到 `completed`，报告能在 `/api/subtitle/reports` 列出。

### 1.2 补全 `Download.vue` 前端

参照 `Scan.vue` 的结构：
- 表单：扫描报告下拉选择 + 语言偏好 + 干运行开关
- 点击"开始下载" → POST `/api/subtitle/download`（**端点不存在，需顺带新建**）
- 表格：实时显示每个视频的下载状态（成功/失败/跳过）

### 1.3 补全 `web/backend/api/subtitle.py` 的 download 端点

```
POST /api/subtitle/download
  body: { report_id, languages, dry_run }
  → 创建 Task，后台调用 SubtitleDownloader
GET  /api/subtitle/download/{task_id}
  → 返回每个视频的下载明细
```

### 1.4 修正 README

把"字幕下载"和"Web 管理界面"从"已完成"列表中调整为"部分完成"，避免继续误导。

---

## Phase 2 — 现有功能补全（P1，3-5 天）

### 2.1 海报下载（前后端）

**后端** `web/backend/api/metadata.py` 新增：
```
GET  /api/metadata/posters?missing=true   # 列出缺海报的电影/剧集
POST /api/metadata/posters/scan           # 扫描 Jellyfin 媒体，标记缺海报项
POST /api/metadata/posters/fix            # 批量从 TMDB 下载并上传
POST /api/metadata/posters/{item_id}/fix  # 单个修复
```

**前端** `Posters.vue`：参照 `Actors.vue` 实现，列表 + 批量修复 + 单条预览。

**底层**：复用 `common/tmdb_client.py`，新增 `get_movie_poster()` / `get_tv_poster()` 方法（TMDB 已有 `images` 接口）。

### 2.2 Settings.vue 改为可编辑

当前只展示配置。改造为：
- 表单编辑各模块配置（Jellyfin/TMDB/字幕等）
- 后端新增 `PUT /api/config` 写回 `config.yaml`
- 敏感字段（API Key/密码）做 mask 显示，留空则保留原值

### 2.3 改善重复检测

当前仅按文件大小分组，误报率高。改为：
1. 第一轮：按文件大小聚类（快速过滤）
2. 第二轮：对同大小的候选计算 SHA1（可选用 xxhash 提速）+ 视频时长（用 ffprobe）
3. 报告中标注"完全相同"vs"大小相同但内容不同"

### 2.4 字幕下载凭证

`config.yaml` 中 `subtitle.opensubtitles_*` 全部为空。两种方案任选：
- **方案 A**：让用户在 Settings 页面填入（依赖 2.2）
- **方案 B**：增加备用源（如 [Subliminal](https://github.com/Diaoul/subliminal) 库支持 OpenSubtitles + Addic7ed + Podnapisi 多源）

推荐 **B**，单一源易失效。

---

## Phase 3 — 内容推荐与下载链路（P1，5-7 天）

> 这是把"管理"扩展为"获取"的关键。Jackett 和 qBittorrent 配置已写，但 0 代码。

### 3.1 数据层

`common/` 下新增：
- `jackett_client.py` — 调用 Jackett 的 `/api/v2.0/indexers/all/results` 端点，参数 `apikey` + `Query`
- `qbittorrent_client.py` — qBittorrent WebUI API，关键端点：
  - `POST /api/v2/auth/login`
  - `POST /api/v2/torrents/add`（支持 magnet/torrent 文件）
  - `GET /api/v2/torrents/info`

### 3.2 后端 API

新建 `web/backend/api/discover.py`：
```
GET  /api/discover/trending          # 热门推荐（豆瓣/TMDB）
POST /api/discover/search            # Jackett 搜索（关键词 + 分类）
POST /api/discover/download          # 推送 magnet 到 qBittorrent
GET  /api/discover/downloads         # 下载任务列表（来自 qBittorrent + 本地 download_tasks 表）
```

数据持久化已经有 `DownloadTask` 表，直接复用。

### 3.3 前端

新增 `views/discover/` 目录：
- `Trending.vue` — 热门榜单（豆瓣 Top250 / TMDB Popular）
- `Search.vue` — 搜索框 + Jackett 结果列表 + "推送下载"按钮
- `Downloads.vue` — qBittorrent 下载状态（进度条、暂停/恢复/删除）

### 3.4 豆瓣数据来源

豆瓣无官方 API。两种选择：
- **爬虫**：`requests` + `BeautifulSoup4`（已在 requirements）—— 注意频控
- **第三方代理**：如 `https://douban-api.com`（不稳定）

推荐先做 TMDB Popular（已有 client），豆瓣作为可选增强。

---

## Phase 4 — 成人内容管理（P2，5-7 天）

> 你专门提到的功能。当前仅有数据库表，需要从零搭建。

### 4.1 数据流设计

```
本地媒体目录
   ↓ (扫描)
番号识别 (regex from filename)
   ↓
JavBus / JavDB 刮削
   ↓
入库 adult_items + 下载封面 + 生成 NFO
   ↓
Jellyfin 重新扫描
```

### 4.2 番号识别

`tools/adult_manager/code_extractor.py`：
- 正则覆盖常见格式：`ABC-123`、`ABC123`、`FC2-PPV-1234567`、`HEYZO-1234`、`Carib-010120-001` 等
- 文件名预清理（去除画质标签 `1080p`、字幕组、扩展名等）
- 返回标准化番号（统一大写、连字符）

### 4.3 刮削器

`tools/adult_manager/scrapers/`：
- `base.py` — `BaseScraper` 抽象类，定义 `search(code)` / `get_detail(url)` 接口
- `javbus.py` — JavBus HTML 解析（`BeautifulSoup`）
- `javdb.py` — JavDB（部分页面需要登录，暂只做公开页）
- `manager.py` — 多源回退：JavBus 失败时尝试 JavDB

**反爬注意**：
- 配置中已有 `adult.scraper_delay`（默认 1.0s），每次请求后 sleep
- 必要时支持代理（新增配置 `adult.proxy`）
- User-Agent 轮换

### 4.4 NFO 生成

按 [Kodi/Jellyfin movie.nfo 规范](https://kodi.wiki/view/NFO_files/Movies)：
```xml
<movie>
  <title>...</title>
  <originaltitle>...</originaltitle>
  <plot>...</plot>
  <studio>...</studio>
  <director>...</director>
  <actor><name>...</name></actor>
  <genre>...</genre>
  <premiered>YYYY-MM-DD</premiered>
  <uniqueid type="num">{code}</uniqueid>
</movie>
```

封面命名：`{filename}-poster.jpg` / `{filename}-fanart.jpg`（Jellyfin 自动识别）

### 4.5 后端 API

新建 `web/backend/api/adult.py`：
```
POST /api/adult/scan                 # 扫描媒体目录，识别番号入库
POST /api/adult/scrape/{code}        # 单条刮削
POST /api/adult/scrape/batch         # 批量刮削（仅未刮削的）
GET  /api/adult/items                # 列表（支持搜索/筛选/分页）
GET  /api/adult/items/{id}           # 详情
PUT  /api/adult/items/{id}           # 手动修正元数据
POST /api/adult/items/{id}/nfo       # 重新生成 NFO
```

### 4.6 前端

新增 `views/adult/` 目录（菜单项需要 `config.yaml` 中 `adult.enabled: true` 才显示）：
- `Library.vue` — 番号库列表（缩略图网格 + 筛选：演员/厂商/标签/已刮削）
- `Detail.vue` — 单条详情 + 手动编辑
- `Scan.vue` — 扫描媒体目录入口
- `Tags.vue` — 演员/标签云（用于快速筛选）

### 4.7 隐私与开关

- `config.yaml` 中 `adult.enabled` 必须为 `true` 才注册路由和挂载菜单
- 前端首次进入需要二次确认（避免在公共环境误显示）
- 可选：Settings 中提供"清空缓存"按钮，一键清空 `adult_items` 表和封面文件

### 4.8 法律与合规提醒

⚠️ 这一块在文档中明确标注：
> 仅供个人在本地媒体库管理使用。请确保你所在地区允许此类内容的持有，并遵守目标网站的 robots.txt 和服务条款。本工具不分发任何受版权保护的内容。

---

## Phase 5 — 工程化收尾（P2，2-3 天）

### 5.1 配置安全

- 创建 `config.yaml.example`（占位符版本）
- `config.yaml` 加入 `.gitignore`
- `.env` 支持（敏感值可走环境变量覆盖 yaml）

### 5.2 依赖整理

- ✅ 已完成：项目移除 CLI 入口与 Docker 部署后，统一为根目录单一 `requirements.txt`
- 后续新增依赖（如 ffmpeg-python 等）逐项追加

### 5.3 测试

目前 0 测试。最小覆盖：
- `tests/test_subtitle_scanner.py` — 用临时目录构造样例 video/srt，验证匹配逻辑
- `tests/test_code_extractor.py`（Phase 4 后）— 一组已知文件名 → 期望番号的映射
- `tests/test_jellyfin_client.py` — 用 `responses` 库 mock HTTP

### 5.4 文档同步

修复 `docs/DEVELOPMENT.md` 和 `README.md` 中所有"已完成"标记，确保和真实代码一致。

### 5.5 错误监控

- Web 后端引入统一异常处理（FastAPI exception handler）
- 任务失败时把 traceback 写入 `Task.message` 字段，前端 Tasks 页可展开查看

---

## 优先级建议总结

| 阶段 | 优先级 | 预计工时 | 价值 |
|------|--------|---------|------|
| Phase 1 关键修复 | **P0 立即** | 1-2 天 | 让现有功能能跑 |
| Phase 2 功能补全 | P1 | 3-5 天 | 兑现已宣传功能 |
| Phase 3 推荐下载 | P1 | 5-7 天 | 项目核心增量 |
| Phase 4 成人内容 | P2 | 5-7 天 | 你专门要求的模块 |
| Phase 5 工程收尾 | P2 | 2-3 天 | 长期可维护性 |

**总计：约 16-24 个工作日**

---

## 建议的执行顺序

考虑到你是 Python 较熟悉、前端较薄弱：
1. **本周**：完成 Phase 1（修 bug + 补 Download 页面），跑通完整字幕链路
2. **下周**：Phase 2.1 海报下载（前后端逻辑接近 Actors，可快速复制）+ Phase 2.3 重复检测改进
3. **第三周**：Phase 4 成人内容（你重点关心的）—— 后端为主，前端用 Element Plus 表格 + 网格组件即可
4. **第四周起**：Phase 3 推荐下载（最大不确定性来自豆瓣数据源）
5. **末尾**：Phase 5 收尾

如果时间紧张，**Phase 3 的"豆瓣榜单"可以先砍掉**，只做 Jackett 搜索 + qBit 推送，仍然实用。

---

## 需要你决策的几个点

1. **OpenSubtitles 凭证**：你是否已有账号？如果没有，建议 Phase 2 改走 Subliminal 多源
2. **重复检测策略**：是否需要 ffprobe 计算视频时长（依赖 ffmpeg 安装）？还是只比文件 hash 即可
3. **Phase 4 范围**：是否同时需要 JavBus + JavDB，还是只做 JavBus 即可？
4. **豆瓣榜单**：是接受爬虫方案，还是直接砍掉只做 TMDB
5. **Settings 编辑功能**：是否需要权限控制（避免局域网内误改）？
