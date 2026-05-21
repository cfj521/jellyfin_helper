"""
Jellyfin SQLite 直读封装（10.9+ 用 BaseItems 主表，旧版 TypedBaseItems 不支持）。

只读 + immutable + nolock：跨 SMB 挂载读 jellyfin 服务器的 library.db 也安全。
  - immutable=1 跳过 WAL 检查和 journal 创建（这是跨 SMB 必需的）
  - nolock=1 跳过 OS 锁（jellyfin 同时在写也不冲突；理论上有读到撕裂帧的可能，
                          但 jellyfin 写入频率极低，可忽略）
  - mode=ro 防止任何写操作误改 DB

所有查询失败抛 JellyfinDBError，由上层 fallback 到 REST API。
"""
import logging
import sqlite3
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


# jellyfin 用完整 .NET 类名作 Type 列值，映射成 REST API 的短名（兼容现有调用方）
_TYPE_MAP = {
    'MediaBrowser.Controller.Entities.Movies.Movie': 'Movie',
    'MediaBrowser.Controller.Entities.TV.Series':    'Series',
    'MediaBrowser.Controller.Entities.TV.Episode':   'Episode',
    'MediaBrowser.Controller.Entities.TV.Season':    'Season',
    'MediaBrowser.Controller.Entities.Folder':       'Folder',
    'MediaBrowser.Controller.Entities.Video':        'Video',
    'MediaBrowser.Controller.Entities.Person':       'Person',
    'MediaBrowser.Controller.Entities.Studio':       'Studio',
    'MediaBrowser.Controller.Entities.Genre':        'Genre',
    'MediaBrowser.Controller.Entities.CollectionFolder': 'CollectionFolder',
}


class JellyfinDBError(Exception):
    """JellyfinDB 操作失败 —— 调用方应 fallback 到 REST API。"""


