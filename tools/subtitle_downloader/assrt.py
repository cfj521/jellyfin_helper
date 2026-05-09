"""
射手字幕 assrt.net API 客户端

文档: https://secure.assrt.net/api/doc

要点：
- Base URL: https://api.assrt.net （备 api.makedie.me）
- 限频 20/min（按 token + IP 共享）
- 响应外层包壳：{ "status": 0, "sub": { "result": "succeed", "subs": [...] } }
- detail 返回的 url 是临时签名，**不能缓存**，每次下载现取
"""
import io
import re
import time
import zipfile
import logging
import threading
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


# 语言代码归一化：assrt 的 lang.langlist 字段值 → 项目内部代码
# 常见原始值：chs, cht, eng, jpn, kor, rus, spa, fre, ger, jap, ja, zh, ...
# 也常见组合 "chs,eng"（双语字幕）
_LANG_NORMALIZE = {
    'chs': 'chs', 'gb': 'chs', 'simplified': 'chs', 'zh': 'chs', 'zh-cn': 'chs', 'zh_cn': 'chs',
    'cht': 'cht', 'big5': 'cht', 'traditional': 'cht', 'zh-tw': 'cht', 'zh_tw': 'cht',
    'eng': 'eng', 'en': 'eng', 'english': 'eng',
    'jpn': 'jpn', 'jap': 'jpn', 'ja': 'jpn', 'japanese': 'jpn',
    'kor': 'kor', 'ko': 'kor', 'korean': 'kor',
}

# 字幕文件扩展名（按优先级；前面的更优）
SUB_EXTENSIONS = ('.ass', '.ssa', '.srt', '.sub', '.vtt', '.idx')


def normalize_lang(raw: str) -> Optional[str]:
    """单个语言原始值 → 内部代码；无法识别返回 None。"""
    if not raw:
        return None
    return _LANG_NORMALIZE.get(str(raw).strip().lower())


def parse_langlist(langlist: Any, lang_desc: str = '') -> List[str]:
    """
    解析 sub 对象的 lang 字段。assrt 实测返回形态多样：
      - {'langlist': {'langchs': True, 'langeng': True}, 'desc': '简体&英文'}
      - {'langlist': ['chs', 'eng'], 'desc': '...'}
      - 'chs,eng'（极少数老数据）
    desc 兜底：用中文描述匹配。
    """
    out: List[str] = []
    if isinstance(langlist, dict):
        # 形态：{'langchs': True, 'langeng': True}
        for k, v in langlist.items():
            if not v:
                continue
            key = k.lower().removeprefix('lang')
            code = normalize_lang(key)
            if code and code not in out:
                out.append(code)
    elif isinstance(langlist, (list, tuple)):
        for item in langlist:
            code = normalize_lang(item)
            if code and code not in out:
                out.append(code)
    elif isinstance(langlist, str):
        for piece in re.split(r'[,\s/|;]+', langlist):
            code = normalize_lang(piece)
            if code and code not in out:
                out.append(code)

    # desc 中文兜底（常见："简体&英文" / "简体中文" / "繁体" 等）
    if not out and lang_desc:
        d = lang_desc
        if '简' in d or 'GB' in d.upper():
            out.append('chs')
        if '繁' in d or 'BIG5' in d.upper():
            out.append('cht')
        if '英' in d or 'ENG' in d.upper():
            out.append('eng')
        if '日' in d:
            out.append('jpn')
        if '韩' in d or '朝' in d:
            out.append('kor')
    return out


class AssrtError(Exception):
    """assrt API 业务错误（API 返回 status != 0）"""
    def __init__(self, code: int, message: str):
        super().__init__(f"[assrt {code}] {message}")
        self.code = code
        self.message = message


class AssrtRateLimitError(AssrtError):
    """限频（30900）"""


