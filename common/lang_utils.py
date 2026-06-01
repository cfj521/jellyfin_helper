"""
字幕语言识别 —— 多源共用。

从文件名 / 描述文本中提取语言代码，输出与项目其他模块一致的内部代码：
  chs / cht / eng / jpn / kor

支持双语（多语）字幕的命名变体，统一返回 dot-separated 复合 code：
  'chs.eng'   ← 'chs.eng' / 'chs&eng' / 'chs_eng' / 'chs-eng'
  'chs.eng'   ← '简&英' / '简体&英文' / '简英' / '简体英文'
  'chs.eng'   ← 'movie.简体.英文.srt'
  'chs.eng'   ← 'movie.zh-CN.eng.srt'     ← 新落盘格式
  'chs'       ← '简体' / '简' / 'GB' / 'GBK' / '.chs.' / '.zh-CN.' / '.zh-Hans.'
  'cht'       ← '繁体' / '繁' / 'BIG5' / '.cht.' / '.zh-TW.' / '.zh-Hant.'
  'eng'       ← 'English' / '英文' / '英' / '.eng.' / '.en.'

落盘 vs 内部编码：
  - **内部代码** 始终是 chs/cht/eng/jpn/kor —— 配置项、preferred_langs、API
    参数等都用这一套。
  - **落盘文件名** 中文统一用 BCP 47 region 形式：chs → zh-CN / cht → zh-TW，
    Jellyfin 和 PotPlayer 都能识别（PotPlayer 不认 script subtag Hans/Hant）。
    其它语言 (eng/jpn/kor) 保持 ISO 639-2/B 三字母，Jellyfin 也认。
  - 旧落盘的 .chs.srt / .cht.srt / .zh-Hans.srt / .zh-Hant.srt 仍能被回读识别
    为 chs / cht（向后兼容）。
  - 写盘时用 `internal_to_filename_token()` 做映射；读盘走 `detect_lang_combo()`。

设计要点：
  - 用 token 边界（前后是分隔符或字符串边界）判定，避免 'punches' 误中 'chs'、
    '日本' 误中 'jpn' 这类常见误命中。
  - 双字关键字（'简体'/'英文'）先扫一遍，按出现位置排序；单字（'简'/'英'）只补充
    双字遗漏的部分。
  - 所有源（assrt / shooter / 文件名识别 / 内容识别 fallback）共用同一函数。
"""
import re
from typing import List, Optional

# 项目内部代码 → 这是其他模块的"标准代码"
# 'zh' = 通用中文（未指定简繁）—— 在缺字幕判定里同时覆盖 chs + cht 需求
_INTERNAL_CODES = ('chs', 'cht', 'zh', 'eng', 'jpn', 'kor')

# 显式 lang code 别名 → 内部代码
# BCP 47 region 形式（zh-CN/zh-TW）是当前落盘 canonical，必须能被反向识别。
# 老的 zh-Hans/zh-Hant 也保留映射 —— 历史落盘文件仍能被回读为简/繁。
# 'zh' 单独存在（无 region / script）→ 通用中文 'zh'，**不再默认归 chs**
#   - 历史代码：'zh': 'chs'（强行当简体）
#   - 现在：'zh': 'zh'，让缺字幕判定可以"未指定简繁也算两边都命中"
_CODE_ALIAS = {
    'chs': 'chs', 'gb': 'chs', 'gbk': 'chs', 'gb2312': 'chs', 'gb18030': 'chs',
    'simplified': 'chs', 'sc': 'chs',
    'zh-cn': 'chs', 'zh_cn': 'chs', 'cn': 'chs',     # 大陆地区 → 简体（约定）
    'zh-hans': 'chs', 'zh_hans': 'chs',              # BCP 47 显式简体
    'cht': 'cht', 'big5': 'cht', 'tc': 'cht', 'traditional': 'cht',
    'zh-tw': 'cht', 'zh_tw': 'cht', 'tw': 'cht', 'zh-hk': 'cht', 'zh_hk': 'cht', 'hk': 'cht',
    'zh-hant': 'cht', 'zh_hant': 'cht',              # BCP 47 显式繁体
    'zh': 'zh', 'zho': 'zh', 'chi': 'zh',            # 通用中文（未指定简繁）
    'chinese': 'zh', 'cmn': 'zh', 'mandarin': 'zh',
    'eng': 'eng', 'en': 'eng', 'english': 'eng',
    'jpn': 'jpn', 'jap': 'jpn', 'ja': 'jpn', 'japanese': 'jpn',
    'kor': 'kor', 'ko': 'kor', 'korean': 'kor',
}

