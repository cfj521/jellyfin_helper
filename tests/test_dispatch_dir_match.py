"""_resolve_target 的「匹配复用 jellyfin 已有目录」集成逻辑。

只 mock jellyfin I/O，验证 _match_existing_library_dir：
  tv/anime → 命中剧目录 + 季子目录（风格跟随）
  movie    → 命中电影目录（item Path 的父目录）
  未命中 / 跨库 → None（退回模板）
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _patch_jf(monkeypatch, *, series=None, movies=None, seasons=None):
    from common.jellyfin_client import JellyfinClient
    # 关键：mock 的 __init__ 必须跟真实签名一样要求 host/api_key，
    # 否则会把「构造时漏传参数」的 bug 藏掉（曾导致每次匹配都异常退回模板）。
    monkeypatch.setattr(JellyfinClient, '__init__', lambda self, host, api_key: None)
    monkeypatch.setattr(JellyfinClient, 'get_all_series', lambda self: series or [])
    monkeypatch.setattr(JellyfinClient, 'get_all_movies', lambda self: movies or [])
    monkeypatch.setattr(JellyfinClient, 'get_seasons_of_series', lambda self, sid: seasons or [])


def test_tv_match_reuses_dir_with_season_style(monkeypatch):
    from backend.api.dispatch import _match_existing_library_dir
    _patch_jf(
        monkeypatch,
        series=[{
            'Id': 'abc', 'Name': 'House of the Dragon',
            'Path': '/library/videos/tv/House.of.the.Dragon',
            'ProviderIds': {'Tmdb': '94997', 'Imdb': 'tt11198330'},
        }],
        seasons=[
            {'Path': '/library/videos/tv/House.of.the.Dragon/S01'},
            {'Path': '/library/videos/tv/House.of.the.Dragon/S02'},
        ],
    )
    out = _match_existing_library_dir(
        {'series_tmdb_id': '94997', 'season': 3, 'series_name': 'House of the Dragon'},
        'tv', '/library/videos/tv',
    )
    assert out == '/library/videos/tv/House.of.the.Dragon/S03'


def test_tv_no_match_returns_none(monkeypatch):
    from backend.api.dispatch import _match_existing_library_dir
    _patch_jf(monkeypatch, series=[{
        'Id': 'x', 'Name': 'The Boys', 'Path': '/library/videos/tv/The Boys',
        'ProviderIds': {'Tmdb': '76479'},
    }])
    out = _match_existing_library_dir(
        {'series_tmdb_id': '94997', 'season': 3, 'series_name': 'House of the Dragon'},
        'tv', '/library/videos/tv',
    )
    assert out is None


def test_tv_match_by_name_when_no_id(monkeypatch):
    from backend.api.dispatch import _match_existing_library_dir
    _patch_jf(
        monkeypatch,
        series=[{
            'Id': 'abc', 'Name': 'House of the Dragon',
            'Path': '/library/videos/tv/House.of.the.Dragon', 'ProviderIds': {},
        }],
        seasons=[{'Path': '/library/videos/tv/House.of.the.Dragon/Season 01'}],
    )
    out = _match_existing_library_dir(
        {'season': 3, 'series_name': 'House.of.the.Dragon'},
        'tv', '/library/videos/tv',
    )
    assert out == '/library/videos/tv/House.of.the.Dragon/Season 03'


def test_season_ignores_seasons_outside_matched_dir(monkeypatch):
    """get_seasons 混入别的目录(重复剧)的季 → 只认落在匹配剧目录下的季来推断风格。"""
    from backend.api.dispatch import _match_existing_library_dir
    _patch_jf(
        monkeypatch,
        series=[{
            'Id': 'abc', 'Name': 'House of the Dragon',
            'Path': '/library/videos/tv/House.of.the.Dragon',
            'ProviderIds': {'Tmdb': '94997'},
        }],
        seasons=[
            {'Path': '/library/videos/tv/House.of.the.Dragon/S01'},
            {'Path': '/library/videos/tv/House.of.the.Dragon/S02'},
            {'Path': '/library/videos/tv/House of the Dragon/Season 03'},  # 别的目录，须忽略
        ],
    )
    out = _match_existing_library_dir(
        {'series_tmdb_id': '94997', 'season': 3, 'series_name': 'House of the Dragon'},
        'tv', '/library/videos/tv',
    )
    assert out == '/library/videos/tv/House.of.the.Dragon/S03'


def test_candidate_outside_library_root_filtered(monkeypatch):
    """匹配项不在目标库根下 → 不复用（避免落到别的库）。"""
    from backend.api.dispatch import _match_existing_library_dir
    _patch_jf(monkeypatch, series=[{
        'Id': 'abc', 'Name': 'House of the Dragon',
        'Path': '/other/anime/House.of.the.Dragon',
        'ProviderIds': {'Tmdb': '94997'},
    }])
    out = _match_existing_library_dir(
        {'series_tmdb_id': '94997', 'season': 3, 'series_name': 'House of the Dragon'},
        'tv', '/library/videos/tv',
    )
    assert out is None


def test_movie_match_uses_parent_dir(monkeypatch):
    from backend.api.dispatch import _match_existing_library_dir
    _patch_jf(monkeypatch, movies=[{
        'Id': 'm1', 'Name': 'The Martian',
        'Path': '/library/videos/movie/The.Martian.2015/The.Martian.2015.mkv',
        'ProviderIds': {'Tmdb': '286217'},
    }])
    out = _match_existing_library_dir(
        {'tmdb_id': '286217', 'title': 'The Martian', 'year': 2015},
        'movie', '/library/videos/movie',
    )
    assert out == '/library/videos/movie/The.Martian.2015'
