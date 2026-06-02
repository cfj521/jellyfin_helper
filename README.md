# Jellyfin Helper

---

## 三大核心功能

### 1. Jellyfin 媒体库辅助管理

补 Jellyfin 自身缺位或弱势的环节：

- **媒体库浏览**：多库列表 + 详情页 + 海报视图；可选本地 SQLite 直读加速
  `path → item` 反查（同机部署时实测 4ms vs REST 1500ms）
- **元数据修复**：演员照片（TMDB + Wikidata 兜底）、海报、NFO；
  jellyfin 演员库归一化
- **字幕全链路**：多源下载（OpenSubtitles / ASSRT / Shooter）→ 评分融合 +
  分层语种排序 → 文件名按 BCP 47 落盘 → 缺字幕智能补齐 → 内嵌字幕轨 ffprobe 探测
- **音轨管理**：MKV 默认音轨按语种偏好批量设置；汉语 / 未识别轨例外保护
- **维护工具**：配置 Web 编辑（保存自动备份）、Sample 清理、强制重扫、日志查看、统计
- **成人内容（可选）**：番号识别、JavBus / JavDB / AVBase / MissAV 多源刮削、
  女优档案库（javdb + Minnano-AV chain）、健康度 + 冷却保护

### 2. 资源（多种）发现与搜索

聚合多个发现源做统一 UI，避免在十几个站点之间反复横跳：

- **发现 / 推荐**：TMDB / Trakt / AniList / 豆瓣榜单聚合，无限滚动 + 预取
- **评分聚合**：TMDB + MDBList（IMDB / RT / Metacritic / Trakt / Letterboxd）
  + 豆瓣，统一存 `media_ratings`，每家独立 TTL；前端融合显示
- **资源搜索**：Jackett 跨 indexer 聚合，结果分类 + 大小 / Seeders 排序，
  一键推送到下载流水线

### 3. 下载流水线

从"加种"到"入库"全自动，不需要手动 mv / 重命名：

- **加种**：Jackett 搜种 → qBittorrent 推送（**要求 qB 5.2+**，建议启用
  API Key 认证）
- **识别**：confidence-driven 链（regex → TMDB → LLM 兜底），低置信落
  needs_review 等人工
- **入库**：按可配置模板 + duplicate_policy 整理到媒体库，自动通知 jellyfin 刷新
- **做种与清理**：state=stop + (ratio≥target OR 完成 N 天) 双条件软清，
  磁盘配额阈值触发硬清；保护用户做种数据
- **任务系统**：后台任务 + SSE 实时进度 + 取消 + shutdown 联动；
  前端任务详情页可展开各步明细

---

> ## ⚠️ 前置提醒：需要"魔法上网"
>
> 项目深度依赖境外服务（TMDB / Trakt / AniList / OpenSubtitles / IMDB / MDBList /
> Wikidata 等），**没有稳定的科学上网链路用户体验会很差**——多数源会超时或被
> 地理屏蔽。强烈建议在路由器层做透明代理（OpenClash / mihomo 等），把整个
> jellyfin-helper 主机的出站流量按域名分流。本项目代码层**不处理代理逻辑**，
> 默认假定网络是通的，所以连不通的报错都按上游故障处理而非代理配置。

> 项目仍在密集迭代。schema 变更时通常**直接清表重扫**而不是写迁移脚本，
> 部署到生产请自行评估并做好数据备份。

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

## 快速开始（Docker 一站式 · 推荐）

整个栈打包成 5 个服务：jellyfin-helper + Jellyfin + Jackett + qBittorrent
5.2+ + PostgreSQL 16。`bootstrap` 一次性脚本会自动设好 qBittorrent /
Jackett 的密码、API Key、indexer、RSS 开关，并回填到 `config.yaml`。