# 内部代码 → 落盘文件名 token（写盘 canonical）
# Jellyfin 用 .NET CultureInfo 解析字幕文件名后缀；chs/cht 不在它的识别表里。
# 选用 BCP 47 region 形式 (zh-CN / zh-TW) 而不是 script 形式 (zh-Hans / zh-Hant) ——
# 两者 Jellyfin 都认，但 PotPlayer 等外部播放器不认 Hans/Hant 这种 script subtag，
# region 形式可以让同一份字幕文件在 Jellyfin 和 PotPlayer 里都被正确识别。
# 其它语言保持原代码（eng/jpn/kor 都是 ISO 639-2/B，jellyfin 认识）。
_INTERNAL_TO_FILENAME = {
    'chs': 'zh-CN',
    'cht': 'zh-TW',
    'zh':  'zh',          # 通用中文：写盘也用 BCP 47 'zh'，jellyfin 显示"中文"
    'eng': 'eng',
    'jpn': 'jpn',
    'kor': 'kor',
}


def internal_to_filename_token(code: str) -> str:
    """
    把内部 lang code 映射为落盘文件名用的 token。

    单语：'chs' → 'zh-CN', 'cht' → 'zh-TW', 'eng' → 'eng', ...
    多语（dot 复合）：'chs.eng' → 'zh-CN.eng', 'cht.eng' → 'zh-TW.eng'

    未识别的 code 原样返回（兜底，避免误改外来代码）。
    """
    if not code:
        return code
    parts = str(code).split('.')
    mapped = [_INTERNAL_TO_FILENAME.get(p.lower(), p) for p in parts]
    return '.'.join(mapped)

# 双字关键字（按"中文区块顺序"扫文件名，多语字幕的命名顺序通常是 简-繁-英-日-韩）
_DOUBLE_KEYWORDS = (
    ('简体', 'chs'), ('简中', 'chs'),
    ('繁体', 'cht'), ('繁中', 'cht'),
    ('英文', 'eng'), ('英语', 'eng'), ('英语', 'eng'),
    ('日文', 'jpn'), ('日语', 'jpn'),
    ('韩文', 'kor'), ('韩语', 'kor'),
)

# 单字 / 连写中文关键字 → 内部代码（用于识别 '简' / '简英' / '简繁英' 等）
# —— 序列匹配后逐字映射，自然支持任意组合
_CHAR_TO_CODE_MAP = {'简': 'chs', '繁': 'cht', '英': 'eng', '日': 'jpn', '韩': 'kor'}

# 通用分隔符集合（正则字符类）—— 中文 / 英文常见的语言之间分隔
# 包含 ASCII 标点 (._-\s+|/) + 中文顿号、空格、加号等
_SEP = r'[._\-\s&+|/、　,，]'


def _normalize(raw: str) -> Optional[str]:
    """单个原始 lang 字符串 → 内部代码；无法识别返回 None。"""
    if not raw:
        return None
    return _CODE_ALIAS.get(str(raw).strip().lower())


