"""
跨种子目标路径去重决策。

organizer 渲染出 dst 后调本模块的 resolve()，按 DispatchRule.duplicate_policy
判断这次复制是 proceed / skip / raise（让上层标 needs_review）。

跟 copier.CrossTorrentCollisionError 的区别：
  - copier 那个是"字节级冲突"：dst 已经物理存在且 mtime 老于阈值，避免 append 损坏；
    属于 fail-safe 防线（即使 DB 漏判也不会写坏文件）。
  - 本模块是"目标级冲突"：dst 还没物理写，但 DB 里 dispatched_files 已经记录了该路径
    属于另一个种子；属于 main path 防线（提前决策，能避免无谓 IO）。
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Dict, Literal, Optional

from sqlalchemy import cast
from sqlalchemy.dialects.postgresql import JSONB

from tools.dispatch.quality import compare as quality_compare
from backend.database import DownloadDispatchMap

logger = logging.getLogger(__name__)


# 跳过状态：dispatch_map.phase 含这些值时不算占用（已被清理/被人拒了）
_INACTIVE_PHASES = ('cleaned', 'dismissed')


ACTION_PROCEED: Literal['proceed'] = 'proceed'
ACTION_SKIP:    Literal['skip']    = 'skip'


class DuplicateConflictError(Exception):
    """目标路径被另一个 dispatch_map 行占用，按 policy 需要人工决策。
    上层 (pipeline_worker) 捕获后落 phase_status=needs_review，把 info 写进 error_log。"""

    def __init__(
        self,
        dst: Path,
        my_hash: str,
        my_release_name: str,
        existing_hash: str,
        existing_release_name: str,
        existing_phase: str,
        reason: str = 'policy=needs_review',
    ):
        self.dst = str(dst)
        self.my_hash = my_hash
        self.my_release_name = my_release_name
        self.existing_hash = existing_hash
        self.existing_release_name = existing_release_name
        self.existing_phase = existing_phase
        self.reason = reason
        super().__init__(
            f"目标 {dst} 已被 {existing_hash[:16]}.. 占用 "
            f"(release={existing_release_name!r}, phase={existing_phase}) | reason={reason}"
        )

    def to_dict(self) -> Dict:
        """写入 error_log 用的结构化上下文。"""
        return {
            'kind': 'duplicate_conflict',
            'dst': self.dst,
            'my_hash': self.my_hash,
            'my_release_name': self.my_release_name,
            'existing_hash': self.existing_hash,
            'existing_release_name': self.existing_release_name,
            'existing_phase': self.existing_phase,
            'reason': self.reason,
        }


def _move_to_trash(file_path: Path, trash_dir: Optional[Path], owner_hash: str) -> Optional[Path]:
    """把要被替换的旧文件移到 trash 子目录。
    路径：<trash_dir>/_replaced/<YYYYMMDD-HHMMSS>_<owner_hash[:8]>/<basename>
    trash_cleaner 跟 junk 走同一套保留期。"""
    if not trash_dir:
        logger.warning(f"trash_dir 未配置，被替换的旧文件 {file_path} 将直接 unlink")
        try:
            file_path.unlink()
        except Exception as e:
            logger.warning(f"unlink 旧文件失败 {file_path}: {e}")
        return None
    ts = time.strftime('%Y%m%d-%H%M%S')
    sub = Path(trash_dir) / '_replaced' / f"{ts}_{owner_hash[:8]}"
    sub.mkdir(parents=True, exist_ok=True)
    target = sub / file_path.name
    try:
        shutil.move(str(file_path), str(target))
        logger.info(f"replaced → trash: {file_path} → {target}")
        return target
    except Exception as e:
        logger.warning(f"被替换文件入 trash 失败 {file_path} → {target}: {e}（尝试 unlink 兜底）")
        try:
            file_path.unlink()
        except Exception as e2:
            logger.warning(f"unlink 旧文件也失败 {file_path}: {e2}")
        return None


def _find_existing_owner(db, dst: Path, current_hash: str) -> Optional[DownloadDispatchMap]:
    """反查 dispatch_map：是否有别的行的 dispatched_files 数组含这个 dst。"""
    dst_str = str(dst)
    # JSONB 包含运算 @>：dispatched_files 数组中含 dst_str
    return (
        db.query(DownloadDispatchMap)
        .filter(cast(DownloadDispatchMap.dispatched_files, JSONB).contains([dst_str]))
        .filter(DownloadDispatchMap.torrent_hash != current_hash)
        .filter(~DownloadDispatchMap.phase.in_(_INACTIVE_PHASES))
        .first()
    )


def resolve(
    db,
    current_hash: str,
    current_release_name: str,
    src: Path,
    dst: Path,
    policy: str,
    trash_dir: Optional[Path],
) -> str:
    """
    判断当前 src→dst 的复制要不要做。
    返回 ACTION_PROCEED 或 ACTION_SKIP；policy=needs_review 时抛 DuplicateConflictError。

    policy:
      - 'higher_quality_wins': 比较 release name 的质量 tier
      - 'always_skip':         有占用就跳
      - 'always_replace':      有占用就替换（旧入 trash）
      - 'needs_review':        有占用就抛错让用户决策
    """
    existing = _find_existing_owner(db, dst, current_hash)
    if existing is None:
        return ACTION_PROCEED

    existing_name = existing.title or ''
    # release name 可以从 dst 文件名 / src 文件名兜底 —— file_template 之后的 title 通常不带 release tag
    # 用 src 文件名做质量比较更准（含 1080p/2160p/REMUX/PROPER 等）
    my_name = src.name
    their_name = Path(existing.dispatched_files[0]).name if existing.dispatched_files else existing_name

    logger.info(
        f"duplicate-detect: dst={dst} 已被 {existing.torrent_hash[:16]}.. 占用 "
        f"(policy={policy}, my={my_name!r}, their={their_name!r})"
    )

    if policy == 'always_skip':
        logger.info(f"duplicate: always_skip → 跳过 {dst}")
        return ACTION_SKIP

    if policy == 'always_replace':
        logger.info(f"duplicate: always_replace → 旧文件入 trash 后覆盖 {dst}")
        if dst.exists():
            _move_to_trash(dst, trash_dir, existing.torrent_hash)
        return ACTION_PROCEED

    if policy == 'needs_review':
        raise DuplicateConflictError(
            dst=dst,
            my_hash=current_hash, my_release_name=my_name,
            existing_hash=existing.torrent_hash,
            existing_release_name=their_name,
            existing_phase=existing.phase or '',
            reason='policy=needs_review',
        )

    # higher_quality_wins（默认）
    verdict = quality_compare(their_name, my_name)
    if verdict == 'new_wins':
        logger.info(
            f"duplicate: quality new_wins → 旧入 trash 后覆盖 ({their_name!r} → {my_name!r})"
        )
        if dst.exists():
            _move_to_trash(dst, trash_dir, existing.torrent_hash)
        return ACTION_PROCEED
    if verdict == 'old_wins':
        logger.info(f"duplicate: quality old_wins → 跳过 ({my_name!r} 不如 {their_name!r})")
        return ACTION_SKIP
    # tie：保守起见走人工
    raise DuplicateConflictError(
        dst=dst,
        my_hash=current_hash, my_release_name=my_name,
        existing_hash=existing.torrent_hash,
        existing_release_name=their_name,
        existing_phase=existing.phase or '',
        reason='quality_tie',
    )
