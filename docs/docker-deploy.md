# Docker 一站式部署

把 jellyfin-helper、Jellyfin、Jackett、qBittorrent、PostgreSQL 16 五件套
打成一个 `docker compose up -d` 拉起的栈。

---

## 拓扑

```
┌──────────────────────────────────────────────────────────┐
│ jellyfin-helper-net (bridge)                             │
│                                                          │
│  helper:8099 ─┬─► postgres:5432                          │
│               ├─► jellyfin:8096                          │
│               ├─► jackett:9117                           │
│               └─► qbittorrent:8080                       │
│                                                          │
└──────────────────────────────────────────────────────────┘
        ▲                  ▲
        │                  │
    ${MEDIA_DIR}      ${DOWNLOADS_DIR}
    （宿主路径，必填）  （宿主路径，必填）
```

容器内媒体路径统一为 `/media`、下载路径统一为 `/downloads`，
helper 和 jellyfin 看到的是同一个值，`config.yaml` 里 `path_mappings`
**留空即可**（不需要再配翻译规则）。

---

## 镜像版本

| 服务 | 镜像 | 说明 |
|---|---|---|
| postgres | `postgres:16-alpine` | README 要求 12+，这里用 16 |
| jellyfin | `lscr.io/linuxserver/jellyfin:latest` | linuxserver 全家桶，PUID/PGID 友好 |
| jackett | `lscr.io/linuxserver/jackett:latest` | 启用 AUTO_UPDATE |
| qbittorrent | `lscr.io/linuxserver/qbittorrent:latest` | 对齐 README 强制要求 5.2+，latest tag 已是 5.x |
| helper | 本仓 `Dockerfile` 构建 | 多阶段：node:20 build 前端 + python:3.12-slim 运行时 |

---

## 首次部署

### 1. 准备 `.env`

```bash
cp .env.example .env
# 必改两项：
#   MEDIA_DIR=/your/media/root
#   DOWNLOADS_DIR=/your/downloads
# POSTGRES_PASSWORD 默认 jellyfin_helper，stack 内用、5432 不外露，不用改。
# 想外露或安全洁癖再改强密码（注意同步 config.yaml database.password）。
```

`MEDIA_DIR` / `DOWNLOADS_DIR` 没填的话 compose 会拒绝启动并报错。

### 2. 准备 `config.yaml`

```bash
cp config.yaml.example config.yaml
```

只需要手动改 `auth` 节（管理员密码 + JWT secret）和**第三方 API 凭据**
（TMDB / MDBList / OpenSubtitles / ASSRT 等你自己申请的）。
**Postgres / Jackett / qBittorrent / Jellyfin 这四样的 host 不用动**——下一步 bootstrap
脚本会自动把它们的容器内地址和 API Key 写回来。

> **建议填 `external_url`（浏览器跳转链接用）**：helper 里点「打开 Jellyfin /
> Jackett / qBittorrent」的链接走的是 `external_url`，留空才 fallback 到 `host`。
> 而 Docker 部署下 `host` 是容器名（`http://jellyfin:8096`），**你的浏览器在
> 容器网络外解析不了**，跳转会打不开。给三个服务的 `external_url` 填上宿主可访问
> 地址即可：
>
> ```yaml
> jellyfin:    { external_url: "http://<宿主IP>:8096" }
> jackett:     { external_url: "http://<宿主IP>:9117" }
> qbittorrent: { external_url: "http://<宿主IP>:8080" }
> ```
>
> `host`（容器名）给 helper 后端内部调用，`external_url`（宿主 IP）给浏览器跳转，各司其职。

### 3. 一次性 bootstrap（关键一步）

> 💡 不用手动 `mkdir` / `sudo chown`：bootstrap 和 helper 容器都以 root 启动，
> entrypoint 自动 `chown -R PUID:PGID /app/data /app/logs`（bootstrap 同理改
> `/workspace/data`），然后 `gosu` 降权到 PUID:PGID 跑业务。所有 bind mount
> 的属主问题在容器内自动处理。

bootstrap 拆成两个 service，按阶段分别跑（防止误跑导致状态混乱）：

| service | 何时跑 | 干啥 |
|---|---|---|
| `bootstrap-prep` | docker compose up 之前 | 预填 qb/jackett 的 conf 文件 + 回写 config.yaml |
| `bootstrap-connect` | docker compose up 之后 | 连 jackett 加 indexer + 跑 Jellyfin Setup Wizard + 申请 API Key |

两个阶段都**幂等**，重复跑安全。

