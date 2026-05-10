"""
跨盘复制：/downloads(NVMe) → /library(HDD)。

简单、稳、有进度回调。用 shutil.copyfileobj 而不是 copy2/copy 是为了能流式
回调字节进度（大文件可能跑几十分钟，UI 实时更新很重要）。
完成后用 shutil.copystat 保留 mtime/permissions（jellyfin 元数据时间敏感）。
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# 复制缓冲：8MB 在 NVMe→HDD 上速率充足；大缓冲也无明显增益但占内存
DEFAULT_BUFFER = 8 * 1024 * 1024

# 续传安全阈值：dst.mtime 距今超过这个时间，视为来自不同种子的旧文件，拒绝 append
# （来自当前种子的同一次复制 mtime 几乎都在 1 小时内；隔 1 小时仍未完成的极少见）
RESUME_MTIME_GUARD_SECONDS = 3600


class CrossTorrentCollisionError(Exception):
    """目标文件存在但不像是当前种子的续传产物 —— 多半是另一个种子（D7 PROPER/REPACK 类）已经写过同一路径。
    上层应该把这条 dispatch 标 needs_review，让用户决策（覆盖 / 跳过 / 改名）。"""


def copy_file_with_progress(
    src: Path,
    dst: Path,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    buffer_size: int = DEFAULT_BUFFER,
    resume: bool = True,
) -> int:
    """
    流式复制单文件，progress_cb(bytes_done, bytes_total) 每个 buffer 块回调一次。
    返回复制的总字节数。复制完后 copystat 保留属性。

    resume=True（默认）：
      - 目标已存在且 size 等于源 → 跳过整个复制（视为已完成，0 字节传输）
      - 目标已存在但 size < 源 size 且 mtime 是新的 → 续传（append + 余下字节）
      - 目标已存在但 size < 源 size 且 mtime 老于 RESUME_MTIME_GUARD_SECONDS →
        视为跨种子冲突（D7 PROPER/REPACK），抛 CrossTorrentCollisionError，由上层决策
      - 目标已存在但 size > 源 size 或 mtime 异常 → 删除后从头复制（防文件损坏）
      - 目标不存在 → 从头复制
    """
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    total = src.stat().st_size
    skip = 0  # 已写字节（resume 起点）
    open_mode = 'wb'

    if resume and dst.exists():
        try:
            dst_stat = dst.stat()
            dst_size = dst_stat.st_size
            dst_mtime = dst_stat.st_mtime
        except OSError:
            dst_size = 0
            dst_mtime = 0.0
        if dst_size == total:
            # 完整匹配 → 已复制过，跳过整个传输
            logger.info(f"resume: {dst.name} 已存在且 size 一致，跳过复制")
            if progress_cb:
                try:
                    progress_cb(total, total)
                except Exception:
                    pass
            return 0
        if 0 < dst_size < total:
            # partial → 续传，但要先确认这是当前种子的中断产物，不是别的种子留下的脏数据
            age = time.time() - dst_mtime
            if age > RESUME_MTIME_GUARD_SECONDS:
                # 老文件 → 多半是另一个种子（D7 PROPER/REPACK / 同 TMDB 不同质量）写过这里
                # 此时 append 会把新种子的内容追加到旧种子文件尾部 → 文件结构损坏
                # 抛出明确错误让 organizer/pipeline 处理（建议: 备份旧文件到 trash 后重写）
                raise CrossTorrentCollisionError(
                    f"目标 {dst} 已存在 ({dst_size}/{total} bytes) 但 mtime 老于 "
                    f"{int(age/60)} 分钟前 —— 多半是另一个种子的旧文件。"
                    f"为防 append 损坏文件，已拒绝续传。请人工决策（覆盖/改名/跳过）。"
                )
            logger.info(f"resume: {dst.name} 续传，已有 {dst_size}/{total} bytes (age={int(age)}s)")
            skip = dst_size
            open_mode = 'ab'
        else:
            # 异常（dst_size > total 或为 0 或 stat 失败）→ 重头复制
            try:
                dst.unlink()
            except Exception:
                pass

    done = skip

    with open(src, 'rb') as f_in, open(dst, open_mode) as f_out:
        if skip:
            f_in.seek(skip)
        while True:
            chunk = f_in.read(buffer_size)
            if not chunk:
                break
            f_out.write(chunk)
            done += len(chunk)
            if progress_cb:
                try:
                    progress_cb(done, total)
                except Exception:
                    logger.exception("progress_cb 抛错，已忽略")

    try:
        shutil.copystat(src, dst)
    except Exception as e:
        logger.warning(f"copystat 失败 {dst}: {e}")

    return done - skip   # 实际新写入的字节数


def copy_tree_with_progress(
    src_root: Path,
    dst_root: Path,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    file_filter: Optional[Callable[[Path], bool]] = None,
    buffer_size: int = DEFAULT_BUFFER,
) -> dict:
    """
    递归复制目录。progress_cb(bytes_done, bytes_total, current_file) 实时回调。
    file_filter(src_path) -> bool 决定是否复制此文件（False 跳过）。

    返回: {
        'files_copied': N,
        'files_skipped': M,
        'bytes_copied': total,
        'src_files': List[Path],     # 实际复制的源文件
        'dst_files': List[Path],     # 对应目标位置
    }
    """
    src_root = Path(src_root)
    dst_root = Path(dst_root)

    # 第一遍扫描：列出所有要复制的文件 + 累计字节
    targets: list = []  # [(src_file, dst_file, size), ...]
    skipped = 0
    if src_root.is_file():
        if file_filter is None or file_filter(src_root):
            targets.append((src_root, dst_root, src_root.stat().st_size))
        else:
            skipped += 1
    else:
        for entry in src_root.rglob('*'):
            if not entry.is_file():
                continue
            if file_filter is not None and not file_filter(entry):
                skipped += 1
                continue
            rel = entry.relative_to(src_root)
            dst = dst_root / rel
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            targets.append((entry, dst, size))

    total_bytes = sum(t[2] for t in targets)
    bytes_done = 0
    src_files = []
    dst_files = []

    for src, dst, size in targets:
        if progress_cb:
            try:
                progress_cb(bytes_done, total_bytes, str(src))
            except Exception:
                pass
        copied = copy_file_with_progress(
            src, dst,
            progress_cb=lambda d, _t, base=bytes_done: (
                progress_cb(base + d, total_bytes, str(src)) if progress_cb else None
            ),
            buffer_size=buffer_size,
        )
        bytes_done += copied
        src_files.append(src)
        dst_files.append(dst)

    if progress_cb:
        try:
            progress_cb(bytes_done, total_bytes, '')
        except Exception:
            pass

    return {
        'files_copied': len(src_files),
        'files_skipped': skipped,
        'bytes_copied': bytes_done,
        'src_files': src_files,
        'dst_files': dst_files,
    }
