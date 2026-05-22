"""
集中定义媒体文件扩展名 + Jellyfin 库类型到"主体文件"扩展名集合的映射。

历史背景：项目里 VIDEO_EXTS / AUDIO_EXTS / IMAGE_EXTS 在多处重复定义且飘移
（maintenance.py 缺 .webm、adult_scanner.py 缺 .rmvb 等），EBOOK_EXTS 完全没有。
本模块作为单一来源，其它模块统一 import 这里的常量。

库类型摸排（对应 Jellyfin CollectionType）：
  movies / tvshows / musicvideos / homevideos      → 视频为主
  music                                            → 音频为主
  photos                                           → 图片为主
  books                                            → 电子书为主
  mixed                                            → 视频 + 音频
  boxsets                                          → 虚拟合集，无物理路径（不放入映射）
"""
from pathlib import Path


# ----------------------------------------------------------------------------
# 扩展名集合（项目级单一来源）
# ----------------------------------------------------------------------------

VIDEO_EXTS = {
    '.mkv', '.mp4', '.m4v', '.mov', '.avi', '.wmv', '.flv',
    '.ts', '.webm', '.rmvb',
}

AUDIO_EXTS = {
    '.mp3', '.flac', '.wav', '.aac', '.m4a', '.ogg', '.wma',
    '.opus', '.ape', '.dsf',
}

IMAGE_EXTS = {
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.heic', '.svg',
}

SUBTITLE_EXTS = {
    '.srt', '.ass', '.ssa', '.sub', '.idx', '.vtt', '.sup',
}

EBOOK_EXTS = {
    '.epub', '.pdf', '.mobi', '.azw', '.azw3',
    '.cbz', '.cbr', '.fb2', '.djvu', '.txt',
}


# ----------------------------------------------------------------------------
# 库类型 → 期望的"主体文件"扩展名集合
# ----------------------------------------------------------------------------

PRIMARY_EXTS_BY_COLLECTION = {
    'movies':       VIDEO_EXTS,
    'tvshows':      VIDEO_EXTS,
    'musicvideos':  VIDEO_EXTS,
    'homevideos':   VIDEO_EXTS | IMAGE_EXTS,   # 宽松：家庭视频库允许纯照片记录
    'music':        AUDIO_EXTS,
    'photos':       IMAGE_EXTS,
    'books':        EBOOK_EXTS,
    'mixed':        VIDEO_EXTS | AUDIO_EXTS,
    # 'boxsets' 是虚拟合集，无物理路径 → 不放入映射；调用方收到 None 时按"未知类型"保守处理
}


# ----------------------------------------------------------------------------
# 主体文件存在性检查（auto-identify 等流程过滤"空壳目录"用）
# ----------------------------------------------------------------------------

def has_primary_media(local_path, collection_type):
    """
    检查 local_path（已 path_translator 翻译过的本地路径）下是否存在该库类型期望的主体文件。

    判定规则：
      - 路径不存在     → 返回 True（让上游业务报错，而非这里静默跳过）
      - 未知库类型     → 返回 True（保守，不主动过滤）
      - 单文件 item    → 看自身扩展名是否在期望集合
      - 目录 item      → 递归扫子内容，找到第一个匹配就 early-exit
      - 不限递归深度，但用 resolve() 后的真实路径 visited set 防软链接死循环

    返回 True = "有主体内容，应该继续走自动识别"
        False = "纯空壳目录，应该跳过"
    """
    exts = PRIMARY_EXTS_BY_COLLECTION.get(collection_type)
    if not exts:
        return True

    p = Path(local_path)
    if not p.exists():
        return True

    if p.is_file():
        return p.suffix.lower() in exts

    visited = set()
    return _walk_first_match(p, exts, visited)


def _walk_first_match(directory, exts, visited):
    """递归扫 directory，找到第一个扩展名 ∈ exts 的文件就返回 True。

    visited：已访问过的 resolve() 后真实路径，防 symlink 循环 / hardlink 重复扫。
    """
    try:
        real = directory.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    if real in visited:
        return False
    visited.add(real)

    try:
        entries = list(directory.iterdir())
    except (OSError, PermissionError):
        return False

    # 先扫文件（命中可立即返回，避免无谓的目录下钻）
    subdirs = []
    for entry in entries:
        try:
            if entry.is_file():
                if entry.suffix.lower() in exts:
                    return True
            elif entry.is_dir():
                subdirs.append(entry)
        except (OSError, PermissionError):
            continue

    for sub in subdirs:
        if _walk_first_match(sub, exts, visited):
            return True
    return False
