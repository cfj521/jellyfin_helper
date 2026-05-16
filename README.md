# Jellyfin Helper

围绕 Jellyfin 的自动化辅助工具集 —— 把"找资源 → 下载 → 入库 → 元数据 / 字幕 / 音轨修复"整条链路用一个 Web 后台串起来。

不是 Sonarr/Radarr/Bazarr 的替代品，而是把它们 + 国内场景常见的工具（豆瓣评分、JavBus/JavDB、ASSRT 字幕、Jackett）按个人偏好揉成一套。

> ⚠️ 个人项目，仍在密集迭代。schema 变更时通常**直接清表重扫**而不是写迁移脚本，部署到生产请自行评估。

---

## 主要能力

| 领域 | 核心能力 |
|---|---|
| **媒体库浏览** | 多库列表 + 详情页 + 海报视图，调 jellyfin REST 拿一手数据；支持本地 SQLite 直读加速 `path → item` 反查（同机部署可选） |
| **下载入库自动化** | Jackett 搜种 → qBittorrent 推送 → 流水线 confidence-driven 识别（regex / TMDB / LLM 兜底）→ 按模板 + duplicate_policy 整理到媒体库 → 自动通知 jellyfin 刷新 |
| **字幕全链路** | 多源下载（OpenSubtitles / ASSRT / Shooter）→ 评分融合 + 分层语种排序 → 文件名按 BCP 47 落盘 → 缺字幕智能补齐 → 内嵌字幕轨 ffprobe 探测 |
| **元数据修复** | 演员照片（TMDB + Wikidata 兜底）、海报、NFO；jellyfin 演员库归一化 |
| **音轨管理** | MKV 默认音轨按语种偏好批量设置；汉语/未识别轨例外保护 |
| **评分聚合** | MDBList + 豆瓣双源，DB 缓存 + 异步 worker 后台补齐；前端融合显示 |
| **发现 / 推荐** | TMDB / Trakt / AniList / 豆瓣榜单聚合，无限滚动 + 预取 |
| **资源搜索** | Jackett 跨 indexer 聚合，结果分类 + 大小 / Seeders 排序 |
| **成人内容（可选）** | 番号识别、JavBus + JavDB 双源刮削、女优档案库（javdb + Minnano-AV chain）、健康度 + 冷却保护 |
| **任务系统** | 后台任务 + SSE 实时进度 + 取消 + shutdown 联动；前端任务详情页可展开各步明细 |
| **维护工具** | 配置 Web 编辑（保存自动备份）、Sample 清理、强制重扫、日志查看、统计 |

---

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.12 / FastAPI / SQLAlchemy / uvicorn |
| 前端 | Vue 3 (Composition API) / Element Plus / Vite |
| 数据库 | PostgreSQL 12+（业务库） |
| 任务推送 | SSE（不用 WebSocket）|
| 反爬 | curl-cffi（TLS impersonation 过 Cloudflare 弱反爬） |
| LLM | 任何 OpenAI 兼容 endpoint（DeepSeek / 阿里通义 / OpenAI 本身） |

---

## 项目结构

```
jellyfin-helper/
├── config.yaml                    # 主配置（含 API Key，git 不提交）
├── config.yaml.example            # 配置模板
├── common/                        # 第三方服务客户端 + 工具
│   ├── jellyfin_client.py         #   jellyfin REST
│   ├── jellyfin_db.py             #   jellyfin SQLite 直读（path→item 反查加速，可选）
│   ├── tmdb_client.py
│   ├── trakt_client.py            #   Trakt 趋势 / 评分
│   ├── anilist_client.py          #   动漫元数据
│   ├── mdblist_client.py          #   评分聚合
│   ├── douban_client.py           #   中文评分（带熔断）
│   ├── wikidata_client.py         #   演员照片兜底
│   ├── jackett_client.py
│   ├── qbittorrent_client.py
│   ├── llm_client.py              #   LLM 媒体识别兜底
│   ├── lang_utils.py              #   字幕语言代码归一化（含 zh / chs / cht / BCP 47）
│   └── label_cleaner.py
│
├── tools/                         # 业务模块（被后端 import）
│   ├── subtitle_manager/          #   扫描 / 重命名 / 内嵌探测
│   ├── subtitle_downloader/       #   多源下载 + 评分融合
│   ├── audio_manager/             #   MKV 音轨调整
│   ├── actor_fix/                 #   演员照片
│   ├── adult_manager/             #   番号刮削 + 女优档案
│   └── dispatch/                  #   下载入库自动化流水线
│       ├── pipeline_worker.py     #     状态机推进
│       ├── analyzer.py            #     identify confidence-driven
│       ├── organizer.py           #     按模板复制 + duplicate_policy
│       ├── adopt.py               #     qB 外部加种发现
│       ├── post_process.py        #     字幕 / 音轨后处理编排
│       └── ...
│
├── web/
│   ├── backend/                   # FastAPI 后端
│   │   ├── main.py                #   应用入口 + lifespan + shutdown
│   │   ├── run.py                 #   uvicorn 启动器
│   │   ├── config.py              #   配置 + pydantic settings
│   │   ├── database.py            #   SQLAlchemy models
│   │   ├── diagnostics.py         #   性能 + DB 池监控 + access log 过滤
│   │   ├── api/                   #   18 个 router（按功能切片，见下）
│   │   ├── services/              #   后台服务（jellyfin WS 监听等）
│   │   └── scrapers/              #   旧字幕抓取（保留兼容）
│   │
│   └── frontend/                  # Vue SPA
│       ├── package.json
│       ├── vite.config.js
│       └── src/
│           ├── views/             #   页面（含 medialibraries / downloadpipeline / settings 等）
│           ├── components/        #   通用组件（含 task-detail 系列）
│           └── api/               #   axios 封装
│
└── docs/
    ├── DEVELOPMENT.md             # 开发指南
    ├── external-services.md       # 第三方服务清单 + 代理建议
    └── archive/                   # 已实现 PRD / 历史决策归档
```

