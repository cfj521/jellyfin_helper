"""TorrentOrganizer 命名回归测试。

核心 bug（svr-jellyfin 日志 2026-06-06 The Boys S05E05）：
  单集下载（src 内只有 1 个视频，videos==1）走 organize 的「单文件」分支，
  该分支不从文件名提取 SxxExx，而 pipeline 传入的 metadata 又没有 season/episode，
  于是模板 '{series_name} S{season:02d}E{episode:02d}' 里的 {season}/{episode}
  被「空占位符智能去除」删掉，得到 'The Boys SE.mkv'。

  正确行为：单集也应像整季 pack 一样从文件名提取集号 → 'The Boys S05E05.mkv'。
"""
import sys
from pathlib import Path

# 仓库布局：tests/ 与 tools/ common/ 平行
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TV_RULE = {'file_template': '{series_name} S{season:02d}E{episode:02d}'}
# pipeline_worker._step_copy 实际构造的 metadata —— 没有 season/episode 字段
TV_METADATA = {
    'title': 'The Boys',
    'year': None,
    'series_name': 'The Boys',
    'anime_name': 'The Boys',
    'code': 'The Boys',
}


def _make_video(dirpath: Path, name: str, size: int = 1024) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / name
    p.write_bytes(b'\x00' * size)
    return p


def test_organize_single_tv_episode_keeps_sxxexx(tmp_path):
    """单集 tv（videos==1）也要从文件名提取 SxxExx，不能渲染成 'The Boys SE'。"""
    from tools.dispatch.organizer import TorrentOrganizer

    src = tmp_path / 'www.UIndex.org    -    The Boys S05E05 One-Shots 2160p'
    _make_video(
        src,
        'The Boys S05E05 One-Shots 2160p AMZN WEB-DL DDP5 1 Atmos DV HDR H 265-FLUX.mkv',
    )
    target = tmp_path / 'lib' / 'The Boys' / 'Season 05'

    org = TorrentOrganizer(trash_dir=tmp_path / 'trash')
    result = org.organize(
        src_root=src,
        target_dir=target,
        media_type='tv',
        metadata=TV_METADATA,
        rule=TV_RULE,
    )

    dispatched = result['dispatched_files']
    assert len(dispatched) == 1, f"应复制 1 个视频，实得 {dispatched!r}"
    name = Path(dispatched[0]).name
    assert name == 'The Boys S05E05.mkv', f"单集应保留 SxxExx，实得 {name!r}"


def test_organize_season_pack_still_works(tmp_path):
    """整季 pack（videos>1）原行为不回归：每集按 SxxExx 命名。"""
    from tools.dispatch.organizer import TorrentOrganizer

    src = tmp_path / 'The Boys S04 COMPLETE 2160p'
    _make_video(src, 'The.Boys.S04E01.2160p.AMZN.WEB-DL-BEN.mkv')
    _make_video(src, 'The.Boys.S04E02.2160p.AMZN.WEB-DL-BEN.mkv')
    target = tmp_path / 'lib' / 'The Boys' / 'Season 04'

    org = TorrentOrganizer(trash_dir=tmp_path / 'trash')
    result = org.organize(
        src_root=src,
        target_dir=target,
        media_type='tv',
        metadata=TV_METADATA,
        rule=TV_RULE,
    )

    names = sorted(Path(p).name for p in result['dispatched_files'])
    assert names == ['The Boys S04E01.mkv', 'The Boys S04E02.mkv'], f"实得 {names!r}"


def test_organize_single_movie_unaffected(tmp_path):
    """电影单文件不应被加 SxxExx，仍按 '{title} ({year})' 命名。"""
    from tools.dispatch.organizer import TorrentOrganizer

    src = tmp_path / 'The.Martian.2015.2160p.BluRay'
    _make_video(src, 'The.Martian.2015.2160p.BluRay-GROUP.mkv')
    target = tmp_path / 'lib' / 'movies'

    org = TorrentOrganizer(trash_dir=tmp_path / 'trash')
    result = org.organize(
        src_root=src,
        target_dir=target,
        media_type='movie',
        metadata={'title': 'The Martian', 'year': 2015, 'series_name': '', 'anime_name': '', 'code': ''},
        rule={'file_template': '{title} ({year})'},
    )

    name = Path(result['dispatched_files'][0]).name
    assert name == 'The Martian (2015).mkv', f"电影命名被改动，实得 {name!r}"
