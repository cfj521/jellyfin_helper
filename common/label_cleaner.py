"""
通用 label 清洗：把抓来的 actors / tags / genres 列表统一去标点 / 空白 / 重复。

清洗策略：**只保留"合理"字符**，其它一律替换为空格再 trim：
  保留集合：
    - 字母数字（含下划线）
    - CJK（中日韩、假名、谚文 —— UNICODE 模式 \\w 自动覆盖）
    - 空格
    - 连字符 `-`（"Mary-Jane" / "Sci-Fi" 等）
    - 中点 `·`（"乔治·克鲁尼" 等）
    - 斜杠 `/` `\\`（"巨乳/中文字幕" 这种内部分隔保留原貌）
    - 逗号 `,` `，` `、`（多人 / 多 tag 合写时保留）

清洗的字符：括号 () [] {} （）【】《》「」 / 管道 |
   / 终止标点 .;:!? 。；：！？ / 引号 " ' "" '' / @ # $ % ^ & * + = < > ~ ` 等

中间字符保留，但前后这些字符也会被一并 trim 掉（避免 '/巨乳' 残留前导斜杠）：
  - 空白、连字符、中点、下划线、所有斜杠、所有逗号

后处理：合并多空格 → trim → 长度 > 50 丢弃 → 空 / 无效丢弃 → 大小写不敏感去重保序

例：
  '  上原亜衣，'   → '上原亜衣'        (trim 空白 + 全角逗号)
  '/巨乳'         → '巨乳'             (trim 前导斜杠)
  'A/B/C'         → 'A/B/C'            (中间斜杠保留)
  '巨乳/中文字幕' → '巨乳/中文字幕'    (保留)
  '上原, 成瀬'    → '上原, 成瀬'        (内部逗号保留)
  '(VR)'          → 'VR'              (括号清掉)
  'Mary-Jane'     → 'Mary-Jane'
  '。。'          → 丢弃
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional

# 要保留的字符：\\w（字母数字 + 下划线 + CJK）+ 空白 + - + · + 斜杠 + 逗号
# 其它（括号 / 管道 / 标点 / 引号 / 各种符号）替换成空格
_KEEP_RE = re.compile(r'[^\w\s\-·,，、/\\]', flags=re.UNICODE)

# 前后 trim 字符集（同样不希望出现在边缘）：空白 / 连字符 / 中点 / 下划线 / 所有斜杠 / 所有逗号
_TRIM_CHARS = ' -·_/\\,，、'


def clean_one_label(s) -> Optional[str]:
    """单条 label 清洗。无效返回 None。"""
    if not s or not isinstance(s, str):
        return None
    # 1) 把不允许的字符替换为空格（() [] {} | . ; : ! ? " ' 等）
    s = _KEEP_RE.sub(' ', s)
    # 2) 合并连续空白（换行 / tab 也都被 \\s 覆盖）
    s = re.sub(r'\s+', ' ', s)
    # 3) trim 前后的空白 / 连字符 / 中点 / 斜杠 / 逗号
    s = s.strip(_TRIM_CHARS)
    if not s:
        return None
    # 长度上限：演员名 / 标签合理范围 ≤ 50；超过多半是描述性文字误入
    if len(s) > 50:
        return None
    return s


def clean_label_list(items: Optional[Iterable]) -> List[str]:
    """对一组 label 做清洗；保序去重（大小写不敏感）。"""
    if not items:
        return []
    out: List[str] = []
    seen: set = set()
    for x in items:
        c = clean_one_label(x)
        if not c:
            continue
        key = c.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out
