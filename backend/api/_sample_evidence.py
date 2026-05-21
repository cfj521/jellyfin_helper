"""
Sample 证据收集器
给一个 Jellyfin 条目（含 Path / RunTimeTicks），返回是否是 sample 的证据 + 判定。

判定优先级：
  - high_confidence 信号任一命中 → verdict = 'sample'      （强证据，可放心删）
  - 至少 2 条 medium 信号        → verdict = 'sample-likely' （建议删，但要复核）
  - 1 条 medium                  → verdict = 'unclear'     （让用户自己判断）
  - 都不命中                     → verdict = 'main-content' （不像 sample）

不删除文件本身——只产出证据。删除动作在调用方控制。
"""
from pathlib import Path
from typing import Dict, List, Optional

from backend.api._media_exts import (
    VIDEO_EXTS as _VIDEO_EXTS,
    IMAGE_EXTS as _IMAGE_EXTS,
)
# 元数据/字幕等小文件（本地特化集合：sample 识别专用，含 .nfo/.txt）
_META_EXTS = {'.nfo', '.txt', '.srt', '.ass', '.ssa', '.sub', '.vtt', '.idx'}

# 路径关键字 —— 含这些子串视为 sample 强信号
# 注意：extras / trailer / featurettes 等是 Jellyfin 官方约定的合法附加内容子目录，
# 不算 sample，所以路径关键字只保留 sample
_PATH_KEYWORDS = ('/sample', '\\sample')

# Sample 子目录 —— 真正应清理的 sample 内容
_SAMPLE_DIR_NAMES = {'sample', 'samples'}

# Extras 子目录 —— Jellyfin 官方约定的合法附加内容（trailers / behind the scenes / 花絮等），
# 绝不应被 sample 清理任务误删。参考：
# https://jellyfin.org/docs/general/server/media/movies/#movie-extras
_EXTRAS_DIR_NAMES = {
    'trailer', 'trailers',
    'extras', 'extra',
    'featurette', 'featurettes',
    'behind the scenes', 'behindthescenes',
    'deleted scenes', 'deletedscenes',
    'interview', 'interviews',
    'bonus', 'bonusdisc', 'bonus disc', 'bonus features',
    'scene', 'scenes',
    'short', 'shorts',
    'other', 'others',
}

# 总集合：用于 _find_release_dir / _scan_release_videos 穿越"非主目录的子目录"
_SUB_RELEASE_DIR_NAMES = _SAMPLE_DIR_NAMES | _EXTRAS_DIR_NAMES


def _find_release_dir(file_path: Path) -> Path:
    """
    找"release 目录"—— release 内容真正的根目录。

    策略：从文件 parent 向上走，**优先穿过 sub-release 子目录**（sample/extras/featurettes 等），
    直到遇到一个目录名不在 _SUB_RELEASE_DIR_NAMES 里。最多走 3 级，避免穿到媒体库根。

    历史 bug：之前先看"有无其他视频"再判 sub-release dir name，导致 Featurettes/ 里
    有多个花絮 mkv 互为兄弟时，被误判为 release dir，无法穿过去。
    """
    current = file_path.parent
    for _ in range(3):  # 最多 3 跳
        # 优先：当前目录名是 sub-release 子目录 → 直接穿过去
        # （即使目录里有其他视频，多个 featurettes 互为"其他视频"也不能阻止穿越）
        if current.name.lower() in _SUB_RELEASE_DIR_NAMES:
            parent = current.parent
            if parent == current:
                return current
            current = parent
            continue
        # 不是 sub-release dir：看当前目录是否有"别的"视频文件
        try:
            other_videos = [
                f for f in current.iterdir()
                if f.is_file() and f.suffix.lower() in _VIDEO_EXTS and f != file_path
            ]
        except (PermissionError, OSError):
            return current
        if other_videos:
            return current
        # 没有别的视频，又不是 sub-release dir → 当前就是 release dir
        return current
    return current


def _scan_release_videos(release_dir: Path) -> List[Path]:
    """
    扫 release 目录下的所有视频文件：
      - 直接子文件
      - sub-release 子目录里的视频（sample/extras/...）
    不递归普通子目录，避免把媒体库其它电影一起扫进来。
    """
    videos: List[Path] = []
    try:
        for f in release_dir.iterdir():
            if f.is_file() and f.suffix.lower() in _VIDEO_EXTS:
                videos.append(f)
            elif f.is_dir() and f.name.lower() in _SUB_RELEASE_DIR_NAMES:
                try:
                    for sub_f in f.iterdir():
                        if sub_f.is_file() and sub_f.suffix.lower() in _VIDEO_EXTS:
                            videos.append(sub_f)
                except (PermissionError, OSError):
                    pass
    except (PermissionError, OSError):
        pass
    return videos


