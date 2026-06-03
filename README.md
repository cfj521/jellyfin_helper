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
├── config.yaml.example            # 配置模板（复制为 config.yaml 填真实值，后者 git 不提交）
├── requirements.txt               # Python 依赖
├── VERSION                        # 版本号（前后端共用的 single source）
│
├── backend/                       # FastAPI 后端
│   ├── main.py                    #   应用入口 + lifespan + SPA 静态托管
│   ├── run.py                     #   uvicorn 启动器
│   ├── config.py / config_models.py  #   pydantic settings
│   ├── database.py                #   SQLAlchemy models + 一次性迁移
│   ├── auth_middleware.py         #   JWT 认证中间件
│   ├── diagnostics.py             #   性能 / DB 池监控 / access log 过滤
│   ├── api/                       #   按功能切片的 router（见下表）
│   └── services/                  #   后台服务（jellyfin 事件监听等）
│
├── common/                        # 第三方服务客户端
│   ├── jellyfin_client.py         #   jellyfin REST
│   ├── jellyfin_db.py             #   jellyfin SQLite 直读（path→item 反查加速，可选）
│   ├── tmdb_client.py / trakt_client.py / anilist_client.py
│   ├── mdblist_client.py / douban_client.py / wikidata_client.py
│   ├── jackett_client.py / qbittorrent_client.py / llm_client.py
│   ├── lang_utils.py              #   字幕语言代码归一化（zh / chs / cht / BCP 47）
│   └── label_cleaner.py / rate_limiter.py
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
│       └── ...
│
└── frontend/                      # Vue 3 SPA
    ├── package.json / vite.config.js
    └── src/
        ├── views/                 #   页面（medialibraries / downloadpipeline / settings 等）
        ├── components/            #   通用组件
        ├── composables/ stores/   #   组合式函数 / Pinia store
        └── api/ router/ utils/ styles/
```

> `docs/`（开发文档）和 `data/` `logs/`（运行时数据）默认 `.gitignore`，不入库。

### Backend API 路由

| 前缀 | 文件 | 主要职责 |
|---|---|---|
| `/api/auth` | `auth.py` | 登录 / JWT 签发 / 用户列表 |
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
| `/api/diagnostics` | `diagnostics.py` | 可用性检测（本地工具 + 各网络服务）/ 性能监控 |

---

## 快速开始

### 环境要求

- Python 3.12（推荐用 conda 隔离环境）
- Node.js 20+
- PostgreSQL 12+（先建库和用户）
- 一台运行中的 Jellyfin（10.9+ 推荐）+ 管理员 API Key
- qBittorrent **5.2+ 必需**（用 API Key 认证替代 admin/admin 弱密；老版本无 API Key 支持，存在被植入挖矿木马的真实风险，详见下方「外部服务 → qBittorrent」）
- 系统级工具（见下方「系统级依赖」章节）
- **稳定的代理 / 透明科学上网链路**（顶部前置提醒已说明）

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
python -m backend.run
```

端口优先级：环境变量 `BACKEND_PORT` > `config.yaml: server.backend_port` > 默认 8000。

开发热重载：

```bash
# bash
BACKEND_RELOAD=1 python -m backend.run

# PowerShell
$env:BACKEND_RELOAD='1'; python -m backend.run
```

### 3. 前端

新开终端：

```bash
cd frontend
npm install
npm run dev
```

vite 自动读 `config.yaml` 的 `server.frontend_port`。

### 4. 访问

- 前端：http://localhost:5173
- API 文档（Swagger）：http://localhost:8000/docs

### 5. 首次启动验证 ★

打开 **前端 → 配置 → 可用性检测**（左侧导航第一项，进页面就在）。一屏看清楚：

- **本地环境**（进入即跑，零网络成本）：FFmpeg · FFprobe · MKVPropEdit · bsdtar
- **网络服务**（手动按钮）：Jellyfin · qBittorrent · Jackett · TMDB · 豆瓣 · MDBList · Trakt · AniList · Wikidata · LLM · 字幕源 · 成人刮削站

每项显示 `状态 / 信息 / 耗时`。未启用的源灰显，按钮禁用。**遇到问题先来这里看一眼，能省一半排查时间**。

---

## 系统级依赖

除 Python 包（`requirements.txt`）外，部分功能依赖以下系统工具。**非必需**——缺失时对应功能自动退化（仅扫描/建议，不写入），不会崩溃。