def detect_langs_from_filename(name: str) -> List[str]:
    """
    从文件名 / 字符串中识别所有出现过的语言代码，按出现先后排序、去重。
    返回 list（可能为空）；推荐使用 detect_lang_combo() 拿 dot-separated string。
    """
    if not name:
        return []
    n = name.lower()
    found: List[str] = []

    # 1) 显式 lang token：被分隔符包围的英文 code（chs/eng/zh-cn/...）
    #    匹配如 '.chs.' '_eng_' '-zh-cn-' '|big5|' 等
    #
    # 关键：必须用 (?:...) 把 alternation 包起来，否则 `(?:^|SEP)A|B|C(?=SEP|$)`
    # 实际被解析为 `(?:^|SEP)A` OR `B` OR `C(?=SEP|$)` —— 边界检查只对第一个分支生效，
    # 后果：'eng' 会误中 'wan-feng-' 这种串里的子序列 'eng'（实测 bug 来源）。
    code_pattern = '|'.join(sorted(_CODE_ALIAS.keys(), key=len, reverse=True))
    for m in re.finditer(rf'(?:^|{_SEP})(?:{code_pattern})(?={_SEP}|$)', n):
        token = re.sub(rf'^{_SEP}', '', m.group(0))
        code = _normalize(token)
        if code and code not in found:
            found.append(code)

    # 2) 双字中文关键字（按出现位置排序）
    double_hits: List[tuple] = []
    for kw, code in _DOUBLE_KEYWORDS:
        idx = name.find(kw)
        if idx >= 0:
            double_hits.append((idx, code))
    double_hits.sort()
    for _, code in double_hits:
        if code not in found:
            found.append(code)

    # 3) 单字 / 连写中文 lang 字符序列：'简' / '简英' / '简繁英' 等
    #    要求该序列前后是分隔符或字符串边界（避免 '日本' / '英雄' 等误命中）
    for m in re.finditer(r'[简繁英日韩]+', name):
        seq, start, end = m.group(0), m.start(), m.end()
        before_ok = (start == 0) or re.match(_SEP, name[start - 1])
        after_ok = (end == len(name)) or re.match(_SEP, name[end])
        if not (before_ok and after_ok):
            continue
        for ch in seq:
            code = _CHAR_TO_CODE_MAP[ch]
            if code not in found:
                found.append(code)

    return found


def detect_lang_combo(name: str) -> Optional[str]:
    """
    从文件名 / 文本里识别语言，返回 dot-separated 复合 code 或 None。

      'movie.简体&英文.srt'    → 'chs.eng'
      'movie.chs&eng.ass'     → 'chs.eng'
      'movie.简&英.srt'        → 'chs.eng'
      'movie.chs.eng.srt'     → 'chs.eng'
      'movie.简体.srt'         → 'chs'
      'punches.srt'           → None（边界保护，不误中 chs）
    """
    found = detect_langs_from_filename(name)
    return '.'.join(found) if found else None


def expand_zh_coverage(parts: set) -> set:
    """
    扩展"通用中文 zh"的覆盖语义：file 标了 'zh' 就同时算覆盖了 'chs' 和 'cht'。
    场景：字幕只写"中文"没分简繁 / 内容嗅探嗅不出简繁 → 不该让用户既缺简又缺繁。

    返回新 set，不动入参。
    """
    if 'zh' in parts:
        return parts | {'chs', 'cht'}
    return parts


def lang_covers(file_lang: Optional[str], required_lang: str) -> bool:
    """
    判断 file_lang 是否"覆盖"了 required_lang。

    覆盖语义：required 中的所有 token 都在 file 中出现。
      - 'chs.eng' 覆盖 'chs'（双语包含简体）✓
      - 'chs.eng' 覆盖 'eng'（双语包含英文）✓
      - 'chs.eng' 覆盖 'chs.eng'（完全相等）✓
      - 'chs'     覆盖 'chs.eng' ✗（单语不能满足双语需求）
      - 'cht.eng' 覆盖 'chs' ✗
      - 'zh'      覆盖 'chs' / 'cht'（通用中文，未指定简繁 → 两边都算）✓
    """
    if not file_lang or not required_lang:
        return False
    file_parts = expand_zh_coverage(set(file_lang.split('.')))
    req_parts = set(required_lang.split('.'))
    return req_parts.issubset(file_parts)


