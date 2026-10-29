"""回填历史下载的原始 release 名。

背景：`source_name` 是后加的字段。新下载会在 confirm/push 时直接记录 qB 的
torrent name；但功能上线前已经在库里、且 qB 里还保留种子的条目仍然是空值。
本脚本用 dispatch_map.torrent_hash 去 qB 反查 name，再回填到
download_dispatch_map / media_items / adult_items。

用法（服务器上，项目根目录）：
    /opt/miniconda3/envs/env_jellyfin_helper/bin/python -m tools.backfill_source_names --dry-run
    /opt/miniconda3/envs/env_jellyfin_helper/bin/python -m tools.backfill_source_names

qB 已经删种/清种的历史条目无法反查原始 release 名，只能保持空值；
脚本会统计这类条目数量。
"""
from __future__ import annotations

import argparse
import logging

from sqlalchemy import or_

from backend.config import settings
from backend.database import AdultItem, DownloadDispatchMap, MediaItem, SessionLocal
from backend.services.source_names import record_source_names_for_paths

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)
logger = logging.getLogger('backfill_source_names')


def _empty(value) -> bool:
    return not value or not str(value).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description='回填 qB 原始 release 名')
    parser.add_argument('--dry-run', action='store_true', help='只打印，不写库')
    parser.add_argument('--limit', type=int, default=0, help='最多处理多少条（0=全部）')
    args = parser.parse_args()

    from common.qbittorrent_client import QBittorrentClient

    qb = QBittorrentClient(
        settings.qbittorrent_host,
        settings.qbittorrent_username,
        settings.qbittorrent_password,
    )
    if not qb.login():
        logger.error('qBittorrent 登录失败，无法反查历史 release 名')
        return 1

    recovered = 0
    missing = 0
    propagated = 0

    with SessionLocal() as db:
        query = (
            db.query(DownloadDispatchMap)
            .filter(or_(
                DownloadDispatchMap.source_name.is_(None),
                DownloadDispatchMap.source_name == '',
            ))
            .order_by(DownloadDispatchMap.created_at.desc())
        )
        if args.limit:
            query = query.limit(args.limit)
        rows = query.all()
        logger.info(f'待回填 dispatch 行: {len(rows)}')

        for row in rows:
            try:
                meta = qb.get_torrent_meta(row.torrent_hash) or {}
                name = (meta.get('name') or '').strip()
            except Exception as e:
                logger.warning(f'查询 qB 失败 hash={row.torrent_hash[:16]}..: {e}')
                name = ''

            if not name:
                missing += 1
                continue

            recovered += 1
            if args.dry_run:
                logger.info(f'[dry-run] {row.torrent_hash[:16]}.. {name[:100]}')
                continue

            row.source_name = name
            db.commit()

            # 回填已经入库的媒体/成人条目（按 dispatched_files 路径匹配）
            n = record_source_names_for_paths(
                db, name, row.dispatched_files or [],
            )
            propagated += n
            logger.info(
                f'回填 hash={row.torrent_hash[:16]}.. rows={n} '
                f'name={name[:100]}'
            )

        if args.dry_run:
            logger.info(
                f'[dry-run] 可回填 {recovered} / 无种子 {missing} / 未写库'
            )
        else:
            # qB 已删种、但 media_items 里可能已经有 source_name 的条目不需要动
            media_with = (
                db.query(MediaItem).filter(MediaItem.source_name.isnot(None)).count()
            )
            adult_with = (
                db.query(AdultItem).filter(AdultItem.source_name.isnot(None)).count()
            )
            logger.info(
                f'完成：dispatch 回填 {recovered}，无种子 {missing}，'
                f'媒体条目回填 {propagated}；'
                f'当前 source_name 非空 media={media_with} adult={adult_with}'
            )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
