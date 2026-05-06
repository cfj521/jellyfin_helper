"""
qBittorrent WebUI API 客户端
https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-4.1)
"""
import logging
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


class QBittorrentClient:
    """qBittorrent WebUI 客户端"""

    def __init__(self, host: str, username: str, password: str, timeout: int = 30):
        self.host = host.rstrip('/')
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self._logged_in = False

    def login(self) -> bool:
        """登录获取 session cookie。"""
        try:
            r = self.session.post(
                f"{self.host}/api/v2/auth/login",
                data={'username': self.username, 'password': self.password},
                headers={'Referer': self.host},
                timeout=self.timeout,
            )
            r.raise_for_status()
            if r.text.strip().lower() == 'ok.':
                self._logged_in = True
                return True
            logger.error(f"qBittorrent 登录失败: {r.text}")
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"qBittorrent 登录异常: {e}")
            return False

    def _ensure_login(self) -> bool:
        if self._logged_in:
            return True
        return self.login()

    def add_torrent(
        self,
        magnet: Optional[str] = None,
        torrent_url: Optional[str] = None,
        save_path: Optional[str] = None,
        category: Optional[str] = None,
    ) -> bool:
        """添加种子（支持 magnet 链接或种子文件直链）。"""
        if not self._ensure_login():
            return False

        data: Dict = {}
        if magnet:
            data['urls'] = magnet
        elif torrent_url:
            data['urls'] = torrent_url
        else:
            raise ValueError("必须提供 magnet 或 torrent_url")

        if save_path:
            data['savepath'] = save_path
        if category:
            data['category'] = category

        try:
            r = self.session.post(
                f"{self.host}/api/v2/torrents/add",
                data=data,
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.text.strip().lower() == 'ok.'
        except requests.exceptions.RequestException as e:
            logger.error(f"添加种子失败: {e}")
            return False

    def list_torrents(self, filter_status: Optional[str] = None) -> List[Dict]:
        """
        列出所有种子。

        filter_status: all | downloading | seeding | completed | paused | active | inactive
        """
        if not self._ensure_login():
            return []

        params: Dict = {}
        if filter_status:
            params['filter'] = filter_status

        try:
            r = self.session.get(
                f"{self.host}/api/v2/torrents/info",
                params=params,
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"获取种子列表失败: {e}")
            return []

    def pause(self, torrent_hash: str) -> bool:
        return self._action('pause', torrent_hash)

    def resume(self, torrent_hash: str) -> bool:
        return self._action('resume', torrent_hash)

    def delete(self, torrent_hash: str, delete_files: bool = False) -> bool:
        if not self._ensure_login():
            return False
        try:
            r = self.session.post(
                f"{self.host}/api/v2/torrents/delete",
                data={'hashes': torrent_hash, 'deleteFiles': str(delete_files).lower()},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"删除种子失败: {e}")
            return False

    def _action(self, action: str, torrent_hash: str) -> bool:
        if not self._ensure_login():
            return False
        try:
            r = self.session.post(
                f"{self.host}/api/v2/torrents/{action}",
                data={'hashes': torrent_hash},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"qBit 操作 {action} 失败: {e}")
            return False
