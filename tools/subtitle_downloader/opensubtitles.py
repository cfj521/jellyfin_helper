"""
OpenSubtitles API 客户端
https://opensubtitles.stoplight.io/docs/opensubtitles-api
"""
import os
import re
import hashlib
import requests
import logging
from pathlib import Path
from typing import Optional, List, Dict

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

    def __init__(self, api_key: str, username: str = None, password: str = None):
        """
        初始化客户端

        Args:
            api_key: OpenSubtitles API Key
            username: 用户名（可选，用于下载）
            password: 密码（可选，用于下载）
        """
        self.api_key = api_key
        self.username = username
        self.password = password
        self.token = None
        self.headers = {
            'Api-Key': api_key,
            'Content-Type': 'application/json',
            'User-Agent': 'JellyfinTools v1.0'
        }

    def login(self) -> bool:
        """登录获取token（下载需要）"""
        if not self.username or not self.password:
            logger.warning("未配置用户名密码，无法登录")
            return False

        try:
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

    def search(self, video_path: Path, languages: List[str] = None) -> List[Dict]:
        """
        搜索字幕

        Args:
            video_path: 视频文件路径
            languages: 语言列表 ['chs', 'eng']

        Returns:
            字幕列表
        """
        if languages is None:
            languages = ['chs', 'eng']

        # 转换语言代码
        api_langs = [self.LANG_MAP.get(lang, lang) for lang in languages]

        # 提取信息
        info = self.extract_info(video_path.stem)

        params = {
            'query': info['query'],
            'languages': ','.join(api_langs),
        }

        if 'season_number' in info:
            params['season_number'] = info['season_number']
        if 'episode_number' in info:
            params['episode_number'] = info['episode_number']
        if 'year' in info:
            params['year'] = info['year']

        try:
            response = requests.get(
                f"{self.BASE_URL}/subtitles",
                headers=self.headers,
                params=params
            )
            response.raise_for_status()
            data = response.json()
            return data.get('data', [])
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
            response = requests.post(
                f"{self.BASE_URL}/download",
                headers=headers,
                json={'file_id': file_id}
            )
            response.raise_for_status()
            data = response.json()

            download_url = data.get('link')
            if not download_url:
                logger.error("未获取到下载链接")
                return False

            # 下载文件
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