```bash
# 3a. phase prep：预填 qb/jackett conf + 回写 config.yaml
docker compose --profile bootstrap run --rm bootstrap-prep

# 3b. 拉起所有服务
docker compose up -d

# 3c. phase connect：连 jackett 加 7 个公开 indexer + 跑 Jellyfin Wizard
#     + 申请 API Key 回写 config.yaml
#     （52BT / dmhy / OneJAV / ThePirateBay / TheRARBG / TorrentKitty / YTS）
docker compose --profile bootstrap run --rm bootstrap-connect

# 3d. 让 helper 重读 config.yaml
docker compose restart helper
```

bootstrap 干了这些（**两阶段都幂等**，重复跑安全）：

**phase prep**（容器没起前）：
- 写 `data/qbittorrent/qBittorrent/qBittorrent.conf`：用户名 `admin` /
  密码 `jellyfin_helper`、启用 RSS + 自动下载规则处理（API Key 不预填，见步骤 6）
- 写 `data/jackett/Jackett/ServerConfig.json`：生成 API Key、`admin/jellyfin_helper`、`AllowExternal=true`
- 把 Jackett API Key 与容器内地址 (`postgres` / `jellyfin:8096` / `jackett:9117` /
  `qbittorrent:8080`) 回写到 `config.yaml`，并备份到 `data/config.yaml.bak.bootstrap.*`

**phase connect**（服务起来后）：
- 添加 7 个 Jackett indexer（公开站点，无需 cookie）
- 跑 Jellyfin Setup Wizard 建 admin/jellyfin_helper、申请 API Key 回写 `config.yaml.jellyfin.api_key`

### 4. Jellyfin 媒体库添加（API Key 已自动）

第 4 步 bootstrap 已经替你跑完 Jellyfin Setup Wizard（admin/jellyfin_helper）、
申请好 API Key 并写回 `config.yaml.jellyfin.api_key`，**不需要手动操作**。

唯一要做的是加媒体库（bootstrap 不动这个，避免猜错你的目录结构）：

1. 浏览器开 `http://<宿主IP>:8096`，用 `admin` / `jellyfin_helper` 登录
2. 控制台 → 媒体库 → 添加媒体库 → 选类型（Movies / TV Shows / Music 等）
   → 文件夹指向 `/media/电影`、`/media/剧集` 等子目录
3. 点保存，Jellyfin 自动扫描

> 如果 phase connect 报「Jellyfin bootstrap 未拿到 api_key」，说明
> jellyfin 容器还没起完。等 `docker compose ps` 看 jellyfin 状态 `Up`，
> 重跑 phase connect（幂等，已加过的 indexer / 已完成的 wizard 都会自动跳过）：
>
> ```bash
> docker compose --profile bootstrap run --rm bootstrap-connect
> docker compose restart helper
> ```

### 5. Web UI 登录凭据汇总

bootstrap 预填 qb / Jackett 的 conf：**Jackett 的 API Key 能预填**（helper 直接用）；
**qBittorrent 的 API Key 不能预填**——qB 5.2 的 key 是内部生成的 `qbt_` 串，写进
conf 无效（qB 不认，helper 会拿到 403）。所以 qb 的 key 要起来后手动生成一次（步骤 6）。
各服务 WebUI 登录凭据：

| 服务 | URL | 账号 | 密码 | 备注 |
|---|---|---|---|---|
| **jellyfin-helper** | `http://<宿主IP>:8099` | `admin` | `jellyfin_helper` | 改密码：编辑 `config.yaml.auth.users` + restart helper |
| **qBittorrent** | `http://<宿主IP>:8080` | `admin` | `jellyfin_helper` | 改密码见下方 |
| **Jackett** | `http://<宿主IP>:9117` | `admin` | `jellyfin_helper` | 改密码：UI → Configuration → Admin password |
| **Jellyfin** | `http://<宿主IP>:8096` | `admin` | `jellyfin_helper` | bootstrap 自动跑 Wizard；API Key 已回填 |
| **PostgreSQL** | `postgres:5432` | `jellyfin_helper` | `jellyfin_helper` | 仅栈内访问，5432 不暴露宿主 |

### 6.（必做）生成 qBittorrent API Key

qB 5.2 的 API Key 是内部生成的 `qbt_` 串，bootstrap 无法预填，要手动生成一次：

1. 浏览器开 `http://<宿主IP>:8080`，用 `admin / jellyfin_helper` 登录
2. 选项 → WebUI → 「API 密钥」→ 生成，复制 `qbt_` 开头的 key
3. 写进 `config.yaml` 的 `qbittorrent.api_key`
4. `docker compose restart helper`

