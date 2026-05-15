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
        """直接 HTTP GET 下载链接；返回 (字节, 推断扩展名)。

        推断顺序：content magic bytes (最可靠) → URL 后缀 → content-type
        实测 assrt 的 URL 经常 .zip 后缀但内容是裸 srt，反向也有；
        所以先嗅探 magic 字节，URL/header 只兜底。
        """
        try:
            resp = self.session.get(url, timeout=60, stream=True)
            resp.raise_for_status()
            content = resp.content
        except requests.RequestException as e:
            raise AssrtError(-1, f"下载失败: {e}")

        ext = _sniff_subtitle_ext(content)
        if not ext:
            # magic 嗅不出来 → 退回 URL 后缀（双扩展名 tar.gz 等优先匹配）
            path = urlparse(url).path.lower()
            for guess in (
                '.tar.gz', '.tar.bz2', '.tar.xz',
                '.zip', '.rar', '.7z', '.tar', '.tgz', '.tbz', '.txz', '.gz',
                '.srt', '.ass', '.ssa', '.sub', '.vtt',
            ):
                if path.endswith(guess):
                    ext = guess
                    break
        if not ext:
            # 最后兜底：content-type
            ctype = (resp.headers.get('Content-Type') or '').lower()
            if 'zip' in ctype:
                ext = '.zip'
            elif 'rar' in ctype:
                ext = '.rar'
            elif '7z' in ctype or 'x-7z' in ctype:
                ext = '.7z'
            elif 'gzip' in ctype:
                ext = '.gz'
            elif 'x-tar' in ctype:
                ext = '.tar'
            elif 'subrip' in ctype or 'plain' in ctype:
                ext = '.srt'
        return content, ext