class AssrtClient:
    """
    assrt.net API 客户端。

    自带极简串行限频：每次请求间隔 >= request_delay 秒。
    多线程共享一个实例时也能保证不超过 20/min。
    """
    BASE_URL = "https://api.assrt.net"
    DEFAULT_TIMEOUT = 15

    def __init__(self, token: str, request_delay: float = 3.0, base_url: Optional[str] = None):
        if not token:
            raise ValueError("缺少 assrt API token")
        self.token = token
        self.request_delay = max(0.0, float(request_delay))
        self.base_url = (base_url or self.BASE_URL).rstrip('/')
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'JellyfinTools/1.0'
        self._last_call = 0.0
        self._lock = threading.Lock()

    # ---- 内部 ----

    def _wait_quota(self):
        """两次请求间最小间隔。"""
        with self._lock:
            elapsed = time.monotonic() - self._last_call
            if elapsed < self.request_delay:
                time.sleep(self.request_delay - elapsed)
            self._last_call = time.monotonic()

    def _request(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        params = dict(params or {})
        params['token'] = self.token
        url = f"{self.base_url}{path}"

        self._wait_quota()
        try:
            resp = self.session.get(url, params=params, timeout=self.DEFAULT_TIMEOUT)
        except requests.RequestException as e:
            raise AssrtError(-1, f"网络错误: {e}")

        # 限频用 5xx 表达，专门解析
        if resp.status_code >= 500:
            try:
                data = resp.json()
                code = int(data.get('status', resp.status_code))
                msg = data.get('sub', {}).get('result') or data.get('errmsg') or resp.text[:200]
            except Exception:
                code, msg = resp.status_code, resp.text[:200]
            if code == 30900:
                raise AssrtRateLimitError(30900, '配额超限（20/min），请稍后重试')
            raise AssrtError(code, f"服务端错误: {msg}")

        if resp.status_code >= 400:
            try:
                data = resp.json()
                code = int(data.get('status', resp.status_code))
                msg = data.get('sub', {}).get('result') or data.get('errmsg') or resp.text[:200]
            except Exception:
                code, msg = resp.status_code, resp.text[:200]
            raise AssrtError(code, f"请求错误: {msg}")

        try:
            data = resp.json()
        except ValueError:
            raise AssrtError(-1, f"非 JSON 响应: {resp.text[:200]}")

        status = int(data.get('status', 0))
        if status != 0:
            sub_block = data.get('sub') or {}
            msg = sub_block.get('result') or data.get('errmsg') or 'unknown'
            if status == 30900:
                raise AssrtRateLimitError(status, msg)
            raise AssrtError(status, msg)
        return data

    # ---- API endpoints ----

    def search(self, query: str, *, cnt: int = 15, pos: int = 0,
               is_file: bool = False, no_muxer: bool = False,
               with_filelist: bool = False) -> List[Dict[str, Any]]:
        """搜索字幕；返回 sub.subs 列表（不含 url，要拿下载链接调 detail）。"""
        if not query or len(query) < 3:
            raise ValueError("query 长度需 >= 3 字符")
        params = {
            'q': query,
            'cnt': max(1, min(15, cnt)),
            'pos': pos,
        }
        if is_file:
            params['is_file'] = 1
        if no_muxer:
            params['no_muxer'] = 1
        if with_filelist:
            params['filelist'] = 1
        data = self._request('/v1/sub/search', params)
        return (data.get('sub') or {}).get('subs') or []

    def detail(self, sub_id: int) -> Optional[Dict[str, Any]]:
        """取字幕详情（含临时下载 url）。返回单个 sub 对象。"""
        data = self._request('/v1/sub/detail', {'id': int(sub_id)})
        subs = (data.get('sub') or {}).get('subs') or []
        return subs[0] if subs else None

    def similar(self, sub_id: int) -> List[Dict[str, Any]]:
        data = self._request('/v1/sub/similar', {'id': int(sub_id)})
        return (data.get('sub') or {}).get('subs') or []

    def quota(self) -> Dict[str, Any]:
        data = self._request('/v1/user/quota', {})
        return data.get('user') or {}

    # ---- 高级：下载 + 解压 + 落盘 ----

    def fetch_archive(self, url: str) -> Tuple[bytes, str]:
        """直接 HTTP GET 下载链接；返回 (字节, 推断扩展名)。"""
        # 注意：下载 url 是 assrt 临时签名，不需要 token；但走 session 复用连接
        try:
            resp = self.session.get(url, timeout=60, stream=True)
            resp.raise_for_status()
            content = resp.content
        except requests.RequestException as e:
            raise AssrtError(-1, f"下载失败: {e}")

        # 推断扩展名：先看 content-type，再看 url 路径
        ctype = (resp.headers.get('Content-Type') or '').lower()
        path = urlparse(url).path.lower()
        ext = ''
        for guess in ('.zip', '.rar', '.7z', '.srt', '.ass', '.ssa', '.sub', '.vtt'):
            if path.endswith(guess):
                ext = guess
                break
        if not ext:
            if 'zip' in ctype:
                ext = '.zip'
            elif 'rar' in ctype:
                ext = '.rar'
            elif '7z' in ctype:
                ext = '.7z'
            elif 'subrip' in ctype or 'plain' in ctype:
                ext = '.srt'
        return content, ext


def _decode_zip_filename(name_bytes_or_str: Any, is_utf8_flag: bool) -> str:
    """
    zip 里中文文件名常见两种：
      1) UTF-8（通用位标志位 11 = 1）
      2) GBK（Windows 中文压缩软件默认；标志位 = 0，python 误用 cp437 解析变乱码）
    用 cp437 → bytes → gbk 的方式还原。
    """
    if isinstance(name_bytes_or_str, bytes):
        raw = name_bytes_or_str
    else:
        # zipfile 已经把 raw bytes 用 cp437 decode 过了
        if is_utf8_flag:
            return name_bytes_or_str
        try:
            raw = name_bytes_or_str.encode('cp437')
        except Exception:
            return name_bytes_or_str
    for enc in ('utf-8', 'gbk', 'gb18030', 'big5'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace')


def extract_subtitles_from_archive(content: bytes, ext: str) -> List[Tuple[str, bytes]]:
    """
    从压缩包里抽出所有字幕文件，返回 [(原始文件名, 字节内容)]。
    目前只支持 zip；其他格式（rar/7z）返回空列表，调用方自行兜底。
    """
    out: List[Tuple[str, bytes]] = []
    if ext == '.zip' or content[:4] == b'PK\x03\x04':
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    is_utf8 = bool(info.flag_bits & 0x800)
                    real_name = _decode_zip_filename(info.filename, is_utf8)
                    if not real_name.lower().endswith(SUB_EXTENSIONS):
                        continue
                    try:
                        data = zf.read(info)
                    except Exception as e:
                        logger.warning(f"读取 zip 内 {real_name} 失败: {e}")
                        continue
                    out.append((real_name, data))
        except zipfile.BadZipFile as e:
            logger.warning(f"zip 解析失败: {e}")
    return out


# 文件名 → 语言代码：统一走 common.lang_utils（多源共用，支持双语）
from common.lang_utils import (
    detect_lang_combo as _detect_lang_combo,
    lang_match_score as _lang_match_score,
)


def guess_lang_from_filename(name: str) -> Optional[str]:
    """
    从字幕文件名猜语言（兼容性别名 → common.lang_utils.detect_lang_combo）。

    支持双语 / 多语，返回 dot-separated 复合 code：
      'movie.chs.eng.srt'      → 'chs.eng'
      'movie.简体&英文.srt'     → 'chs.eng'
      'movie.简&英.srt'         → 'chs.eng'
      'movie.繁体.ass'          → 'cht'
    无法识别返回 None。
    """
    return _detect_lang_combo(name)


def _build_format_score(preferred_formats: Optional[List[str]]) -> Dict[str, int]:
    """
    把 ['ass','srt','sup'] 这种格式偏好列表转成 ext → score 字典（数值越大越优）。

    'ass' 和 'ssa' 视为同一类（ASS 是 SSA v4+），同分。
    无配置时返回项目默认偏好。
    """
    if not preferred_formats:
        preferred_formats = ['ass', 'srt', 'sup']
    n = len(preferred_formats)
    out: Dict[str, int] = {}
    for i, fmt in enumerate(preferred_formats):
        ext = '.' + str(fmt).lower().lstrip('.')
        score = n - i
        out[ext] = score
        # ass 和 ssa 是同一族，给同分
        if ext == '.ass':
            out['.ssa'] = score
        elif ext == '.ssa':
            out['.ass'] = score
    return out


def _extract_episode(name: str) -> Optional[Tuple[Optional[int], int]]:
    """
    从字幕 / 视频文件名提取 (season, episode)；只有集号时返回 (None, ep)；都没匹配返回 None。
    支持 SxxExx / xxExx / Sxx.Exx / -xx- / 第xx话 / Exx 等常见格式。
    """
    if not name:
        return None
    for pat in (r'[Ss](\d{1,2})[\s._-]?[Ee](\d{1,3})', r'(\d{1,2})[xX](\d{1,3})'):
        m = re.search(pat, name)
        if m:
            return (int(m.group(1)), int(m.group(2)))
    for pat in (r'[Ee][Pp]?(\d{1,3})', r'第(\d{1,3})[集话話]', r'[\s._\-\[]([0-9]{2,3})[\s._\-\]\(]'):
        m = re.search(pat, name)
        if m:
            return (None, int(m.group(1)))
    return None


def _episode_match(a: Optional[Tuple[Optional[int], int]],
                   b: Optional[Tuple[Optional[int], int]]) -> bool:
    """两个集号是否匹配。任一为 None 视为不匹配；缺 season 时只比 episode。"""
    if not a or not b:
        return False
    sa, ea = a
    sb, eb = b
    if ea != eb:
        return False
    if sa is not None and sb is not None and sa != sb:
        return False
    return True


def pick_best_subtitle(
    files: List[Tuple[str, bytes]],
    preferred_langs: List[str],
    preferred_formats: Optional[List[str]] = None,
    fallback_lang: Optional[str] = None,
    video_filename: Optional[str] = None,
) -> Optional[Tuple[str, bytes, str]]:
    """
    从一堆候选字幕里挑最匹配的。
    返回 (filename, content, lang_code) 或 None。lang_code 可能是复合（如 'chs.eng'）。

    打分（按 sort key tuple，越小越优）：
      1. **集数匹配**：video_filename 给定 + 文件名能提集号 → 不匹配的排到最后。
         避免"包内整季 S08E01-E06 时搜 E06 拿到 E01"这种坑。
      2. **lang_match_score**：双语包覆盖更多 preferred 项 → 排前
         例：preferred_langs=['chs','eng'] 时 'chs.eng' 文件覆盖 2 项 → 优先于单语
      3. **preferred_formats**：用户配置的格式偏好（默认 ass > srt > sup）
      4. 文件名越短越优先（避免选到 NFO 之类）

    fallback_lang：当**文件名识别不到 lang token**（如裸 release 名 'movie.S08E06.ass'）时，
      退到这个值（通常是 assrt 的 sub.lang.desc 解析出的包级语言）。
      没给 fallback 就当作"无语言信息"参与打分（rank=999，但同包文件并列时仍可比较 ext/集数）。

    preferred_formats=None 时回退到项目默认 ['ass','srt','sup']。
    """
    if not files:
        return None
    ext_score = _build_format_score(preferred_formats)

    video_ep = _extract_episode(video_filename) if video_filename else None

    def resolve_lang(name: str) -> Optional[str]:
        """文件名 → lang。识别不出退到 fallback_lang。"""
        return _detect_lang_combo(name) or fallback_lang

    def score(item):
        name, _ = item
        # 1) episode 匹配优先：包内有多集时，挑跟视频集号一致的
        if video_ep is not None:
            sub_ep = _extract_episode(name)
            ep_mismatch = 1 if (sub_ep is not None and not _episode_match(video_ep, sub_ep)) else 0
        else:
            ep_mismatch = 0
        # 2) lang 匹配
        lang = resolve_lang(name)
        lang_key = _lang_match_score(lang, preferred_langs)
        # 3) 格式偏好
        ext = Path(name).suffix.lower()
        return (ep_mismatch, lang_key, -ext_score.get(ext, 0), len(name))

    files_sorted = sorted(files, key=score)
    best_name, best_data = files_sorted[0]
    best_lang = resolve_lang(best_name) or (preferred_langs[0] if preferred_langs else 'chs')
    return best_name, best_data, best_lang


def pick_one_per_lang(
    files: List[Tuple[str, bytes]],
    preferred_langs: List[str],
    preferred_formats: Optional[List[str]] = None,
) -> List[Tuple[str, bytes, str]]:
    """
    按"语言去重"挑选：每个识别出的语言（含双语 combo）只保留按 preferred_formats
    排名最高的一份。返回 [(filename, content, lang_code), ...]。

    用于 assrt 包"顺手落其他语言"的场景：避免同一语言把 ass + srt 都落地（用户配置
    `preferred_formats=['ass','srt','sup']` 时，只下 ass）。

    过滤规则：
      - 识别不到 lang 的文件丢弃
      - lang 完全不沾 preferred_langs 的丢弃（例：preferred=['chs','eng'] 时 cht 单语丢）
    """
    if not files:
        return []
    ext_score = _build_format_score(preferred_formats)

    # 按 lang 分桶
    by_lang: Dict[str, List[Tuple[str, bytes]]] = {}
    for fname, fdata in files:
        lang = _detect_lang_combo(fname)
        if not lang:
            continue
        # lang 必须与 preferred_langs 有交集（否则不下载）
        score = _lang_match_score(lang, preferred_langs)
        if score[0] >= 1:  # neg_covered=1 表示无任何覆盖（兜底交集也不沾）
            continue
        by_lang.setdefault(lang, []).append((fname, fdata))

    # 每桶按 format 排序取第一
    out: List[Tuple[str, bytes, str]] = []
    for lang, group in by_lang.items():
        group.sort(key=lambda it: (-ext_score.get(Path(it[0]).suffix.lower(), 0), len(it[0])))
        fname, fdata = group[0]
        out.append((fname, fdata, lang))
    return out
