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
**Postgres / Jackett / qBittorrent / Jellyfin 这四样不用动**——下一步 bootstrap
脚本会自动把它们的容器内地址和 API Key 写回来。

### 3. 预创建数据目录（首次部署，**必须**）

bind mount 的源目录如果不存在，docker daemon 会以 **root** 创建——但 helper /
bootstrap 容器以 `${PUID}:${PGID}` 启动写不进 root 拥有的目录，启动会直接报
"data 不可写"退出。所以首次部署前手动建好并 chown：

```bash
mkdir -p data/{postgres,jellyfin,jackett,qbittorrent,helper} logs

# Linux / macOS：把属主改成 .env 里的 PUID:PGID
sudo chown -R 1000:1000 data logs       # 改成你的 PUID:PGID
```

Windows + Docker Desktop 一般不存在这个问题（bind mount 走 SMB 转换层，权限模型不同）。

### 4. 一次性 bootstrap（关键一步）

```bash
# 4a. 预填 qbittorrent / jackett 的配置（生成 admin/jellyfin_helper 密码 + API Key）
docker compose --profile bootstrap run --rm bootstrap

# 3b. 拉起所有服务
docker compose up -d

# 3c. 再跑一次 bootstrap，给 Jackett 添加 7 个公开 indexer
#     （52BT / dmhy / OneJAV / ThePirateBay / TheRARBG / TorrentKitty / YTS）
docker compose --profile bootstrap run --rm bootstrap

# 3d. 让 helper 重读 config.yaml
docker compose restart helper
```

bootstrap 干了这些（**幂等**，重复跑安全）：

- 写 `data/qbittorrent/qBittorrent/qBittorrent.conf`：用户名 `admin` /
  密码 `jellyfin_helper`、生成随机 API Key、启用 RSS + 自动下载规则处理
- 写 `data/jackett/Jackett/ServerConfig.json`：生成 API Key、`AllowExternal=true`
- 添加 7 个 indexer（公开站点，无需 cookie）
- 把以上 API Key 与容器内地址 (`postgres` / `jellyfin:8096` / `jackett:9117` /
  `qbittorrent:8080`) 回写到 `config.yaml`，并备份原文件

### 5. 拿 Jellyfin API Key

只有 Jellyfin 需要手动拿 Key（因为它有 Setup Wizard 必须走一遍）：

1. 浏览器开 `http://<宿主IP>:8096` 走完 Jellyfin 首启向导（建账号、添加媒体库
   指向 `/media`）
2. 控制台 → API Keys → 新建一个 → 复制
3. 填进 `config.yaml` 的 `jellyfin.api_key`
4. `docker compose restart helper`

### 6. 访问 jellyfin-helper

浏览器打开 `http://<宿主IP>:8099`，用 `config.yaml` 里 `auth.users` 配的账号登录。

### 7.（可选）改后台密码

- qBittorrent / Jackett 的 Web UI 都已经能用 `admin` / `jellyfin_helper` 登录
  （Jackett 默认没强制密码，可在它 UI 里加）
- 想换 qBittorrent 密码：直接在 qB Options → Web UI 里改，**改完后同步**
  容器内的 `data/qbittorrent/qBittorrent/qBittorrent.conf` 已自动落盘，
  无需手工同步

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
docker compose --profile bootstrap run --rm bootstrap
docker compose start qbittorrent jackett
docker compose --profile bootstrap run --rm bootstrap   # phase 2 加 indexer
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
