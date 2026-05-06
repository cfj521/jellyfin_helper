"""
Jellyfin 库条目健康检查
检测 Jellyfin 自动识别可能出错的条目，便于用户清理。

当前实现的诊断类别：
  - unrecognized   : type=Folder（Jellyfin 没识别成 Movie/Series）
  - short_runtime  : Movie 时长 < 15 分钟（疑似 sample/trailer）；Episode < 5 分钟
  - sample_path    : Path 含 sample / trailer / extras 关键字
  - name_mismatch  : 释放文件夹名 与 Jellyfin 匹配的 Name 差异过大
  - year_mismatch  : 文件夹名带的年份与 ProductionYear 相差 > 1 年
  - empty_series   : Series 的 ChildCount = 0
  - empty_season   : Season 的 ChildCount = 0
"""
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Dict, List, Optional, Tuple


# 释放命名里要剥离的画质/编码/源标签
_RELEASE_TAGS = {
    # 分辨率
    '1080p', '2160p', '720p', '480p', '4k', '8k', 'uhd', 'hd', 'sd',
    # 编码（无点形式；带点的 H.264 / H.265 在切词前处理）
    'x264', 'x265', 'h264', 'h265', 'hevc', 'avc', 'av1', 'xvid', 'divx',
    # 色彩 / HDR
    'hdr', 'hdr10', 'hdr10plus', 'dv', 'dovi', 'dolby', 'vision', 'sdr', '10bit', '8bit',
    # 来源 / 流媒体平台
    'bluray', 'blu', 'ray', 'web', 'webrip', 'webdl', 'bdrip', 'brrip',
    'hdrip', 'dvdrip', 'remux', 'hdtv', 'pdtv',
    'ma', 'amzn', 'nf', 'hmax', 'dsnp', 'atvp', 'hulu', 'mubi',  # 流媒体平台缩写
    # 音频
    'dts', 'dtshd', 'dtsma', 'ac3', 'aac', 'eac3', 'truehd', 'atmos',
    'flac', 'mp3', 'opus', 'dd', 'ddp',
    # 版本
    'extended', 'directors', 'cut', 'unrated', 'theatrical', 'imax',
    'edition', 'version',
    'proper', 'repack', 'internal', 'limited', 'final',
    # 语言代码（跟 release 命名约定相符的）
    'eng', 'chs', 'cht', 'chi', 'zho', 'jpn', 'kor', 'fre', 'fra', 'ger', 'spa',
    'ita', 'por', 'rus', 'ara', 'tha', 'vie',
    # 其它常见
    'multi', 'multilang', 'sub', 'subs', 'dubbed',
}


def _basename(path: str) -> str:
    """从 path 提取最后一段（兼容 Windows / Posix 分隔符）。"""
    if not path:
        return ''
    # 同时处理两种分隔符（Jellyfin 在 Windows 上可能存反斜杠）
    cleaned = path.replace('\\', '/').rstrip('/')
    return cleaned.rsplit('/', 1)[-1] if '/' in cleaned else cleaned


def _parent_name(path: str) -> str:
    """提取父目录名（适合 Movie/Episode 拿 release folder 名）。"""
    if not path:
        return ''
    cleaned = path.replace('\\', '/').rstrip('/')
    parts = cleaned.split('/')
    if len(parts) < 2:
        return ''
    return parts[-2]


def _looks_like_file(name: str) -> bool:
    """判断名字是否是文件（含扩展名）。"""
    if not name or '/' in name:
        return False
    last_dot = name.rfind('.')
    if last_dot < 1:
        return False
    ext = name[last_dot + 1:].lower()
    # 简单判断：扩展名 1–5 字符且全字母数字
    return 1 <= len(ext) <= 5 and ext.isalnum()


def _release_names_for(item: Dict) -> List[str]:
    """
    候选 release 名列表，按优先级排序：
      1) 视频文件名（去扩展名）—— 通常带最完整的 release 信息
      2) 父目录名 —— 兜底，文件夹常被用户改名
      3) 自身名（Folder 类型，没有 file 概念）

    比对时对每个候选都算一次相似度取最大值，避免任一方"被改坏"导致误报。
    """
    path = item.get('Path') or ''
    if not path:
        return []
    base = _basename(path)
    out: List[str] = []
    if _looks_like_file(base):
        # 视频文件：先文件名（去扩展名）
        stem = base.rsplit('.', 1)[0]
        out.append(stem)
        # 再加父目录名（如果跟文件名不一样的话）
        parent = _parent_name(path)
        if parent and parent != stem:
            out.append(parent)
    else:
        # 目录类型：用自身
        out.append(base)
    return out


