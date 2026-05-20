"""
修复 adult_items 表里残留的 Windows 视角 path 字段。

历史背景：之前曾在 Windows 后端实例（Z:/X:/... 盘符）跑过刮削，
reverse_translate_path_with_settings 没命中规则，poster_path / nfo_path
原样写成了 `Z:\\videos\\adult\\...` 这种 Windows 路径串。
当前 Linux 部署看不到这些"路径"，但 DB 字段还在，影响：
  - UI 显示路径错乱
  - repair_metadata stage 3 物理 stat 始终 False（误判"丢了"）

本脚本逻辑：
  1. 扫 poster_path / nfo_path 含 Windows 风格盘符的条目
  2. 从 file_path（已经是 Linux 视角）推算"正确"的 Linux 路径：
       poster:  <video_dir>/<video_stem>-poster.jpg
       nfo:     <video_dir>/<video_stem>.nfo
  3. stat 物理文件：
       存在 → 写回正确路径（修正）
       不存在 → 清空字段 + 把 source 降级为 partial（让"修复识别错误"补救）
  4. dry-run（默认开）：只打印不写库。--apply 才落库。

用法（在 jellyfin-helper 部署机器执行）：
  python -m tools.adult_manager.fix_paths              # dry-run
  python -m tools.adult_manager.fix_paths --apply      # 实际写库
  python -m tools.adult_manager.fix_paths --apply --limit 50  # 限量
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# Windows 路径的特征：盘符冒号开头（Z:\... / Z:/... / C:\... 等）
_WINDOWS_PATH_RE = re.compile(r'^[A-Za-z]:[\\/]')


def _is_windows_path(s: Optional[str]) -> bool:
    return bool(s) and bool(_WINDOWS_PATH_RE.match(s))


def _derive_linux_paths(file_path: str) -> Tuple[Optional[Path], Optional[Path]]:
    """从 file_path（Linux 视角）推 poster / nfo 应该所在的位置。
    file_path 自身要是 Linux 风格才有效。"""
    if not file_path or _is_windows_path(file_path):
        return None, None
    p = Path(file_path)
    return (
        p.with_name(p.stem + '-poster.jpg'),
        p.with_name(p.stem + '.nfo'),
    )


def _classify_source_to_partial(source: Optional[str]) -> str:
    """把 'merged:javbus,javdb' 降级成 'partial:javbus,javdb'。其它情况保持或返回 partial。"""
    s = source or ''
    if s.startswith('partial'):
        return s  # 已经是 partial
    if s.startswith('merged:'):
        return s.replace('merged:', 'partial:', 1)
    return 'partial'


def fix_paths(apply: bool = False, limit: Optional[int] = None) -> dict:
    """返回 stats 字典。"""
    # 延迟 import 让 --help 不依赖 DB
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from web.backend.database import SessionLocal, AdultItem

    stats = {
        'scanned': 0,
        'poster_fixed': 0, 'poster_cleared': 0,
        'nfo_fixed': 0, 'nfo_cleared': 0,
        'source_demoted': 0,
        'skipped_file_path_windows': 0,
        'unchanged': 0,
    }

    with SessionLocal() as db:
        # 任一字段是 Windows 风格 → 候选（再单字段细判）
        items = db.query(AdultItem).all()
        candidates = [
            it for it in items
            if _is_windows_path(it.poster_path) or _is_windows_path(it.nfo_path)
        ]
        if limit:
            candidates = candidates[:limit]
        logger.info(f"扫到 {len(candidates)} 条疑似含 Windows 残留 path 的条目（共 {len(items)} 总）")

        for it in candidates:
            stats['scanned'] += 1

            # file_path 自身就是 Windows 风格 → 没有可靠的 Linux 路径参考，跳过
            if _is_windows_path(it.file_path):
                stats['skipped_file_path_windows'] += 1
                logger.warning(
                    f"[{it.code}] file_path 也是 Windows 风格，跳过整条修复"
                    f"\n  file_path = {it.file_path!r}"
                )
                continue

            poster_target, nfo_target = _derive_linux_paths(it.file_path or '')
            if poster_target is None:
                logger.warning(f"[{it.code}] file_path 为空，跳过 (id={it.id})")
                continue

            mutations = []
            demoted_any = False

            # ---- poster ----
            if _is_windows_path(it.poster_path):
                if poster_target.exists():
                    new_value = str(poster_target)
                    mutations.append(('poster_path', it.poster_path, new_value))
                    stats['poster_fixed'] += 1
                else:
                    mutations.append(('poster_path', it.poster_path, None))
                    stats['poster_cleared'] += 1
                    demoted_any = True

            # ---- nfo ----
            if _is_windows_path(it.nfo_path):
                if nfo_target.exists():
                    new_value = str(nfo_target)
                    mutations.append(('nfo_path', it.nfo_path, new_value))
                    stats['nfo_fixed'] += 1
                else:
                    mutations.append(('nfo_path', it.nfo_path, None))
                    stats['nfo_cleared'] += 1
                    demoted_any = True

            # ---- source 降级（有清空动作时）----
            new_source = None
            if demoted_any and (it.source or '').startswith('merged'):
                new_source = _classify_source_to_partial(it.source)
                if new_source != it.source:
                    mutations.append(('source', it.source, new_source))
                    stats['source_demoted'] += 1

            if not mutations:
                stats['unchanged'] += 1
                continue

            # 打印
            tag = '[APPLY]' if apply else '[DRY  ]'
            logger.info(f"{tag} {it.code} (id={it.id}):")
            for field, old, new in mutations:
                old_short = (old or '')[-60:] if old else 'NULL'
                new_short = (new or 'NULL')[-60:] if new else 'NULL'
                logger.info(f"  {field}: ...{old_short}  →  ...{new_short}")

            if apply:
                for field, _old, new in mutations:
                    setattr(it, field, new)

        if apply:
            db.commit()
            logger.info("✓ 已 commit 到 DB")
        else:
            logger.info("(dry-run 未写库；要落库加 --apply)")

    return stats


def main():
    parser = argparse.ArgumentParser(
        description='修复 adult_items.poster_path / nfo_path 里残留的 Windows 风格路径',
    )
    parser.add_argument('--apply', action='store_true', help='实际写库（默认 dry-run）')
    parser.add_argument('--limit', type=int, default=None, help='只处理前 N 条（dry-run 测试用）')
    parser.add_argument('--verbose', '-v', action='store_true', help='打印更多日志')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S',
    )

    stats = fix_paths(apply=args.apply, limit=args.limit)

    print()
    print('=== 统计 ===')
    for k, v in stats.items():
        print(f'  {k:30s}: {v}')


if __name__ == '__main__':
    main()
