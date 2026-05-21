"""
日志 API：在 Web UI 里看日志 + 调日志级别。

后端日志文件由 main.py 配置的 RotatingFileHandler 写到 logs/jellyfin-helper.log。
本模块对外暴露：
  GET  /api/logs/tail?lines=500&level=INFO   读最后 N 行（可选按 level 过滤）
  GET  /api/logs/level                       当前 root logger 级别
  POST /api/logs/level   body={level: ...}   动态改 level（不写盘，重启失效）
  GET  /api/logs/files                       列出 jellyfin-helper.log + rotate 备份
"""
from __future__ import annotations

import logging
import re
from collections import deque
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

# 与 main.py 的 _LOG_FILE 保持一致；模块 import 时一次性算出
_ROOT_DIR = Path(__file__).parent.parent.parent
_LOG_DIR = _ROOT_DIR / 'logs'
_LOG_FILE = _LOG_DIR / 'jellyfin-helper.log'

# 日志行级别匹配：'2026-05-08 21:06:54,667 [INFO] ...'
_LEVEL_RE = re.compile(r'\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]')

_VALID_LEVELS = {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}

logger = logging.getLogger(__name__)


class LogLevelRequest(BaseModel):
    level: str


@router.get("/logs/tail")
def tail_logs(lines: int = 500, level: Optional[str] = None):
    """
    读 jellyfin-helper.log 末尾 N 行；可按 level 过滤（>= 该 level 的才返回）。
    lines 上限 5000 防止前端加载过大。
    """
    lines = max(10, min(lines, 5000))
    if not _LOG_FILE.exists():
        return {"file": str(_LOG_FILE), "exists": False, "lines": []}

    # 用 deque 滑动窗口拿末尾 N 行（避免整个 200MB 文件读进内存）
    try:
        with open(_LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            tail = deque(f, maxlen=lines)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"读日志失败: {e}")

    raw = list(tail)
    # level 过滤
    if level:
        level_up = level.upper()
        if level_up not in _VALID_LEVELS:
            raise HTTPException(status_code=400, detail=f"未知 level: {level}")
        # 仅保留 ≥ 该 level 的行（按 level 数值序：DEBUG < INFO < WARNING < ERROR < CRITICAL）
        threshold = logging.getLevelName(level_up)  # str → int
        filtered: List[str] = []
        for line in raw:
            m = _LEVEL_RE.search(line)
            if not m:
                # 非标准行（continuation / traceback）保留：跟随上一条
                if filtered:
                    filtered.append(line)
                continue
            line_lvl = logging.getLevelName(m.group(1))  # str → int
            if isinstance(line_lvl, int) and line_lvl >= threshold:
                filtered.append(line)
        raw = filtered

    return {
        "file": str(_LOG_FILE),
        "exists": True,
        "size_bytes": _LOG_FILE.stat().st_size if _LOG_FILE.exists() else 0,
        "lines": raw,
        "count": len(raw),
    }


@router.get("/logs/level")
def get_log_level():
    """当前 root logger 级别（数字 + 名字）+ 各 handler 的级别。"""
    root = logging.getLogger()
    return {
        "root_level": logging.getLevelName(root.level),
        "root_level_int": root.level,
        "handlers": [
            {
                "name": type(h).__name__,
                "level": logging.getLevelName(h.level),
                "file": getattr(h, 'baseFilename', None),
            }
            for h in root.handlers
        ],
    }


@router.post("/logs/level")
def set_log_level(req: LogLevelRequest):
    """
    动态改 root logger 级别 + 同步到所有 handlers。
    注意：仅当前进程有效；重启后端会回到默认 INFO。
    """
    level_up = (req.level or '').upper()
    if level_up not in _VALID_LEVELS:
        raise HTTPException(status_code=400, detail=f"未知 level: {req.level}")
    root = logging.getLogger()
    old_level = logging.getLevelName(root.level)
    # 用 WARNING 而非 INFO：日志级别变更是审计事件——事后看"为什么日志爆炸 / 为什么没 INFO"时
    # 需要明确知道发生过这步切换；WARNING 不管将来 level 怎么调都能保留这条
    logger.warning(
        f"/logs/level POST: root logger {old_level} → {level_up}（用户主动；进程重启会失效）"
    )
    root.setLevel(level_up)
    for h in root.handlers:
        h.setLevel(level_up)
    return {
        "ok": True,
        "level": level_up,
        "note": "仅当前进程有效；重启后端将回到默认 INFO",
    }


@router.get("/logs/files")
def list_log_files():
    """列出 jellyfin-helper.log 主文件 + rotate 备份（jellyfin-helper.log.1 / .2 ...）"""
    if not _LOG_DIR.exists():
        return {"dir": str(_LOG_DIR), "files": []}
    out = []
    for p in sorted(_LOG_DIR.glob('jellyfin-helper.log*')):
        try:
            stat = p.stat()
            out.append({
                "name": p.name,
                "size_bytes": stat.st_size,
                "modified": stat.st_mtime,
            })
        except OSError:
            continue
    return {"dir": str(_LOG_DIR), "files": out}