def _format_size_mb(bytes_: int) -> float:
    """旧助手：保留兼容，仅内部信号判定还在用（>100MB / >300MB 阈值都用 MB 比较方便）。"""
    return round(bytes_ / (1024 * 1024), 1)


def _format_size(bytes_: int) -> str:
    """
    自适应单位的字节格式化：B / KB / MB / GB / TB / PB。
    < 1KB 取整；其它保留 1 位小数。
    """
    if bytes_ is None or bytes_ <= 0:
        return '0 B'
    units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
    size = float(bytes_)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    if idx == 0:
        return f'{int(size)} {units[idx]}'
    return f'{size:.1f} {units[idx]}'


def gather_sample_evidence(item: Dict) -> Dict:
    """
    输入 jellyfin item dict（至少有 Path、RunTimeTicks 字段）。
    返回结构：
      {
        'verdict': 'sample' | 'sample-likely' | 'unclear' | 'main-content' | 'unknown',
        'reason_codes': [...],       # 触发的信号代码
        'high_confidence': [str],    # 强证据描述
        'medium_confidence': [str],  # 弱证据描述
        'path': str,
        'path_accessible': bool,     # 后端是否能 stat 到文件
        'file_size_bytes': int,
        'file_size_mb': float,
        'runtime_min': float | None,
        'siblings': [{'name', 'path', 'size_bytes', 'size_mb', 'is_larger'}, ...],
        'largest_sibling_ratio': float | None,  # 兄弟最大文件 / 本文件
      }
    """
    path_str = item.get('Path') or ''
    runtime_ticks = item.get('RunTimeTicks') or 0
    runtime_min = round(runtime_ticks / 600_000_000.0, 1) if runtime_ticks else None

    result: Dict = {
        'verdict': 'unknown',
        'reason_codes': [],
        'high_confidence': [],
        'medium_confidence': [],
        'path': path_str,
        'path_accessible': False,
        'file_size_bytes': 0,
        'file_size_mb': 0.0,
        'file_size_display': '0 B',
        'runtime_min': runtime_min,
        'siblings': [],
        'largest_sibling_ratio': None,
        # 解析得到的 release 目录（而不是文件的 immediate parent）
        'release_dir': None,
        # 在 release 目录里 scan 到的视频总数（含本文件）
        'release_video_count': 0,
        # 本文件是否是 release 目录里最大的视频（判定"是否正片"的核心信号）
        'is_largest_in_dir': True,
    }

    if not path_str:
        result['verdict'] = 'unknown'
        result['high_confidence'].append('条目没有 Path 字段')
        return result

    # 路径转换：把 Jellyfin 给的路径（可能是 Linux/Docker 风格）映射为本后端可访问的路径
    # 配置项 path_mappings 控制映射规则；没配置就原样使用
    try:
        from backend.path_translator import translate_path_with_settings
        local_path_str = translate_path_with_settings(path_str)
    except Exception:
        local_path_str = path_str

    file_path = Path(local_path_str)
    # path_lower 仍用原始 Jellyfin 路径做关键字匹配（cover/poster 子目录名靠它）
    path_lower = path_str.lower()
    # 转换后的本地路径也存一下，前端调试方便
    result['local_path'] = local_path_str if local_path_str != path_str else None

    # === 路径/文件名关键字（不依赖文件系统） ===
    if any(k in path_lower for k in _PATH_KEYWORDS):
        result['reason_codes'].append('path_keyword')
        result['high_confidence'].append('路径包含 sample/trailer/extras 关键字')

    name_lower = file_path.stem.lower() if path_str else ''
    if any(k in name_lower for k in ('sample', 'trailer')):
        if 'name_keyword' not in result['reason_codes']:
            result['reason_codes'].append('name_keyword')
            result['high_confidence'].append('文件名含 sample/trailer 关键字')

    # === 时长（不依赖文件系统） ===
    if runtime_min is not None and runtime_min > 0:
        if runtime_min < 5:
            result['reason_codes'].append('very_short_runtime')
            result['high_confidence'].append(
                f'时长仅 {runtime_min:.1f} 分钟，正片几乎不可能这么短'
            )
        elif runtime_min < 15:
            result['reason_codes'].append('short_runtime')
            result['medium_confidence'].append(f'时长仅 {runtime_min:.1f} 分钟，疑似 sample/trailer')

    # === 文件系统信号（需要后端能访问该路径） ===
    try:
        if file_path.exists() and file_path.is_dir():
            # === 目录路径：Jellyfin 把整个目录当成一个 Folder/BoxSet 等条目了 ===
            # 文件大小汇总=目录里所有视频大小之和；时长 Jellyfin 不给目录提供，留空
            result['path_accessible'] = True
            video_files: List[Path] = []
            image_count = 0
            meta_count = 0
            other_count = 0
            total_video_bytes = 0
            try:
                for inner in file_path.rglob('*'):
                    if not inner.is_file():
                        continue
                    ext = inner.suffix.lower()
                    if ext in _VIDEO_EXTS:
                        video_files.append(inner)
                        try:
                            total_video_bytes += inner.stat().st_size
                        except OSError:
                            pass
                    elif ext in _IMAGE_EXTS:
                        image_count += 1
                    elif ext in _META_EXTS:
                        meta_count += 1
                    else:
                        other_count += 1
            except (PermissionError, OSError):
                pass

            total_files = len(video_files) + image_count + meta_count + other_count
            result['release_video_count'] = len(video_files)
            # 目录的"文件大小"= 内部所有视频大小之和，方便用户判断
            result['file_size_bytes'] = total_video_bytes
            result['file_size_mb'] = _format_size_mb(total_video_bytes)
            result['file_size_display'] = _format_size(total_video_bytes)

            # 把目录里的视频列出来，前端 siblings 表格能直接展示用户要删什么
            for v in video_files:
                try:
                    sz = v.stat().st_size
                except OSError:
                    continue
                result['siblings'].append({
                    'name': v.name,
                    'path': str(v),
                    'size_bytes': sz,
                    'size_mb': _format_size_mb(sz),
                    'size_display': _format_size(sz),
                    'is_larger': False,
                })
            result['siblings'].sort(key=lambda s: -s['size_bytes'])

            if total_files == 0:
                result['reason_codes'].append('empty_folder')
                result['high_confidence'].append('目录是空的，没有任何文件')
            elif not video_files:
                result['reason_codes'].append('non_media_folder')
                # 描述区分：纯图片 vs 杂项
                if image_count > 0 and meta_count == 0 and other_count == 0:
                    result['high_confidence'].append(
                        f'目录里没有视频文件，只有 {image_count} 张图片 —— '
                        f'这是 cover/poster/screenshots 之类的辅助目录，不是媒体内容'
                    )
                else:
                    parts = []
                    if image_count: parts.append(f'{image_count} 张图片')
                    if meta_count:  parts.append(f'{meta_count} 个元数据/字幕')
                    if other_count: parts.append(f'{other_count} 个其它文件')
                    result['high_confidence'].append(
                        f'目录里没有视频文件（共 {total_files} 个文件：'
                        f"{'、'.join(parts)}）—— 不是媒体内容"
                    )

            # 目录不参与 file_size / siblings / is_largest 比较
            # 直接走 verdict（综合判定段会处理）

        elif file_path.exists() and file_path.is_file():
            result['path_accessible'] = True
            result['file_size_bytes'] = file_path.stat().st_size
            result['file_size_mb'] = _format_size_mb(result['file_size_bytes'])
            result['file_size_display'] = _format_size(result['file_size_bytes'])

            # 找 release 目录（穿过 sample/extras 等 sub-release 子目录）
            release_dir = _find_release_dir(file_path)
            result['release_dir'] = str(release_dir)

            # 在 release 目录里扫所有视频（包括 sub-release 子目录里的）
            all_videos = _scan_release_videos(release_dir)
            result['release_video_count'] = len(all_videos)
            for v in all_videos:
                if v == file_path:
                    continue
                try:
                    sib_size = v.stat().st_size
                except OSError:
                    continue
                result['siblings'].append({
                    'name': v.name,
                    'path': str(v),
                    'size_bytes': sib_size,
                    'size_mb': _format_size_mb(sib_size),
                    'size_display': _format_size(sib_size),
                    'is_larger': sib_size > result['file_size_bytes'],
                })

            # 大小信号（显示用自适应单位，阈值仍按 MB 比较）
            size_mb = result['file_size_mb']
            size_disp = result['file_size_display']
            if 0 < size_mb < 100:
                result['reason_codes'].append('tiny_file')
                result['high_confidence'].append(
                    f'文件大小仅 {size_disp}，远小于正常 1080p+ 视频'
                )
            elif 100 <= size_mb < 300:
                result['reason_codes'].append('small_file')
                result['medium_confidence'].append(
                    f'文件大小仅 {size_disp}，偏小'
                )

            # 兄弟比对：目录里有更大的视频 → 本文件不可能是正片
            if result['siblings']:
                largest_sib = max(result['siblings'], key=lambda s: s['size_bytes'])
                # is_largest：本文件 ≥ 最大兄弟才算最大
                result['is_largest_in_dir'] = (
                    result['file_size_bytes'] >= largest_sib['size_bytes']
                )
                if result['file_size_bytes'] > 0:
                    ratio = largest_sib['size_bytes'] / result['file_size_bytes']
                    result['largest_sibling_ratio'] = round(ratio, 1)
                    if ratio >= 5.0:
                        result['reason_codes'].append('dwarfed_by_sibling')
                        result['high_confidence'].append(
                            f'同目录里有 "{largest_sib["name"]}"（{largest_sib["size_display"]}），'
                            f'是本文件的 {ratio:.1f} 倍 —— 那个才是正片，本文件极可能是 sample'
                        )
                    elif ratio >= 2.0:
                        result['reason_codes'].append('smaller_than_sibling')
                        result['medium_confidence'].append(
                            f'同目录有更大的视频 "{largest_sib["name"]}"（{ratio:.1f} 倍）'
                        )
                    elif ratio > 1.0:
                        # 比兄弟略小（1x–2x）：弱证据
                        result['medium_confidence'].append(
                            f'本文件不是目录最大（最大是 "{largest_sib["name"]}"，{ratio:.2f} 倍）'
                        )

            # 排序兄弟，按大小降序
            result['siblings'].sort(key=lambda s: -s['size_bytes'])
        else:
            # 文件不存在或后端无访问权限 —— 仍然有路径/时长信号可用
            result['path_accessible'] = False
    except (PermissionError, OSError) as e:
        result['path_accessible'] = False
        result['medium_confidence'].append(f'文件系统访问失败: {e}')

    # === 综合判定 ===
    high_n = len(result['high_confidence'])
    med_n = len(result['medium_confidence'])

    # 优先判定"非媒体"——目录里没有视频，不应当存在于媒体库里
    if 'non_media_folder' in result['reason_codes'] or 'empty_folder' in result['reason_codes']:
        result['verdict'] = 'non-media'
        return result

    # ===== Extras 子目录优先判定（短路所有 sample 信号）=====
    # 文件位于 Jellyfin 官方约定的 extras 子目录（Featurettes / Trailers /
    # Behind The Scenes / Deleted Scenes 等）→ 视为合法附加内容，绝不应清理
    direct_parent_name = ''
    try:
        if path_str:
            direct_parent_name = Path(path_str).parent.name
    except Exception:
        pass
    if direct_parent_name and direct_parent_name.lower() in _EXTRAS_DIR_NAMES:
        result['verdict'] = 'extras'
        # 把"这是合法附加内容"作为最高优先级的说明放在前面
        result['high_confidence'] = [
            f'文件位于 "{direct_parent_name}/" 子目录 —— '
            f'Jellyfin 官方约定的附加内容（extras）目录，不是 sample，绝不应清理'
        ] + result['high_confidence']
        return result

    # 信号判定 —— 强信号 / 弱信号优先
    if high_n >= 1:
        result['verdict'] = 'sample'
    elif med_n >= 2:
        result['verdict'] = 'sample-likely'
    elif med_n == 1:
        result['verdict'] = 'unclear'
    else:
        # 没有任何 sample 信号：默认偏向 main-content，但要看是否真"正片"
        # —— 如果 release 目录里有更大的视频（is_largest_in_dir=False），仍不算正片
        if not result['is_largest_in_dir']:
            result['verdict'] = 'unclear'
        elif result['release_video_count'] == 0 and not result['path_accessible']:
            # 后端无法访问文件、又没拿到信号 → 信息不足
            result['verdict'] = 'unknown'
        else:
            result['verdict'] = 'main-content'

    # 防误删兜底：必须**同时**满足下列条件才锁定为 main-content（覆盖前面的判定）
    #   1. 是 release 目录里最大的视频
    #   2. 文件 ≥ 500MB
    #   3. 时长 ≥ 30 分钟
    # 缺一不可。这样 600MB / 35min 的 behindthescenes 旁边有 4GB 主片时不会被误锁
    safe_size = result['file_size_bytes'] >= 500 * 1024 * 1024  # 500MB
    safe_runtime = (runtime_min or 0) >= 30
    if result['is_largest_in_dir'] and safe_size and safe_runtime:
        result['verdict'] = 'main-content'

    return result
