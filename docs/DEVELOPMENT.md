# 开发指南

面向想加功能 / 调 bug / 看代码的开发者。如果你是要"跑起来"的用户，看根目录 [README.md](../README.md) 就够了。

---

## 1. 架构速览

### 1.1 进程模型

```
┌─────────────────────────────────────────────────────────────────┐
│   Vue SPA (前端, vite dev / vite build + nginx 等)              │
│   - 11 个主页面 (medialibraries / downloadpipeline / settings   │
│     / tasks / discover / resourcesearch / ...)                  │
│   - Composition API, Element Plus                              │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTP + SSE
┌──────────────────────────▼──────────────────────────────────────┐
│   FastAPI 后端 (uvicorn 单进程多线程)                            │
│   - 18 个 API router (按功能切片)                                │
│   - SSE 端点: /api/tasks/stream, /api/tasks/{id}/stream         │
│   - 后台:                                                       │
│       * task 系统 (TaskRunner + cancellable_task 装饰器)         │
│       * dispatch 流水线 (analyzer / pipeline_worker /           │
│                         post_process_worker / adopt /           │
│                         sweeper / jellyfin_watcher)             │
│       * actress_builder (女优库后台构建)                         │
│       * jellyfin WS 监听 (item 变更实时同步)                     │
│       * ratings 异步 worker                                      │
│   - lifespan shutdown + 15s force-exit watchdog                 │
└─────┬──────────┬──────────┬──────────┬──────────┬───────────────┘
      │          │          │          │          │
   ┌──▼──┐   ┌──▼──┐    ┌──▼──┐    ┌──▼──┐    ┌──▼──┐
   │ PG  │   │ JF  │    │ qB  │    │Jack │    │ ... │
   │ DB  │   │ REST│    │ Web │    │ ett │    │     │
   └─────┘   └─────┘    └─────┘    └─────┘    └─────┘
```

### 1.2 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 推送方式 | **SSE** 而非 WebSocket | 单向推送够用，proxy / shutdown 更好处理 |
| 后台任务 | **进程内线程 + DB 状态** | 不引入 celery / redis；DB 作为消息总线 |
| 任务取消 | **`update_task_progress` 拦截 + `TaskCancelledError`** | 长任务每次 emit 进度都检查 cancelled / shutdown，自动抛出退出 |
| dispatch 阶段机 | **DB phase + status 列驱动** | 每个 worker 按 phase claim，崩溃恢复幂等 |
| 跨进程限流 | **类级 `_global_lock` + `_global_last_call`** | assrt 等 API 必须进程级单例（实例化多次会失去节流） |
| 反爬 | **`curl_cffi`** TLS impersonation | 过 CloudFlare 弱反爬（TLS fingerprint），但过不了 Turnstile JS challenge |
| schema 变更 | **开发期直接清表** | 不维护迁移脚本；只在 schema 稳定后才考虑 alembic |

### 1.3 关键代码入口

| 入口 | 位置 | 备注 |
|---|---|---|
| 后端启动 | `web/backend/run.py` | uvicorn 启动器；端口决议 |
| 应用入口 | `web/backend/main.py` | lifespan / 信号 / 中间件 / router 注册 |
| 任务装饰器 | `web/backend/api/tasks.py: cancellable_task` | 所有长任务包一层 |
| dispatch 主循环 | `tools/dispatch/pipeline_worker.py` | 状态机推进 |
| 字幕核心 | `web/backend/api/subtitle.py: run_subtitle_auto_fix_inline` | 所有调用方共享（dispatch / MediaToolbar / maintenance.run_all）|
| 配置单例 | `web/backend/config.py: settings` | pydantic settings + yaml load |
| 数据库模型 | `web/backend/database.py` | 全部 SQLAlchemy declarative |
| 性能诊断 | `web/backend/diagnostics.py` | TimingMiddleware / DB 池监控 / 静音轮询白名单 |

---

## 2. 开发环境

### Windows

