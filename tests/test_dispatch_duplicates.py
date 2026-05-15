"""跨种子目标冲突去重测试。

覆盖 quality.py + duplicate.resolve() + copier on_displace 钩子 + organizer 跳过逻辑。
不依赖 PostgreSQL：用 MagicMock 模拟 db.query() 链。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.dispatch import quality
from tools.dispatch.duplicate import (
    DuplicateConflictError,
    ACTION_PROCEED,
    ACTION_SKIP,
    resolve as duplicate_resolve,
)


# ---------- quality 模块：纯函数 ----------

@pytest.mark.parametrize("name,expected_min", [
    ("Movie.2024.2160p.BluRay.Remux.HDR.mkv", 48),   # 2160p+remux+hdr
    ("Movie.2024.2160p.WEB-DL.mkv",           44),   # 2160p+web-dl
    ("Movie.2024.1080p.BluRay.x264.mkv",      36),   # 1080p+bluray
    ("Movie.2024.1080p.WEB-DL.mkv",           34),   # 1080p+web-dl
    ("Movie.2024.720p.HDTV.mkv",              22),   # 720p+hdtv
])
def test_quality_extract_tier_baseline(name, expected_min):
    assert quality.extract_tier(name) >= expected_min


def test_quality_higher_resolution_beats_lower():
    a = quality.extract_tier("X.2160p.WEB-DL.mkv")
    b = quality.extract_tier("X.1080p.BluRay.Remux.mkv")
    assert a > b, f"2160p({a}) should beat 1080p remux({b})"


def test_quality_repack_detection():
    assert quality.is_repack("Movie.2024.1080p.PROPER.BluRay.mkv")
    assert quality.is_repack("Movie.S01E01.REPACK.720p.mkv")
    assert quality.is_repack("Movie.RERIP.1080p.WEB-DL.mkv")
    assert not quality.is_repack("Movie.2024.1080p.BluRay.mkv")


def test_quality_compare_4k_beats_1080p():
    # D4 升级场景
    old = "Movie.2024.1080p.BluRay.x264-GROUP.mkv"
    new = "Movie.2024.2160p.UHD.BluRay.Remux.HDR-OTHER.mkv"
    assert quality.compare(old, new) == 'new_wins'


def test_quality_compare_lower_loses():
    old = "Movie.2024.2160p.UHD.BluRay.mkv"
    new = "Movie.2024.1080p.WEB-DL.mkv"
    assert quality.compare(old, new) == 'old_wins'


def test_quality_compare_repack_wins_on_tie():
    # D7 同质量修订版
    old = "Movie.2024.1080p.BluRay.mkv"
    new = "Movie.2024.1080p.BluRay.PROPER.mkv"
    assert quality.compare(old, new) == 'new_wins'


def test_quality_compare_tie_returns_tie():
    old = "Movie.2024.1080p.BluRay.mkv"
    new = "Other.Movie.1080p.BluRay.mkv"
    assert quality.compare(old, new) == 'tie'


# ---------- duplicate.resolve：mock DB ----------

def _mk_db_no_conflict():
    """db.query(...).filter(...).filter(...).filter(...).first() = None"""
    db = MagicMock()
    db.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
    return db


def _mk_db_with_existing(*, hash_='aaaa' * 16, dispatched=('/lib/Old.1080p.mkv',), phase='all_jobs_done', title='Old'):
    existing = MagicMock()
    existing.torrent_hash = hash_
    existing.dispatched_files = list(dispatched)
    existing.phase = phase
    existing.title = title
    db = MagicMock()
    db.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = existing
    return db, existing


def test_resolve_no_conflict_proceeds(tmp_path):
    db = _mk_db_no_conflict()
    out = duplicate_resolve(
        db=db, current_hash='bbbb' * 16, current_release_name='X.2160p.mkv',
        src=tmp_path / 'X.2160p.mkv', dst=tmp_path / 'lib' / 'X.2160p.mkv',
        policy='higher_quality_wins', trash_dir=tmp_path / 'trash',
    )
    assert out == ACTION_PROCEED


def test_resolve_higher_quality_wins(tmp_path):
    # D4: 1080p 已在库，4K Remux 来 → 旧入 trash，proceed
    dst = tmp_path / 'lib' / 'Movie.mkv'
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b'old')

    db, existing = _mk_db_with_existing(dispatched=[str(dst)])
    existing.dispatched_files = [str(dst)]
    existing.title = 'Movie.2024.1080p.BluRay.x264.mkv'

    src = tmp_path / 'Movie.2024.2160p.UHD.BluRay.Remux.HDR.mkv'
    src.write_bytes(b'new')

    out = duplicate_resolve(
        db=db, current_hash='bbbb' * 16, current_release_name=src.name,
        src=src, dst=dst,
        policy='higher_quality_wins', trash_dir=tmp_path / 'trash',
    )
    assert out == ACTION_PROCEED
    assert not dst.exists(), "旧 dst 应该被移走"
    moved = list((tmp_path / 'trash' / '_replaced').rglob('Movie.mkv'))
    assert len(moved) == 1, f"旧文件应该在 trash/_replaced 下，找到: {moved}"


def test_resolve_lower_quality_skipped(tmp_path):
    # 4K 已在库，1080p 来 → 跳过新种子
    dst = tmp_path / 'lib' / 'Movie.mkv'
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b'old')

    db, existing = _mk_db_with_existing(dispatched=[str(dst)])
    existing.dispatched_files = ['/lib/Movie.2160p.UHD.Remux.mkv']

    out = duplicate_resolve(
        db=db, current_hash='bbbb' * 16, current_release_name='Movie.1080p.mkv',
        src=tmp_path / 'Movie.1080p.mkv', dst=dst,
        policy='higher_quality_wins', trash_dir=tmp_path / 'trash',
    )
    assert out == ACTION_SKIP
    assert dst.exists(), "旧文件应该保留"


def test_resolve_always_skip(tmp_path):
    # adult 默认策略
    dst = tmp_path / 'lib' / 'code.mp4'
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b'old')
    db, _ = _mk_db_with_existing(dispatched=[str(dst)])
    out = duplicate_resolve(
        db=db, current_hash='bbbb' * 16, current_release_name='code-newer.mp4',
        src=tmp_path / 'code-newer.mp4', dst=dst,
        policy='always_skip', trash_dir=tmp_path / 'trash',
    )
    assert out == ACTION_SKIP


def test_resolve_always_replace_moves_to_trash(tmp_path):
    dst = tmp_path / 'lib' / 'M.mkv'
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b'old')
    db, _ = _mk_db_with_existing(dispatched=[str(dst)])
    out = duplicate_resolve(
        db=db, current_hash='bbbb' * 16, current_release_name='M.new.mkv',
        src=tmp_path / 'M.new.mkv', dst=dst,
        policy='always_replace', trash_dir=tmp_path / 'trash',
    )
    assert out == ACTION_PROCEED
    assert not dst.exists()
    assert any((tmp_path / 'trash' / '_replaced').rglob('M.mkv'))


def test_resolve_needs_review_raises(tmp_path):
    dst = tmp_path / 'lib' / 'M.mkv'
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b'old')
    db, _ = _mk_db_with_existing(dispatched=[str(dst)])
    with pytest.raises(DuplicateConflictError) as exc_info:
        duplicate_resolve(
            db=db, current_hash='bbbb' * 16, current_release_name='M.new.mkv',
            src=tmp_path / 'M.new.mkv', dst=dst,
            policy='needs_review', trash_dir=tmp_path / 'trash',
        )
    info = exc_info.value.to_dict()
    assert info['kind'] == 'duplicate_conflict'
    assert info['reason'] == 'policy=needs_review'
    assert info['dst'] == str(dst)


def test_resolve_tie_raises_needs_review(tmp_path):
    # 质量持平 + 都非 repack → 保守起见走 needs_review
    dst = tmp_path / 'lib' / 'M.mkv'
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b'old')

    db, existing = _mk_db_with_existing(dispatched=[str(dst)])
    existing.dispatched_files = ['/lib/Movie.A.1080p.WEB-DL.mkv']

    with pytest.raises(DuplicateConflictError) as exc_info:
        duplicate_resolve(
            db=db, current_hash='bbbb' * 16, current_release_name='Movie.B.1080p.WEB-DL.mkv',
            src=tmp_path / 'Movie.B.1080p.WEB-DL.mkv', dst=dst,
            policy='higher_quality_wins', trash_dir=tmp_path / 'trash',
        )
    assert exc_info.value.to_dict()['reason'] == 'quality_tie'


# ---------- copier on_displace 钩子 ----------

def test_copier_on_displace_called_on_oversized_dst(tmp_path):
    """dst_size > total 的异常分支：on_displace 钩子接管，不再 hard delete。"""
    from tools.dispatch.copier import copy_file_with_progress

    src = tmp_path / 'src.mkv'
    src.write_bytes(b'x' * 100)

    dst = tmp_path / 'dst.mkv'
    dst.write_bytes(b'x' * 200)  # 比 src 大 → 触发异常分支

    captured = []
    def hook(p):
        captured.append(Path(p))
        Path(p).unlink()

    copy_file_with_progress(src, dst, on_displace=hook, resume=True)
    assert captured == [dst], f"on_displace 应该被调一次，传入 dst；实际: {captured}"
    assert dst.exists() and dst.stat().st_size == 100, "新文件应正常落地"
