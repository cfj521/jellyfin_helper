# Jellyfin Tools 开发文档

## 1. 项目概述

### 1.1 项目简介
Jellyfin Tools 是一套用于管理和优化 Jellyfin 媒体服务器的 Web 应用，帮助用户解决元数据缺失、字幕管理、媒体库维护、内容刮削等常见问题。

### 1.2 目标用户
- Jellyfin 自建服务器用户
- 有大量影视内容需要管理的用户
- 需要批量处理媒体文件的用户

### 1.3 技术栈
| 层级 | 技术选型 |
|------|----------|
| 后端 | Python 3.11+, FastAPI |
| 前端 | Vue.js 3, Element Plus, Vite |
| 数据库 | PostgreSQL 12+ |

### 1.4 服务配置

| 服务 | 地址 | 说明 |
|------|------|------|
| PostgreSQL | 192.168.89.11:5432 | 数据库 |
| Jellyfin | 192.168.89.8:8096 | 媒体服务器 |
| Jackett | 192.168.89.12:9117 | 种子搜索 |
| qBittorrent | 192.168.89.6 | 下载管理 |
| 本应用部署 | 192.168.89.12:8000 | Web 应用 |

---

## 2. 功能模块

### 2.1 功能总览

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           Jellyfin Tools                                 │
├─────────────┬─────────────┬─────────────┬──────────────┬────────────────┤
│   元数据    │    字幕     │   媒体库    │   内容推荐   │   成人内容     │
├─────────────┼─────────────┼─────────────┼──────────────┼────────────────┤
│ 演员照片修复│ 扫描报告    │ 重复检测    │ 豆瓣/TMDB    │ 番号识别       │
│ 海报下载    │ 自动重命名  │ 规范化命名  │ Jackett搜索  │ JavBus/JavDB   │
│ NFO修复     │ 自动下载    │ 存储分析    │ qBit下载     │ NFO生成        │
└─────────────┴─────────────┴─────────────┴──────────────┴────────────────┘
```

### 2.2 功能详细说明

#### 2.2.1 元数据管理

| 功能 | 状态 | 优先级 | 说明 |
|------|------|--------|------|
| 演员照片修复 | ✅ 已完成 | P0 | 从 TMDB 获取演员图片上传到 Jellyfin |
| 海报/背景图下载 | 待开发 | P1 | 批量下载缺失的电影/剧集海报 |
| NFO元数据修复 | 待开发 | P2 | 修复/生成 NFO 文件 |

#### 2.2.2 字幕管理

| 功能 | 状态 | 优先级 | 说明 |
|------|------|--------|------|
| 字幕扫描报告 | ✅ 已完成 | P0 | 扫描媒体目录，生成HTML报告 |
| 字幕重命名 | ✅ 已完成 | P0 | 自动匹配视频文件重命名字幕 |
| 字幕下载 | ✅ 已完成 | P0 | 从 OpenSubtitles 下载字幕 |

#### 2.2.3 媒体库管理

| 功能 | 状态 | 优先级 | 说明 |
|------|------|--------|------|
| 重复文件检测 | 基础完成 | P1 | 基于文件名/大小检测重复 |
| 文件规范化命名 | 待开发 | P1 | 按 Jellyfin 推荐格式重命名 |
| 存储空间分析 | 基础完成 | P2 | 可视化存储占用 |

#### 2.2.4 内容推荐与下载

| 功能 | 状态 | 优先级 | 说明 |
|------|------|--------|------|
| 热门推荐聚合 | 待开发 | P1 | 从豆瓣/TMDB获取热门内容 |
| Jackett搜索 | 待开发 | P1 | 搜索种子资源 |
| qBittorrent下载 | 待开发 | P1 | 自动添加下载任务 |

#### 2.2.5 成人内容管理

| 功能 | 状态 | 优先级 | 说明 |
|------|------|--------|------|
| 番号识别 | 待开发 | P1 | 从文件名提取番号 |
| 元数据刮削 | 待开发 | P1 | 从JavBus/JavDB获取信息 |
| NFO生成 | 待开发 | P1 | 生成Jellyfin兼容的NFO |

---

## 3. 系统架构

### 3.1 整体架构

```
┌────────────────────────────────────────────────────────────────┐
│                         用户层                                  │
│  ┌──────────────┐                       ┌──────────────┐       │
│  │   Web UI     │                       │   API 调用   │       │
│  └──────┬───────┘                       └──────┬───────┘       │
└─────────┼─────────────────────────────────────┼────────────────┘
          │                                     │
