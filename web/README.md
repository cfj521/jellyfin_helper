# Jellyfin Tools Web

Jellyfin 媒体服务器工具集 Web 应用。

> 启动、配置等公共内容请看项目根目录的 [README.md](../README.md)。本文件只补充 Web 子项目的细节。

## 目录结构

```
web/
├── backend/                # FastAPI 后端
│   ├── main.py             #   应用入口（路由注册、中间件）
│   ├── run.py              #   启动器（按 BACKEND_PORT > config.yaml > 8000 决定端口）
│   ├── config.py           #   配置管理（pydantic-settings）
│   ├── database.py         #   SQLAlchemy 模型 + 会话
│   ├── path_translator.py  #   路径转换（容器路径 ↔ 真实路径）
│   ├── api/                #   REST API 路由
│   │   ├── subtitle.py     #     字幕扫描 / 重命名 / 下载
│   │   ├── metadata.py     #     演员图、海报
│   │   ├── media.py        #     浏览、重复检测、存储分析
│   │   ├── audio.py        #     MKV 默认音轨设置
│   │   ├── discover.py     #     TMDB 热门 / Jackett 搜索 / qBit 推送
│   │   ├── adult.py        #     成人内容刮削（可选）
│   │   ├── jellyfin.py     #     Jellyfin 库浏览代理
│   │   ├── stats.py        #     总览与历史
│   │   ├── tasks.py        #     后台任务管理
│   │   ├── config_api.py   #     读写 config.yaml
│   │   └── maintenance.py  #     数据库维护
│   ├── services/           #   常驻后台服务
│   │   ├── jellyfin_ws.py  #     Jellyfin WebSocket 事件订阅
│   │   └── adult_watcher.py
│   └── scrapers/           #   网页刮削器
└── frontend/               # Vue.js 前端
    ├── package.json
    ├── vite.config.js      # 代理 /api 到后端，端口读 config.yaml
    └── src/
        ├── main.js
        ├── App.vue
        ├── api/            # 后端 API 封装
        ├── router/         # 路由配置
        ├── components/     # 通用组件
        └── views/
            ├── Home.vue
            ├── Tasks.vue
            ├── Settings.vue
            ├── subtitle/
            ├── metadata/
            ├── media/
            ├── discover/
            └── adult/
```

## 后端启动方式

进入项目根目录，安装依赖后用启动器：

```bash
pip install -r requirements.txt
python -m web.backend.run
```

启动器会按这个优先级决定监听端口：

1. 环境变量 `BACKEND_PORT`
2. `config.yaml` 的 `server.backend_port`
3. 默认 `8000`

开发热重载用 `BACKEND_RELOAD=1`：

```bash
# bash
BACKEND_RELOAD=1 python -m web.backend.run

# PowerShell
$env:BACKEND_RELOAD='1'; python -m web.backend.run
```

## 前端启动方式

```bash
cd web/frontend
npm install
npm run dev
```

`vite.config.js` 会自动读 `config.yaml` 的 `server.frontend_port` 决定 dev server 端口，并把 `/api` 反向代理到后端。

## 数据库

表会在首次启动时自动创建。

| 表名 | 说明 |
|------|------|
| `tasks` | 后台任务记录 |
| `scan_reports` | 扫描报告存档 |
| `actors` | 演员信息缓存 |
| `media_items` | 媒体文件元数据 |
| `download_tasks` | 下载任务 |
| `adult_items` | 成人内容元数据（可选） |

如需手动连接数据库：

```bash
psql -h <host> -U jellyfin_tools -d jellyfin_tools
```

## API 文档

后端启动后访问：

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
- 健康检查: `GET /api/health` → `{"status":"ok",...}`

## 常见问题

### 后端启动失败

1. 检查 PostgreSQL 是否可达，库和用户是否已创建
2. 确认 `config.yaml` 的 `database` 段填写正确
3. 确认 `requirements.txt` 全部装好

### 前端无法连接后端

1. 确认后端已启动在 `config.yaml` 中配置的端口
2. 检查 `vite.config.js` 中的代理配置
3. 检查 `cors_origins`（默认 `["*"]`）

### 数据库连接错误

```bash
# 测试连接
psql -h <host> -p 5432 -U jellyfin_tools -d jellyfin_tools
```

如果连不上，依次排查：网络、防火墙、`pg_hba.conf` 是否允许该 IP、用户密码、数据库是否存在。