| 工具 | 用途 | 缺失时影响 |
|---|---|---|
| **ffmpeg / ffprobe** | 音轨扫描、字幕内嵌轨探测 | 无法检测内嵌字幕和音轨信息 |
| **mkvtoolnix (mkvpropedit)** | 修改 MKV 文件默认音轨 flag | 音轨管理仅返回建议，不实际写入 |
| **bsdtar (libarchive ≥ 3.6)** | 解压 rar / 7z 字幕包 | rar 字幕包无法解压，zip 不受影响 |

> **bsdtar 的 libarchive 版本要 ≥ 3.6 才支持 RAR5**（现代字幕包基本都是 RAR5）。对照表：Ubuntu 22.04+ / Debian 12+ / macOS Homebrew / conda-forge 都满足。Ubuntu 20.04 / Debian 11 自带 libarchive 3.4，解 RAR5 会失败 —— 建议升级发行版，或者从 conda-forge 装。

安装：

```bash
# Debian / Ubuntu 22.04+ / Debian 12+
sudo apt install ffmpeg mkvtoolnix libarchive-tools

# macOS（自带 bsdtar，无需额外装）
brew install ffmpeg mkvtoolnix

# Windows (Chocolatey)
choco install ffmpeg mkvtoolnix
# bsdtar 通过 conda 装：见下

# Conda（跨平台一键，libarchive 3.7+ 自带 bsdtar，支持 RAR5）
conda install -c conda-forge ffmpeg mkvtoolnix libarchive
```

验证：命令行 `ffprobe -version` / `mkvpropedit --version` / `bsdtar --version` 输出版本号即可。
也可以直接打开 **前端 → 配置 → 可用性检测**，本地环境列里一目了然哪个工具在 PATH 上。

---

## 文件系统权限

后端进程运行的用户（systemd `User=` / Docker `user:` / 裸跑时的 shell 用户）必须有以下访问权限。**这是最常见的"看起来跑起来了但功能没动作"根因**：

| 路径 | 需要权限 | 用途 | 配错时的症状 |
|---|---|---|---|
| Jellyfin SQLite（如 `/var/lib/jellyfin/data/jellyfin.db`） | **读** | `path → item` 反查直读加速（可选，4ms vs REST 1500ms） | 启动日志 `PermissionError`；运行时 fallback 到 REST，慢 100× |
| qB 下载目录（如 `/download`） | **读 + 写** | 入库时 mv/cp 源文件；软清/硬清 `rm` 完成种子；磁盘配额监视 | 流水线搬不动文件；日志 `配额: 后端 stat 不到 '/download'，禁用配额监视/清理` |
| Jellyfin 媒体库目录（如 `/library/videos`） | **读 + 写** | dispatch 入库写文件 + 落 NFO/poster；adult_scanner 探测本地附件 | 入库失败；NFO / poster 不落地；扫描结果"看见文件但识别不到" |

**典型 systemd 部署**（推荐）：让后端用户加入 `jellyfin` 组，继承 jellyfin DB 默认 640 权限即可读；库目录用 group ownership + 775：

```ini
# /etc/systemd/system/jellyfin-helper.service.d/user.conf
[Service]
User=jellyfin_helper
Group=jellyfin
UMask=0002
```

```bash
# 把后端用户加入 jellyfin 组（如果库目录归 jellyfin 用户）
sudo usermod -aG jellyfin jellyfin_helper

# 验证：以后端用户身份读 jellyfin DB / 写库目录
sudo -u jellyfin_helper sqlite3 /var/lib/jellyfin/data/jellyfin.db ".tables" | head
sudo -u jellyfin_helper touch /library/videos/_perm_test && rm /library/videos/_perm_test
```

**Docker 部署**：`docker-compose.yml` 里 `user: "1000:1000"` 跟挂载的宿主机目录 owner/group 对齐；或者在 entrypoint 里 `chown` 让数据卷 ownership 跟进。

**裸跑（开发期）**：图省事可以 `sudo -u jellyfin python -m backend.run`，免去权限协调；生产不推荐共享 shell 用户。

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

详见 [docs/external-services.md](docs/external-services.md)。摘要：