```powershell
# 用 conda 隔离（推荐）
conda create -n env_jellyfin_helper python=3.12
conda activate env_jellyfin_helper

cd D:\path\to\jellyfin-helper
pip install -r requirements.txt

# 后端（热重载）
$env:BACKEND_RELOAD='1'; python -m web.backend.run

# 前端（新终端）
cd web\frontend
npm install
npm run dev
```

### Linux / macOS

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

BACKEND_RELOAD=1 python -m web.backend.run

# 前端
cd web/frontend && npm install && npm run dev
```

### 访问

- 前端：http://localhost:5173
- API Swagger：http://localhost:8000/docs

### 常用环境变量

| 变量 | 作用 |
|---|---|
| `BACKEND_PORT` | 临时覆盖后端端口 |
| `FRONTEND_PORT` | 临时覆盖前端端口 |
| `BACKEND_RELOAD=1` | 启用 uvicorn `--reload` 热重载 |

> ⚠️ `BACKEND_RELOAD=1` 在跑长任务时会假阻塞前端（reload 会断 SSE）。如果调试长任务，**关掉热重载**。

---

## 3. 数据库

### 3.1 业务库 (PostgreSQL)

启动时自动建表（`web/backend/database.py: init_db`），不写迁移脚本。开发阶段改 schema 直接清表重扫。

主要表（节选）：

| 表 | 用途 |
|---|---|
| `tasks` | 后台任务（含 result JSON、status、progress、cancel 标志） |
| `scan_reports` | 字幕 / 元数据扫描结果归档（10 分钟内可被 auto-fix 复用） |
| `actors` | 演员缓存（jellyfin id → tmdb id + image） |
| `media_items` | jellyfin item 长缓存（带 path / 字幕语言 / 时长 / 分辨率等） |
| `media_metadata` | 媒体元数据实体表（评分 / 海报 / 演员等多源合并） |
| `download_dispatch_map` | dispatch 流水线 phase 状态机的主表 |
| `download_quota` | 配额 / 节流 |
| `adult_items` | 番号元数据 |
| `adult_actresses` | 女优档案（多源 chain：javdb + minnano_av） |
| `ratings` | MDBList + 豆瓣评分缓存 |
| `video_annotations` | 用户手工标注（如硬字幕语言） |
| `llm_classify_cache` | LLM 识别结果缓存 |

完整 schema 看 [web/backend/database.py](../web/backend/database.py)。

### 3.2 Jellyfin SQLite 直读（可选）

`common/jellyfin_db.py` 实现了 jellyfin 10.9+ `BaseItems` 主表的只读访问，用于 path → item 反查加速（实测 4ms vs REST 1500ms）。

- 开关：`config.yaml: jellyfin.db_path`，留空走 REST
- 安全：`?mode=ro&nolock=1&immutable=1`，跨 SMB 也能读
- Fallback：schema 不兼容 / 权限不通 / SQL 失败 → 自动禁用 + 退到 REST，业务无感知
- 永远只读，**绝不可写**

---

## 4. 添加新功能

### 4.1 新 API router

```python
# web/backend/api/foo.py
from fastapi import APIRouter
router = APIRouter()

@router.get("/something")
def get_something():
    return {"ok": True}
```

然后在 `web/backend/main.py` 注册：
```python
from web.backend.api import foo
app.include_router(foo.router, prefix="/api/foo", tags=["foo"])
```

### 4.2 长任务

走 `tasks.py` 的 task wrapper 模式：

```python
from web.backend.api.tasks import cancellable_task, update_task_progress

@cancellable_task
def run_my_task(task_id: int, ...):
    for i, item in enumerate(items):
        # update_task_progress 内部会检查 cancelled / shutdown，
        # 触发 TaskCancelledError，被装饰器自动 catch + 标 cancelled
        update_task_progress(db, task_id, pct=int(i/len(items)*100),
                             msg=f"处理 {item}")
        do_work(item)
    complete_task(db, task_id, result={"done": True})
```

### 4.3 前端调 API

`web/frontend/src/api/index.js` 已配置全局 axios 实例：

```javascript
import { discoverApi } from '@/api'

