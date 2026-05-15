r"""
路径模板渲染 + 文件名清理。两条职责：

1. render_template(template, values)
   - 支持 Python 标准 {var} / {var:fmt} 占位符
   - **空值（None / 空字符串）智能去括号**：
       '{title} ({year})' + year=None       → '{title}'                → 'The Martian'
       '({series_name})S{season:02d}'       + series_name='X'           → '(X)S01'
       '({series_name})S{season:02d}E{ep:02d}' + series_name=None, s=1, e=2 → 'S01E02'
     规则：占位符值为空时，去掉占位符 + 紧邻它的左/右配对包裹符（() [] {}）+ 任何前后空白
   - 不抛异常；缺 key 当 None 处理；坏 fmt spec 退化为原值 str

2. sanitize_path_segment(s)
   - 替换 Windows 非法字符 <>:"/\\|?* + 控制字符 → '_'
   - 去首尾空白和 '.' (Windows: 尾点会被静默吃，前点 = 隐藏文件)
   - 折叠连续空格
   - 长度截断（默认 200 chars，给扩展名留余地）
   - 处理 Windows 保留名（CON / PRN / AUX / NUL / COM1-9 / LPT1-9）

3. sanitize_path(path)
   - 按 '/' '\\' 分段，每段过 sanitize_path_segment；保留分隔符与盘符
   - 整体长度不卡（Windows 260 默认上限要在外层处理 / 用 \\?\ prefix）
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ============================================================================
# 模板渲染
# ============================================================================

# 占位符正则：{name} 或 {name:format_spec}
_PLACEHOLDER_RE = re.compile(r'\{([a-zA-Z_][a-zA-Z_0-9]*)(:[^{}]*)?\}')

# 配对的包裹符：紧邻空占位符时一并去掉
_OPEN_WRAPPER_RE = re.compile(r'\s*[(\[{]\s*$')
_CLOSE_WRAPPER_RE = re.compile(r'^\s*[)\]}]\s*')


def _is_empty(value: Any) -> bool:
    """空值：None / '' / 仅空白；0 和 False 不算空（年份 0、季 0 罕见但不能误删）。"""
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def render_template(template: str, values: Optional[Dict[str, Any]] = None) -> str:
    """智能渲染模板：空占位符 + 相邻包裹符一起去掉，结果折叠多空格。

    缺 key / None / 空字符串都视为空。
    数字 0 不视为空（{year:04d} year=0 极少见但合法）。
    """
    if not template:
        return ''
    values = values or {}

    # Tokenize：交替的 literal 段 与 placeholder 段
    tokens = []          # [('lit', str) | ('var', name, fmt_spec_or_None)]
    last = 0
    for m in _PLACEHOLDER_RE.finditer(template):
        if m.start() > last:
            tokens.append(('lit', template[last:m.start()]))
        tokens.append(('var', m.group(1), m.group(2) or ''))
        last = m.end()
    if last < len(template):
        tokens.append(('lit', template[last:]))

    # Render：空 var 时把上一个 lit 的尾部包裹符 + 下一个 lit 的头部包裹符 一并 trim
    out_segments = []     # 累计输出字符串段；可回头改最后一段
    skip_next_open_close = False    # 标记：下一个 lit 段开头如果是 )]} 要 trim 掉

    for tok in tokens:
        if tok[0] == 'lit':
            seg = tok[1]
            if skip_next_open_close:
                seg = _CLOSE_WRAPPER_RE.sub('', seg, count=1)
                skip_next_open_close = False
            out_segments.append(seg)
            continue

        # 'var'
        _, name, fmt = tok
        raw = values.get(name)
        if _is_empty(raw):
            # 上一个 lit 末尾如果是 '(' '[' '{' + 周围空白 → 一并 trim
            if out_segments:
                trimmed = _OPEN_WRAPPER_RE.sub('', out_segments[-1])
                out_segments[-1] = trimmed
            # 标记下一个 lit 开头处理对称的右括号
            skip_next_open_close = True
            continue

        # 有值：尝试用 str.format 规格化（处理 :02d 等）
        if fmt:
            try:
                formatted = ('{0' + fmt + '}').format(raw)
            except (ValueError, TypeError) as e:
                logger.warning(f"格式 spec {fmt!r} 跟值 {raw!r} 不匹配: {e}（退化为 str）")
                formatted = str(raw)
        else:
            formatted = str(raw)
        out_segments.append(formatted)

    result = ''.join(out_segments)
    # 多空格折叠 + 首尾收
    result = re.sub(r'[ \t]+', ' ', result).strip()
    return result


# ============================================================================
# 文件名 / 路径清理
# ============================================================================

# Windows 文件名禁用字符：<>:"/\|?* （含反斜杠正斜杠）
_INVALID_FN_CHARS_RE = re.compile(r'[<>:"/\\|?*]')
# 非空白控制字符（去掉 \t \n \r —— 它们后面归到空白折叠那条线，呈现为空格而非 '_'）
_NON_WS_CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')

# Windows 保留设备名（不区分大小写；扩展名也算）
_WIN_RESERVED = {
    'CON', 'PRN', 'AUX', 'NUL',
    *(f'COM{i}' for i in range(1, 10)),
    *(f'LPT{i}' for i in range(1, 10)),
}


def sanitize_path_segment(name: str, max_len: int = 200) -> str:
    """清理**单个**路径段（文件名 或 单个目录名）。

    - 替换 Windows 非法字符 + 控制字符 → '_'
    - 去首尾空白 / '.'（Windows 静默吃尾点；前点是 *nix 隐藏文件）
    - 折叠连续空格
    - Windows 保留名后加 '_' 防冲突（CON / PRN / NUL 等）
    - 长度截断：保留尾部扩展名
    """
    if not name:
        return ''
    # 1. 非空白控制字符 → '_'；\t \n \r 留给空白折叠那步当成空格
    s = _NON_WS_CTRL_RE.sub('_', name)
    # 2. 替换 Windows 路径非法字符 → '_'
    s = _INVALID_FN_CHARS_RE.sub('_', s)
    # 3. 折叠空白（含 tab / 多空格）→ 单空格
    s = re.sub(r'\s+', ' ', s)
    # 4. 去首尾空白 + '.'（重复 strip，处理 ".  .name." 这种）
    s = s.strip().strip('.').strip()
    if not s:
        return ''
    # Windows 保留名：核心名（去扩展名）撞 → 加 '_'
    core = s.rsplit('.', 1)[0].upper()
    if core in _WIN_RESERVED:
        s = f'{s}_'
    # 长度截断（保留扩展名）
    if len(s) > max_len:
        idx = s.rfind('.')
        if 0 < idx and (len(s) - idx) <= 6:           # 扩展名 5 字符内
            ext = s[idx:]
            s = s[: max_len - len(ext)].rstrip() + ext
        else:
            s = s[:max_len].rstrip()
    return s


def sanitize_path(path: str, max_segment_len: int = 200) -> str:
    """清理**完整路径**：保留盘符 / 根、每段单独 sanitize。

    保留分隔符（首选 '/'，传入是 '\\' 也容忍）；末尾分隔符保留。
    """
    if not path:
        return ''
    # 探测分隔符（混用时优先 / 输出）
    sep = '/'
    if '\\' in path and '/' not in path:
        sep = '\\'

    # 盘符或 UNC 头部（'C:/' 'C:\\' '\\\\share\\'）单独保留
    head = ''
    rest = path
    m = re.match(r'^([a-zA-Z]:[\\/]+)', path)
    if m:
        head = m.group(1).replace('\\', sep)
        rest = path[m.end():]
    elif path.startswith(('\\\\', '//')):
        # UNC 头：保留前两个 sep
        head = sep + sep
        rest = path.lstrip('\\/')
    elif path.startswith(('\\', '/')):
        head = sep
        rest = path.lstrip('\\/')

    # 拆段、清理、重组
    parts = re.split(r'[\\/]+', rest)
    cleaned = [sanitize_path_segment(p, max_segment_len) for p in parts if p]
    out = head + sep.join(cleaned)
    return out
