"""
后端启动入口。
端口读取优先级：
  1. 环境变量 BACKEND_PORT（compose / 命令行覆盖）
  2. config.yaml 的 server.backend_port
  3. 默认 8000
host 默认 0.0.0.0（容器内 / 局域网均可访问）。
"""
import os
import sys
from pathlib import Path

import uvicorn

# 让 backend.* 能被 import。run.py 在 backend/ 层，父目录（含 backend 包）是
# parent.parent —— 跟 backend/config.py、backend/main.py 的 ROOT_DIR 一致。
# 注意 backend/api/*.py 在更深一层，那里才是 parent.parent.parent。
# （原来误用 parent.parent.parent，容器里 /app/backend/run.py 会算到 /，
#  导致 from backend.config 报 ModuleNotFoundError: No module named 'backend'）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import settings


def main():
    port = int(os.environ.get('BACKEND_PORT') or settings.backend_port)
    host = os.environ.get('BACKEND_HOST', '0.0.0.0')
    reload = os.environ.get('BACKEND_RELOAD', '').lower() in ('1', 'true', 'yes')
    workers = int(os.environ.get('BACKEND_WORKERS', '1'))

    print(f'[run] starting uvicorn on http://{host}:{port}  (reload={reload}, workers={workers})', flush=True)

    uvicorn.run(
        'backend.main:app',
        host=host,
        port=port,
        reload=reload,
        workers=workers if not reload else 1,
    )


if __name__ == '__main__':
    main()