```bash
git clone <this-repo> && cd jellyfin-helper

# 1) 必填 .env：MEDIA_DIR / DOWNLOADS_DIR（POSTGRES_PASSWORD 默认 jellyfin_helper 不用改）
cp .env.example .env && $EDITOR .env

# 2) 必填 config.yaml：JWT secret_key + 你自己申请的第三方 API Key
#    （TMDB 必填，OpenSubtitles / ASSRT / MDBList 等按需）。
#    完整凭据清单和申请地址见下面「外部服务 → 凭据获取速查表」。
#    database / jellyfin / jackett / qbittorrent / auth.users[0] 这几段
#    下一步 bootstrap 会自动写（含 helper 默认密码 jellyfin_helper）。
cp config.yaml.example config.yaml && $EDITOR config.yaml

# 3) bootstrap phase prep：预填 qb/jackett 配置 + 回写 config.yaml
#    bootstrap / helper 容器以 root 启动，entrypoint 自动 chown
#    ./data/* 和 ./logs 到 PUID:PGID，**不需要手动 mkdir / chown**
docker compose --profile bootstrap run --rm bootstrap --phase prep

# 4) 起 5 个服务
docker compose up -d

# 5) bootstrap phase connect：连 Jackett 加 7 个 indexer + 跑 Jellyfin
#    Setup Wizard + 申请 API Key 回写 config.yaml
#    （52BT / dmhy / OneJAV / ThePirateBay / TheRARBG / TorrentKitty / YTS）
docker compose --profile bootstrap run --rm bootstrap --phase connect

# 6) 让 helper 重读 config.yaml
docker compose restart helper
```

完成。浏览器打开 `http://<宿主IP>:8099` 用 `config.yaml` 里的账号登录。

### Web UI 登录凭据

bootstrap 用「预填 conf 文件」方式把 qb / Jackett 的密码和 API Key 在容器启动前
就一起写进去（不走 WebUI 登录交互），所以 helper 拿 API Key 不需要先登录。
但 **qb WebUI 仍需密码登录**（浏览种子、改设置时）—— 用下面这套：

| 服务 | URL | 账号 | 密码 | 说明 |
|---|---|---|---|---|
| **jellyfin-helper** | `http://<宿主IP>:8099` | `admin` | `jellyfin_helper` | 主入口；改密码：编辑 `config.yaml.auth.users` 后 restart helper |
| **qBittorrent** | `http://<宿主IP>:8080` | `admin` | `jellyfin_helper` | 改密码：WebUI → Options → Web UI → Authentication |
| **Jackett** | `http://<宿主IP>:9117` | `admin` | `jellyfin_helper` | 改密码：WebUI → Configuration → Admin password |
| **Jellyfin** | `http://<宿主IP>:8096` | `admin` | `jellyfin_helper` | bootstrap 自动跑 Wizard 设置好；API Key 已自动写回 config.yaml；进 UI 后第一件事加媒体库指向 `/media` |
| **PostgreSQL** | `postgres:5432`（仅栈内） | `jellyfin_helper` | `jellyfin_helper` | 5432 不暴露宿主，仅栈内访问 |

> 想改 qb 默认密码：上 qb WebUI 改完后，**同步**生成新 API Key 写回
> `config.yaml.qbittorrent.api_key`（helper 用的是 API Key，不是密码，
> 改密码本身不会影响 helper，但改 API Key 会）。

完整流程（含升级、备份、常见问题）：[docs/docker-deploy.md](docs/docker-deploy.md)

### 首次验证

打开 **前端 → 配置 → 可用性检测**（左侧导航第一项）。一屏看清楚：

- **本地环境**：FFmpeg · FFprobe · MKVPropEdit · bsdtar（容器里都已预装）
- **网络服务**：Jellyfin · qBittorrent · Jackett · TMDB · 豆瓣 · MDBList ·
  Trakt · AniList · Wikidata · LLM · 字幕源 · 成人刮削站

每项显示 `状态 / 信息 / 耗时`。**遇到问题先来这里看一眼**，能省一半排查时间。

---

## 裸机 / 开发模式

不想用 Docker、要直接在宿主跑（开发或定制场景）需要自备：

- Python 3.12（推荐 conda 隔离环境）+ Node 20+
- PostgreSQL 12+（先建库和用户）
- 一台 Jellyfin（10.9+ 推荐，需管理员 API Key）
- qBittorrent **5.2+**（用 API Key 认证，详见下方「外部服务」）
- Jackett
- 系统工具：`ffmpeg / mkvtoolnix / bsdtar (libarchive ≥ 3.6 才支持 RAR5)`
- 稳定代理 / 透明科学上网链路（前置提醒已说明）

启动：

```bash
pip install -r requirements.txt
python -m backend.run            # 后端 (默认 8000，BACKEND_PORT 覆盖)

cd frontend && npm install && npm run dev   # 前端 (默认 5173)
```

