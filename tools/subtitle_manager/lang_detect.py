"""
字幕文件语言识别（基于内容）

针对"裸名字幕文件"（如 movie.srt，没有 .zh / .en 之类语言后缀）：
读前 N 条字幕的"第一行文本"，按 Unicode 区段判断主语言。
对双语字幕来说，每条 cue 的第一行通常是主语言（用户规则）。

输出代码与 embedded_probe.normalize_lang 一致：chs / cht / eng / jpn / kor。
"""
import logging
import re
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# 默认采样多少条 cue —— 20 条够稳，对每条单独识别后投票（少数注释 cue 不会带偏判定）
DEFAULT_SAMPLE_CUES = 20

# 简繁辨别用的高频独占字符表（不求穷举，只求实战足够）：
# 这些字符基本只出现在对应字符集的语料中，命中即可作为强信号
SIMPLIFIED_ONLY = set(
    '这们国时见现实开关问题对应当为东西见会个发动学这觉过认识来'
    '说话听爱样进让让让让远办给给给给亲业级'
)
TRADITIONAL_ONLY = set(
    '這們國時見現實開關問題對應當為東見會個發動學這覺過認識來'
    '說話聽愛樣進讓遠辦給親業級'
)


def _read_text(path: Path, max_bytes: int = 8 * 1024 * 1024) -> Optional[str]:
    """
    读取字幕文件文本，并尽量正确地解码。

    编码探测策略（按可信度排序）：
      0. UTF-16 BOM（FF FE / FE FF）→ 直接 utf-16 解码（中文字幕组 .ass 常见）
      1. utf-8-sig / utf-8（覆盖 95% 现代字幕）
      2. utf-8 截断检测：头部合法但末尾因截断报错 → utf-8 ignore 兜底
      3. UTF-16 无 BOM 启发式：偶数位置大量 \\x00 字节 → utf-16 LE
      4. gb18030 / big5（中文老字幕）
      5. latin-1（永远成功，但语义不一定对）

    关键 bug 修复历史：
      - 早期：utf-8 截断错误会 fallback 到 gb18030 把 UTF-8 字节强解成乱码，
        导致语言识别误判为 eng。修复为 utf-8 ignore 兜底。
      - 当前：很多中文字幕组的 .ass 是 UTF-16 LE 带 BOM，旧实现里 BOM 后面
        FF FE 不是合法 utf-8 → 落到 gb18030/latin-1 → 解出全是乱码 →
        所有 `Dialogue:` 行都不再以字面量 'Dialogue:' 开头 → 采样为空。

    max_bytes 默认 8MB，覆盖含 [Fonts] 内嵌字体的复杂 .ass 文件。
    """
    try:
        raw = path.read_bytes()[:max_bytes]
    except OSError as e:
        logger.warning(f"读取字幕失败: {path} - {e}")
        return None

    # Step 0：UTF-16 BOM 优先识别（最高优先级，BOM 是强信号）
    # FF FE = UTF-16 LE，FE FF = UTF-16 BE。utf-16 解码器自动识别 BOM 并剥离
    if raw.startswith(b'\xff\xfe') or raw.startswith(b'\xfe\xff'):
        try:
            return raw.decode('utf-16')
        except UnicodeDecodeError:
            # BOM 在但末尾被截断在 surrogate 中间：用 ignore 兜底
            return raw.decode('utf-16', errors='ignore')

    # Step 1：严格 utf-8 优先（utf-8-sig 处理 BOM）
    for enc in ('utf-8-sig', 'utf-8'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue

    # Step 2：判断是不是 utf-8 但被截断在多字节中间
    # 启发式：开头能用 utf-8 解码就当是 utf-8
    try:
        raw[:4096].decode('utf-8')
        # 头部是合法 utf-8 → 全文用 utf-8 ignore 兜底，丢掉尾部损坏字节
        return raw.decode('utf-8', errors='ignore')
    except UnicodeDecodeError:
        pass  # 头部就不是 utf-8 → 真不是 utf-8

    # Step 3：UTF-16 无 BOM 启发式
    # 中英文字符在 UTF-16 LE 里偶数位是 ASCII 字节、奇数位是 \x00（中文则两位都非零）
    # 取头部 1024 字节看零字节占比，> 25% 就当 UTF-16 LE
    head = raw[:1024]
    if len(head) >= 32:
        zero_count = head.count(b'\x00')
        if zero_count * 4 > len(head):
            try:
                return raw.decode('utf-16-le', errors='ignore')
            except UnicodeDecodeError:
                pass

    # Step 4：尝试中文老编码
    for enc in ('gb18030', 'big5'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue

    # Step 5：兜底，永远成功（但 latin-1 解出来的字符不一定有意义）
    return raw.decode('latin-1', errors='ignore')


# ---- SRT 解析 ----

# SRT 块结构：
#   1
#   00:00:01,000 --> 00:00:03,000
#   text line 1
#   text line 2
#   <空行>
_SRT_BLOCK_RE = re.compile(
    r'(?:\d+\s*\n)?\s*\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}\s*-->\s*'
    r'\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}[^\n]*\n([\s\S]*?)(?=\n\s*\n|\Z)',
    re.MULTILINE,
)


def _extract_srt_cues(text: str, n: int) -> List[str]:
    """从 SRT 文本中提取前 n 条 cue 的"第一行文本"。"""
    out: List[str] = []
    for m in _SRT_BLOCK_RE.finditer(text):
        body = m.group(1).strip()
        if not body:
            continue
        first_line = body.split('\n', 1)[0].strip()
        # 去掉 SRT 常见的 HTML 样式标签
        first_line = re.sub(r'<[^>]+>', '', first_line).strip()
        if first_line:
            out.append(first_line)
        if len(out) >= n:
            break
    return out


# ---- ASS / SSA 解析 ----
# Dialogue 行格式（V4+ Styles）：10 个字段，前 9 个用逗号分隔，第 10 个 Text 可包含逗号
#   Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,实际文本（可含逗号）
# 用 split(',', 9) 而不是正则——正则在 Text 含 ASCII 逗号时会误截

def _strip_ass_overrides(s: str) -> str:
    """去掉 ASS 的 {\\xx} 覆盖标签和 \\N 换行符（保留首段文本）。"""
    s = re.sub(r'\{[^}]*\}', '', s)
    # \N / \n 是 ASS 的硬换行；按它切，只取第一段
    parts = re.split(r'\\[Nn]', s, maxsplit=1)
    return parts[0].strip()


# ASS 矢量绘图命令：以 m x y 开头，后面是 m/l/b/s/p/c/n/h 命令字母 + 数字
# 特效字幕（如片头 logo、字体设计）会用 Drawing 模式画图，{\p1}...{\p0} 包围
# 我们在 _strip_ass_overrides 去掉 {\xx} 后，剩下的就是纯绘图命令
# 这种 cue 含拉丁字母会被误判为 eng，必须跳过
_ASS_DRAWING_HEAD_RE = re.compile(r'^\s*m\s+-?\d+(?:\.\d+)?\s+-?\d+', re.IGNORECASE)


def _looks_like_ass_drawing(text: str) -> bool:
    """启发式：开头是 'm <数字> <数字>'（move to）→ 是 ASS 绘图命令"""
    return bool(_ASS_DRAWING_HEAD_RE.match(text or ''))


def _extract_ass_cues(text: str, n: int) -> List[str]:
    """
    从 ASS/SSA 文本提取前 n 条 Dialogue 的"第一行文本"。
    跳过 ASS Drawing（矢量绘图）cue —— 这种 cue 是特效图形，不是字幕文本，
    内含拉丁字母会让语言识别误判为 eng。
    """
    out: List[str] = []
    skipped_drawings = 0
    for line in text.splitlines():
        line = line.lstrip()
        if not line.startswith('Dialogue:'):
            continue
        # 去掉 "Dialogue:" 前缀，按逗号切 9 次 → 拿到第 10 段（Text）
        body = line[len('Dialogue:'):].lstrip()
        parts = body.split(',', 9)
        if len(parts) < 10:
            continue
        first_line = _strip_ass_overrides(parts[9])
        if not first_line:
            continue
        if _looks_like_ass_drawing(first_line):
            skipped_drawings += 1
            continue
        out.append(first_line)
        if len(out) >= n:
            break
    if skipped_drawings:
        logger.debug(f"_extract_ass_cues: skipped {skipped_drawings} drawing cues")
    return out


# ---- 语言判定 ----

def _has_cjk_ideograph(s: str) -> bool:
    """是否包含 CJK 统一汉字。"""
    return any('一' <= ch <= '鿿' for ch in s)


def _has_kana(s: str) -> bool:
    """是否包含日文假名（平假名 + 片假名）。"""
    return any('぀' <= ch <= 'ヿ' for ch in s)


def _has_hangul(s: str) -> bool:
    """是否包含韩文谚文。"""
    return any('가' <= ch <= '힯' for ch in s)


def _has_latin_letter(s: str) -> bool:
    return any(ch.isalpha() and ord(ch) < 128 for ch in s)


def _classify_chinese_simp_trad(samples: List[str]) -> str:
    """
    在已确认是中文的前提下区分简体/繁体。
    取所有样本拼起来做字符集对比；命中传统字符多则 cht，否则 chs。
    """
    text = ''.join(samples)
    simp_hits = sum(1 for ch in text if ch in SIMPLIFIED_ONLY)
    trad_hits = sum(1 for ch in text if ch in TRADITIONAL_ONLY)
    if trad_hits > simp_hits and trad_hits > 0:
        return 'cht'
    return 'chs'


def _detect_one_cue_lang(text: str) -> Optional[str]:
    """
    判定单条 cue 的主要语言（粗粒度，返回 jpn/kor/zh/eng 或 None）。
    中文先返回 'zh'，由调用方累计后再细分简繁。

    判定优先级（强信号优先）：
      1) 含日文假名 → jpn
      2) 含韩文 → kor
      3) 含 CJK 汉字（无假名/谚文）→ zh
      4) 仅含拉丁字母 → eng
    """
    if not text:
        return None
    if _has_kana(text):
        return 'jpn'
    if _has_hangul(text):
        return 'kor'
    if _has_cjk_ideograph(text):
        return 'zh'
    if _has_latin_letter(text):
        return 'eng'
    return None


def detect_lang_from_samples(samples: List[str]) -> Optional[str]:
    """
    投票法：对每条 cue 单独识别语言，统计票数，返回票数最多的那个。
    避免少数注释/混入 cue 把判定带偏（例如英文电影里偶尔出现的中文人名注释）。

    返回 chs/cht/eng/jpn/kor 或 None。
    中文票胜出后用拼接的中文 cue 走 _classify_chinese_simp_trad 细分简繁。
    平票时按"语言出现先后"取第一个（dict 插入序）。
    """
    if not samples:
        return None

    votes: dict = {}            # lang(zh/jpn/kor/eng) -> count
    zh_cues: List[str] = []     # 投了 zh 票的 cue 集合，用于简繁判定

    for s in samples:
        lang = _detect_one_cue_lang(s)
        if lang is None:
            continue
        votes[lang] = votes.get(lang, 0) + 1
        if lang == 'zh':
            zh_cues.append(s)

    if not votes:
        return None

    # 按票数降序，票数相同时保持原插入序（Python dict 有序）
    best_lang = max(votes, key=lambda k: votes[k])
    if best_lang == 'zh':
        return _classify_chinese_simp_trad(zh_cues)
    return best_lang


def detect_subtitle_language(
    path: Path,
    sample_cues: int = DEFAULT_SAMPLE_CUES,
) -> Optional[str]:
    """
    检测字幕文件语言。

    Args:
        path: 字幕文件路径
        sample_cues: 采样多少条 cue（默认 5）

    Returns:
        归一化语言代码（chs / cht / eng / jpn / kor）；无法识别返回 None。

    格式支持：
      - .srt / .ass / .ssa：解析文本 cue 后判定（核心场景）
      - .sub（VobSub 文本部分）：按非时间戳行采样判定
      - .sup（PGS / 蓝光图形字幕）：二进制图像格式，无法读字符——返回 None
        （只能依赖文件名后缀；如果裸名 .sup，调用方应当作"语言未知"处理）
    """
    ext = path.suffix.lower()

    # PGS/蓝光 SUP 是图像位图，没有可读文本，直接放弃
    if ext == '.sup':
        logger.debug(f".sup 是图像字幕，无法基于内容识别语言: {path}")
        return None

    text = _read_text(path)
    if text is None:
        return None

    if ext == '.srt':
        samples = _extract_srt_cues(text, sample_cues)
    elif ext in ('.ass', '.ssa'):
        samples = _extract_ass_cues(text, sample_cues)
    else:
        # .sub 等：粗暴退化为按行扫描，跳过纯数字/时间戳行，取前 sample_cues 个文本行
        samples = []
        for line in text.splitlines():
            line = line.strip()
            if line and not re.match(r'^[\d:.,\->\s\{\}]+$', line):
                samples.append(line)
                if len(samples) >= sample_cues:
                    break

    if not samples:
        # 诊断信息：把"为啥采样为空"说清楚，方便排查
        try:
            file_size = path.stat().st_size
        except OSError:
            file_size = -1
        if ext in ('.ass', '.ssa'):
            dialogue_lines = sum(1 for ln in text.splitlines() if ln.lstrip().startswith('Dialogue:'))
            comment_lines = sum(1 for ln in text.splitlines() if ln.lstrip().startswith('Comment:'))
            head_preview = text[:200].replace('\n', '\\n').replace('\r', '\\r')
            logger.warning(
                f"未能从字幕采样到任何文本（{ext}）: {path}\n"
                f"  文件大小={file_size} bytes, "
                f"Dialogue 行数={dialogue_lines}, Comment 行数={comment_lines}\n"
                f"  解码后头 200 字符: {head_preview!r}"
            )
        else:
            logger.warning(
                f"未能从字幕采样到任何文本（{ext}）: {path} 文件大小={file_size}"
            )
        return None

    detected = detect_lang_from_samples(samples)
    logger.debug(
        f"字幕语言识别 [{detected or '未知'}] "
        f"{path.name} (samples: {[s[:30] for s in samples[:3]]})"
    )
    return detected
