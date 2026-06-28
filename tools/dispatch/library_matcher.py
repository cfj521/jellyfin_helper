"""把识别结果匹配到 jellyfin 库里已存在的同一作品目录。

纯逻辑、无 I/O：调用方（backend.api.dispatch._resolve_target）负责从 jellyfin 拉
候选项（series / movies），整理成 [{name, path, tmdb, imdb}] 丢进来。

匹配优先级：tmdb_id → imdb_id → 归一化 name 精确相等。
name 用**精确相等**（不是包含），避免 'The Terminal List' 误匹配
'The Terminal List Dark Wolf' 这类假阳性。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

# 尾部 (括号) 段：常见 '(2015)'、'(US)' 等附注
_TRAILING_PAREN_RE = re.compile(r'\s*\([^()]*\)\s*$')
# release 分隔符 → 空格
_SEP_RE = re.compile(r'[._\-]+')
# 尾部年份 token（要求前面有内容，避免把纯年份标题清空）
_TRAILING_YEAR_RE = re.compile(r'\s+(?:19|20)\d{2}$')


def normalize_title(s: Optional[str]) -> str:
    """归一化标题用于兜底比较：
    - 小写
    - 去尾部 (括号) 段
    - [._-] → 空格、折叠空格
    - 去尾部年份（仅尾部，剧名内部数字不动）
    """
    if not s:
        return ''
    s = s.lower()
    s = _TRAILING_PAREN_RE.sub('', s)
    s = _SEP_RE.sub(' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    s = _TRAILING_YEAR_RE.sub('', s).strip()
    return s


_SEASON_DIR_RE = re.compile(r'^(S|Season)(\s*)(\d+)$', re.IGNORECASE)


def choose_season_dirname(existing_subdirs: List[str], season: int) -> str:
    """根据剧目录下已有季子目录的风格，给出第 season 季的子目录名。

    - 目标季已存在该子目录 → 原样复用
    - 否则跟随最高季的风格（前缀 'S' vs 'Season '、补零宽度）
    - 无任何季目录可参考 → 退回模板 'Season {season:02d}'
    """
    parsed = []  # (num, form, sep, width, original_name)
    for name in existing_subdirs:
        m = _SEASON_DIR_RE.match((name or '').strip())
        if not m:
            continue
        form = 'season' if m.group(1).lower() == 'season' else 's'
        parsed.append((int(m.group(3)), form, m.group(2), len(m.group(3)), name))

    if not parsed:
        return f"Season {season:02d}"

    # 目标季已存在 → 复用既有子目录名
    for num, _form, _sep, _width, name in parsed:
        if num == season:
            return name

    # 跟随最高季的风格
    parsed.sort(key=lambda x: x[0])
    _num, form, sep, width, _name = parsed[-1]
    num_str = f"{season:0{width}d}"
    if form == 'season':
        return f"Season{sep or ' '}{num_str}"
    return f"S{num_str}"


def _basename(path: str) -> str:
    if not path:
        return ''
    return re.split(r'[\\/]', path.rstrip('\\/'))[-1]


def match_library_dir(
    candidates: List[Dict],
    *,
    tmdb_id: Optional[str] = None,
    imdb_id: Optional[str] = None,
    name: Optional[str] = None,
) -> Optional[Dict]:
    """candidates: [{'name','path','tmdb','imdb'}, ...]。返回命中的 candidate 或 None。"""
    if not candidates:
        return None

    # 1. tmdb_id
    if tmdb_id:
        tid = str(tmdb_id)
        for c in candidates:
            if c.get('tmdb') and str(c['tmdb']) == tid:
                return c

    # 2. imdb_id
    if imdb_id:
        iid = str(imdb_id).lower()
        for c in candidates:
            if c.get('imdb') and str(c['imdb']).lower() == iid:
                return c

    # 3. 归一化 name 精确相等（比对库项的 name 与 path 末段）
    qn = normalize_title(name)
    if qn:
        for c in candidates:
            for t in (c.get('name'), _basename(c.get('path') or '')):
                if t and normalize_title(t) == qn:
                    return c

    return None