class JellyfinDB:
    """jellyfin library.db 只读访问。

    用法：
        db = JellyfinDB('/mnt/jellyfin/jellyfin.db')
        info = db.find_by_path('/library/videos/movie/Foo.mkv')  # 返回 dict 或 None
        # 异常：JellyfinDBError(db 不可用 / schema 不匹配)

    设计：schema 探测一次缓存结果；连接每次重开（SQLite 连接不该跨线程复用）。
    """

    # 每张依赖表的必需列。任何一个不满足都禁用直读模式 → fallback REST
    # 单点维护：将来 jellyfin schema 变更，先在这里加新列名作 OR 兼容（或加新版本探测），
    # 永远不要让 SQL 引用没在这里声明过的列
    _REQUIRED_SCHEMA = {
        'BaseItems': {
            'Id', 'Type', 'Path', 'Name', 'ProductionYear',
            'RunTimeTicks', 'TopParentId', 'IsVirtualItem',
        },
        'BaseItemProviders': {'ItemId', 'ProviderId', 'ProviderValue'},
        'BaseItemImageInfos': {'ItemId', 'ImageType'},
    }

    def __init__(self, db_path: str):
        self.db_path = Path(db_path) if db_path else None
        # None=未探测；True/False=已知
        self._schema_ok: Optional[bool] = None

    def is_available(self) -> bool:
        """快速判断 DB 可用否（懒探测 + 缓存）。失败原因写 warning，不抛。

        探测覆盖所有依赖的表/列（不止主表）。schema 任一不匹配 → False，
        后续调用直接被本方法拦截，不会到 SQL 执行才崩。
        """
        if self._schema_ok is not None:
            return self._schema_ok
        if not self.db_path:
            self._schema_ok = False
            return False
        if not self.db_path.exists():
            logger.warning(f"JellyfinDB 文件不存在: {self.db_path}")
            self._schema_ok = False
            return False
        # 权限预检：sqlite3.connect 失败时只报"unable to open database file"，
        # 区分不出"权限"还是别的，先 os.access 探一下给运维明确建议。
        # 注意：常见的 jellyfin 部署 db 文件是 0644 + 父目录 0755，所有人可读；
        # 仅在发行版/运维手动收紧到 0750 时才需要把后端 user 加进 jellyfin 组
        import os
        if not os.access(str(self.db_path), os.R_OK):
            parent = self.db_path.parent
            logger.warning(
                f"JellyfinDB 文件无读权限: {self.db_path}\n"
                f"  → 检查文件本身权限: ls -l {self.db_path}\n"
                f"  → 检查父目录可达: ls -ld {parent}\n"
                f"  → 若父目录是 0750（owner=jellyfin），把后端进程 user 加进 jellyfin 组:\n"
                f"    sudo usermod -a -G jellyfin <后端 user> 然后重启后端进程"
            )
            self._schema_ok = False
            return False
        try:
            with self._connect() as con:
                cur = con.cursor()
                for table, need_cols in self._REQUIRED_SCHEMA.items():
                    cur.execute(f"PRAGMA table_info({table})")
                    cols = {r[1] for r in cur.fetchall()}
                    if not cols:
                        logger.warning(
                            f"JellyfinDB schema 缺表 {table!r}（"
                            f"jellyfin < 10.9 用 TypedBaseItems / 升级后改名？），禁用直读 → 走 REST"
                        )
                        self._schema_ok = False
                        return False
                    missing = need_cols - cols
                    if missing:
                        logger.warning(
                            f"JellyfinDB schema 表 {table!r} 缺列 {missing}，"
                            f"禁用直读 → 走 REST"
                        )
                        self._schema_ok = False
                        return False
            self._schema_ok = True
            logger.info(f"JellyfinDB 已挂载: {self.db_path}")
            return True
        except Exception as e:
            logger.warning(f"JellyfinDB schema 探测失败 db_path={self.db_path}: {e} → 走 REST")
            self._schema_ok = False
            return False

    def _disable(self, reason: str):
        """永久禁用直读（直到进程重启）。供 SQL 抛错时调用，避免每次重试都崩 + 刷屏。"""
        if self._schema_ok is not False:
            logger.warning(f"JellyfinDB 进入禁用状态：{reason} → 后续全走 REST")
        self._schema_ok = False

    def _connect(self) -> sqlite3.Connection:
        # immutable=1 + nolock=1 是跨 SMB 读 SQLite 的关键，绝不能省
        uri = f"file:{self.db_path.as_posix()}?mode=ro&nolock=1&immutable=1"
        return sqlite3.connect(uri, uri=True, timeout=3.0)

    # ---------- 核心接口 ----------

    def find_by_path(self, path: str) -> Optional[Dict[str, Any]]:
        """精确匹配 Path 反查 item。

        返回字段与 medialibraries._build_path_index 的 info 兼容：
            {id, name, type, year, runtime_min, tmdb_id, imdb_id, has_image, _via}
        其中 `_via='db'` 标记数据来源（性能 benchmark 用）。

        path 必须是 jellyfin 容器视角的路径（如 /library/videos/movie/X.mkv）。
        本机路径反向翻译由调用方负责（避免本类依赖 backend.path_translator）。

        查不到返回 None；DB 不可用抛 JellyfinDBError。
        """
        if not self.is_available():
            raise JellyfinDBError("JellyfinDB unavailable")
        if not path:
            return None

        # 一次 join 拿全：tmdb/imdb 在 BaseItemProviders；primary image 在 BaseItemImageInfos
        # ImageType=0 = Primary（jellyfin 内部枚举）
        sql = """
            SELECT b.Id, b.Type, b.Name, b.ProductionYear, b.RunTimeTicks,
                   (SELECT ProviderValue FROM BaseItemProviders
                    WHERE ItemId = b.Id AND ProviderId = 'Tmdb' LIMIT 1) AS tmdb,
                   (SELECT ProviderValue FROM BaseItemProviders
                    WHERE ItemId = b.Id AND ProviderId = 'Imdb' LIMIT 1) AS imdb,
                   (SELECT 1 FROM BaseItemImageInfos
                    WHERE ItemId = b.Id AND ImageType = 0 LIMIT 1) AS has_primary
            FROM BaseItems b
            WHERE b.Path = ? AND b.IsVirtualItem = 0
            LIMIT 1
        """
        try:
            with self._connect() as con:
                cur = con.cursor()
                cur.execute(sql, (path,))
                row = cur.fetchone()
        except sqlite3.OperationalError as e:
            # "no such column / table" 等 schema 错 → 永久禁用，本次抛
            # 其他 OperationalError（locked / busy / IO error）也走永久禁用 ——
            # SMB 持续抖动的话进程重启后会重新探测；本进程内不必反复试错刷屏
            self._disable(f"find_by_path SQL 错误 path={path!r}: {e}")
            raise JellyfinDBError(str(e)) from e
        except Exception as e:
            # 罕见的非 OperationalError（如 sqlite3.DatabaseError 文件损坏）
            self._disable(f"find_by_path 异常 path={path!r}: {e}")
            raise JellyfinDBError(str(e)) from e

        if not row:
            return None

        item_id, item_type, name, year, rt, tmdb, imdb, has_primary = row
        return {
            'id': _normalize_guid(item_id),
            'name': name,
            'type': _TYPE_MAP.get(item_type, item_type),
            'year': year,
            'runtime_min': round(rt / 600_000_000.0, 1) if rt else None,
            'tmdb_id': tmdb,
            'imdb_id': imdb,
            'has_image': bool(has_primary),
            '_via': 'db',
        }


def _normalize_guid(s: str) -> str:
    """jellyfin DB 存 36-char GUID（带连字符大写）；REST API 返回 32-char 小写 hex。
    统一返回 32-char 小写 hex 跟 REST 行为对齐。"""
    if not s:
        return s
    return s.replace('-', '').lower()


# ---------- 模块级单例（懒初始化）----------

_singleton: Optional[JellyfinDB] = None


def get_jellyfin_db() -> JellyfinDB:
    """拿模块级单例。settings.jellyfin_db_path 没配时返回的实例 is_available() = False。"""
    global _singleton
    if _singleton is None:
        try:
            from backend.config import settings
            db_path = getattr(settings, 'jellyfin_db_path', '') or ''
        except Exception:
            db_path = ''
        _singleton = JellyfinDB(db_path)
    return _singleton