更详细的模块划分、调试技巧、添加新 router 见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。

---

## 数据库

表会在首次启动时自动创建，无需手动建表。

| 表名 | 说明 |
|---|---|
| `users` | 用户账号（JWT 认证） |
| `tasks` | 后台任务记录 |
| `scan_reports` | 扫描报告存档 |
| `actors` | 演员信息缓存 |
| `media_items` | 媒体文件元数据 |
| `media_metadata` | 媒体扩展元数据（海报、简介等） |
| `media_ratings` | 评分聚合（TMDB / IMDB / RT / Metacritic / Trakt / Letterboxd / 豆瓣），按 `(tmdb_id, media_type)` 唯一；每家独立 `*_fetched_at` |
| `video_annotations` | 视频标注（硬字幕标记等） |
| `adult_items` | 成人内容元数据（可选） |
| `adult_actresses` | 演员资料库（成人内容，可选） |
| `download_dispatch_map` | 下载入库映射（torrent → 目标路径） |
| `kv_cache` | 通用 KV 缓存 |
| `llm_classify_cache` | LLM 分类结果缓存 |

---

## 外部服务

### 凭据获取速查表

下面这些 Key / Token 需要你自己去对应站点申请，然后填进 `config.yaml`。
**Docker 部署**下 qBittorrent / Jackett / Postgres 三个 Key 由 bootstrap 自动生成 +
回填，**不需要手动申请**。

| 服务 | 必需性 | 申请地址 | 填入字段 |
|---|---|---|---|
| **Jellyfin API Key** | 必需 | Docker：bootstrap 自动跑 Wizard + 申请；裸机：Jellyfin Web → 控制台 → API Keys → 新建 | `jellyfin.api_key` |
| **TMDB API Key** | 必需 | https://www.themoviedb.org/settings/api（免费注册申请） | `tmdb.api_key` |
| **PostgreSQL 密码** | Docker 自动 | Docker 默认 `jellyfin_helper`（stack 内用，不外露）；裸机自己定 | `database.password` |
| **qBittorrent API Key** | 必需（5.2+ 强制） | Docker：bootstrap 自动；裸机：qB → Preferences → WebUI → API Key Generate | `qbittorrent.api_key`（**不需要** username/password） |
| **Jackett API Key** | 必需 | Docker：bootstrap 自动；裸机：Jackett UI 右上角直接显示 | `jackett.api_key` |
| **MDBList API Key** | 推荐（评分） | https://mdblist.com/api 登录后生成（免费 1000 req/天） | `mdblist.api_key` |
| **OpenSubtitles 三件套** | 推荐（字幕） | API Consumer → https://www.opensubtitles.com/consumers + 注册账号 | `subtitle.opensubtitles_api_key` + `opensubtitles_username` + `opensubtitles_password` |
| **ASSRT API Token** | 推荐（中文字幕主力） | 注册后 → https://secure.assrt.net/usercp.php | `subtitle.assrt_api_token` |
| **Trakt Client ID** | 可选（推荐源） | https://trakt.tv/oauth/applications 创建 app | `trakt.client_id` |
| **LLM API Key** | 可选（识别兜底） | 任何 OpenAI 兼容服务（DeepSeek / 阿里通义 / OpenAI） | `llm.api_key` + `llm.base_url` |
| **JWT secret_key** | 必需 | 自己生成：`python -c "import secrets; print(secrets.token_urlsafe(32))"` | `auth.secret_key` |
| **管理员账号密码** | Docker 自动 | Docker 默认 `admin` / `jellyfin_helper`（bootstrap 写）；裸机自己定 | `auth.users[].password` |

**无需 Key 的源**（默认就能用，不用申请）：

- 射手字幕 Shooter（hash 匹配）
- AniList（GraphQL 公开端点，动漫元数据）
- 豆瓣（网页爬取，带 5 次失败熔断）
- Wikidata（演员图兜底，但要在 `wikidata.user_agent` 填你的联系方式，Wikimedia 要求）

**成人内容**（按需启用）：JavBus / JavDB / AVBase / MissAV，无 API Key 但有地理屏蔽和反爬，
详见 [docs/external-services.md](docs/external-services.md) 的代理建议。

