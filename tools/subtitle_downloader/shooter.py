"""
Shooter (射手网) API 客户端 — https://www.shooter.cn/api/subapi.php

特点：
  - 中文电影 hash 命中率高（用户上传字幕时 Shooter 索引了文件 hash）
  - 一次请求返回多个语言的字幕直链
  - 不需要注册 / API Key
  - 缺点：服务有时不稳定；字幕通常无字段表示语言，靠返回的 Desc/Files 推断

文件 hash 算法（Shooter 协议规定）：
  从文件取 4 个 4096 byte 块，位置:
    P1 = 4096
    P2 = file_size / 3 * 1
    P3 = file_size / 3 * 2
    P4 = file_size - 8192
  对每个块算 md5（hex），用 ";" 拼接得到 hash 字符串。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


SHOOTER_API_URL = "https://www.shooter.cn/api/subapi.php"
SHOOTER_HASH_BLOCK = 4096


def compute_shooter_hash(video_path: Path) -> Optional[str]:
    """
    计算 Shooter 文件 hash。返回 hex 字符串（4 段拼接），失败返回 None。
    协议：4 个 4KB 块的 md5(hex) 用 ";" 拼接。
    """
    try:
        size = video_path.stat().st_size
    except OSError as e:
        logger.warning(f"shooter hash 失败 stat {video_path}: {e}")
        return None
    if size < 8192 + SHOOTER_HASH_BLOCK:
        # 文件太小，hash 无意义
        return None

    offsets = [
        SHOOTER_HASH_BLOCK,
        size // 3,
        size // 3 * 2,
        size - 8192,
    ]
    parts: List[str] = []
    try:
        with open(video_path, 'rb') as f:
            for off in offsets:
                f.seek(off)
                block = f.read(SHOOTER_HASH_BLOCK)
                if not block:
                    return None
                parts.append(hashlib.md5(block).hexdigest())
    except OSError as e:
        logger.warning(f"shooter hash 失败 read {video_path}: {e}")
        return None
    return ';'.join(parts)


class ShooterClient:
    """Shooter API 客户端。"""

    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

    def __init__(self, request_delay: float = 2.0, timeout: float = 15.0):
        self.request_delay = max(0.0, float(request_delay))
        self.timeout = float(timeout)
        self._last_call = 0.0
        self._lock = threading.Lock()

    def _wait_quota(self):
        with self._lock:
            elapsed = time.monotonic() - self._last_call
            if elapsed < self.request_delay:
                time.sleep(self.request_delay - elapsed)
            self._last_call = time.monotonic()

    def search(
        self,
        video_path: Path,
        lang: str = 'Chn',
    ) -> List[Dict]:
        """
        Shooter 协议: POST 到 subapi.php, form 字段 filehash + pathinfo + format=json + lang
          lang 取 'Chn'（中文）或 'Eng'（英文）；其它语言 Shooter 不支持
        返回原始 sub list（每个 sub 含 Desc / Files: [{Ext, Link}]）。
        """
        file_hash = compute_shooter_hash(video_path)
        if not file_hash:
            logger.info(f"shooter: 无法对 {video_path.name} 计算 hash，跳过")
            return []

        data = {
            'filehash': file_hash,
            'pathinfo': str(video_path),
            'format': 'json',
            'lang': lang,
        }
        self._wait_quota()
        try:
            r = requests.post(
                SHOOTER_API_URL,
                data=data,
                headers={'User-Agent': self.USER_AGENT},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            logger.warning(f"shooter 请求异常: {e}")
            return []

        if r.status_code != 200:
            logger.warning(f"shooter 状态码 {r.status_code}")
            return []

        # Shooter 没结果时返回 b'\xff' 单字节
        if r.content == b'\xff' or not r.content:
            return []

        try:
            data = r.json()
        except ValueError:
            logger.warning(f"shooter 非 JSON 响应: {r.content[:200]!r}")
            return []

        if not isinstance(data, list):
            return []
        return data

    def download(self, link: str, target: Path) -> bool:
        """从 Files[i].Link 下载字幕文件到 target。"""
        self._wait_quota()
        try:
            r = requests.get(
                link,
                headers={'User-Agent': self.USER_AGENT, 'Referer': 'https://www.shooter.cn/'},
                timeout=self.timeout,
                stream=True,
            )
            r.raise_for_status()
            target.write_bytes(r.content)
            return True
        except requests.RequestException as e:
            logger.warning(f"shooter 下载失败 {link}: {e}")
            return False


def guess_lang_from_desc(desc: str, files: List[Dict]) -> str:
    """
    Shooter 不返回标准语言代码；从 Desc + 文件名启发式推断。
    返回内部代码：chs / cht / eng / jpn / kor，可能是复合（如 'chs.eng'）；
    完全识别不出返回 'unknown'。

    实现复用 common.lang_utils（与 assrt 等其他源共享同一套规则，支持双语）。
    """
    from common.lang_utils import detect_lang_combo

    if not desc:
        desc = ''
    haystack = desc
    for f in files or []:
        haystack += ' ' + str(f.get('Link') or '')
    combo = detect_lang_combo(haystack)
    return combo or 'unknown'
