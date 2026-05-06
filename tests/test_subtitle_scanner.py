"""测试字幕扫描和重命名核心逻辑"""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.subtitle_manager.scanner import SubtitleScanner, episodes_match
from tools.subtitle_manager.renamer import SubtitleRenamer


def test_episodes_match_basic():
    assert episodes_match((1, 2), (1, 2)) is True
    assert episodes_match((1, 2), (1, 3)) is False
    assert episodes_match((1, 2), (2, 2)) is False


def test_episodes_match_partial_season():
    """字幕可能只写 Ep02，应能匹配视频的 S01E02"""
    assert episodes_match((1, 2), (None, 2)) is True
    assert episodes_match((None, 2), (1, 2)) is True


def test_episodes_match_none():
    assert episodes_match(None, (1, 2)) is False
    assert episodes_match((1, 2), None) is False


@pytest.fixture
def media_dir():
    """构造一个临时媒体目录：1 部电影 + 1 部 3 集剧集"""
    tmp = Path(tempfile.mkdtemp(prefix='jftest_'))

    movie = tmp / 'My.Movie.2024.1080p'
    movie.mkdir()
    (movie / 'My.Movie.2024.1080p.mkv').write_bytes(b'x')
    (movie / 'sub_chs.srt').write_text('s')

    tv = tmp / 'TV.Show.S01.1080p'
    tv.mkdir()
    for ep in [1, 2, 3]:
        (tv / f'TV.Show.S01E0{ep}.1080p.mkv').write_bytes(b'x')
    (tv / 'TV.Show.S01E01.chs.srt').write_text('s')
    (tv / 'random_name_ep02.eng.srt').write_text('s')
    (tv / 'another_第03话.chs.srt').write_text('s')

    yield tmp

    import shutil
    shutil.rmtree(tmp)


def test_scanner_counts_correctly(media_dir):
    sc = SubtitleScanner(preferred_langs=['chs', 'eng'])
    res = sc.scan(media_dir, recursive=True)
    assert res.total_videos == 4
    # 电影目录下 sub_chs.srt 和电影名无任何共同片段，scanner 不会匹配它
    # 但电视剧 3 集都能配上字幕（包括只标 ep02 的字幕，bug 修复后能配上 S01E02）
    assert res.total_with_sub == 3
    assert res.total_without_sub == 1


def test_renamer_dry_run_returns_dict_list(media_dir):
    rn = SubtitleRenamer()
    results = rn.process_directory(media_dir, dry_run=True, recursive=True)
    assert isinstance(results, list)
    # renamer 是按目录处理的：电影目录 1 个字幕(只看是否单视频单字幕) + 电视剧 3 个能配上 ep 的字幕
    assert len(results) == 4
    for r in results:
        assert {'type', 'directory', 'old_name', 'new_name', 'success'}.issubset(r.keys())
        assert r['success'] is True


def test_renamer_execute_actually_renames(media_dir):
    rn = SubtitleRenamer()
    rn.process_directory(media_dir, dry_run=False, recursive=True)

    movie = media_dir / 'My.Movie.2024.1080p'
    assert (movie / 'My.Movie.2024.1080p.srt').exists()
    assert not (movie / 'sub_chs.srt').exists()

    tv = media_dir / 'TV.Show.S01.1080p'
    assert (tv / 'TV.Show.S01E02.1080p.eng.srt').exists()  # ep02 → S01E02
    assert (tv / 'TV.Show.S01E03.1080p.chs.srt').exists()  # 第03话 → S01E03


def test_renamer_extract_episode_formats():
    rn = SubtitleRenamer()
    assert rn.extract_episode('Show.S01E02.mkv') == (1, 2)
    assert rn.extract_episode('show.s01e02.mkv') == (1, 2)
    assert rn.extract_episode('Show.1x02.mkv') == (1, 2)
    assert rn.extract_episode('Show.ep02.srt') == (None, 2)
    assert rn.extract_episode('Show.第02话.srt') == (None, 2)
    assert rn.extract_episode('no_episode_here.mkv') is None