const r = await discoverApi.someMethod({ foo: 'bar' })
```

**注意 list query 参数**：axios 1.x 全局已配 `paramsSerializer: { indexes: null }`，所以 `{ ids: ['a', 'b'] }` 会序列化为 `?ids=a&ids=b`，FastAPI 的 `Query(List[str])` 能正常接收。**不要回退到默认 serializer**（会变成 `?ids[]=a&ids[]=b`，FastAPI 收不到）。

### 4.4 新增第三方服务客户端

按 `common/*_client.py` 风格放进 `common/`，对外暴露一个类，构造函数接 base_url + key。`config.py` 加对应字段，`config.yaml.example` 同步更新。

---

## 5. 调试技巧

### 5.1 性能问题先加日志

诊断慢请求看 `[diag] HTTP SLOW ...` 日志（`web/backend/diagnostics.py` 自动打 >500ms 的请求）。

DB 连接持有过久（>500ms）也会 warning 并打调用栈。

### 5.2 静音高频轮询日志

`diagnostics.py: _SILENT_POLL_PATHS` 维护一份白名单，命中的端点 INFO 日志被静音，但 SLOW WARNING 仍打。新增高频轮询接口时把路径加进去。

### 5.3 任务失败排查

每条 task 有 `result` JSON 列，运行期会增量更新。出错时 result 含 `error` 字段。前端任务详情页右上角"原始数据"按钮可看完整 result。

### 5.4 dispatch 流水线卡住

看 `download_dispatch_map.phase + phase_status` 列。常见阻塞 phase：
- `analyzing` 卡住 → 看 analyzer 日志，多半是 identify 走 LLM 超时
- `copying` 卡住 → 看 copier 日志（大文件 + 跨网络盘很慢正常）
- `needs_review` → 前端会弹审核 modal，等用户决策
- `jellyfin_recognizing` → 看 jellyfin_watcher 日志，是不是 jellyfin 真的没扫到

### 5.5 字幕下载结果迷茫

`run_subtitle_auto_fix_inline` 末尾会打 per-video 决策日志：

```
auto-fix 扫描汇总: videos=N 已有字幕=X 完全无字幕=Y 缺所需语言=Z required=[chs,eng]
  ✓ XXX.mp4  内嵌=[eng,chs,...] 外挂=[] → 已覆盖 [chs,eng]，跳过下载
  ↓ YYY.mp4  内嵌=[eng] 外挂=[] → 缺 [chs]，启动下载
```

---

## 6. 代码风格

- Python: 3.12 语法，type hint 鼓励但不强制；模块顶部 docstring 说明"这个文件干啥的"
- 注释写"为什么"不写"是什么"；hidden constraint / 历史踩坑必须留注释
- 异常：**业务层不裸 catch Exception**，分类型 catch 或显式说理由
- DB session 短事务：`with SessionLocal() as db:` 包，**不持久跨 HTTP**
- 不写 backward-compat 兜底（开发期 schema 变更直接清表）

---

## 7. 历史与归档

设计阶段的 PRD / 决策报告在 [docs/archive/](archive/)：

- `2026-05-09-download-pipeline-plan.md` — dispatch 流水线 PRD（已实现）
- `2026-05-11-dispatch-partial-duplicate.md` — duplicate_policy 8 场景决策表
- `2026-05-11-ratings-system-review.md` — 评分系统 review
- `2026-05-15-media-metadata-store.md` — 媒体元数据实体表 PRD（已实现）

阅读时注意时间戳，部分细节可能跟当前实现有出入。

---

## 8. 相关项目参考

- [Sonarr](https://sonarr.tv/) / [Radarr](https://radarr.video/) / [Bazarr](https://www.bazarr.media/) — 主流自动化方案
- [Jellyfin API 文档](https://api.jellyfin.org/) — REST API 参考
- [TMDB API](https://developers.themoviedb.org/3) / [OpenSubtitles API](https://opensubtitles.stoplight.io/) — 主要外部服务文档