helper 调 qb 用的是 **API Key（Bearer）不是密码**。没填 key 时 qb 相关功能
（下载列表 / 推种 / 配额）不可用，helper 本身仍正常启动。

> 想换 qb 登录密码：同一页面 Authentication 改即可，不影响 helper（helper 用 API Key 不用密码）。
> qb 会把设置落盘到 `data/qbittorrent/qBittorrent/qBittorrent.conf`，重启仍生效。

---

## 升级

### 拉最新镜像（jellyfin / jackett / qb / postgres）

```bash
docker compose pull jellyfin jackett qbittorrent postgres
docker compose up -d
```

### 升级 jellyfin-helper 自身

```bash
git pull                       # 拉新代码
docker compose build helper    # 重建镜像（多阶段构建会自动拿新前端 / 后端）
docker compose up -d helper
```

---

## 备份

要备份的目录全在项目里，打包 `./data` 和 `./config.yaml` 即可：

```bash
docker compose stop
tar czf jellyfin-helper-backup-$(date +%F).tar.gz data/ config.yaml .env
docker compose start
```

`data/postgres/` 是 Postgres 的数据卷（pg_dump 更优雅，但停服 + 文件拷贝在小数据量下够用）。

---

## 常见问题

### 媒体扫不到 / jellyfin 看不见文件

容器内路径都是 `/media`，确认宿主 `MEDIA_DIR` 在容器内可读：

```bash
docker compose exec jellyfin ls /media
docker compose exec helper ls /media
```

如果 jellyfin 能看见而 helper 看不见，多半是 PUID/PGID 不一致——helper 默认以
`${PUID}:${PGID}` 运行（compose `user:` 字段），调成跟宿主目录属主一致。

### qBittorrent 默认密码

如果跑过 `bootstrap`，密码就是 `admin` / `jellyfin_helper`（API Key 已生成
并写进 config.yaml）。

如果**没跑** bootstrap（直接 `docker compose up -d`），linuxserver 镜像首次启动
会随机生成 admin 密码并打印到日志：

```bash
docker compose logs jh-qbittorrent | grep -i "temporary password"
```

### Bootstrap 报错重置

如果 bootstrap 跑出问题（写歪了 conf、密码忘了、想全清重来），停服后清掉对应数据卷：

```bash
docker compose stop qbittorrent jackett
rm -rf data/qbittorrent data/jackett
docker compose --profile bootstrap run --rm bootstrap-prep
docker compose start qbittorrent jackett
docker compose --profile bootstrap run --rm bootstrap-connect
```

注意：清掉 `data/qbittorrent` 会丢做种历史，仅在初始化阶段这么做。

### Postgres 改密码

`POSTGRES_PASSWORD` 只在 **首次启动**（data 目录还是空的）时生效，后续改要
进容器 `ALTER USER`，并同步 `config.yaml`：

```bash
docker compose exec postgres psql -U jellyfin_helper -c "ALTER USER jellyfin_helper PASSWORD 'new-pwd';"
# 改 config.yaml database.password
docker compose restart helper
```

### 端口冲突

宿主 8099 / 8096 / 9117 / 8080 / 6881 已被占用时，改 `.env` 里对应的
`*_PORT` 即可（容器内端口不变，只改宿主映射）。

### 硬件转码（Jellyfin）

Intel iGPU：在 `docker-compose.yml` 的 `jellyfin` 段下加 `devices: ["/dev/dri:/dev/dri"]`。
NVIDIA：装好 nvidia-container-toolkit，按文件里注释的 `deploy.resources.reservations.devices`
配置打开。

---

## 已知限制

- **代理 / 科学上网**：README 顶部前置提醒说过了，TMDB / Trakt / AniList /
  OpenSubtitles 等都在境外。compose 自身不处理代理；如果你的代理在宿主上，
  helper 容器需要 `environment` 里加 `HTTP_PROXY` / `HTTPS_PROXY` 指向
  `http://host.docker.internal:<port>`（Linux 还需 `extra_hosts`）。
- **schema 变更**：项目仍在迭代，DB schema 变更走「清表重扫」而不是迁移脚本
  （README 已明示）。升级 helper 后看启动日志判断是否需要清 `data/postgres`。
- **config.yaml.bak.\* 备份文件**：helper 改配置时会生成时间戳备份，会落在
  项目根目录而非 `data/`，按需手动清理。
