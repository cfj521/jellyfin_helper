"""
整理已识别的成人内容文件。

逻辑：
  对 DB 中所有 code IS NOT NULL（已识别）的 AdultItem，把它们移到 --target 目录。
  移动单元 = file_path 在 --source 下的"顶层祖先"：
    - 文件直接在 source 根下 → 只移这个文件 + 同名附件（poster/nfo/字幕等）
    - 文件在 source 的某个子目录里 → 移整个顶层子目录（连同其中的所有内容）

  移动后会同步更新 DB 里的 file_path / nfo_path / poster_path 前缀。

默认 --dry-run 只列计划；要实际执行须显式加 --apply。

用法：
  python -m tools.adult_manager.organize --source "Z:\\videos\\adult" --target "Z:\\videos\\adult\\_organized"
  python -m tools.adult_manager.organize --source "Z:\\videos\\adult" --target "Z:\\videos\\adult\\_organized" --apply
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Tuple

# Windows 默认 GBK 终端遇到 UTF-8 中文 / emoji 会崩，强制 stdout/stderr UTF-8
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# 把项目根加入 sys.path，使 backend.* 可导入
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _path_in(child: Path, parent: Path) -> bool:
    """child 是否在 parent 下（不调 resolve，避免盘符 ↔ UNC 转换破坏前缀匹配）"""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _to_local(jf_str) -> str:
    """DB 里 file_path 是 Jellyfin view → 本机视角（做磁盘 op / 比对 source/target）"""
    if not jf_str:
        return ''
    from backend.path_translator import translate_path_with_settings
    return translate_path_with_settings(str(jf_str)) or str(jf_str)


def _to_jf(local_str) -> str:
    """本机视角字符串 → Jellyfin view（写 DB / 按 DB.file_path 查）"""
    if not local_str:
        return ''
    from backend.path_translator import reverse_translate_path_with_settings
    return reverse_translate_path_with_settings(str(local_str)) or str(local_str)


def _top_unit(file_path: Path, source: Path) -> Path:
    """
    返回 file_path 在 source 下的"移动单元"：
      - 直接在 source 根下：返回文件本身
      - 在 source 的子目录里：返回 source 下面那一层目录
    注意：所有 Path 都不 resolve()，保持与 DB 中 file_path 字符串完全一致
    """
    rel = file_path.relative_to(source)
    parts = rel.parts
    if len(parts) == 1:
        return file_path
    return source / parts[0]


def _sibling_attachments(video_file: Path) -> List[Path]:
    """
    散文件场景：找视频同名的附件（NFO / 海报 / 字幕等）。
    返回去重后的存在文件列表。
    """
    if not video_file.is_file():
        return []
    parent = video_file.parent
    stem = video_file.stem
    out: List[Path] = []
    seen = set()
    # 同 stem 的所有变体
    patterns = [
        f"{stem}.nfo",
        f"{stem}-poster.jpg", f"{stem}-poster.png",
        f"{stem}-fanart.jpg", f"{stem}-fanart.png",
        f"{stem}-thumb.jpg",
        f"{stem}.jpg", f"{stem}.png",
    ]
    # 通配字幕：stem.* 后缀在 SUB_EXTS 中
    sub_exts = {'.srt', '.ass', '.ssa', '.vtt', '.sub', '.idx'}
    for p in patterns:
        f = parent / p
        if f.is_file() and f != video_file and str(f) not in seen:
            out.append(f)
            seen.add(str(f))
    for f in parent.glob(f"{stem}*"):
        if f.suffix.lower() in sub_exts and f.is_file() and str(f) not in seen:
            out.append(f)
            seen.add(str(f))
    return out


def _safe_target_path(target_root: Path, name: str) -> Path:
    """
    目标已有同名 → 加 (1)(2) 后缀。返回最终目标路径（未实际创建）。
    """
    candidate = target_root / name
    if not candidate.exists():
        return candidate
    stem, suffix = (name.rsplit('.', 1) + [''])[:2]
    if suffix:
        base, ext = stem, '.' + suffix
    else:
        # 目录或无后缀文件
        base, ext = name, ''
    i = 1
    while True:
        candidate = target_root / f"{base} ({i}){ext}"
        if not candidate.exists():
            return candidate
        i += 1


def build_plan(source: Path, target: Path) -> Tuple[List[dict], List[str]]:
    """
    返回 (plans, warnings)
    plans: [{
      'unit': Path,                      # 移动单元（source 下的散文件 / 顶层子目录）
      'attachments': List[Path],         # 散文件场景的附属文件
      'target': Path,                    # 目标路径（unit 移动后的新位置）
      'items': [{id, code, file_path, nfo_path, poster_path}],
    }]
    """
    from backend.database import SessionLocal, AdultItem

    db = SessionLocal()
    try:
        recognized = db.query(AdultItem).filter(AdultItem.code != None).all()  # noqa: E711
    finally:
        db.close()

    warnings: List[str] = []
    # unit_str → {'unit': Path, 'items': [...]}
    grouped: Dict[str, dict] = {}

    for it in recognized:
        if not it.file_path:
            warnings.append(f"#{it.id} {it.code}: file_path 为空，跳过")
            continue
        # DB 是 Jellyfin view，磁盘比对要本机视角
        fp = Path(_to_local(it.file_path))
        if not fp.exists():
            warnings.append(f"#{it.id} {it.code}: 源文件不存在 {fp}，跳过")
            continue
        if not _path_in(fp, source):
            warnings.append(f"#{it.id} {it.code}: 不在 source 内（{fp}），跳过")
            continue

        unit = _top_unit(fp, source)
        if _path_in(unit, target) or unit == target:
            # unit 已经在 target 内，跳过（目标在 source 内时常见）
            continue

        key = str(unit)
        if key not in grouped:
            grouped[key] = {'unit': unit, 'items': [], 'is_file': unit.is_file()}
        grouped[key]['items'].append({
            'id': it.id,
            'code': it.code,
            'file_path': it.file_path,    # 保留 DB 原值（Jellyfin view），后续更新 DB 时配合 _to_jf 比对
            'nfo_path': it.nfo_path,
            'poster_path': it.poster_path,
        })

    # 第二遍：把同一 unit 下所有 AdultItem（含 unrecognized）也加进来
    # 否则一个目录被整体搬走时，目录里的未识别项 DB 路径不会同步
    db = SessionLocal()
    try:
        import sqlalchemy as _sa
        for key, g in grouped.items():
            # unit 是本机 Path；DB 里 file_path 是 Jellyfin view，前缀比对前转一次
            unit_str = str(g['unit'])
            unit_jf = _to_jf(unit_str)
            if g['is_file']:
                rows = db.query(AdultItem).filter(AdultItem.file_path == unit_jf).all()
            else:
                rows = db.query(AdultItem).filter(
                    _sa.or_(
                        AdultItem.file_path == unit_jf,
                        AdultItem.file_path.like(unit_jf + '\\%'),
                        AdultItem.file_path.like(unit_jf + '/%'),
                    )
                ).all()
            g['items'] = [{
                'id': it.id,
                'code': it.code,
                'file_path': it.file_path,
                'nfo_path': it.nfo_path,
                'poster_path': it.poster_path,
            } for it in rows]
    finally:
        db.close()

    # 决定每个 unit 的最终目标路径
    plans: List[dict] = []
    used_names = set()
    for key, g in grouped.items():
        unit: Path = g['unit']
        # 名字基础：散文件用文件名，目录用目录名
        base_name = unit.name
        # 目标路径（避免冲突）
        candidate = target / base_name
        if candidate.exists() or str(candidate) in used_names:
            candidate = _safe_target_path(target, base_name)
            # _safe_target_path 已检查 exists，但防 used_names 也撞
            i = 1
            while str(candidate) in used_names:
                stem, ext = (base_name.rsplit('.', 1) + [''])[:2]
                if ext:
                    candidate = target / f"{stem} ({i}).{ext}"
                else:
                    candidate = target / f"{base_name} ({i})"
                i += 1
        used_names.add(str(candidate))

        attachments: List[Path] = []
        if g['is_file']:
            attachments = _sibling_attachments(unit)

        plans.append({
            'unit': unit,
            'is_file': g['is_file'],
            'attachments': attachments,
            'target': candidate,
            'items': g['items'],
        })

    return plans, warnings


def print_plan(plans: List[dict], warnings: List[str], source: Path, target: Path) -> None:
    print(f"source: {source}")
    print(f"target: {target}")
    print(f"移动单元: {len(plans)}")
    print(f"涉及条目: {sum(len(p['items']) for p in plans)}")
    print()
    for i, p in enumerate(plans, 1):
        kind = '[F]' if p['is_file'] else '[D]'
        codes = ', '.join(it['code'] for it in p['items'][:3])
        if len(p['items']) > 3:
            codes += f', ... +{len(p["items"]) - 3}'
        print(f"  {kind} [{i}] {p['unit'].name}")
        print(f"      -> {p['target']}")
        print(f"      含番号: {codes}")
        if p['attachments']:
            print(f"      附件: {len(p['attachments'])} 个 ({', '.join(a.name for a in p['attachments'][:3])}{'...' if len(p['attachments']) > 3 else ''})")
    if warnings:
        print()
        print(f"[!] 警告 {len(warnings)} 条:")
        for w in warnings[:20]:
            print(f"  - {w}")
        if len(warnings) > 20:
            print(f"  ... 共 {len(warnings)} 条，仅显示前 20")


def apply_plan(plans: List[dict], dry_run: bool = False) -> Tuple[int, int]:
    """
    返回 (moved_units, db_updated_items)
    """
    from backend.database import SessionLocal, AdultItem

    if dry_run:
        return 0, 0

    moved = 0
    db_updated = 0

    db = SessionLocal()
    try:
        for p in plans:
            unit: Path = p['unit']
            tgt: Path = p['target']
            tgt.parent.mkdir(parents=True, exist_ok=True)

            # 1. 移动单元
            try:
                shutil.move(str(unit), str(tgt))
            except Exception as e:
                print(f"  [ERR] 移动失败 {unit} -> {tgt}: {e}")
                continue
            moved += 1

            # 2. 散文件场景：移附件到同目录（与 tgt 平级）
            if p['is_file']:
                tgt_dir = tgt.parent
                for att in p['attachments']:
                    if not att.exists():
                        continue
                    att_target = _safe_target_path(tgt_dir, att.name)
                    try:
                        shutil.move(str(att), str(att_target))
                    except Exception as e:
                        print(f"  [ERR] 附件移动失败 {att}: {e}")

            # 3. 更新 DB 中所有受影响 item 的路径前缀
            # 路径在 DB 里存 Jellyfin view，前缀比对/替换都得在 Jellyfin view 下做
            old_prefix = _to_jf(str(unit))
            new_prefix = _to_jf(str(tgt))
            for item_brief in p['items']:
                item = db.query(AdultItem).filter(AdultItem.id == item_brief['id']).first()
                if not item:
                    continue
                changed = False
                for attr in ('file_path', 'nfo_path', 'poster_path'):
                    val = getattr(item, attr)
                    if not val:
                        continue
                    if val == old_prefix or val.startswith(old_prefix + '\\') or val.startswith(old_prefix + '/'):
                        new_val = new_prefix + val[len(old_prefix):]
                        setattr(item, attr, new_val)
                        changed = True
                if changed:
                    db_updated += 1
            db.commit()

            print(f"  [OK] {unit.name} -> {tgt}")
    finally:
        db.close()

    return moved, db_updated


def main():
    ap = argparse.ArgumentParser(description='整理已识别番号的视频到一个目录')
    ap.add_argument('--source', required=True, help='源根目录，例如 Z:\\videos\\adult')
    ap.add_argument('--target', required=True, help='目标目录，会自动创建')
    ap.add_argument('--apply', action='store_true', help='默认只做 dry-run；加 --apply 才会动文件')
    args = ap.parse_args()

    # 不调 .resolve()：DB 里 file_path 是用户配的形式（如 'Z:\\videos\\adult\\xxx'），
    # resolve() 会把盘符展开成 UNC（'\\\\server\\share\\...'），破坏前缀比对。
    source = Path(args.source)
    target = Path(args.target)

    if not source.exists() or not source.is_dir():
        print(f"[ERR] source 不存在或不是目录: {source}")
        sys.exit(1)

    # 如果 target 在 source 内，明确告知；如果 target 等于 source，拒绝
    if target == source:
        print("[ERR] target 不能等于 source")
        sys.exit(1)

    plans, warnings = build_plan(source, target)
    print_plan(plans, warnings, source, target)
    print()

    if not plans:
        print("没有可整理的条目。")
        return

    if not args.apply:
        print("[dry-run] 未实际移动；要执行请加 --apply")
        return

    print("=" * 60)
    print("开始执行...")
    target.mkdir(parents=True, exist_ok=True)
    moved, db_updated = apply_plan(plans, dry_run=False)
    print(f"\n完成：移动 {moved} 个单元，更新 DB {db_updated} 条记录")


if __name__ == '__main__':
    main()