def _sniff_subtitle_ext(content: bytes) -> str:
    """按 magic bytes 嗅探 (字幕 / 压缩包) 文件类型。识别失败返回 ''。"""
    if not content:
        return ''
    head4 = content[:4]
    head6 = content[:6]
    # 压缩包
    if head4 == b'PK\x03\x04':
        return '.zip'
    if content[:4] == b'Rar!' or content[:7] == b'Rar!\x1a\x07\x00':
        return '.rar'
    if head6 == b'7z\xbc\xaf\x27\x1c':
        return '.7z'
    # gzip：1f 8b（可能是 tar.gz 也可能是单文件 .gz；交给 _extract_gz_single / _extract_tar 自己分辨
    # —— 这里返回 .gz；extract 函数会先按 .gz 试，失败再 fallback）
    if head4[:2] == b'\x1f\x8b':
        return '.gz'
    # tar：偏移 257 处的 "ustar" magic（POSIX tar 格式）
    if len(content) > 263 and content[257:262] == b'ustar':
        return '.tar'
    # bzip2：'BZh' magic（裸 .bz2，少见单字幕场景；tar.bz2 走 tarfile）
    if head4[:3] == b'BZh':
        return '.bz2'
    # xz: 'fd 37 7a 58 5a 00' magic
    if head6 == b'\xfd7zXZ\x00':
        return '.xz'
    # 字幕文本（取前 1KB 解码看看；多个编码备选，常见 UTF-8/GBK/UTF-16）
    sample = content[:1024]
    text = None
    for enc in ('utf-8-sig', 'utf-8', 'gbk', 'utf-16'):
        try:
            text = sample.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        return ''
    text_lower = text.lstrip().lower()
    if text_lower.startswith('[script info]') or '[v4+ styles]' in text_lower:
        return '.ass'
    # SRT: 第一条形如 "1\n00:00:..."；用宽松的"行首数字 + 后续时间戳"判
    if re.search(r'^\s*\d+\s*[\r\n]+\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->', text, re.MULTILINE):
        return '.srt'
    if text_lower.startswith('webvtt'):
        return '.vtt'
    # html 错误页：看到 <html / <!DOCTYPE 就放弃，免得当字幕落盘
    if text_lower.startswith('<!doctype') or text_lower.startswith('<html'):
        return ''
    return ''


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

    按格式分派；多种探测信号（magic / ext）任一命中即试：
        zip           → zipfile (stdlib)
        7z            → py7zr (纯 Python) → bsdtar 兜底
        rar           → rarfile (运行时调 bsdtar/unrar) → bsdtar 直接兜底
        tar / tar.gz / tar.bz2 / tar.xz / tar.zst → tarfile (stdlib，自动解压)
        gz (单文件)   → gzip 解压后整体当字幕落

    任一路径失败静默返回 []，让上层（main.py）报"未识别压缩格式"用户级提示。
    """
    if not content:
        return []
    head4 = content[:4]
    head6 = content[:6]

    # zip：magic 'PK\x03\x04' 或 ext
    if ext == '.zip' or head4 == b'PK\x03\x04':
        return _extract_zip(content)

    # 7z：先 py7zr（纯 Python），不行才 bsdtar
    if ext == '.7z' or head6 == b'7z\xbc\xaf\x27\x1c':
        return _extract_7z(content) or _extract_via_bsdtar(content, hint_ext='.7z')

    # rar：rarfile 先试，失败 bsdtar 兜底
    if ext == '.rar' or head4 == b'Rar!':
        return _extract_rar(content) or _extract_via_bsdtar(content, hint_ext='.rar')

    # tar 系列：tarfile 自动识别 gz/bz2/xz 透明解压
    if ext in ('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz', '.tar.xz', '.txz'):
        return _extract_tar(content)
    # 也认 tar magic（"ustar"在偏移 257）—— 但裸 tar 没固定 magic head，
    # 主要场景靠 ext 走（上游 fetch_archive 会按 URL 后缀给出）。

    # 单文件 gzip：内容就是字幕本身
    if ext == '.gz' or head4[:2] == b'\x1f\x8b':
        return _extract_gz_single(content)

    return []


def _extract_zip(content: bytes) -> List[Tuple[str, bytes]]:
    out: List[Tuple[str, bytes]] = []
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


def _extract_7z(content: bytes) -> List[Tuple[str, bytes]]:
    """纯 Python 7z（py7zr）。
    py7zr 1.x 没了 read/readall，只能 extract() 到 tempdir 再读回内存。
    失败返回 []，让 bsdtar 兜底。
    """
    try:
        import py7zr
    except ImportError:
        return []
    import tempfile
    import os
    out: List[Tuple[str, bytes]] = []
    try:
        with py7zr.SevenZipFile(io.BytesIO(content)) as z:
            names = [n for n in z.getnames() if n.lower().endswith(SUB_EXTENSIONS)]
            if not names:
                return []
            with tempfile.TemporaryDirectory(prefix='py7zr_') as td:
                z.extract(path=td, targets=names)
                for name in names:
                    fpath = os.path.join(td, name)
                    if not os.path.isfile(fpath):
                        # py7zr 可能扁平化路径或加前缀，做一次后备搜索
                        for root, _, files in os.walk(td):
                            if os.path.basename(name) in files:
                                fpath = os.path.join(root, os.path.basename(name))
                                break
                    if os.path.isfile(fpath):
                        with open(fpath, 'rb') as f:
                            out.append((name, f.read()))
    except Exception as e:
        logger.warning(f"7z 解析失败（py7zr）: {e}")
        return []
    return out


def _extract_rar(content: bytes) -> List[Tuple[str, bytes]]:
    """rarfile：自身没有解码能力，调 bsdtar/unrar/7z 子进程。失败返回 []。"""
    try:
        import rarfile
    except ImportError:
        return []
    # rarfile 默认找 unrar.exe；Windows 上常没装。这里强制让它用 bsdtar（libarchive，
    # miniconda Library/bin 自带，已经在 PATH 上）。bsdtar 不存在再回退默认搜索。
    import shutil as _shutil
    if _shutil.which('bsdtar') and getattr(rarfile, 'UNRAR_TOOL', 'unrar') == 'unrar':
        rarfile.UNRAR_TOOL = 'bsdtar'
    out: List[Tuple[str, bytes]] = []
    try:
        # rarfile 要求文件对象，BytesIO 可用
        with rarfile.RarFile(io.BytesIO(content)) as rf:
            for info in rf.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                if not name.lower().endswith(SUB_EXTENSIONS):
                    continue
                try:
                    out.append((name, rf.read(info)))
                except Exception as e:
                    logger.warning(f"读取 rar 内 {name} 失败: {e}")
    except rarfile.NeedFirstVolume:
        logger.warning("rar 是分卷的首卷以外部分，跳过")
        return []
    except rarfile.BadRarFile as e:
        logger.warning(f"rar 解析失败: {e}")
        return []
    except rarfile.RarCannotExec as e:
        logger.warning(f"rar 需要外部 unrar/bsdtar，未在 PATH 上: {e}")
        return []
    except Exception as e:
        logger.warning(f"rar 处理异常: {e}")
        return []
    return out


def _extract_tar(content: bytes) -> List[Tuple[str, bytes]]:
    """tar / tar.gz / tar.bz2 / tar.xz —— tarfile 自动识别透明解压。"""
    import tarfile
    out: List[Tuple[str, bytes]] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode='r:*') as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                if not member.name.lower().endswith(SUB_EXTENSIONS):
                    continue
                try:
                    f = tf.extractfile(member)
                    if f is not None:
                        out.append((member.name, f.read()))
                except Exception as e:
                    logger.warning(f"读取 tar 内 {member.name} 失败: {e}")
    except tarfile.TarError as e:
        logger.warning(f"tar 解析失败: {e}")
    return out


def _extract_gz_single(content: bytes) -> List[Tuple[str, bytes]]:
    """单文件 gzip：解压后整体当一个字幕文件（无包内文件名，给个占位）。"""
    import gzip
    try:
        data = gzip.decompress(content)
    except OSError as e:
        logger.warning(f"gz 解压失败: {e}")
        return []
    # 不知道真名 → 用默认 .srt（上层会按内容嗅探纠正）
    return [('subtitle.srt', data)]


def _extract_via_bsdtar(content: bytes, hint_ext: str = '') -> List[Tuple[str, bytes]]:
    """用 bsdtar (libarchive) 解压 rar/7z/tar 等。需 PATH 上有 bsdtar 或 tar。

    策略：解压整个包到 tempdir，再走文件系统读字幕文件。
    避免之前"列条目 → 逐个 -xOf"的 Unicode 罗盘问题——bsdtar 在 Windows 控制台
    输出文件名时受 CP936 影响，subprocess decode 后中文变 '?'，再喂回去就找不到了。
    """
    import shutil
    import subprocess
    import tempfile
    import os

    bsdtar = shutil.which('bsdtar') or shutil.which('tar')
    if not bsdtar:
        logger.warning(f"未找到 bsdtar/tar，{hint_ext or '非 zip'} 压缩包无法解压；请安装 libarchive")
        return []

    suffix = hint_ext if hint_ext else '.archive'
    with tempfile.NamedTemporaryFile(mode='wb', suffix=suffix, delete=False) as tf:
        tf.write(content)
        tf_path = tf.name

    out: List[Tuple[str, bytes]] = []
    try:
        with tempfile.TemporaryDirectory(prefix='bsdtar_') as tdir:
            try:
                r = subprocess.run(
                    [bsdtar, '-xf', tf_path, '-C', tdir],
                    capture_output=True, timeout=60,
                )
            except subprocess.TimeoutExpired:
                logger.warning(f"bsdtar 解压超时: {tf_path}")
                return []
            if r.returncode != 0:
                # 即便部分文件成功也可能 rc != 0；先看 tempdir 有没有字幕，没有再认输
                stderr_short = (r.stderr[:300] if r.stderr else b'').decode('utf-8', errors='replace')
                logger.warning(f"bsdtar 解压 rc={r.returncode}（部分失败正常）: {stderr_short}")

            # 走文件系统找字幕（os.walk 给的是 OS 真实文件名，Unicode 安全）
            for root, _, files in os.walk(tdir):
                for fname in files:
                    if not fname.lower().endswith(SUB_EXTENSIONS):
                        continue
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, 'rb') as f:
                            data = f.read()
                    except OSError as e:
                        logger.warning(f"读取解压后的 {fname} 失败: {e}")
                        continue
                    # 相对路径作为条目名（保留子目录信息），统一斜杠
                    rel = os.path.relpath(fpath, tdir).replace('\\', '/')
                    out.append((rel, data))
        logger.info(f"bsdtar 解压成功: {len(out)} 个字幕文件")
    finally:
        try:
            os.unlink(tf_path)
        except OSError:
            pass
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
