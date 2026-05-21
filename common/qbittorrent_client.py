"""
qBittorrent WebUI API 客户端
https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-5.0)

支持 qB 4.6 ~ 5.2+，自动按 webapiVersion 选老/新端点。

## 关键 4.6 → 5.2 变化（已自适应）

| 变化 | 4.x | 5.0+（webapi ≥ 2.11）|
|------|-----|----------------------|
| pause 端点 | `/torrents/pause` | `/torrents/stop` |
| resume 端点 | `/torrents/resume` | `/torrents/start` |
| `/torrents/add` 的暂停参数 | `paused=true` | `stopped=true` |
| 状态名 | `pausedDL` / `pausedUP` | `stoppedDL` / `stoppedUP`（同义） |
| login 成功响应 | `200 "Ok."` | `204` 空 body（5.2.0+） |
| 空响应通用 | `200` | `204` |

## 认证两种模式（按优先级）

  1. **API Key**（qB ≥ 5.2.0 推荐）—— `Authorization: Bearer qbt_xxx` header，
     stateless，不用调 /auth/login。Preferences → WebUI → API Key 段生成。
  2. **Username/Password** —— 传统 cookie 会话；qB 5.2.0 起 /auth/login 成功
     响应从 `200 "Ok."` 变成 `204 空 body`，本客户端两者都兼容。
"""
import logging
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


