"""
trash_dir 定期清理：调度周期到了就清空整个 trash_dir。

策略：trash_interval_seconds 控制清理周期；周期内被 organizer 扔进来的
"sample/nfo/RARBG.txt"等都在下次调度时整体清空，不再按文件保留天数判定。
（用户想保留更久 → 调大 trash_interval_seconds 即可。）
"""
from __future__ import annotations

import logging
from pathlib import Path

from web.backend.config import settings

logger = logging.getLogger(__name__)


def clean_trash() -> dict:
    """清空 trash_dir 下所有文件 + 删空目录（保留 trash_dir 本身）。"""
    cfg = settings.dispatch
    if not cfg.trash_dir:
        logger.debug("trash_cleaner: trash_dir 未配置，跳过")
        return {'deleted': 0, 'errors': 0}
    trash = Path(cfg.trash_dir)
    if not trash.exists():
        logger.debug(f"trash_cleaner: {trash} 不存在，跳过")
        return {'deleted': 0, 'errors': 0}

    deleted = 0
    errors = 0
    freed_bytes = 0

    # 删所有文件
    for f in trash.rglob('*'):
        try:
            if f.is_file():
                try:
                    sz = f.stat().st_size
                except OSError:
                    sz = 0
                f.unlink()
                deleted += 1
                freed_bytes += sz
        except Exception as e:
            logger.warning(f"trash 文件删除失败 {f}: {e}")
            errors += 1

    # 再删空目录（自下而上，trash 根本身不动）
    for d in sorted(trash.rglob('*'), key=lambda p: -len(str(p))):
        if d.is_dir():
            try:
                if not any(d.iterdir()):
                    d.rmdir()
            except Exception:
                pass

    if deleted or errors:
        logger.info(
            f"trash 清理：删除 {deleted} 个文件 ({freed_bytes/1e9:.2f}GB)"
            + (f"，{errors} 个失败" if errors else "")
        )
    else:
        logger.debug("trash 清理：无需处理")
    return {'deleted': deleted, 'errors': errors}