### Backend API 路由

| 前缀 | 文件 | 主要职责 |
|---|---|---|
| `/api/medialibraries` | `medialibraries.py` | 库列表 / 详情 / item 反查（DB 直读 + REST 兜底）|
| `/api/media` | `media.py` | 文件浏览、重复检测、存储分析 |
| `/api/subtitle` | `subtitle.py` | 扫描 / 重命名 / 自动下载 / 字幕语言探测 |
| `/api/metadata` | `metadata.py` | 演员照片、海报、NFO 修复 |
| `/api/audio` | `audio.py` | MKV 默认音轨 |
| `/api/adult` | `adult.py` | 番号刮削、女优档案 |
| `/api/ratings` | `ratings.py` | MDBList + 豆瓣评分聚合 |
| `/api/discover` | `discover.py` | TMDB / Trakt / AniList / 豆瓣榜单 |
| `/api/resourcesearch` | `resourcesearch.py` | Jackett 聚合搜索 |
| `/api/downloadpipeline` | `downloadpipeline.py` | qB 状态监控 + 推种入口 |
| `/api/dispatch` | `dispatch.py` | 流水线 needs_review / quota / dispatch_map |
| `/api/maintenance` | `maintenance.py` | Sample 清理、强制重扫、自动修复编排 |
| `/api/tasks` | `tasks.py` | 任务列表 / 详情 / 取消 / SSE 推送 |
| `/api/stats` | `stats.py` | 总览统计 |
| `/api/config` | `config_api.py` | 读写 config.yaml + 自动备份 |
| `/api/logs` | `logs.py` | 后端日志查看 |
| `/api/img_proxy` | `img_proxy.py` | 第三方图片代理（绕跨域 + CDN） |

---

## 快速开始

### 环境要求

- Python 3.12（推荐用 conda 隔离环境）
- Node.js 20+
- PostgreSQL 12+（先建库和用户）
- 一台运行中的 Jellyfin（10.9+ 推荐）+ 管理员 API Key

### 1. 配置

```bash
cp config.yaml.example config.yaml
```

至少填这几项（其它都可选）：

```yaml
database:
  host: "127.0.0.1"
  name: "jellyfin_helper"
  user: "jellyfin_helper"
  password: "your_password"

jellyfin:
  host: "http://your-jellyfin:8096"
  api_key: "your_jellyfin_admin_api_key"
  # 可选：jellyfin DB 直读加速 path→item 反查（同机或 SMB 挂载时填）
  # db_path: "/var/lib/jellyfin/data/jellyfin.db"

tmdb:
  api_key: "your_tmdb_api_key"
```

完整字段见 [config.yaml.example](config.yaml.example)；也可先空跑，再到前端 `/settings` 编辑（保存自动备份）。

### 2. 后端

```bash
pip install -r requirements.txt
python -m web.backend.run
```

端口优先级：环境变量 `BACKEND_PORT` > `config.yaml: server.backend_port` > 默认 8000。

开发热重载：

```bash
# bash
BACKEND_RELOAD=1 python -m web.backend.run

# PowerShell
$env:BACKEND_RELOAD='1'; python -m web.backend.run
```

### 3. 前端

新开终端：

```bash
cd web/frontend
npm install
npm run dev
```

vite 自动读 `config.yaml` 的 `server.frontend_port`。

### 4. 访问

- 前端：http://localhost:5173
- API 文档（Swagger）：http://localhost:8000/docs

---

## 外部服务

详见 [docs/external-services.md](docs/external-services.md)。摘要：

- **必需**：Jellyfin、TMDB、PostgreSQL
- **强烈推荐**：Jackett + qBittorrent（启用下载入库流水线）
- **字幕**：OpenSubtitles + ASSRT 任选其一即可，配齐两个最佳
- **评分**：MDBList（可选）+ 豆瓣（无需 key）
- **LLM**：任意 OpenAI 兼容服务（识别兜底，可选）
- **成人内容**：JavBus / JavDB / AVBase / MissAV（带地理屏蔽与代理建议）

---

## 文档

- 开发者向：[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) —— 模块组织、添加新 router 的流程、调试技巧
- 第三方服务：[docs/external-services.md](docs/external-services.md) —— 调用频率、地理屏蔽、OpenClash 分流示例
- 历史 PRD / 决策记录：[docs/archive/](docs/archive/) —— 已实现功能的设计文档归档

---

## License

MIT