class QBittorrentClient:
    """qBittorrent WebUI 客户端"""

    def __init__(
        self,
        host: str,
        username: str = '',
        password: str = '',
        timeout: int = 30,
        api_key: Optional[str] = None,
    ):
        """
        api_key 默认 None → 自动从 settings.qbittorrent_api_key 读（让 14 个老调用点
        零修改获得 api_key 支持）。显式传 '' 可以强制走 username/password 路径。
        """
        self.host = host.rstrip('/')
        self.username = username
        self.password = password
        self.timeout = timeout
        # 自动从 settings 兜底（common/ 反向 import web.backend.config 在本项目有先例）
        if api_key is None:
            try:
                from web.backend.config import settings as _settings
                api_key = getattr(_settings, 'qbittorrent_api_key', '') or ''
            except Exception:
                api_key = ''
        self.api_key = (api_key or '').strip()
        self.session = requests.Session()
        self._logged_in = False
        # 最近一次 add_torrent 失败的原因（HTTP 状态 + body 摘要 / 异常文本）。
        # qB add API 拒绝时只回 'Fails.'，没有细节；调用方读这里把上下文带到前端 detail。
        self.last_add_error: Optional[str] = None
        # 最近一次 add_torrent 失败的 HTTP 状态码（qB 5.2.0+ 用 409=冲突，500=其他错误）；
        # 调用方可据此回 409 给前端，避免笼统的 502
        self.last_add_status_code: Optional[int] = None

        # API Key 模式：所有请求自动带 Bearer header，跳过 /auth/login 流程
        if self.api_key:
            self.session.headers['Authorization'] = f'Bearer {self.api_key}'
            self._logged_in = True  # 短路 _ensure_login（虽然 cookie 没设但 Bearer 等价）

    def login(self) -> bool:
        """获取 session cookie；api_key 模式直接 True 不发请求。"""
        if self.api_key:
            return True
        try:
            r = self.session.post(
                f"{self.host}/api/v2/auth/login",
                data={'username': self.username, 'password': self.password},
                headers={'Referer': self.host},
                timeout=self.timeout,
            )
            r.raise_for_status()
            # qB 5.2.0+：成功 = HTTP 204 + 空 body
            # qB ≤ 5.1.x：成功 = HTTP 200 + "Ok."
            # 失败统一：HTTP 200 + "Fails."（不是 401，所以 raise_for_status 不抛）
            body = r.text.strip()
            ok = r.status_code == 204 or body.lower() == 'ok.'
            if ok:
                self._logged_in = True
                return True
            logger.error(f"qBittorrent 登录失败: HTTP {r.status_code} body={body!r}")
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"qBittorrent 登录异常: {e}")
            return False

    def _ensure_login(self) -> bool:
        if self._logged_in:
            return True
        return self.login()

    # ============================================================================
    # 版本探测 —— 缓存一次结果，给端点自适应用
    # ============================================================================

    def _webapi_version(self) -> Optional[tuple]:
        """探测 /api/v2/app/webapiVersion，返回 (major, minor, [patch]) 元组。失败返 None。
        结果实例缓存，重复调用零开销。"""
        cached = getattr(self, '_cached_webapi_version', '__unset__')
        if cached != '__unset__':
            return cached
        result: Optional[tuple] = None
        try:
            if self._ensure_login():
                r = self.session.get(
                    f"{self.host}/api/v2/app/webapiVersion",
                    timeout=self.timeout,
                )
                r.raise_for_status()
                ver_str = r.text.strip().strip('"')
                parts = tuple(int(x) for x in ver_str.split('.') if x.isdigit())
                if parts:
                    result = parts
        except Exception as e:
            logger.warning(f"探测 qB webapiVersion 失败（按 4.x 保守处理）: {e}")
        self._cached_webapi_version = result
        return result

    def _is_v5_api(self) -> bool:
        """webapi ≥ 2.11 ⇔ qB ≥ 5.0 ⇔ 用 stop/start 端点 + add 的 stopped 参数。

        探测失败保守按 4.x 处理（用老 pause/resume 端点；qB 4.x 老版本上仍能跑）。
        """
        v = self._webapi_version()
        if not v or v[0] != 2:
            return False
        return v[1] >= 11

    def add_torrent(
        self,
        magnet: Optional[str] = None,
        torrent_url: Optional[str] = None,
        torrent_file: Optional[bytes] = None,
        save_path: Optional[str] = None,
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
        paused: bool = False,
        download_limit: Optional[int] = None,
        skip_checking: bool = False,
        stop_condition: Optional[str] = None,   # qB 4.6+: MetadataReceived / FilesChecked / None
    ) -> bool:
        """
        添加种子。支持 magnet / 种子文件 URL / 种子文件 bytes 三选一。

        **metadata-only 预添加正确用法**（qB 4.6+）：
            add_torrent(magnet=..., stop_condition='MetadataReceived', download_limit=1)
        qB 会连 peer 拉 metadata（DHT/tracker），拿到后**自动停止**。
        注意：单 paused=True 不行 —— paused 状态下 qB 不连 peer，metadata 永远到不了。
        """
        if not self._ensure_login():
            return False

        data: Dict = {}
        files = None

        if magnet:
            data['urls'] = magnet
        elif torrent_url:
            data['urls'] = torrent_url
        elif torrent_file:
            files = {'torrents': ('upload.torrent', torrent_file, 'application/x-bittorrent')}
        else:
            raise ValueError("必须提供 magnet / torrent_url / torrent_file 之一")

        if save_path:
            data['savepath'] = save_path
        if category:
            data['category'] = category
        if tags:
            data['tags'] = ','.join(tags)
        if paused:
            # qB 5.0+：参数名 paused → stopped（issue #22766: paused=true 在 5.x 不再生效）。
            # 4.x：用 paused。版本探测失败按 4.x 保守。
            data['stopped' if self._is_v5_api() else 'paused'] = 'true'
        if download_limit is not None:
            data['dlLimit'] = str(int(download_limit))
        if skip_checking:
            data['skip_checking'] = 'true'
        if stop_condition:
            data['stopCondition'] = stop_condition

        self.last_add_error = None
        self.last_add_status_code = None
        try:
            r = self.session.post(
                f"{self.host}/api/v2/torrents/add",
                data=data,
                files=files,
                timeout=self.timeout,
            )
            self.last_add_status_code = r.status_code
            body_preview = (r.text or '').strip()[:200]

            # 按官方 wiki 列出的状态码 + 5.2 实际表现，逐档判定：
            #   409 Conflict：重复 hash / 已存在（5.2 把这个从 200 "Fails." 改 409）
            #   415 Unsupported Media Type：种子文件无效（非 bencode）
            #   400 Bad Request：参数错（罕见，add 本身参数少）
            #   200/204：成功（body 可能是 "Ok." / JSON / 空，下面再细分）
            if r.status_code == 409:
                self.last_add_error = "种子已存在或与现有任务冲突 (HTTP 409 Conflict)"
                logger.warning(f"qB 加种 409 Conflict: body={body_preview!r}")
                return False
            if r.status_code == 415:
                self.last_add_error = "种子文件无效或不支持 (HTTP 415 Unsupported Media Type)"
                logger.warning(f"qB 加种 415 Invalid torrent: body={body_preview!r}")
                return False
            if 400 <= r.status_code < 500:
                self.last_add_error = f"qB 拒绝请求 (HTTP {r.status_code}): {body_preview}"
                logger.warning(f"qB 加种 {r.status_code}: body={body_preview!r}")
                return False
            if r.status_code >= 500:
                self.last_add_error = f"qB 服务端错误 (HTTP {r.status_code}): {body_preview}"
                logger.error(f"qB 加种服务端 {r.status_code}: body={body_preview!r}")
                return False
            # 到这里只剩 2xx
            body = r.text.strip()
            # 三种成功响应（按 qB 版本演进）：
            #   1. qB 5.2.0+：JSON {"success_count": N, "failure_count": M, "added_torrent_ids": [...]}
            #      只要 success_count>=1（或 pending_count>=1，metadata 拉取中）就算成功
            #   2. qB 4.x ~ 5.1：纯文本 "Ok."
            #   3. qB 5.2.0 空 body 端点（理论上 add 不属于此类，但保留 204 兼容）
            ok = False
            failure_reason = ''
            if body.startswith('{'):
                try:
                    import json as _json
                    j = _json.loads(body)
                    sc = int(j.get('success_count') or 0)
                    pc = int(j.get('pending_count') or 0)
                    fc = int(j.get('failure_count') or 0)
                    ok = sc > 0 or pc > 0
                    if not ok and fc > 0:
                        failure_reason = f'success=0 failure={fc} pending={pc}'
                except Exception as e:
                    self.last_add_error = f"HTTP {r.status_code} JSON 解析失败: {e} body={body[:200]!r}"
                    logger.error(f"qB add 响应解析失败: {self.last_add_error}")
                    return False
            elif body.lower() == 'ok.' or r.status_code == 204:
                ok = True
            if not ok:
                # qB 加种被拒的常见原因：种子重复、默认保存路径不存在/无权限、category 不存在、
                # 磁链/种子文件解析失败。'Fails.' 文本不带细节，JSON 模式 failure_count>0 也只给计数。
                self.last_add_error = (
                    f"HTTP {r.status_code} {failure_reason} body={body!r}"
                    if failure_reason else f"HTTP {r.status_code} body={body!r}"
                )
                logger.error(f"qB 添加种子被拒: {self.last_add_error}")
            return ok
        except requests.exceptions.RequestException as e:
            self.last_add_error = f"网络异常: {e}"
            logger.error(f"添加种子失败: {e}")
            return False

    def get_torrent_info(self, torrent_hash: str) -> Optional[Dict]:
        """单条种子详情（/properties endpoint），无则 None。
        关键字段：total_size（metadata 拿到才有）、save_path、share_ratio、seeding_time...

        ⚠️ /properties **不返回 content_path**。要拿种子内容路径（含子目录名）请用
        get_torrent_meta()，那个走 /info endpoint。
        """
        if not self._ensure_login():
            return None
        try:
            r = self.session.get(
                f"{self.host}/api/v2/torrents/properties",
                params={'hash': torrent_hash},
                timeout=self.timeout,
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"获取种子详情失败 {torrent_hash}: {e}")
            return None

    def get_torrent_meta(self, torrent_hash: str) -> Optional[Dict]:
        """单条种子的 /info 元数据，含 content_path / save_path / state / progress / name 等。
        没找到 → None。

        跟 get_torrent_info 区别：那个走 /properties（统计/计时类字段），本方法走 /info
        （列表展示字段，含 content_path）。pipeline 取源路径用本方法。
        """
        if not self._ensure_login():
            return None
        try:
            r = self.session.get(
                f"{self.host}/api/v2/torrents/info",
                params={'hashes': torrent_hash},
                timeout=self.timeout,
            )
            r.raise_for_status()
            arr = r.json() or []
            return arr[0] if arr else None
        except requests.exceptions.RequestException as e:
            logger.error(f"获取种子 meta 失败 {torrent_hash}: {e}")
            return None

    def get_files(self, torrent_hash: str) -> List[Dict]:
        """种子文件列表。metadata 未到时返回空 [] 或 404。
        每项含 name / size / progress / priority / piece_range 等。
        """
        if not self._ensure_login():
            return []
        try:
            r = self.session.get(
                f"{self.host}/api/v2/torrents/files",
                params={'hash': torrent_hash},
                timeout=self.timeout,
            )
            if r.status_code == 404:
                return []
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except requests.exceptions.RequestException as e:
            logger.error(f"获取种子文件列表失败 {torrent_hash}: {e}")
            return []

    def list_torrents(self, filter_status: Optional[str] = None) -> List[Dict]:
        """
        列出所有种子。

        filter_status: all | downloading | seeding | completed | stopped | active | inactive
          - 调用方传 'paused' 会自适应：5.0+ 自动改成 'stopped'（5.0+ paused filter 行为
            不稳定，issue #21476），4.x 保持原样
        """
        if not self._ensure_login():
            return []

        params: Dict = {}
        if filter_status:
            # 'paused' 是 4.x 用语，5.0+ 推荐 'stopped'（兼容性更好）
            if filter_status == 'paused' and self._is_v5_api():
                filter_status = 'stopped'
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
        """暂停种子。qB 5.0+ 端点是 /torrents/stop，4.x 是 /torrents/pause（4.x 老端点在 5.x 返 404）。"""
        action = 'stop' if self._is_v5_api() else 'pause'
        return self._action(action, torrent_hash)

    def resume(self, torrent_hash: str) -> bool:
        """恢复种子。qB 5.0+ 端点是 /torrents/start，4.x 是 /torrents/resume。"""
        action = 'start' if self._is_v5_api() else 'resume'
        return self._action(action, torrent_hash)

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

    # ============================================================================
    # 单种子辅助操作（A3 补全）
    # ============================================================================

    def _post(self, path: str, data: Optional[Dict] = None) -> bool:
        """POST 通用包装，返回是否成功（HTTP 2xx）。"""
        if not self._ensure_login():
            return False
        try:
            r = self.session.post(
                f"{self.host}/api/v2{path}",
                data=data or {},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"qB POST {path} 失败: {e}")
            return False

    def _get(self, path: str, params: Optional[Dict] = None):
        """GET 通用包装，返回 JSON 或 None。"""
        if not self._ensure_login():
            return None
        try:
            r = self.session.get(
                f"{self.host}/api/v2{path}",
                params=params or {},
                timeout=self.timeout,
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            try:
                return r.json()
            except ValueError:
                return r.text
        except requests.exceptions.RequestException as e:
            logger.error(f"qB GET {path} 失败: {e}")
            return None

    # ---- 标签 / 分类 ----

    def add_tags(self, torrent_hashes, tags) -> bool:
        """给一个或多个种子添加 tag（已存在的 tag 会自动去重）。"""
        hashes = torrent_hashes if isinstance(torrent_hashes, str) else '|'.join(torrent_hashes)
        tags_str = tags if isinstance(tags, str) else ','.join(tags)
        return self._post('/torrents/addTags', {'hashes': hashes, 'tags': tags_str})

    def remove_tags(self, torrent_hashes, tags) -> bool:
        hashes = torrent_hashes if isinstance(torrent_hashes, str) else '|'.join(torrent_hashes)
        tags_str = tags if isinstance(tags, str) else ','.join(tags)
        return self._post('/torrents/removeTags', {'hashes': hashes, 'tags': tags_str})

    def set_category(self, torrent_hashes, category: str) -> bool:
        hashes = torrent_hashes if isinstance(torrent_hashes, str) else '|'.join(torrent_hashes)
        return self._post('/torrents/setCategory', {'hashes': hashes, 'category': category})

    # ---- 文件优先级 ----

    def set_file_priority(self, torrent_hash: str, file_ids, priority: int) -> bool:
        """
        设置文件下载优先级。priority:
          0 = Do not download   1 = Normal   6 = High   7 = Maximum
        file_ids 可以是 int 或 list[int]。
        """
        ids_str = (
            '|'.join(str(i) for i in file_ids) if isinstance(file_ids, (list, tuple))
            else str(file_ids)
        )
        return self._post('/torrents/filePrio', {
            'hash': torrent_hash, 'id': ids_str, 'priority': priority,
        })

    # ---- 控制类操作 ----

    def recheck(self, torrent_hashes) -> bool:
        hashes = torrent_hashes if isinstance(torrent_hashes, str) else '|'.join(torrent_hashes)
        return self._post('/torrents/recheck', {'hashes': hashes})

    def reannounce(self, torrent_hashes) -> bool:
        hashes = torrent_hashes if isinstance(torrent_hashes, str) else '|'.join(torrent_hashes)
        return self._post('/torrents/reannounce', {'hashes': hashes})

    def set_force_start(self, torrent_hashes, force: bool = True) -> bool:
        """跳过排队限制强制开始。"""
        hashes = torrent_hashes if isinstance(torrent_hashes, str) else '|'.join(torrent_hashes)
        return self._post('/torrents/setForceStart', {
            'hashes': hashes, 'value': str(force).lower(),
        })

    def set_save_path(self, torrent_hashes, location: str) -> bool:
        """修改种子保存目录（qB 会校验路径可写）。"""
        hashes = torrent_hashes if isinstance(torrent_hashes, str) else '|'.join(torrent_hashes)
        return self._post('/torrents/setLocation', {
            'hashes': hashes, 'location': location,
        })

    def rename(self, torrent_hash: str, new_name: str) -> bool:
        return self._post('/torrents/rename', {'hash': torrent_hash, 'name': new_name})

    # ---- 全局速度 / 状态 ----

    def transfer_info(self) -> Optional[Dict]:
        """全局传输状态：dl_info_speed / up_info_speed / dl_info_data / up_info_data 等。"""
        return self._get('/transfer/info')

    def get_global_download_limit(self) -> int:
        v = self._get('/transfer/downloadLimit')
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    def get_global_upload_limit(self) -> int:
        v = self._get('/transfer/uploadLimit')
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    def set_global_download_limit(self, bytes_per_sec: int) -> bool:
        """全局下载限速；0 = 无限。"""
        return self._post('/transfer/setDownloadLimit', {'limit': str(int(bytes_per_sec))})

    def set_global_upload_limit(self, bytes_per_sec: int) -> bool:
        return self._post('/transfer/setUploadLimit', {'limit': str(int(bytes_per_sec))})

    def get_alt_speed_limits_enabled(self) -> bool:
        v = self._get('/transfer/speedLimitsMode')
        return str(v).strip() == '1'

    def toggle_alt_speed_limits(self) -> bool:
        """切换备用速度限制开关（夜间限速）。"""
        return self._post('/transfer/toggleSpeedLimitsMode')

    # ---- 应用版本 ----

    def app_version(self) -> str:
        v = self._get('/app/version')
        return str(v) if v else ''

    def app_preferences(self) -> Optional[Dict]:
        return self._get('/app/preferences')

    def app_set_preferences(self, prefs: Dict) -> bool:
        """更新 qB 设置（部分字段即可，未传的不动）。
        常用字段：
          - rss_processing_enabled (bool): 启用 RSS 抓取
          - rss_auto_downloading_enabled (bool): 启用 RSS 自动下载
          - rss_max_articles_per_feed (int): 每 feed 保留文章数
          - rss_refresh_interval (int): RSS 刷新间隔（分钟）
        """
        import json
        return self._post('/app/setPreferences', {'json': json.dumps(prefs)})

    # ============================================================================
    # RSS 接口（read-only 透传 + 必要的 refresh）
    # ============================================================================

    def rss_items(self, with_data: bool = False) -> Optional[Dict]:
        """列出所有 feeds + 当前 items。with_data=True 含每条 item 全部字段。"""
        return self._get('/rss/items', {'withData': str(with_data).lower()})

    def rss_rules(self) -> Optional[Dict]:
        """列出所有 auto-download 规则。"""
        return self._get('/rss/rules')

    def rss_matching_articles(self, rule_name: str) -> Optional[Dict]:
        """看哪条规则匹配了哪些 article（调试用）。"""
        return self._get('/rss/matchingArticles', {'ruleName': rule_name})

    def rss_refresh_item(self, item_path: str) -> bool:
        """强制立刻刷新某个 feed 路径（如 'My Feed' 或 'Folder\\\\Feed'）。"""
        return self._post('/rss/refreshItem', {'itemPath': item_path})

    def rss_add_feed(self, url: str, path: Optional[str] = None) -> bool:
        """添加 RSS feed 订阅。
        url: feed URL；path: feed 在 qB 里的显示路径/名字（不传时 qB 自己取 feed title）。
        """
        data = {'url': url}
        if path:
            data['path'] = path
        return self._post('/rss/addFeed', data)

    def rss_remove_item(self, item_path: str) -> bool:
        """删除 feed / 文件夹（itemPath = feed 名或文件夹路径）。"""
        return self._post('/rss/removeItem', {'itemPath': item_path})

    def rss_mark_as_read(self, item_path: str, article_id: Optional[str] = None) -> bool:
        """把单条 article 标记为已读；不传 article_id 则把整个 feed 全标已读。"""
        data = {'itemPath': item_path}
        if article_id:
            data['articleId'] = article_id
        return self._post('/rss/markAsRead', data)

    def rss_set_rule(self, rule_name: str, rule_def: Dict) -> bool:
        """创建或更新自动下载规则。rule_def 字段见 qB API 文档（mustContain/savePath 等）。"""
        import json
        return self._post('/rss/setRule', {
            'ruleName': rule_name,
            'ruleDef': json.dumps(rule_def),
        })

    def rss_rename_rule(self, rule_name: str, new_rule_name: str) -> bool:
        """重命名规则。"""
        return self._post('/rss/renameRule', {
            'ruleName': rule_name,
            'newRuleName': new_rule_name,
        })

    def rss_remove_rule(self, rule_name: str) -> bool:
        """删除规则。"""
        return self._post('/rss/removeRule', {'ruleName': rule_name})