def filter_missing_langs(
    file_langs: List[str],
    required_langs: List[str],
) -> List[str]:
    """
    给定文件已有的语言（每个文件可能是单语或多语），返回 required 中尚缺的项。

    例：
      file_langs=['chs.eng']         required=['chs','eng'] → []        # 双语全覆盖
      file_langs=['chs','eng']       required=['chs.eng']  → []        # 多文件凑出双语
        ↑ 注：required='chs.eng' 表示"要双语包"，多文件单语凑不出双语包效果，
           严格语义下应仍缺。这里采用"宽松"语义：只要每个 token 都被某文件覆盖即算满足。
      file_langs=['cht.eng']         required=['chs','eng'] → ['chs']  # 缺简体
      file_langs=['chs']             required=['chs','eng'] → ['eng']  # 缺英文
      file_langs=[]                  required=['chs']      → ['chs']
    """
    if not required_langs:
        return []
    # 把所有 file_langs 的 token 合并成一个大集合
    covered_tokens: set = set()
    for fl in file_langs or []:
        if fl:
            covered_tokens.update(fl.split('.'))
    # 通用中文 'zh' 同时覆盖 'chs' 和 'cht'（未指定简繁也算两边都命中）
    covered_tokens = expand_zh_coverage(covered_tokens)
    missing: List[str] = []
    for req in required_langs:
        req_tokens = set(req.split('.'))
        if not req_tokens.issubset(covered_tokens):
            missing.append(req)
    return missing


def lang_match_score(
    file_lang: Optional[str],
    preferred_langs: List[str],
) -> tuple:
    """
    给候选字幕打分用作 sort key（**越小越优**）。返回 tuple：
        (neg_covered, first_hit_index)

    - **覆盖**（核心语义）：file_lang 覆盖了 preferred[i]（preferred[i] 的所有
      token 都在 file_lang 里）→ 算命中第 i 项。
      - 'chs.eng' 覆盖 'chs'/'eng'/'chs.eng'，所以 preferred=['chs','eng'] 时
        'chs.eng' 命中两项；preferred=['chs.eng','chs','eng'] 时三项都命中。
    - **neg_covered**：被覆盖的 preferred 项数（取负），覆盖越多越优。
    - **first_hit_index**：首个被覆盖的 preferred 索引，作为同覆盖度下的次序。

    示例 preferred=['chs','eng']：
      file 'chs.eng' → (-2, 0)   ← 一份覆盖两项需求，最优
      file 'chs'     → (-1, 0)
      file 'eng'     → (-1, 1)
      file 'cht.eng' → (-1, 1)   ← 仅覆盖 eng
      file 'cht'     → (1, 9999) ← 完全没覆盖；用部分交集兜底（这里 cht 也不沾边）

    示例 preferred=['chs.eng','chs','eng']：
      file 'chs.eng' → (-3, 0)   ← 三项全覆盖
      file 'chs'     → (-1, 1)
      file 'eng'     → (-1, 2)
      file 'cht.eng' → (-1, 2)   ← 仅覆盖 eng

    无任何覆盖时退到"部分交集"兜底（共享至少一个 token）：
      返回 (1, n + first_intersect_idx)。完全无关返回 (1, 9999)。
    """
    if not file_lang or not preferred_langs:
        return (1, 9999)
    n = len(preferred_langs)
    file_parts = set(file_lang.split('.'))

    covered = 0
    first_hit = n  # 哨兵：未命中
    for i, p in enumerate(preferred_langs):
        if set(p.split('.')).issubset(file_parts):
            covered += 1
            if i < first_hit:
                first_hit = i
    if covered > 0:
        return (-covered, first_hit)

    # 没有完全覆盖任何 preferred → 退到部分交集
    for i, p in enumerate(preferred_langs):
        if set(p.split('.')) & file_parts:
            return (1, n + i)
    return (1, 9999)


def lang_match_rank(file_lang: Optional[str], preferred_langs: List[str]) -> int:
    """
    `lang_match_score` 的 int 编码版本，方便当作单值排序键。
    数值无业务含义，只保证"越小越优"且与 lang_match_score 同序。
    """
    neg_cov, first = lang_match_score(file_lang, preferred_langs)
    n = max(len(preferred_langs), 1)
    # neg_cov ∈ [-n, 1]；first ∈ [0, 9999]
    # 编码：(neg_cov + n) * 10000 + first
    return (neg_cov + n) * 10000 + first


# 向后兼容别名 —— 旧代码 `from common.lang_utils import guess_lang_from_filename`
guess_lang_from_filename = detect_lang_combo
