# Jellyfin Tools

Jellyfin 媒体服务器管理工具集 —— 提供 Web 管理界面，用于批量处理元数据、字幕、媒体库维护、内容刮削等常见任务。

## 功能概览

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Jellyfin Tools                              │
├─────────────┬─────────────┬─────────────┬──────────────────────────┤
│   元数据    │    字幕     │   媒体库    │       其他工具            │
├─────────────┼─────────────┼─────────────┼──────────────────────────┤
│ 演员照片修复│ 扫描与报告  │ 重复检测    │ MKV 默认音轨设置         │
│ 海报下载    │ 自动重命名  │ 存储分析    │ Jackett / qBittorrent    │
│ NFO 修复    │ 自动下载    │ 文件浏览    │ 成人内容刮削（可选）     │
└─────────────┴─────────────┴─────────────┴──────────────────────────┘
```

### 已完成

- **演员照片修复** — 从 TMDB 获取演员图片并上传到 Jellyfin
- **海报下载** — 从 TMDB 获取电影/剧集海报并上传到 Jellyfin
- **字幕扫描** — 扫描媒体目录，生成 HTML/JSON 匹配报告
- **字幕重命名** — 自动将字幕文件名与视频文件名对齐（支持 SxxExx / Exx / 第xx话 互通匹配）
- **字幕下载** — 通过 OpenSubtitles.com 下载缺失字幕
- **MKV 音轨设置** — 按语言偏好设置 MKV 文件的默认音轨
- **重复检测** — 文件大小 + 首尾 hash 双层去重，区分"完全相同"和"仅大小相同"
- **存储分析** — 按扩展名 / 目录统计占用
- **TMDB 热门推荐** — 按日/周聚合电影、剧集、综合榜单
- **Jackett 搜索** — 跨 indexer 聚合搜索种子
- **qBittorrent 集成** — Web 一键推送下载，监控进度，暂停 / 恢复 / 删除
- **成人内容管理（可选）** — 番号识别、JavBus + JavDB 双源刮削、自动写 NFO + 封面下载
- **Web 设置编辑** — 浏览器内编辑 config.yaml，自动备份原文件
- **Web 任务管理** — 所有后台任务实时进度、明细、历史

### 计划中

- 海报背景图（fanart）下载
- NFO 元数据修复（普通电影/剧集，区别于成人内容）
- 字幕下载多源（Subliminal 集成）
- 下载完成后自动搬回媒体库
- 订阅式自动追剧（按规则监视 + 自动下载）

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | Python 3.11+ / FastAPI / SQLAlchemy |
| 数据库 | PostgreSQL 12+ |
| 前端 | Vue.js 3 / Element Plus / Vite |

### 外部服务依赖

| 服务 | 用途 |
|------|------|
| [Jellyfin](https://jellyfin.org/) | 媒体服务器（必须） |
| [TMDB](https://www.themoviedb.org/) | 演员/影视元数据（必须） |
| [PostgreSQL](https://www.postgresql.org/) | 数据库（必须） |
| [OpenSubtitles](https://www.opensubtitles.com/) | 字幕下载（可选） |
| [Jackett](https://github.com/Jackett/Jackett) | 种子搜索聚合（可选） |
| [qBittorrent](https://www.qbittorrent.org/) | 下载管理（可选） |
| [MKVToolNix](https://mkvtoolnix.download/) | MKV 音轨编辑（音轨设置功能需要） |

## 项目结构

```
jellyfin-tools/
├── config.yaml              # 主配置文件（含 API Key，勿提交到公开仓库）
├── requirements.txt         # Python 依赖（后端 + tools 模块）
│
├── common/                  # 公共客户端
│   ├── config.py            #   配置加载
│   ├── jellyfin_client.py   #   Jellyfin API 客户端
│   ├── tmdb_client.py       #   TMDB API 客户端
│   ├── jackett_client.py    #   Jackett 客户端
│   └── qbittorrent_client.py
│
├── tools/                   # 业务模块（被 Web 后端 import）
│   ├── actor_fix/           #   演员照片修复
│   ├── subtitle_manager/    #   字幕扫描 / 重命名
│   ├── subtitle_downloader/ #   字幕下载
│   ├── audio_manager/       #   MKV 音轨设置
│   └── adult_manager/       #   成人内容刮削
│
├── web/                     # Web 应用
│   ├── backend/             #   FastAPI 后端
│   │   ├── main.py          #     应用入口
│   │   ├── run.py           #     启动器（读 config / 环境变量决定端口）
│   │   ├── config.py        #     配置管理
│   │   ├── database.py      #     数据库模型
│   │   ├── api/             #     REST API 路由
│   │   ├── services/        #     后台服务（Jellyfin WS 监听等）
│   │   └── scrapers/        #     刮削器
│   └── frontend/            #   Vue.js 前端
│       ├── package.json
│       └── src/
│           ├── views/       #     页面组件
│           └── components/  #     通用组件
│
└── docs/
    ├── DEVELOPMENT.md       # 开发文档
    └── IMPLEMENTATION_PLAN.md
