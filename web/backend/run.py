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

# 让 web.backend.* 能被 import
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from web.backend.config import settings


def main():
    port = int(os.environ.get('BACKEND_PORT') or settings.backend_port)
    host = os.environ.get('BACKEND_HOST', '0.0.0.0')
    reload = os.environ.get('BACKEND_RELOAD', '').lower() in ('1', 'true', 'yes')
    workers = int(os.environ.get('BACKEND_WORKERS', '1'))

    print(f'[run] starting uvicorn on http://{host}:{port}  (reload={reload}, workers={workers})', flush=True)

    uvicorn.run(
        'web.backend.main:app',
        host=host,
        port=port,
        reload=reload,
        workers=workers if not reload else 1,
    )


if __name__ == '__main__':
    main()
