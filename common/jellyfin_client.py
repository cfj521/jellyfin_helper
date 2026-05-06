"""
Jellyfin API 客户端
用于获取演员信息和上传演员图片
"""
import base64
import requests
import logging
from typing import Optional, List, Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)


# 媒体库展示顺序（数字越小越靠前）
COLLECTION_TYPE_ORDER = {
    'movies': 1,
    'tvshows': 2,
    'musicvideos': 3,
    'music': 4,
    'homevideos': 5,
    'boxsets': 6,
    'books': 7,
    'photos': 8,
    'mixed': 9,
}


def collection_type_rank(t: Optional[str]) -> int:
    """返回 collection_type 的排序序号；未知类型排到最后。"""
    return COLLECTION_TYPE_ORDER.get(t or 'mixed', 99)


class JellyfinClient:
    """Jellyfin API客户端"""

    def __init__(self, host: str, api_key: str):
        """
        初始化Jellyfin客户端

        Args:
            host: Jellyfin服务器地址，如 http://192.168.89.8:8096
            api_key: Jellyfin API密钥
        """
        self.host = host.rstrip('/')
        self.api_key = api_key
        self.headers = {
            'X-Emby-Token': api_key,
            'Content-Type': 'application/json'
        }

    def _request(
        self,
        method: str,
        endpoint: str,
        timeout: int = 30,
        retries: int = 0,
        **kwargs,
    ) -> Optional[Any]:
        """
        发送 API 请求。

        Args:
            timeout: 单次请求超时（秒），默认 30；慢操作（如 RemoteSearch）应传更大值
            retries: 网络错误（超时 / 连接错误）的重试次数，默认 0 不重试
                     仅对 Timeout / ConnectionError 重试，4xx/5xx 直接抛
        """
        url = f"{self.host}{endpoint}"
        attempt = 0
        last_exc = None
        while attempt <= retries:
            try:
                response = requests.request(
                    method, url,
                    headers=self.headers,
                    timeout=timeout,
                    **kwargs,
                )
                response.raise_for_status()
                if response.content:
                    return response.json()
                return None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_exc = e
                attempt += 1
                if attempt <= retries:
                    logger.warning(
                        f"API 请求超时/连接失败，重试 {attempt}/{retries}: {url} - {e}"
                    )
                    continue
                logger.error(f"API 请求失败（已重试 {retries} 次）: {url} - {e}")
                raise
            except requests.exceptions.RequestException as e:
                # HTTPError / 其他 4xx/5xx：不重试，直接抛
                logger.error(f"API 请求失败: {url} - {e}")
                raise
        # 不应到达这里
        if last_exc:
            raise last_exc
        return None

    def test_connection(self) -> bool:
        """测试服务器连接"""
        try:
            info = self._request('GET', '/System/Info')
            if info:
                logger.info(f"连接成功: Jellyfin {info.get('Version', 'Unknown')}")
                return True
        except Exception as e:
            logger.error(f"连接失败: {e}")
        return False

    def get_libraries(self) -> List[Dict]:
        """获取所有媒体库（VirtualFolders 原始数据）"""
        result = self._request('GET', '/Library/VirtualFolders')
        return result or []

    # ---------- 重新识别（刮削元数据）----------
    # 来自官方 RemoteSearch API：
    #   POST /Items/RemoteSearch/Movie  | /Series | /BoxSet | /Person ... 拿候选
    #   POST /Items/RemoteSearch/Apply/{itemId}     把候选应用到现有条目并自动刷新

    # ItemType (BaseItemKind) 与 RemoteSearch 端点的对应表
    _REMOTE_SEARCH_ENDPOINTS = {
        'Movie':       '/Items/RemoteSearch/Movie',
        'Series':      '/Items/RemoteSearch/Series',
        'BoxSet':      '/Items/RemoteSearch/BoxSet',
        'Person':      '/Items/RemoteSearch/Person',
        'MusicAlbum':  '/Items/RemoteSearch/MusicAlbum',
        'MusicArtist': '/Items/RemoteSearch/MusicArtist',
        'MusicVideo':  '/Items/RemoteSearch/MusicVideo',
        'Trailer':     '/Items/RemoteSearch/Trailer',
        'Book':        '/Items/RemoteSearch/Book',
    }

    def remote_search(
        self,
        item_id: str,
        item_type: str,
        name: Optional[str] = None,
        year: Optional[int] = None,
        provider_ids: Optional[Dict[str, str]] = None,
        provider_name: Optional[str] = None,
        language: str = "zh-CN",
    ) -> List[Dict]:
        """
        在远端 metadata provider 搜候选条目。

        Args:
            item_id: 现有 Jellyfin 条目 ID（apply 时会用到，搜索时也带上以让 jellyfin 知道上下文）
            item_type: BaseItemKind，如 'Movie' / 'Series' / 'Folder'（Folder 按 Movie 处理）
            name: 标题；和 provider_ids 二选一即可
            year: 年份（可选）
            provider_ids: 例如 {'Tmdb': '27205'}；给了就直接精确锁定
            provider_name: 例如 'TheMovieDb'；不给的话会查所有启用的 provider
            language: 元数据语言

        Returns:
            RemoteSearchResult 列表，每个含 Name / Year / ProviderIds / ImageUrl / Overview 等
        """
        # Folder 类型本质上是未识别的目录，按 Movie 搜
        endpoint = self._REMOTE_SEARCH_ENDPOINTS.get(
            item_type if item_type in self._REMOTE_SEARCH_ENDPOINTS else 'Movie',
            '/Items/RemoteSearch/Movie',
        )

        search_info: Dict[str, Any] = {
            'IsAutomated': False,
            'MetadataLanguage': language,
        }
        if name:
            search_info['Name'] = name
        if year:
            search_info['Year'] = year
        if provider_ids:
            search_info['ProviderIds'] = provider_ids

        body: Dict[str, Any] = {
            'SearchInfo': search_info,
            'ItemId': item_id,
            'IncludeDisabledProviders': False,
        }
        if provider_name:
            body['SearchProviderName'] = provider_name

        # RemoteSearch 是慢操作 —— Jellyfin 内部要去 TMDB 拉候选+海报缩略图，
        # 经典 IP（X-Men 系列等）候选很多时 30 秒经常不够。给 90 秒 + 1 次重试
        # 应对偶尔的网络抖动
        result = self._request('POST', endpoint, json=body, timeout=90, retries=1)
        return result or []

    def delete_item(self, item_id: str) -> bool:
        """
        删除 Jellyfin 条目。
        默认行为（用户有 EnableContentDeletion 权限时）：连同物理文件一起删除。
        没权限的话 Jellyfin 会拒绝，前端会收到错误。
        """
        self._request('DELETE', f'/Items/{item_id}')
        return True

    def get_item(self, item_id: str, fields: str = 'Path,MediaSources,RunTimeTicks') -> Optional[Dict]:
        """根据 itemId 获取单条 Jellyfin 条目（用于取证前查 Path / RunTime）。"""
        # 用 /Items?Ids={id} 不强制要 userId，比 /Users/{u}/Items/{i} 通用
        params = {'Ids': item_id, 'Fields': fields}
        result = self._request('GET', '/Items', params=params)
        items = (result or {}).get('Items') or []
        return items[0] if items else None

    def remote_search_apply(
        self,
        item_id: str,
        candidate: Dict,
        replace_all_images: bool = True,
    ) -> bool:
        """
        把搜来的候选 apply 到现有 itemId。Jellyfin 内部异步触发 metadata refresh。

        Args:
            candidate: remote_search() 返回列表中的一项整对象
            replace_all_images: 是否覆盖已有图片（true=换新海报/背景）

        Returns:
            True 表示请求成功（Jellyfin 返回 204）
        """
        params = {'replaceAllImages': str(replace_all_images).lower()}
        # _request 已经处理 raise_for_status；204 也算成功
        try:
            self._request(
                'POST',
                f'/Items/RemoteSearch/Apply/{item_id}',
                params=params,
                json=candidate,
            )
            return True
        except Exception as e:
            logger.error(f"Apply 失败 itemId={item_id}: {e}")
            raise

    def get_libraries_normalized(self) -> List[Dict]:
        """
        获取媒体库的友好结构（已按"电影 → 剧集 → 音乐 → ..."固定顺序排序）。

        每条返回：
          - id: ItemId（Jellyfin GUID，重启稳定，删除重建会变）
          - name: 库名称
          - collection_type: movies / tvshows / music / mixed / homevideos / boxsets / books / etc
          - locations: 路径列表（一个库可包含多个目录）
          - primary_image_item_id: 库封面图所属条目 ID
          - refresh_status: 刷新状态
        """
        raw = self.get_libraries()
        out = []
        for v in raw:
            out.append({
                "id": v.get('ItemId'),
                "name": v.get('Name'),
                "collection_type": v.get('CollectionType') or 'mixed',
                "locations": v.get('Locations') or [],
                "primary_image_item_id": v.get('PrimaryImageItemId'),
                "refresh_status": v.get('RefreshStatus'),
            })
        # 按 (类型优先级, 名字) 排序
        out.sort(key=lambda x: (collection_type_rank(x['collection_type']), x['name'] or ''))
        return out

    def get_library_items_page(
        self,
        parent_id: str,
        start_index: int = 0,
        limit: int = 50,
        item_types: Optional[str] = None,
        recursive: bool = True,
        fields: str = "Path,ProductionYear,ImageTags,ProviderIds",
        sort_by: str = "SortName",
        sort_order: str = "Ascending",
        search_term: Optional[str] = None,
    ) -> Dict:
        """
        分页获取库内条目。
        返回 {'items': [...], 'total': int}

        search_term：可选，按名称模糊匹配（Jellyfin 原生 SearchTerm 参数）。
        Jellyfin 服务端做匹配，跨整个库，不限于当前分页。
        """
        params: Dict = {
            'ParentId': parent_id,
            'Recursive': str(recursive).lower(),
            'Fields': fields,
            'StartIndex': start_index,
            'Limit': max(1, min(limit, 500)),  # jellyfin 单页上限保守取 500
            'SortBy': sort_by,
            'SortOrder': sort_order,
        }
        if item_types:
            params['IncludeItemTypes'] = item_types
        if search_term:
            params['SearchTerm'] = search_term

        result = self._request('GET', '/Items', params=params) or {}
        return {
            'items': result.get('Items') or [],
            'total': result.get('TotalRecordCount') or 0,
        }

    # ---------- Series 钻取（剧集 → 季 → 集）----------

    def get_seasons_of_series(
        self,
        series_id: str,
        fields: str = "Path,ProductionYear,ImageTags,ProviderIds,ChildCount,Overview",
    ) -> List[Dict]:
        """
        拉某部剧的全部 Season（按季号自然排序，Jellyfin 自带）。

        用 /Shows/{seriesId}/Seasons 而不是 /Items?ParentId=...，因为前者是
        Jellyfin 官方的"取剧的季"语义端点，会自动按 IndexNumber 排序、
        过滤掉一些垃圾 virtual season。
        """
        params = {
            'Fields': fields,
        }
        result = self._request('GET', f'/Shows/{series_id}/Seasons', params=params) or {}
        return result.get('Items') or []

    def get_episodes_of_season(
        self,
        season_id: str,
        fields: str = "Path,ProductionYear,ImageTags,ProviderIds,RunTimeTicks,Overview,IndexNumber,ParentIndexNumber,SeasonName,SeriesName,SeriesId",
    ) -> List[Dict]:
        """
        拉某季下的所有 Episode（按 IndexNumber 排序）。

        用 /Items?ParentId={seasonId}&IncludeItemTypes=Episode，避免引入 series_id
        参数（季展开时只有自己的 id）。Jellyfin 自然把 ParentIndexNumber 当作排序键。

        Episodes 自带：
          IndexNumber       → 集号 (3 = 第 3 集)
          ParentIndexNumber → 季号 (1 = 第一季)
          SeasonName        → "第一季" / "Season 1"（按 Jellyfin 语言）
          SeriesName        → 剧名（用于 Episode 行可视化）
          SeriesId          → 父剧 id（前端跳转 Series 详情用）
        """
        params = {
            'ParentId': season_id,
            'IncludeItemTypes': 'Episode',
            'Recursive': 'false',  # ParentId 已经精确到 Season，不递归更快
            'Fields': fields,
            'SortBy': 'ParentIndexNumber,IndexNumber',
            'SortOrder': 'Ascending',
        }
        result = self._request('GET', '/Items', params=params) or {}
        return result.get('Items') or []

    def get_library_items(
        self,
        parent_id: str,
        item_types: Optional[str] = None,
        recursive: bool = True,
        fields: str = "Path,ProductionYear,ImageTags,ProviderIds",
        limit: int = 0,
    ) -> List[Dict]:
        """
        列出某个媒体库下的条目。

        Args:
            parent_id: 媒体库 ItemId（来自 get_libraries_normalized）
            item_types: 'Movie' / 'Series' / 'Episode' 等；多类用逗号分隔
            recursive: 是否递归子集
            limit: 0 表示不限
        """
        all_items: List[Dict] = []
        start = 0
        page = 500
        while True:
            params: Dict = {
                'ParentId': parent_id,
                'Recursive': str(recursive).lower(),
                'Fields': fields,
                'StartIndex': start,
                'Limit': page,
            }
            if item_types:
                params['IncludeItemTypes'] = item_types

            result = self._request('GET', '/Items', params=params)
            if not result or 'Items' not in result:
                break
            items = result['Items']
            if not items:
                break
            all_items.extend(items)
            total = result.get('TotalRecordCount', 0)
            start += len(items)
            if (limit and len(all_items) >= limit) or start >= total:
                break
        return all_items[:limit] if limit else all_items

    # Jellyfin 三种标准刷新模式（与官方 Web UI 的"扫描媒体库"对话框对齐）
    REFRESH_MODES = {
        # 仅扫描新增/修改的文件，最快，日常使用
        'scan_changes': {
            'MetadataRefreshMode': 'Default',
            'ImageRefreshMode': 'Default',
            'ReplaceAllMetadata': 'false',
            'ReplaceAllImages': 'false',
        },
        # 为缺失元数据/海报的条目重新查询，不覆盖已有元数据
        'missing_metadata': {
            'MetadataRefreshMode': 'FullRefresh',
            'ImageRefreshMode': 'FullRefresh',
            'ReplaceAllMetadata': 'false',
            'ReplaceAllImages': 'false',
        },
        # 重新刮削全部内容并覆盖（包括手动修改的）
        'replace_all': {
            'MetadataRefreshMode': 'FullRefresh',
            'ImageRefreshMode': 'FullRefresh',
            'ReplaceAllMetadata': 'true',
            'ReplaceAllImages': 'true',
        },
    }

    def refresh_library(self, library_id: str, mode: str = 'scan_changes', recursive: bool = True) -> bool:
        """
        触发某个媒体库刷新元数据。等价于 Jellyfin 后台"扫描媒体库"对话框。

        Args:
            library_id: 库 ItemId
            mode: 刷新模式，三选一
                - 'scan_changes'      仅扫描变更（默认，最快）
                - 'missing_metadata'  搜索缺少的元数据
                - 'replace_all'       替换所有元数据（覆盖手动修改）
            recursive: 是否递归处理子项
        """
        params = self.REFRESH_MODES.get(mode)
        if params is None:
            logger.warning(f"未知的刷新模式 {mode}，回退到 scan_changes")
            params = self.REFRESH_MODES['scan_changes']

        try:
            self._request(
                'POST',
                f'/Items/{library_id}/Refresh',
                params={
                    'Recursive': str(recursive).lower(),
                    **params,
                },
            )
            logger.info(f"已触发媒体库刷新: {library_id} (mode={mode})")
            return True
        except Exception as e:
            logger.error(f"刷新媒体库失败: {library_id} - {e}")
            return False

    def refresh_all_libraries(self) -> bool:
        """触发所有媒体库的刷新（全局扫描）。"""
        try:
            self._request('POST', '/Library/Refresh')
            logger.info("已触发全局媒体库刷新")
            return True
        except Exception as e:
            logger.error(f"全局刷新失败: {e}")
            return False

    def get_system_info(self) -> Optional[Dict]:
        """获取 Jellyfin 系统信息（版本、操作系统、配置目录等）"""
        return self._request('GET', '/System/Info')

    def get_all_persons(self) -> List[Dict]:
        """
        获取所有演员/人物（分页获取，无数量限制）

        Returns:
            演员列表，每个演员包含Id, Name, ImageTags等信息
        """
        all_persons = []
        start_index = 0
        page_size = 5000

        while True:
            params = {
                'Recursive': 'true',
                'IncludeItemTypes': 'Person',
                'Fields': 'PrimaryImageAspectRatio,ImageTags,ProviderIds',
                'StartIndex': start_index,
                'Limit': page_size
            }
            result = self._request('GET', '/Items', params=params)

            if not result or 'Items' not in result:
                break

            items = result['Items']
            all_persons.extend(items)

            # 检查是否还有更多
            total = result.get('TotalRecordCount', 0)
            start_index += len(items)

            if start_index >= total or len(items) == 0:
                break

            logger.info(f"已获取 {start_index}/{total} 位演员...")

        return all_persons

    def get_person_details(self, person_id: str) -> Optional[Dict]:
        """获取演员详细信息"""
        return self._request('GET', f'/Items/{person_id}')

    def has_primary_image(self, person: Dict) -> bool:
        """检查演员是否有主图片"""
        image_tags = person.get('ImageTags', {})
        return 'Primary' in image_tags and bool(image_tags['Primary'])

    def get_persons_without_image(self) -> List[Dict]:
        """获取所有没有图片的演员"""
        all_persons = self.get_all_persons()
        return [p for p in all_persons if not self.has_primary_image(p)]

    def get_person_image_url(self, person_id: str) -> str:
        """获取演员图片URL"""
        return f"{self.host}/Items/{person_id}/Images/Primary"

    def upload_person_image(self, person_id: str, image_data: bytes) -> bool:
        """
        上传演员图片

        Args:
            person_id: 演员ID
            image_data: 图片二进制数据

        Returns:
            是否上传成功
        """
        url = f"{self.host}/Items/{person_id}/Images/Primary"
        # Jellyfin API 需要 base64 编码的图片数据
        image_base64 = base64.b64encode(image_data).decode('utf-8')
        headers = {
            'X-Emby-Token': self.api_key,
            'Content-Type': 'image/jpeg'
        }
        try:
            response = requests.post(url, headers=headers, data=image_base64, timeout=60)
            response.raise_for_status()
            logger.info(f"图片上传成功: {person_id}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"图片上传失败: {person_id} - {e}")
            return False

    def get_person_tmdb_id(self, person: Dict) -> Optional[str]:
        """获取演员的TMDB ID（如果有）"""
        provider_ids = person.get('ProviderIds', {})
        return provider_ids.get('Tmdb')

    # ---------- 影视条目（电影/剧集） ----------

    def get_items_by_type(self, include_types: str, fields: str = "ImageTags,ProviderIds,ProductionYear,Path") -> List[Dict]:
        """
        获取指定类型的所有条目（分页）。

        Args:
            include_types: Jellyfin item type，如 'Movie' 或 'Series'
        """
        all_items: List[Dict] = []
        start = 0
        page = 500
        while True:
            params = {
                'Recursive': 'true',
                'IncludeItemTypes': include_types,
                'Fields': fields,
                'StartIndex': start,
                'Limit': page,
            }
            result = self._request('GET', '/Items', params=params)
            if not result or 'Items' not in result:
                break
            items = result['Items']
            if not items:
                break
            all_items.extend(items)
            total = result.get('TotalRecordCount', 0)
            start += len(items)
            if start >= total:
                break
            logger.info(f"已获取 {start}/{total} 个 {include_types}")
        return all_items

    def get_all_movies(self) -> List[Dict]:
        return self.get_items_by_type('Movie')

    def get_all_series(self) -> List[Dict]:
        return self.get_items_by_type('Series')

    @staticmethod
    def has_image(item: Dict, image_type: str = 'Primary') -> bool:
        tags = item.get('ImageTags') or {}
        return bool(tags.get(image_type))

    @staticmethod
    def has_backdrop(item: Dict) -> bool:
        return bool(item.get('BackdropImageTags'))

    @staticmethod
    def get_tmdb_id(item: Dict) -> Optional[str]:
        return (item.get('ProviderIds') or {}).get('Tmdb')

    def upload_item_image(self, item_id: str, image_data: bytes, image_type: str = 'Primary') -> bool:
        """
        上传 item 的图片（电影海报、剧集 Primary、背景图等）。

        image_type:
          - 'Primary'   海报
          - 'Backdrop'  背景图
          - 'Logo'      Logo
          - 'Thumb'     缩略图
        """
        url = f"{self.host}/Items/{item_id}/Images/{image_type}"
        image_base64 = base64.b64encode(image_data).decode('utf-8')
        headers = {
            'X-Emby-Token': self.api_key,
            'Content-Type': 'image/jpeg',
        }
        try:
            response = requests.post(url, headers=headers, data=image_base64, timeout=60)
            response.raise_for_status()
            logger.info(f"图片上传成功: {item_id} ({image_type})")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"图片上传失败: {item_id} ({image_type}) - {e}")
            return False

    def refresh_person(self, person_id: str) -> bool:
        """刷新演员元数据"""
        try:
            self._request(
                'POST',
                f'/Items/{person_id}/Refresh',
                params={
                    'Recursive': 'true',
                    'ImageRefreshMode': 'FullRefresh',
                    'MetadataRefreshMode': 'FullRefresh'
                }
            )
            return True
        except Exception:
            return False
