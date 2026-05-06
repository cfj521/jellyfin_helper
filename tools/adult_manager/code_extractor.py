"""
番号识别
从文件名提取番号（标准化大写、连字符）。
"""
import re
from pathlib import Path
from typing import Optional


# 匹配模式（按优先级）
# 注意：顺序很重要，更具体的模式应放前面避免被通用模式误匹配
# clean_filename 后用空格分隔，所以正则中允许 [-_\s] 任一作为连接符
_SEP = r'[-_\s]?'

_PATTERNS = [
    # FC2 系列：FC2-PPV-1234567 / FC2PPV-1234567 / FC2-1234567
    (rf'\b(FC2){_SEP}(PPV)?{_SEP}(\d{{5,8}})\b',
     lambda m: f"FC2-PPV-{m.group(3)}" if m.group(2) else f"FC2-{m.group(3)}"),
    # HEYZO-1234
    (rf'\b(HEYZO){_SEP}(\d{{4,5}})\b', lambda m: f"HEYZO-{m.group(2)}"),
    # 1pondo / Caribbean / 10mu 系列：日期-编号 例如 010120_001 / 010120-001
    (r'\b(\d{6})[-_\s](\d{3,4})\b', lambda m: f"{m.group(1)}-{m.group(2)}"),
    # T28-123 / RCT-456 等带 T/R 系列
    (rf'\b([TR]28|[A-Z]{{2,5}}){_SEP}(\d{{3,5}})\b',
     lambda m: f"{m.group(1)}-{m.group(2)}"),
    # 通用 ABC-123 / ABCD-123 形式（最常见）
    (rf'\b([A-Z]{{2,6}}){_SEP}(\d{{3,5}})\b',
     lambda m: f"{m.group(1)}-{m.group(2)}"),
    # n0123 / 数字开头
    (r'\b(n)(\d{4})\b', lambda m: f"n{m.group(2)}"),
]


# 文件名清理：移除常见的画质标签 / 字幕组 / 后缀，让正则更容易命中
# 注意：用 (?<![A-Za-z\d]) / (?![A-Za-z\d]) 替代 \b，因为 _ 在 \b 中算字母
_NOISE_PATTERNS = [
    r'(?<![A-Za-z\d])\d{3,4}p(?![A-Za-z\d])',         # 1080p / 720p
    r'(?<![A-Za-z\d])H[._-]?26[45](?![A-Za-z\d])',    # H264 / H265
    r'(?<![A-Za-z\d])(HEVC|AVC|x264|x265|XviD)(?![A-Za-z\d])',
    r'(?<![A-Za-z\d])(BluRay|Blu-Ray|BDRip|WEBRip|WEB-DL|HDTV|DVDRip)(?![A-Za-z\d])',
    r'(?<![A-Za-z\d])(AAC|AC3|DTS|FLAC)(?![A-Za-z\d])',
    r'(?<![A-Za-z\d])(JAV|jav|无码|有码|破解|流出|Uncensored|Censored)(?![A-Za-z\d])',
    r'\[.*?\]',                                        # 中括号内容
    r'\(.*?\)',                                        # 小括号内容
    r'@[A-Za-z0-9]+',                                  # @用户名（不吃后续 _XXX）
]


def clean_filename(filename: str) -> str:
    """清理文件名，去掉扩展名和常见噪音。"""
    name = Path(filename).stem
    for pat in _NOISE_PATTERNS:
        name = re.sub(pat, ' ', name, flags=re.IGNORECASE)
    # 把残留的连续下划线/横线/空格折叠为空格，方便后续正则
    name = re.sub(r'[_\-\s]+', ' ', name)
    return name.strip()


def extract_code(filename: str) -> Optional[str]:
    """
    从文件名提取番号。返回标准化字符串（大写 + 连字符），找不到返回 None。
    """
    cleaned = clean_filename(filename).upper()
    for pattern, formatter in _PATTERNS:
        match = re.search(pattern, cleaned)
        if match:
            return formatter(match)
    return None


def is_likely_adult_video(filename: str) -> bool:
    """判断文件名是否可能是番号视频（用于扫描时筛选）。"""
    return extract_code(filename) is not None
