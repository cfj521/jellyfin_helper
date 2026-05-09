"""
跨盘复制：/downloads(NVMe) → /library(HDD)。

简单、稳、有进度回调。用 shutil.copyfileobj 而不是 copy2/copy 是为了能流式
回调字节进度（大文件可能跑几十分钟，UI 实时更新很重要）。
完成后用 shutil.copystat 保留 mtime/permissions（jellyfin 元数据时间敏感）。
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# 复制缓冲：8MB 在 NVMe→HDD 上速率充足；大缓冲也无明显增益但占内存
DEFAULT_BUFFER = 8 * 1024 * 1024


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
      - 目标已存在但 size < 源 size → 从已写位置续传（append + 余下字节）
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
            dst_size = dst.stat().st_size
        except OSError:
            dst_size = 0
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
            # partial → 续传
            logger.info(f"resume: {dst.name} 续传，已有 {dst_size}/{total} bytes")
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
