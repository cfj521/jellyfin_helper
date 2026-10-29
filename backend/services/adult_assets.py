"""成人视频本地资源（封面）路径工具。

标准命名是 ``<video_stem>-poster.jpg``。当视频文件名本身已经接近文件系统
上限时，这个后缀会让文件名超过 255 字节（Linux ext4）或 MAX_PATH
（Windows），导致 ``exists()`` / ``write_bytes()`` 直接抛 OSError。

这里的处理原则：
1. 优先仍用标准命名，保证 Jellyfin 的本地图片扫描器能识别；
2. 标准名过长时，落一个短名封面（`<code>-<hash>-poster.jpg`），至少让
   Helper 自己的封面接口可用；
3. 短名 Jellyfin 不认识，所以再尝试通过 API 直接把图片挂到对应 item 上。
"""
from __future__ import annotations

import errno
import hashlib
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

POSTER_EXTS = ('.jpg', '.jpeg', '.png', '.webp')


def is_name_too_long(exc: OSError) -> bool:
    """判断 OSError 是否由文件名/路径超长引起（兼容 Windows）。"""
    return exc.errno == errno.ENAMETOOLONG or getattr(exc, 'winerror', None) == 206


def extract_video_code(video_path: Path) -> str:
    """尽力从父目录名或文件名中提取番号；失败返回空串。"""
    try:
        from tools.adult_manager.code_extractor import extract_code
        return extract_code(video_path.parent.name) or extract_code(video_path.name) or ''
    except Exception:
        return ''


def fallback_poster_path(video_path: Path, ext: str = '.jpg') -> Path:
    """生成一个短且稳定的封面文件名。

    用番号（或 cover）+ 路径哈希，既避免同名视频互相覆盖，也保证长度
    远低于文件系统限制。
    """
    code = re.sub(r'[^A-Za-z0-9._-]+', '_', extract_video_code(video_path))
    code = code.strip('._-')[:40] or 'cover'
    digest = hashlib.sha1(str(video_path).encode('utf-8')).hexdigest()[:8]
    return video_path.with_name(f'{code}-{digest}-poster{ext}')


def find_local_poster(video_path: Path) -> Optional[Path]:
    """查找已存在的封面：先标准名，再短名。"""
    if not video_path:
        return None
    for ext in POSTER_EXTS:
        for cand in (
            video_path.with_name(f'{video_path.stem}-poster{ext}'),
            fallback_poster_path(video_path, ext),
        ):
            try:
                if cand.is_file():
                    return cand
            except OSError as e:
                # 标准名过长是预期情况，继续试短名；其他错误只记录。
                if not is_name_too_long(e):
                    logger.warning(f"检查封面失败 {cand}: {e}")
    return None


def save_poster(video_path: Path, content: bytes, ext: str = '.jpg') -> Path:
    """保存封面，必要时降级为短名并直传 Jellyfin。

    返回实际写入的路径。标准名写入失败且不是超长错误时，原样抛出。
    """
    target = video_path.with_name(f'{video_path.stem}-poster{ext}')
    try:
        target.write_bytes(content)
        return target
    except OSError as e:
        if not is_name_too_long(e):
            raise
        fallback = fallback_poster_path(video_path, ext)
        fallback.write_bytes(content)
        logger.warning(
            f"封面文件名超过文件系统限制，已改用短名 {fallback.name}；"
            "Jellyfin 本地扫描不会识别该名称，尝试 API 直传"
        )
        upload_poster_to_jellyfin(video_path, content)
        return fallback


def upload_poster_to_jellyfin(video_path: Path, content: bytes) -> bool:
    """按视频路径找到 Jellyfin item，并把封面作为 Primary 图片上传。"""
    try:
        from backend.config import settings
        if not settings.jellyfin_api_key:
            return False

        from backend.path_translator import reverse_translate_path_with_settings
        from common.jellyfin_client import JellyfinClient

        jf_path = reverse_translate_path_with_settings(str(video_path)) or str(video_path)
        search_term = extract_video_code(video_path) or video_path.stem[:64]
        client = JellyfinClient(settings.jellyfin_host, settings.jellyfin_api_key)
        item = client.find_item_by_path(jf_path, search_term)
        if not item:
            logger.info(f"Jellyfin 尚未收录该路径，跳过封面直传: {jf_path}")
            return False
        return client.upload_item_image(item['Id'], content, 'Primary')
    except Exception as e:
        logger.warning(f"封面直传 Jellyfin 失败 {video_path.name[:80]}: {e}")
        return False