def _release_name_for(item: Dict) -> str:
    """向后兼容：返回首选的 release 名（用于 issue label 显示等）。"""
    names = _release_names_for(item)
    return names[0] if names else ''


# Title Case 时不大写化的英文虚词（仅词中位置；首词永远大写）
_TITLE_LOWERCASE_WORDS = {
    'a', 'an', 'the', 'and', 'or', 'but', 'nor', 'of', 'at', 'by', 'for',
    'in', 'on', 'to', 'up', 'as', 'so', 'if', 'is', 'with', 'from',
}


def _to_title_case(tokens: List[str]) -> str:
    """把 token 列表转成英文电影标题风格的字符串。CJK token 原样保留。"""
    if not tokens:
        return ''
    out = []
    for i, t in enumerate(tokens):
        if not t:
            continue
        # 含非 ASCII（CJK 等）→ 不变
        if not all(c.isascii() for c in t):
            out.append(t)
            continue
        if i > 0 and t.lower() in _TITLE_LOWERCASE_WORDS:
            out.append(t.lower())
        else:
            out.append(t.capitalize())
    return ' '.join(out)


def _split_at_year(name: str) -> Tuple[Optional[str], Optional[int]]:
    """
    在 release 名里找**第一个有效年份**，把年份及其之后的部分**整个抛弃**。
    "有效年份" = 该 4 位年份之前有 alphanumeric 内容（即标题非空）。

    设计动机：用户的电影库 99.6% 符合 "Title.YEAR.<release info>" 格式，
    标题在年份左边、release tag（DD+/DoVi/Dual.Audio/任何编码音频规格）全在右边。
    比起维护越来越长的 release tag 列表，截断方案更简洁、对未知 tag 极度宽容。

    支持标题里就含 4 位数字的电影：
      "1917.2019.1080p" → 第一个 1917 前面是空 → 跳过；第二个 2019 前面有 "1917." → 命中
      返回 ("1917", 2019)

      "2001.A.Space.Odyssey.1968.1080p"
      → 第一个 2001 前空 → 跳过；第二个 1968 前有 "2001.A.Space.Odyssey." → 命中
      返回 ("2001 A Space Odyssey", 1968)

    没找到有效年份 → 返回 (None, None)，调用方走 fallback。
    """
    if not name:
        return (None, None)
    year_re = re.compile(r'(?<![0-9])(19|20)\d{2}(?![0-9])')
    pos = 0
    while True:
        m = year_re.search(name, pos)
        if not m:
            return (None, None)
        before = name[:m.start()]
        if any(c.isalnum() for c in before):
            return (_clean_title_part(before), int(m.group()))
        pos = m.end()


def _clean_title_part(s: str) -> str:
    """标题部分清理：剥 [tags] (tags) → 切 token（按 . _ - 标点）→ Title Case。"""
    s = re.sub(r'\[[^\]]*\]', ' ', s)
    s = re.sub(r'\([^)]*\)', ' ', s)
    s = re.sub(r'[._\-]+', ' ', s)
    s = re.sub(r"[!?:,;'\"/：！？，；。、「」『』]", ' ', s)
    tokens = [t for t in s.split() if t]
    return _to_title_case(tokens)


def extract_suggested_title_year(item: Dict) -> Tuple[Optional[str], Optional[int]]:
    """
    从条目的文件名 / 文件夹名里提取"建议的搜索标题"和"年份"。
    用于"重新识别"对话框的预填、自动化匹配等场景。

    候选源：_release_names_for 返回的列表（文件名优先，父目录兜底）。

    主方案：在每个候选名上找第一个有效年份（_split_at_year），截断后即得标题。
              99.6% 实测命中率，不依赖 release tag 列表。

    Fallback：所有候选都没年份时，退回老方案（token 化 + 释放标签过滤）。
              用于"鬼子来了.1080p"这种没年份的命名。
    """
    release_names = _release_names_for(item)
    if not release_names:
        return (None, None)

    # 主方案：截断年份
    for rname in release_names:
        title, year = _split_at_year(rname)
        if title:
            return (title, year)

    # Fallback：没有任何候选包含有效年份
    parsed = []
    for rname in release_names:
        tokens = _normalize_tokens(rname)
        if not tokens:
            continue
        parsed.append((rname, tokens, None))
    if not parsed:
        return (None, None)
    parsed.sort(key=lambda x: -len(x[1]))
    return (_to_title_case(parsed[0][1]), None)