`config.yaml.example` 里每个字段旁也都贴了对应申请 URL，按节填即可。
完整字段说明、调用频率、地理屏蔽情况、OpenClash 分流示例见
[docs/external-services.md](docs/external-services.md)。

### 为什么强制 qBittorrent 5.2+

老版本（< 5.0）兼容已在 2026-06 移除，**原因是安全**：

- 4.x / 5.0 / 5.1 都没有 API Key 认证，只能 username/password
- qB 默认 admin/adminadmin 弱密被自动化脚本扫满公网，**直接 RCE 装挖矿**
- 真实事件：通过 `Preferences → Downloads → Run external program on torrent added / completed`
  被植入 `wget ... | sh` 后门，XMR 挖矿木马常见手法

5.2.0+ 才引入 `Authorization: Bearer qbt_xxx` 这种 stateless API Key 机制；
本项目用 Docker 部署时 bootstrap 脚本自动生成 API Key 并写进 `config.yaml`，
裸机部署也务必在 qB UI 里手动 Generate 一个填入。

### ⚠️ qB 安全 checklist

不论 Docker 还是裸机，都要做：

1. **WebUI 不暴露公网**（Docker 默认只映射到宿主，配合宿主防火墙；裸机改监听
   `127.0.0.1` 或反代加 IP 白名单）
2. **首次接管现有 qB 前**检查 `Preferences → Downloads → Run external program
   on torrent added / completed` 是否被植入可疑命令——这是历史弱密被打的常见痕迹

---

## 文档

- 开发者向：[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) —— 模块组织、添加新 router 的流程、调试技巧
- 第三方服务：[docs/external-services.md](docs/external-services.md) —— 调用频率、地理屏蔽、OpenClash 分流示例
- 历史 PRD / 决策记录：[docs/archive/](docs/archive/) —— 已实现功能的设计文档归档

---

## 常见问题

> **先去配置页 → 可用性检测**（左侧导航第一项）。本地环境会自动跑、网络服务可一键测，多数问题在那里就能看到根因。

### 后端启动失败

直接看启动日志（DB 连不上根本到不了配置页）：

```bash
docker compose logs -f helper           # Docker 部署
# 或裸机：python -m backend.run 的输出
```

常见根因：

1. `config.yaml` 的 `database` 段不对——Docker 部署 `host: postgres`，
   裸机部署 `host: 127.0.0.1`
2. Docker：bootstrap 没跑或 `POSTGRES_PASSWORD` 跟 `config.yaml` 不一致
3. 裸机：`requirements.txt` 没装全 / 系统级依赖（ffmpeg / mkvtoolnix /
   bsdtar）不在 PATH 上

### 前端无法连接后端（仅裸机模式）

1. 确认后端已启动在 `config.yaml` 中配置的端口
2. 检查 `vite.config.js` 中的代理配置
3. 检查 `cors_origins`（默认 `["*"]`）

Docker 部署下前端是 FastAPI 直接托管的静态资源，不存在跨进程问题。

### 第三方源拉不到数据

打开 **可用性检测** → 找到对应源（TMDB / 豆瓣 / Jellyfin / qB / Jackett ...）点「测试」。结果里会显示具体 HTTP 状态码或异常类型：

- `HTTP 401 / 403`：api_key / 凭据错
- `HTTP 429`：被限流；rate_limiter 会自动暂停（任务详情页 QuotaStatusPanel 看具体剩余配额）
- `Connection*` 异常：网络 / 代理问题
- `not_configured`：还没填 key 或 enabled=false

### 数据库连接错误

Docker 部署：

```bash
docker compose exec postgres psql -U jellyfin_helper -d jellyfin_helper -c "\dt"
```

裸机：

```bash
psql -h <host> -p 5432 -U jellyfin_helper -d jellyfin_helper
```

连不上时依次排查：容器名/网络可达性（Docker）、防火墙（裸机）、用户密码、数据库是否存在。

### 种子添加失败

前端 ElMessage 现在能给出具体原因，按状态码区分：

- **HTTP 409**：种子已在 qB 队列里（pre-check 会先拦下并显示种子名 / 状态）
- **HTTP 415**：种子文件无效（非 bencode 格式）
- **HTTP 502 + "qBittorrent 拒绝加种"**：qB 默认下载目录不存在/无权限，或 category 未在 qB 创建

---

## License

MIT