```

## 快速开始

### 环境要求

- Python 3.11+
- Node.js 20+
- PostgreSQL 12+（需先建好库和用户）
- 一个运行中的 Jellyfin 服务器 + API Key

### 1. 配置

```bash
cp config.yaml.example config.yaml
```

填入你自己的服务地址和 API Key：

```yaml
# config.yaml
server:
  backend_port: 8000
  frontend_port: 5173

jellyfin:
  host: "http://your-jellyfin-server:8096"
  api_key: "your_jellyfin_api_key"

tmdb:
  api_key: "your_tmdb_api_key"

database:
  host: "127.0.0.1"
  port: 5432
  name: "jellyfin_tools"
  user: "jellyfin_tools"
  password: "your_password"
```

> 也可以先用默认配置启动，再到 Web 界面 `/settings` 页面里编辑（保存时会自动备份原文件）。

完整配置项参考 [config.yaml.example](config.yaml.example)。

### 2. 启动后端

```bash
pip install -r requirements.txt
python -m web.backend.run
```

启动器会按以下优先级决定后端端口：环境变量 `BACKEND_PORT` > `config.yaml` 的 `server.backend_port` > 默认 `8000`。

开发热重载：

```bash
# bash
BACKEND_RELOAD=1 python -m web.backend.run

# PowerShell
$env:BACKEND_RELOAD='1'; python -m web.backend.run
```

### 3. 启动前端

新开一个终端：

```bash
cd web/frontend
npm install
npm run dev
```

vite 会自动读 `config.yaml` 中的 `server.frontend_port`。

### 4. 访问

- 前端：http://localhost:5173
- API 文档（Swagger）：http://localhost:8000/docs

## 数据库

Web 应用使用 PostgreSQL 存储任务记录、扫描报告、演员缓存等数据。表会在首次启动时自动创建。

| 表 | 说明 |
|------|------|
| `tasks` | 后台任务记录 |
| `scan_reports` | 扫描报告存档 |
| `actors` | 演员信息缓存 |
| `media_items` | 媒体文件元数据 |
| `download_tasks` | 下载任务队列 |
| `adult_items` | 成人内容元数据（可选） |

## API

Web 后端提供 RESTful API，启动后访问 `/docs` 查看完整文档。

| 模块 | 端点前缀 | 功能 |
|------|----------|------|
| 字幕 | `/api/subtitle` | 扫描、重命名、下载 |
| 元数据 | `/api/metadata` | 演员照片、海报修复 |
| 媒体库 | `/api/media` | 浏览、重复检测、存储分析 |
| 音轨 | `/api/audio` | MKV 默认音轨设置 |
| 发现 | `/api/discover` | TMDB 热门、Jackett 搜索、qBit 下载 |
| 成人 | `/api/adult` | 番号识别、JavBus/JavDB 刮削（需开启） |
| 任务 | `/api/tasks` | 后台任务查看与取消 |
| 统计 | `/api/stats` | 总览与历史数据 |
| 配置 | `/api/config` | 读写 config.yaml |

## 常见问题

**后端启动报数据库连接错误**
检查 PostgreSQL 是否可达，确认 `config.yaml` 中的连接信息，确认数据库和用户已创建。

**前端请求 API 返回 CORS 错误**
确认后端已在 `config.yaml` 中配置的端口启动；检查 `web/frontend/vite.config.js` 中的代理配置。

**演员照片修复没有匹配到 TMDB 结果**
部分演员（尤其中文圈）在 TMDB 上没有收录或被标记为无图片，这种情况脚本会跳过。可尝试手动在 TMDB 上添加后再重试。

**MKV 音轨修改提示找不到 mkvmerge**
安装 [MKVToolNix](https://mkvtoolnix.download/)，确保其加入了系统 PATH，或在 `config.yaml` 里配置安装路径。

## 开发文档

详细的架构设计、功能规划和开发指南请参阅 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) 与 [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)。

## License

MIT