def _normalize_tokens(name: str) -> List[str]:
    """
    把 release 名标准化成单词集合：
      - 小写
      - 去 [tags] 和 (tags)
      - 去 release group 后缀（结尾的 -GROUPNAME）
      - 切词前先吞掉"组合型释放标签"（音频声道、H.264/H.265、WEB-DL）
        防止拆完后剩 'h' '265' 'dl' 'ddp5' '1' 这种零碎垃圾混进来
      - 把 . _ - 和常见标点都当分隔符
      - 过滤释放标签和年份
    """
    if not name:
        return []
    s = name.lower()
    s = re.sub(r'\[[^\]]*\]', ' ', s)
    s = re.sub(r'\([^)]*\)', ' ', s)
    # 释放组名通常是结尾的 "-XXXX"（如 -RARBG / -HONE / -DEFLATE），先剥掉
    s = re.sub(r'-[a-z0-9_]{2,}\s*$', '', s)

    # === 切词前消化"组合型"释放模式（顺序很重要）===
    # 1. 编码 H.264 / H.265 / X.264 / X.265 / H264 / H265
    s = re.sub(r'\b[hx]\.?\d{3}\b', ' ', s)
    # 2. 音频编解码 + "+" 形式（DD+, DDP+, EAC3+, TrueHD+）
    #    必须在 audio channel 之前 —— 这样 "DD+.5.1" 先去掉 "DD+"，再去掉 "5.1"
    s = re.sub(r'\b(?:dd|ddp|eac3|truehd)\+', ' ', s)
    # 3. 音频声道布局（含可选的编解码前缀）：
    #    DDP5.1 / DD5.1 / DTS5.1 / TrueHD7.1 / Atmos7.1 / AAC2.0 / 5.1 / 7.1 / 2.0
    s = re.sub(
        r'\b(?:dd[p]?|dts(?:hd)?(?:ma)?|truehd|atmos|aac|ac3|eac3|flac|opus|mp3)?\d\.\d\b',
        ' ', s,
    )
    # 4. WEB-DL / WEB.DL / WEBRIP / WEB-RIP（防止拆出零散的 'dl' / 'rip'）
    s = re.sub(r'\bweb[\.\-]?(?:dl|rip)\b', ' ', s)
    # 5. 多音轨 / 多语言 组合标识
    #    Dual.Audio / Dual_Audio / Multi.Audio / Multi-Lang
    s = re.sub(r'\b(?:dual|multi)[._\-\s]+(?:audio|lang)\b', ' ', s)

    # 把 . _ - 当分隔符
    s = re.sub(r'[._\-]+', ' ', s)
    # 标点（半角 + 全角）也按分隔符处理：冒号/感叹号/问号/逗号/分号等
    s = re.sub(r"[!?:,;'\"/：！？，；。、「」『』]", ' ', s)
    raw_tokens = s.split()
    out = []
    for t in raw_tokens:
        if not t or t in _RELEASE_TAGS:
            continue
        # 去掉年份样式（4 位数字、19xx/20xx）—— 年份单独处理
        if re.fullmatch(r'(?:19|20)\d{2}', t):
            continue
        # 不再过滤短数字 —— "28"/"12"/"9"/"300" 都可能是合法标题词
        out.append(t)
    return out


def _is_latin_dominant(s: str) -> bool:
    """字符串里 ASCII 字母占字母总数 > 50% 即认为是拉丁文为主。"""
    if not s:
        return False
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return False
    latin = sum(1 for c in letters if c.isascii())
    return latin / len(letters) > 0.5


def _looks_like_release_dir(name: str) -> bool:
    """
    判断目录名是否带 release 标签 —— 即"看起来像 release 文件夹"。
    判据：含年份 (19xx/20xx) 或常见画质/源/编码标签。
    """
    if not name:
        return False
    lower = name.lower()
    # 年份是最强的 release 信号
    if re.search(r'(?<![0-9])(?:19|20)\d{2}(?![0-9])', lower):
        return True
    # 释放标签子串
    release_signals = (
        '1080p', '2160p', '720p', '480p', '4k', 'uhd',
        'x264', 'x265', 'h264', 'h265', 'h.264', 'h.265', 'hevc', 'avc', 'av1',
        'bluray', 'web-dl', 'webdl', 'webrip', 'bdrip', 'brrip', 'hdrip',
        'remux', 'hdtv', 'hdr', 'hdr10', 'dolby',
    )
    return any(tag in lower for tag in release_signals)


def _extract_year(name: str) -> Optional[int]:
    """从名字里抓 4 位年份（首选 (YYYY) 格式，其次 .YYYY.）。"""
    if not name:
        return None
    # 加边界避免误匹配 1080p 之类
    m = re.search(r'(?<![0-9])((?:19|20)\d{2})(?![0-9])', name)
    return int(m.group(1)) if m else None


