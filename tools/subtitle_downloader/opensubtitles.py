"""
OpenSubtitles API 客户端
https://opensubtitles.stoplight.io/docs/opensubtitles-api
"""
import os
import re
import time
import threading
import hashlib
import requests
import logging
from pathlib import Path
from typing import Optional, List, Dict

from common.rate_limiter import quota_guard

logger = logging.getLogger(__name__)


class OpenSubtitlesClient:
    """OpenSubtitles API 客户端"""

    BASE_URL = "https://api.opensubtitles.com/api/v1"

    # 语言代码映射
    LANG_MAP = {
        'chs': 'zh-cn',
        'cht': 'zh-tw',
        'zh': 'zh-cn',
        'eng': 'en',
        'en': 'en',
        'jpn': 'ja',
        'ja': 'ja',
        'kor': 'ko',
        'ko': 'ko',
    }

    def __init__(self, api_key: str, username: str = None, password: str = None,
                 request_delay: float = 2.0, batch: bool = False):
        """
        初始化客户端

        Args:
            api_key: OpenSubtitles API Key
            username: 用户名（可选，用于下载）
            password: 密码（可选，用于下载）
            request_delay: 两次 API 请求之间的最小间隔（秒）。免费层 5/10s 限频，
                建议 ≥2s 留余量。本类内部串行排队，多线程共享同一实例也安全。
            batch: True 表示批量调用方（如全库字幕修复），额外受 batch 配额约束。
        """
        self.api_key = api_key
        self.username = username
        self.password = password
        self.token = None
        self.headers = {
            'Api-Key': api_key,
            'Content-Type': 'application/json',
            'User-Agent': 'JellyfinHelper v1.0'
        }
        self.request_delay = max(0.0, float(request_delay))
        self.batch = batch
        self._last_call = 0.0
        self._lock = threading.Lock()

    def _wait_quota(self):
        """两次 OpenSubtitles API 请求之间的强制间隔。"""
        # 保底层：先检查全局暂停 / batch 配额
        quota_guard.acquire('opensubtitles', batch=self.batch)
        with self._lock:
            elapsed = time.monotonic() - self._last_call
            if elapsed < self.request_delay:
                time.sleep(self.request_delay - elapsed)
            self._last_call = time.monotonic()

    def login(self) -> bool:
        """登录获取token（下载需要）"""
        if not self.username or not self.password:
            logger.warning("未配置用户名密码，无法登录")
            return False

        try:
            self._wait_quota()
            response = requests.post(
                f"{self.BASE_URL}/login",
                headers=self.headers,
                json={'username': self.username, 'password': self.password}
            )
            response.raise_for_status()
            data = response.json()
            self.token = data.get('token')
            logger.info("OpenSubtitles 登录成功")
            return True
        except Exception as e:
            logger.error(f"登录失败: {e}")
            return False

    def compute_hash(self, file_path: Path) -> str:
        """计算视频文件hash（用于精确匹配）"""
        file_size = os.path.getsize(file_path)
        hash_value = file_size

        with open(file_path, 'rb') as f:
            # 读取前64KB
            for _ in range(8192):
                buffer = f.read(8)
                if len(buffer) < 8:
                    break
                hash_value += int.from_bytes(buffer, 'little')
                hash_value &= 0xFFFFFFFFFFFFFFFF

            # 读取后64KB
            f.seek(max(0, file_size - 65536))
            for _ in range(8192):
                buffer = f.read(8)
                if len(buffer) < 8:
                    break
                hash_value += int.from_bytes(buffer, 'little')
                hash_value &= 0xFFFFFFFFFFFFFFFF

        return format(hash_value, '016x')

    def extract_info(self, filename: str) -> Dict:
        """从文件名提取信息"""
        info = {'query': filename}

        # 提取年份
        year_match = re.search(r'[.\s\(](\d{4})[.\s\)]', filename)
        if year_match:
            info['year'] = year_match.group(1)

        # 提取剧集信息
        ep_match = re.search(r'[Ss](\d{1,2})[Ee](\d{1,3})', filename)
        if ep_match:
            info['season_number'] = int(ep_match.group(1))
            info['episode_number'] = int(ep_match.group(2))

        # 清理查询字符串
        clean_name = re.sub(r'[.\[\]()_]', ' ', filename)
        clean_name = re.sub(r'\d{3,4}p.*', '', clean_name, flags=re.IGNORECASE)
        clean_name = re.sub(r'(BluRay|WEB-DL|HDTV|DVDRip|BRRip).*', '', clean_name, flags=re.IGNORECASE)
        clean_name = re.sub(r'[Ss]\d{1,2}[Ee]\d{1,3}.*', '', clean_name)
        info['query'] = clean_name.strip()

        return info

    def search(
        self,
        video_path: Optional[Path] = None,
        languages: Optional[List[str]] = None,
        imdb_id: Optional[str] = None,
        tmdb_id: Optional[str] = None,
        query: Optional[str] = None,
    ) -> List[Dict]:
        """
        搜索字幕。优先级（命中即停）：
          1. imdb_id 给定 → 用 imdb_id（OpenSubtitles 接受不带 'tt' 前缀的纯数字）
          2. tmdb_id 给定 → 用 tmdb_id
          3. query 给定 → 直接用
          4. video_path 给定 → 从 stem 提取 query / year / episode hint

        ID 搜索精度远高于文本搜（同名电影 / 不同年份版本完全不会混淆）。
        """
        if languages is None:
            languages = ['chs', 'eng']
        api_langs = [self.LANG_MAP.get(lang, lang) for lang in languages]
        params: Dict = {'languages': ','.join(api_langs)}

        if imdb_id:
            # OpenSubtitles 接受纯数字 imdb_id；'tt0468569' 要剥前缀
            clean = str(imdb_id).lower().lstrip('t')
            params['imdb_id'] = clean
        elif tmdb_id:
            params['tmdb_id'] = str(tmdb_id)
        else:
            # 文本搜模式
            if query is None and video_path is not None:
                info = self.extract_info(video_path.stem)
                query = info.get('query')
                if 'year' in info:
                    params['year'] = info['year']
                if 'season_number' in info:
                    params['season_number'] = info['season_number']
                if 'episode_number' in info:
                    params['episode_number'] = info['episode_number']
            if not query:
                logger.warning("OpenSubtitles search 缺少 query / IDs / video_path，跳过")
                return []
            params['query'] = query

        try:
            self._wait_quota()
            response = requests.get(
                f"{self.BASE_URL}/subtitles",
                headers=self.headers,
                params=params
            )
            if response.status_code == 429:
                quota_guard.report_limited('opensubtitles', 'HTTP 429 搜索')
                logger.warning("OpenSubtitles 搜索限流 (429)")
                return []
            response.raise_for_status()
            quota_guard.report_success('opensubtitles')
            data = response.json()
            return data.get('data', [])
        except requests.exceptions.HTTPError as e:
            if hasattr(e, 'response') and e.response is not None and e.response.status_code == 429:
                quota_guard.report_limited('opensubtitles', 'HTTP 429 搜索')
            logger.error(f"搜索失败: {e}")
            return []
        except Exception as e:
            logger.error(f"搜索失败: {e}")
            return []

    def download(self, file_id: int, output_path: Path) -> bool:
        """
        下载字幕

        Args:
            file_id: 字幕文件ID
            output_path: 输出路径

        Returns:
            是否成功
        """
        if not self.token:
            if not self.login():
                return False

        headers = {**self.headers, 'Authorization': f'Bearer {self.token}'}

        try:
            # 获取下载链接
            self._wait_quota()
            response = requests.post(
                f"{self.BASE_URL}/download",
                headers=headers,
                json={'file_id': file_id}
            )
            if response.status_code == 429:
                quota_guard.report_limited('opensubtitles', 'HTTP 429 下载')
                logger.warning("OpenSubtitles 下载限流 (429)")
                return False
            response.raise_for_status()
            quota_guard.report_success('opensubtitles')
            data = response.json()

            download_url = data.get('link')
            if not download_url:
                logger.error("未获取到下载链接")
                return False

            # 下载文件（CDN 链接，不走限频）
            response = requests.get(download_url)
            response.raise_for_status()

            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'wb') as f:
                f.write(response.content)

            logger.info(f"下载成功: {output_path}")
            return True

        except Exception as e:
            logger.error(f"下载失败: {e}")
            return False