┌─────────▼─────────────────────────────────────▼────────────────┐
│                       API 网关层                                │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                    FastAPI Server                        │   │
│  │   /api/subtitle/*  /api/media/*  /api/recommend/*       │   │
│  └─────────────────────────────────────────────────────────┘   │
└────────────────────────────┬───────────────────────────────────┘
                             │
┌────────────────────────────▼───────────────────────────────────┐
│                        业务逻辑层                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │ SubtitleSvc  │  │  MediaSvc    │  │ DownloadSvc  │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
└────────────────────────────┬───────────────────────────────────┘
                             │
┌────────────────────────────▼───────────────────────────────────┐
│                        数据访问层                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │ Jellyfin API │  │   TMDB API   │  │ Jackett API  │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │  PostgreSQL  │  │  qBittorrent │  │  文件系统    │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
└────────────────────────────────────────────────────────────────┘
```

### 3.2 目录结构

```
jellyfin-tools/
├── config.yaml                 # 主配置文件
├── requirements.txt            # Python 依赖（后端 + tools 模块）
├── docs/                       # 文档
│   ├── DEVELOPMENT.md
│   └── IMPLEMENTATION_PLAN.md
├── common/                     # 共用模块
│   ├── __init__.py
│   ├── config.py
│   ├── jellyfin_client.py
│   ├── tmdb_client.py
│   ├── jackett_client.py
│   └── qbittorrent_client.py
├── tools/                      # 业务模块（被 Web 后端 import）
│   ├── actor_fix/
│   ├── subtitle_manager/
│   ├── subtitle_downloader/
│   ├── audio_manager/
│   └── adult_manager/
└── web/                        # Web 应用
    ├── README.md
    ├── backend/                # FastAPI 后端
    │   ├── main.py
    │   ├── run.py
    │   ├── config.py
    │   ├── database.py
    │   ├── api/
    │   │   ├── subtitle.py
    │   │   ├── metadata.py
    │   │   ├── media.py
    │   │   ├── audio.py
    │   │   ├── discover.py
    │   │   ├── adult.py
    │   │   ├── stats.py
    │   │   ├── tasks.py
    │   │   └── config_api.py
    │   ├── services/
    │   └── scrapers/
    └── frontend/               # Vue.js 前端
        ├── package.json
        ├── vite.config.js
        └── src/
            ├── views/
            ├── components/
            ├── api/
            └── router/
```

---

## 4. 数据库设计

### 4.1 PostgreSQL 连接信息

- **Host**: 192.168.89.11
- **Port**: 5432
- **Database**: jellyfin_tools
- **User**: jellyfin_tools
- **Password**: JfTools@2026

### 4.2 表结构

```sql
-- 后台任务
CREATE TABLE tasks (
    id SERIAL PRIMARY KEY,
    task_type VARCHAR(50) NOT NULL,
    status VARCHAR(20) DEFAULT 'pending',
    progress FLOAT DEFAULT 0.0,
    message TEXT,
    result TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP
);

-- 扫描报告
CREATE TABLE scan_reports (
    id SERIAL PRIMARY KEY,
    report_type VARCHAR(50) NOT NULL,
    scan_path VARCHAR(500) NOT NULL,
    total_items INTEGER DEFAULT 0,
    issues_count INTEGER DEFAULT 0,
    report_data TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 演员信息缓存
CREATE TABLE actors (
    id SERIAL PRIMARY KEY,
    jellyfin_id VARCHAR(100) UNIQUE NOT NULL,
    name VARCHAR(200) NOT NULL,
    tmdb_id INTEGER,
    has_image BOOLEAN DEFAULT FALSE,
    image_url VARCHAR(500),
    last_checked TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 媒体项目
CREATE TABLE media_items (
    id SERIAL PRIMARY KEY,
    jellyfin_id VARCHAR(100) UNIQUE,
    title VARCHAR(500) NOT NULL,
    media_type VARCHAR(20),
    file_path VARCHAR(1000),
    file_size BIGINT,
    resolution VARCHAR(20),
    codec VARCHAR(50),
    has_subtitle BOOLEAN DEFAULT FALSE,
    subtitle_langs VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 下载任务
CREATE TABLE download_tasks (
    id SERIAL PRIMARY KEY,
    title VARCHAR(500) NOT NULL,
    source VARCHAR(50),
    magnet_link TEXT,
    torrent_hash VARCHAR(100),
    status VARCHAR(20) DEFAULT 'pending',
    progress FLOAT DEFAULT 0.0,
    download_path VARCHAR(1000),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

-- 成人内容
CREATE TABLE adult_items (
    id SERIAL PRIMARY KEY,
    code VARCHAR(50) UNIQUE NOT NULL,
    title VARCHAR(500),
    release_date VARCHAR(20),
    studio VARCHAR(200),
    director VARCHAR(200),
    actors TEXT,
    tags TEXT,
    cover_url VARCHAR(500),
    poster_path VARCHAR(500),
    nfo_path VARCHAR(500),
    file_path VARCHAR(1000),
    rating FLOAT,
    source VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## 5. API 设计

### 5.1 字幕管理 API

```
POST /api/subtitle/scan      # 扫描字幕
POST /api/subtitle/rename    # 重命名字幕
GET  /api/subtitle/reports   # 获取报告列表
GET  /api/subtitle/reports/{id}  # 获取报告详情
```

### 5.2 元数据管理 API

```
GET  /api/metadata/actors           # 获取演员列表
POST /api/metadata/actors/scan      # 扫描演员
POST /api/metadata/actors/fix       # 批量修复演员图片
POST /api/metadata/actors/{id}/fix  # 修复单个演员
```

### 5.3 媒体库 API

```
GET  /api/media/browse          # 浏览目录
POST /api/media/scan            # 扫描媒体
GET  /api/media/duplicates      # 检测重复
GET  /api/media/storage         # 存储分析
```

### 5.4 任务管理 API

```
GET    /api/tasks               # 获取任务列表
GET    /api/tasks/{id}          # 获取任务详情
POST   /api/tasks/{id}/cancel   # 取消任务
DELETE /api/tasks/{id}          # 删除任务
```

### 5.5 统计 API

```
GET /api/stats/overview         # 总览统计
GET /api/stats/tasks/history    # 任务历史
GET /api/stats/actors/stats     # 演员统计
```

---

## 6. 开发计划

### Phase 1: 基础框架 ✅ 已完成
- [x] FastAPI 后端框架
- [x] Vue.js 前端框架
- [x] PostgreSQL 数据库

### Phase 2: 字幕管理 Web化 (待完善)
- [x] 字幕扫描 API + 页面 (基础)
- [x] 字幕重命名 API + 页面 (基础)
- [ ] 字幕下载 API + 页面
- [ ] WebSocket 实时更新

### Phase 3: 元数据管理 (待完善)
- [x] 演员照片修复 API + 页面 (基础)
- [ ] 海报下载功能
- [ ] NFO 修复功能

### Phase 4: 媒体库管理 (待完善)
- [x] 文件浏览
- [x] 重复检测 (基础)
- [x] 存储分析 (基础)
- [ ] 文件规范化命名

### Phase 5: 内容推荐与下载 (待开发)
- [ ] 豆瓣/TMDB 热门数据
- [ ] Jackett API 集成
- [ ] qBittorrent API 集成

### Phase 6: 成人内容管理 (待开发)
- [ ] 番号识别
- [ ] JavBus/JavDB 刮削
- [ ] NFO 生成

---

## 7. 开发环境

### Windows

```powershell
# 后端
cd D:\colex\codes\projects\jellyfin-tools
conda activate env_jf
pip install -r requirements.txt
$env:BACKEND_RELOAD='1'; python -m web.backend.run

# 前端 (新终端)
cd D:\colex\codes\projects\jellyfin-tools\web\frontend
npm install
npm run dev
```

### Linux / macOS

```bash
# 后端
pip install -r requirements.txt
BACKEND_RELOAD=1 python -m web.backend.run

# 前端 (新终端)
cd web/frontend
npm install
npm run dev
```

### 访问地址
- 前端: http://localhost:5173
- 后端 API: http://localhost:8000/docs

端口可在 `config.yaml` 的 `server` 段调整，或通过 `BACKEND_PORT` / `FRONTEND_PORT` 环境变量临时覆盖。

---

## 8. 配置说明

### config.yaml

```yaml
# 数据库配置 (PostgreSQL)
database:
  host: "192.168.89.11"
  port: 5432
  name: "jellyfin_tools"
  user: "jellyfin_tools"
  password: "JfTools@2026"

# Jellyfin 配置
jellyfin:
  host: "http://192.168.89.8:8096"
  api_key: "your_api_key"

# TMDB 配置
tmdb:
  api_key: "your_api_key"

# 字幕设置
subtitle:
  preferred_langs: ["chs", "chs.eng", "eng"]
  opensubtitles_api_key: ""

# Jackett 配置
jackett:
  host: "http://192.168.89.12:9117"
  api_key: "0w69x0scwq346shr2iddhszlak90znbq"

# qBittorrent 配置
qbittorrent:
  host: "http://192.168.89.6"
  username: "admin"
  password: "your_password"
  download_path: "/downloads"

# 成人内容配置
adult:
  enabled: false
  media_path: ""
  scraper_delay: 1.0
```

---

## 9. 附录

### 9.1 外部 API 参考
- [Jellyfin API](https://api.jellyfin.org/)
- [TMDB API](https://developers.themoviedb.org/3)
- [OpenSubtitles API](https://opensubtitles.stoplight.io/)
- [Jackett API](https://github.com/Jackett/Jackett)
- [qBittorrent WebUI API](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API)

### 9.2 相关工具
- [Sonarr](https://sonarr.tv/) - 电视剧自动化
- [Radarr](https://radarr.video/) - 电影自动化
- [Bazarr](https://www.bazarr.media/) - 字幕自动化
- [Jackett](https://github.com/Jackett/Jackett) - 种子索引器