def _jaccard(a: List[str], b: List[str]) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    union = sa | sb
    if not union:
        return 0.0
    return len(sa & sb) / len(union)


def _similarity(folder_tokens: List[str], title_tokens: List[str]) -> float:
    """
    标题/文件夹相似度。综合三个指标取最大值：

      Jaccard               = |F ∩ T| / |F ∪ T|   （对称，看整体重叠）
      Containment(标题)     = |F ∩ T| / |T|       （非对称，看标题是否落在文件夹里）
      Concat-equal fallback = 标题 token 拼接后能匹配文件夹某个 token  （处理"Ne Zha" 对应 "nezha"）

    单 token 标题（Joker/Inception）：靠 Containment 不被释放噪音稀释
    分词差异（"Ne Zha" / "Iron Man" / "King Kong"）：靠 Concat-equal fallback
    误报场景下三者都低，识别能力不退化
    """
    if not folder_tokens or not title_tokens:
        return 0.0
    F = set(folder_tokens)
    T = set(title_tokens)
    inter = F & T

    jaccard = len(inter) / len(F | T) if F | T else 0.0
    contain = len(inter) / len(T) if T else 0.0

    # Fallback: 标题 token 拼接后是否等于文件夹的某个 token
    # （Jellyfin 把 "Nezha" 显示成 "Ne Zha"，文件夹是 "Nezha"）
    # 加长度门槛防止 "Anna" 误命中 "Annapurna"
    concat_score = 0.0
    title_concat = ''.join(t.lower() for t in title_tokens)
    if len(title_concat) >= 3:
        for ft in folder_tokens:
            ft_lower = ft.lower()
            # 完全相等 → 满分
            if ft_lower == title_concat:
                concat_score = 1.0
                break
            # 标题包含在文件夹 token 里，但要求长度差不大（防止 "anna" ⊂ "annapurna"）
            if title_concat in ft_lower and len(ft_lower) <= len(title_concat) * 1.25:
                concat_score = max(concat_score, 0.9)
            # 反向：单 token 文件夹包含在多 token 标题拼接里
            elif ft_lower in title_concat and len(title_concat) <= len(ft_lower) * 1.25:
                concat_score = max(concat_score, 0.9)

    return max(jaccard, contain, concat_score)