- **必需**：Jellyfin、TMDB、PostgreSQL
- **强烈推荐**：Jackett + qBittorrent（启用下载入库流水线）
- **字幕**：OpenSubtitles + ASSRT 任选其一即可，配齐两个最佳
- **评分**：MDBList（可选）+ 豆瓣（无需 key）
- **LLM**：任意 OpenAI 兼容服务（识别兜底，可选）
- **成人内容**：JavBus / JavDB / AVBase / MissAV（带地理屏蔽与代理建议）

### qBittorrent 版本要求：**5.2+ 必需**

老版本（< 5.0）兼容已在 2026-06 移除，**原因是安全**：

- 4.x / 5.0 / 5.1 都没有 API Key 认证，只能 username/password
- qB 默认 admin/adminadmin 弱密被自动化脚本扫满公网，**直接 RCE 装挖矿**
- 真实事件：通过 `Preferences → Downloads → Run external program on torrent added / completed`
  被植入 `wget ... | sh` 后门，XMR 挖矿木马常见手法

5.2.0+ 才引入 `Authorization: Bearer qbt_xxx` 这种 stateless API Key 机制，
本项目要求最低版本，让认证强度有保证。

### 配置 API Key

去 qB **Preferences → WebUI → API Key 段** 点 Generate，复制 `qbt_xxx...` 到 `config.yaml`：

```yaml
qbittorrent:
  host: "http://127.0.0.1:8080"
  api_key: "qbt_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"   # 强烈推荐：stateless，不发明文密码
  # 退而求其次（仍要求 qB 5.2+ 才能跑通其它 API 兼容）：
  username: ""
  password: ""
```

或者前端 **设置 → qBittorrent 下载管理** 里粘贴。配了 api_key 就**不必再填 username/password**。

### ⚠️ qB 安全 checklist

无论是否启用 API Key，都建议：

1. **监听地址改 `127.0.0.1`**（仅本机）或反代后加 IP 白名单
2. **首次接管现有 qB 前**检查 `Preferences → Downloads → Run external program on torrent added / completed`
   是否被植入可疑命令——这是历史弱密被打的常见痕迹
3. 不要把 qB WebUI 直接暴露公网

---

## 文档

- 开发者向：[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) —— 模块组织、添加新 router 的流程、调试技巧
- 第三方服务：[docs/external-services.md](docs/external-services.md) —— 调用频率、地理屏蔽、OpenClash 分流示例
- 历史 PRD / 决策记录：[docs/archive/](docs/archive/) —— 已实现功能的设计文档归档

---

## 常见问题

> **先去配置页 → 可用性检测**（左侧导航第一项）。本地环境会自动跑、网络服务可一键测，多数问题在那里就能看到根因。

### 后端启动失败

1. 检查 PostgreSQL 是否可达，库和用户是否已创建（DB 连不上后端进程根本起不来，**到不了配置页**——直接看启动日志）
2. 确认 `config.yaml` 的 `database` 段填写正确
3. 确认 `requirements.txt` 全部装好
4. 确认系统级依赖已安装（可用性检测「本地环境」会逐项显示）

### 前端无法连接后端

1. 确认后端已启动在 `config.yaml` 中配置的端口
2. 检查 `vite.config.js` 中的代理配置
3. 检查 `cors_origins`（默认 `["*"]`）

### 第三方源拉不到数据

打开 **可用性检测** → 找到对应源（TMDB / 豆瓣 / Jellyfin / qB / Jackett ...）点「测试」。结果里会显示具体 HTTP 状态码或异常类型：

- `HTTP 401 / 403`：api_key / 凭据错
- `HTTP 429`：被限流；rate_limiter 会自动暂停（任务详情页 QuotaStatusPanel 看具体剩余配额）
- `Connection*` 异常：网络 / 代理问题
- `not_configured`：还没填 key 或 enabled=false

### 数据库连接错误

```bash
psql -h <host> -p 5432 -U jellyfin_helper -d jellyfin_helper
```

连不上时依次排查：网络、防火墙、`pg_hba.conf` 是否允许该 IP、用户密码、数据库是否存在。

### 种子添加失败

前端 ElMessage 现在能给出具体原因，按状态码区分：

- **HTTP 409**：种子已在 qB 队列里（pre-check 会先拦下并显示种子名 / 状态）
- **HTTP 415**：种子文件无效（非 bencode 格式）
- **HTTP 502 + "qBittorrent 拒绝加种"**：qB 默认下载目录不存在/无权限，或 category 未在 qB 创建

---

## License

MIT
