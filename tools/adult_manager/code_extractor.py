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

# 右边界：数字结尾后只要不是数字即可终止（避免 \b 在 "数字+字母" 间不成立的坑）
# 例如 MIDA-039ch / STARS-977V 这种番号 + 后缀字符的情况
_RIGHT = r'(?!\d)'

_PATTERNS = [
    # FC2 系列：FC2-PPV-1234567 / FC2PPV-1234567 / FC2-1234567
    (rf'\b(FC2){_SEP}(PPV)?{_SEP}(\d{{5,8}}){_RIGHT}',
     lambda m: f"FC2-PPV-{m.group(3)}" if m.group(2) else f"FC2-{m.group(3)}"),
    # HEYZO-1234
    (rf'\b(HEYZO){_SEP}(\d{{4,5}}){_RIGHT}', lambda m: f"HEYZO-{m.group(2)}"),
    # 1pondo / Caribbean / 10mu 系列：日期-编号 例如 010120_001 / 010120-001
    (rf'\b(\d{{6}})[-_\s](\d{{3,4}}){_RIGHT}', lambda m: f"{m.group(1)}-{m.group(2)}"),
    # T28-123 / RCT-456 等带 T/R 系列
    (rf'\b([TR]28|[A-Z]{{2,5}}){_SEP}(\d{{3,5}}){_RIGHT}',
     lambda m: f"{m.group(1)}-{m.group(2)}"),
    # 通用 ABC-123 / ABCD-123 形式（最常见）
    (rf'\b([A-Z]{{2,6}}){_SEP}(\d{{3,5}}){_RIGHT}',
     lambda m: f"{m.group(1)}-{m.group(2)}"),
    # N0123（TOKYO HOT 等）：cleaned 已 upper，所以这里写大写 N
    (rf'\b(N)(\d{{4}}){_RIGHT}', lambda m: f"N{m.group(2)}"),
]


# 文件名清理：移除常见的画质标签 / 字幕组 / 后缀，让正则更容易命中
# 注意：用 (?<![A-Za-z\d]) / (?![A-Za-z\d]) 替代 \b，因为 _ 在 \b 中算字母
_NOISE_PATTERNS = [
    # ISO 日期 / 日期+时间戳：2024-12-18 / 2024-12-18_21-23-11 / 2024.01.15T08:30:00
    # 必须放在通用番号正则之前清掉，否则 "video_2024-12-18..." 会被识别为 VIDEO-2024
    r'(?<![A-Za-z\d])\d{4}[-_./]\d{1,2}[-_./]\d{1,2}(?:[-_T\s]\d{1,2}[-_:.]\d{1,2}[-_:.]\d{1,2})?(?![A-Za-z\d])',
    r'(?<![A-Za-z\d])\d{3,4}p(?![A-Za-z\d])',         # 1080p / 720p
    r'(?<![A-Za-z\d])H[._-]?26[45](?![A-Za-z\d])',    # H264 / H265
    r'(?<![A-Za-z\d])(HEVC|AVC|x264|x265|XviD)(?![A-Za-z\d])',
    r'(?<![A-Za-z\d])(BluRay|Blu-Ray|BDRip|WEBRip|WEB-DL|HDTV|DVDRip)(?![A-Za-z\d])',
    r'(?<![A-Za-z\d])(AAC|AC3|DTS|FLAC)(?![A-Za-z\d])',
    r'(?<![A-Za-z\d])(JAV|jav|无码|有码|破解|流出|Uncensored|Censored)(?![A-Za-z\d])',
    # 通用域名水印：xxx.com / xxx.fun / xxx.tv 等
    # 主体必须至少含一个字母（避免误清 "XXX-12345.com" 中的 "12345.com" 而吃掉番号尾巴）
    r'(?<![A-Za-z\d])[A-Za-z0-9]*[A-Za-z][A-Za-z0-9]*\.(com|net|org|fun|tv|cn|tw|jp|me|io|xyz|cc|biz|info)(?![A-Za-z\d])',
    # 无 TLD 形式的常见水印 token（容易被通用 ABC-123 模式误识别为番号）
    r'(?<![A-Za-z\d])(hhd800|nyap2p|guochan2048|ssstwitter|tumblr|missav|javbus|javdb|sukebei|18comic|227766)(?![A-Za-z\d])',
    # 括号字符本身：单独清掉 [ ] ( ) 但保留内部内容（[FC2-PPV-xxx] 这种番号不能整段吃）
    r'[\[\]\(\)]',
    # @用户名 / @网站名：只清明显是水印的
    #   `(?![A-Za-z0-9])` 阻止部分回溯（避免吞掉 @PFE 留下 S-103）
    #   后面 lookahead 排除"水印@番号"格式：
    #     - @PFES-103         （@xxx-数字）
    #     - @FC2-PPV-2386297  （@xxx-字母-数字，FC2 系列常见）
    r'@[A-Za-z0-9]+(?![A-Za-z0-9])(?!-(?:[A-Za-z]+-)?\d)',
]


def clean_filename(filename: str) -> str:
    """清理文件名，去掉扩展名和常见噪音。"""
    name = Path(filename).stem
    for pat in _NOISE_PATTERNS:
        name = re.sub(pat, ' ', name, flags=re.IGNORECASE)
    # 把所有非 ASCII 字符（中文 / 假名 / emoji 等）替换成空格
    # 否则 \b 在中英文交界处不成立，例如 "PPPE-135处理" 中的 "5处" 不算 boundary，正则匹配不到
    name = re.sub(r'[^\x00-\x7f]+', ' ', name)
    # 把残留的连续下划线/横线/空格折叠为空格，方便后续正则
    name = re.sub(r'[_\-\s]+', ' ', name)
    return name.strip()


def extract_code(filename: str) -> Optional[str]:
    """
    从文件名提取番号。返回标准化字符串（大写 + 连字符），找不到返回 None。
    """
    cleaned = clean_filename(filename).upper()
    for pattern, formatter in _PATTERNS:
        # re.ASCII：让 \b/\w/\d 仅基于 ASCII（虽然 cleaned 已无非 ASCII，加上更保险）
        match = re.search(pattern, cleaned, re.ASCII)
        if match:
            return formatter(match)
    return None


def is_likely_adult_video(filename: str) -> bool:
    """判断文件名是否可能是番号视频（用于扫描时筛选）。"""
    return extract_code(filename) is not None