def compute_health(item: Dict) -> Dict:
    """
    给 Jellyfin 单条 Item 计算健康状况。
    返回:
      {
        'level': 'ok' | 'warning' | 'error',
        'issues': [{'code': str, 'label': str}, ...]
      }
    """
    issues: List[Dict] = []
    item_type = item.get('Type')
    name = item.get('Name') or ''
    path = item.get('Path') or ''
    item_year = item.get('ProductionYear')

    # ---- A. 未识别 ----
    if item_type == 'Folder':
        issues.append({
            'code': 'unrecognized',
            'label': '未被识别为电影/剧集（type=Folder），Jellyfin 没匹配到元数据',
        })

    # ---- B. Sample / Trailer ----
    runtime_ticks = item.get('RunTimeTicks') or 0
    runtime_min = runtime_ticks / 600_000_000.0  # 1 tick = 100ns

    if item_type == 'Movie' and 0 < runtime_min < 15:
        issues.append({
            'code': 'short_runtime',
            'label': f'时长仅 {runtime_min:.1f} 分钟，疑似 sample/trailer（电影通常 ≥60 分钟）',
        })
    elif item_type == 'Episode' and 0 < runtime_min < 5:
        issues.append({
            'code': 'short_runtime',
            'label': f'时长仅 {runtime_min:.1f} 分钟，疑似预告片',
        })

    path_lower = path.lower()
    sample_keywords = ('/sample', '\\sample', '/trailer', '\\trailer', '/extras', '\\extras')
    is_sample_path = any(k in path_lower for k in sample_keywords)
    if is_sample_path:
        issues.append({
            'code': 'sample_path',
            'label': '路径包含 sample/trailer/extras 关键字',
        })

    # ---- D. 名称不匹配 ----
    # sample 路径已经命中时跳过名称比较（父目录就叫 sample，对比无意义）
    if not is_sample_path and item_type in ('Movie', 'Series', 'Folder') and path and name:
        # 候选 release 名：文件名优先，父目录名兜底
        release_names = _release_names_for(item)

        # 同时跟 Name 和 OriginalTitle 比对，取最高相似度
        # —— 解决国际化场景：Jellyfin 显示中文 Name，文件夹用英文释放名
        candidates = [name]
        original_title = item.get('OriginalTitle')
        if original_title and original_title != name:
            candidates.append(original_title)

        # 文字体系不匹配时直接跳过比较：
        # release 名都是拉丁字母（英文释放名），但 Name 和 OriginalTitle 都是 CJK/非拉丁
        # 这种情况无从可靠比对（如《流浪地球》对应 The.Wandering.Earth），不报告
        all_release_latin = all(_is_latin_dominant(r) for r in release_names) if release_names else False
        candidate_has_latin = any(_is_latin_dominant(c) for c in candidates)
        scripts_compatible = not (all_release_latin and not candidate_has_latin)

        if scripts_compatible and release_names:
            best_sim = 0.0
            best_release = release_names[0]
            for rname in release_names:
                folder_tokens = _normalize_tokens(rname)
                if not folder_tokens:
                    continue
                for cand in candidates:
                    cand_tokens = _normalize_tokens(cand)
                    if not cand_tokens:
                        continue
                    sim = _similarity(folder_tokens, cand_tokens)
                    if sim > best_sim:
                        best_sim = sim
                        best_release = rname

            if best_sim < 0.3 and any(_normalize_tokens(c) for c in candidates):
                title_display = (
                    name if len(candidates) == 1
                    else f'{name} / {original_title}'
                )
                issues.append({
                    'code': 'name_mismatch',
                    'label': (
                        f'文件名/夹名 "{best_release}" 与匹配标题 "{title_display}" 差异较大'
                        f'（最高相似度 {best_sim:.0%}）'
                    ),
                })

        # 年份对照（取所有候选中的最大年份信号，兼容文件名带年份但目录不带的情况）
        folder_year = None
        for rname in release_names:
            y = _extract_year(rname)
            if y:
                folder_year = y
                break
        if folder_year and item_year and abs(folder_year - item_year) > 1:
            issues.append({
                'code': 'year_mismatch',
                'label': f'文件夹年份 {folder_year} 与匹配年份 {item_year} 不一致',
            })

    # ---- E. 空 Series ----
    if item_type == 'Series':
        child_count = item.get('ChildCount')
        # ChildCount 可能是 None（没在 Fields 里要）；只在明确为 0 时报警
        if child_count is not None and child_count == 0:
            issues.append({
                'code': 'empty_series',
                'label': 'Series 没有任何子集（ChildCount=0）',
            })

    # ---- E2. 空 Season ----
    if item_type == 'Season':
        child_count = item.get('ChildCount')
        if child_count is not None and child_count == 0:
            issues.append({
                'code': 'empty_season',
                'label': 'Season 没有任何 Episode（ChildCount=0）',
            })

    # ---- F. 主媒体文件不在 release 根目录 ----
    # 期望：/Movies/Movie.2024.1080p/Movie.2024.mkv  （文件在 release 目录下）
    # 异常：/Movies/Movie.2024.1080p/Disc1/movie.mkv（文件嵌套在 release 子目录里）
    # 不再跳过 sample 路径：substring 模式下 "Sample.Movie" 这种合法标题也会被误标为 sample，
    # 强行跳过会丢掉嵌套信号；真 sample 同时拿到两个 flag 也不会伤
    if (
        item_type in ('Movie', 'Episode')
        and path
        and _looks_like_file(_basename(path))  # path 是文件
    ):
        cleaned = path.replace('\\', '/').rstrip('/')
        parts = cleaned.split('/')
        if len(parts) >= 3:
            parent_name = parts[-2]
            grandparent_name = parts[-3]
            parent_is_release = _looks_like_release_dir(parent_name)
            grand_is_release = _looks_like_release_dir(grandparent_name)
            # 父目录不像 release，但祖父像 release —— 主文件应该上移一层
            if not parent_is_release and grand_is_release:
                issues.append({
                    'code': 'nested_main_file',
                    'label': (
                        f'主文件嵌套在子目录 "{parent_name}" 中，'
                        f'实际 release 目录是 "{grandparent_name}"。'
                        '多碟版本属正常；否则建议把主文件挪到 release 根'
                    ),
                })

    if not issues:
        return {'level': 'ok', 'issues': []}

    # error: 强信号（基本可以确定要修） → unrecognized / name_mismatch / year_mismatch
    # warning: 弱信号（值得复核） → short_runtime / sample_path / empty_series / empty_season
    error_codes = {'unrecognized', 'name_mismatch', 'year_mismatch'}
    level = 'error' if any(it['code'] in error_codes for it in issues) else 'warning'
    return {'level': level, 'issues': issues}
