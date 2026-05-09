r"""
路径转换工具
把 Jellyfin 报上来的路径转换成本机可访问的路径。

典型场景：
  - Jellyfin 跑在 Linux/Docker，路径形如 /library/videos/movie/Anna.2019/
  - 本后端跑在 Windows，通过 SMB 把同一份媒体目录挂为 Z:\videos\movie\Anna.2019\
  - 我们在配置 path_mappings 里声明 [{from: /library/videos, to: Z:/videos}]，
    所有要做文件系统操作的代码先调 translate_path 再 open / iterdir / stat。

设计要点：
  - 只比较 forward-slash 形式（避免 Windows 反斜杠的歧义）
  - 严格前缀匹配（避免 /library 误匹配 /library2）
  - 按 mappings 列表顺序尝试，先命中先返回
  - 没命中的路径原样返回（兼容同机部署 / 容器路径一致的场景）
"""
from typing import List, Dict


def _normalize_for_match(p: str) -> str:
    """统一用 forward slash + 去末尾斜杠，仅用于前缀匹配。"""
    return p.replace('\\', '/').rstrip('/')


def translate_path(path: str, mappings: List[Dict]) -> str:
    """
    Args:
        path: Jellyfin 给的原始路径
        mappings: [{from: 'xxx', to: 'yyy'}, ...] 列表

    Returns:
        转换后的路径；没命中映射规则就原样返回。
    """
    if not path:
        return path
    if not mappings:
        return path

    # 用 forward slash 形式做前缀匹配
    normalized = path.replace('\\', '/')

    for m in mappings:
        from_prefix = m.get('from')
        to_prefix = m.get('to')
        if not from_prefix or not to_prefix:
            continue

        from_norm = _normalize_for_match(from_prefix)
        # 严格前缀：要么完全相等，要么后面紧跟 / —— 防止 /library 误匹配 /library2/
        if normalized == from_norm:
            return to_prefix
        prefix_with_sep = from_norm + '/'
        if normalized.startswith(prefix_with_sep):
            tail = normalized[len(from_norm):]  # 含开头的 /
            return to_prefix.rstrip('/') + tail
    return path


def translate_path_with_settings(path: str) -> str:
    """便捷封装：读 settings 配置来转换。enabled=False 时直接原样返回。"""
    from web.backend.config import settings
    if not settings.path_mappings_enabled:
        return path
    return translate_path(path, settings.path_mappings_rules)


def reverse_translate_path(path: str, mappings: List[Dict]) -> str:
    """
    反向翻译：本机路径 → Jellyfin 视角路径。
    用于通知 jellyfin 时（/Library/Media/Updated）传它能识别的路径。

    把 mappings 里的 from / to 翻转再走 translate_path 即可。
    """
    if not path or not mappings:
        return path
    flipped = [
        {'from': m.get('to'), 'to': m.get('from')}
        for m in mappings
        if m.get('from') and m.get('to')
    ]
    return translate_path(path, flipped)


def reverse_translate_path_with_settings(path: str) -> str:
    """便捷封装：读 settings 反向转换。enabled=False 时直接原样返回。"""
    from web.backend.config import settings
    if not settings.path_mappings_enabled:
        return path
    return reverse_translate_path(path, settings.path_mappings_rules)
