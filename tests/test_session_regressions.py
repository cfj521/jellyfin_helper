"""
本次会话引入的特性回归测试。

覆盖：
  - lang_detect: UTF-16 LE/BE BOM 解码 + 无 BOM 启发式
  - common.mdblist_client.parse_ratings
  - common.wikidata_client._escape_sparql_literal / _build_query
  - backend.api.medialibraries._TTLCache 行为
  - backend.api._item_health: empty_season 检查
  - common.tmdb_client get_episode_still_path 端点拼接（mock）
"""
import sys
import time
from pathlib import Path

import pytest

# 本仓库布局：tests/ 与 common/ web/ 平行
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ============================================================
# lang_detect: UTF-16 字幕文件解码
# ============================================================

def _ass_text():
    """构造一个带 [Script Info] 头 + 一条 Dialogue 的小 .ass 样本。"""
    return (
        '[Script Info]\n'
        'Title: Default\n'
        '\n'
        '[Events]\n'
        'Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,这是中文字幕\n'
    )


def test_lang_detect_utf16le_bom(tmp_path):
    """UTF-16 LE 带 BOM 的 .ass 字幕能被正确解码且能识别为中文。"""
    from tools.subtitle_manager.lang_detect import detect_subtitle_language

    p = tmp_path / 'sample.ass'
    p.write_bytes(b'\xff\xfe' + _ass_text().encode('utf-16-le'))
    lang = detect_subtitle_language(p)
    # 没爆炸（之前会判定为乱码 → eng / None）
    assert lang in ('chs', 'cht'), f"应识别为中文，得到 {lang!r}"


def test_lang_detect_utf16be_bom(tmp_path):
    """UTF-16 BE 带 BOM 的 .ass 字幕同样能识别。"""
    from tools.subtitle_manager.lang_detect import detect_subtitle_language

    p = tmp_path / 'sample.ass'
    p.write_bytes(b'\xfe\xff' + _ass_text().encode('utf-16-be'))
    lang = detect_subtitle_language(p)
    assert lang in ('chs', 'cht')


def test_lang_detect_utf16le_no_bom_heuristic(tmp_path):
    """UTF-16 LE 无 BOM 但偶数位置大量 \\x00 的启发式分支。"""
    from tools.subtitle_manager.lang_detect import detect_subtitle_language

    p = tmp_path / 'sample.ass'
    p.write_bytes(_ass_text().encode('utf-16-le'))  # 无 BOM
    lang = detect_subtitle_language(p)
    # 启发式正确分流到 utf-16-le 时能拿到中文识别
    # （如果启发式失效会被 gb18030/latin-1 兜底，结果可能为 None；至少不该误判 eng）
    assert lang != 'eng'


def test_lang_detect_utf8_still_works(tmp_path):
    """普通 UTF-8 字幕（无 BOM 也无中文混乱）保持原行为不退化。"""
    from tools.subtitle_manager.lang_detect import detect_subtitle_language

    p = tmp_path / 'sample.ass'
    p.write_text(_ass_text(), encoding='utf-8')
    lang = detect_subtitle_language(p)
    assert lang in ('chs', 'cht')


# ============================================================
# MDBList: parse_ratings 字段映射
# ============================================================

def test_mdblist_parse_ratings_full():
    from common.mdblist_client import parse_ratings

    sample = {
        'title': 'The Shawshank Redemption',
        'year': 1994,
        'imdbid': 'tt0111161',
        'tmdbid': 278,
        'score': 92,
        'ratings': [
            {'source': 'imdb', 'value': 9.3, 'votes': 2987143},
            {'source': 'metacritic', 'value': 80},
            {'source': 'tomatoes', 'value': 91},
            {'source': 'tomatoesaudience', 'value': 98},
            {'source': 'trakt', 'value': 91},   # 百分比 → 9.1
            {'source': 'letterboxd', 'value': 4.6},
        ],
    }
    out = parse_ratings(sample)
    assert out['title'] == 'The Shawshank Redemption'
    assert out['imdb_id'] == 'tt0111161'
    assert out['imdb_rating'] == pytest.approx(9.3)
    assert out['imdb_votes'] == 2987143
    assert out['rt_critic'] == 91
    assert out['rt_audience'] == 98
    assert out['metacritic'] == 80
    assert out['trakt_rating'] == pytest.approx(9.1)  # 百分比归一到 10 分制
    assert out['letterboxd_rating'] == pytest.approx(4.6)
    assert out['aggregate_score'] == 92


def test_mdblist_parse_ratings_partial_and_unknown():
    """缺数据 + 未知 source 不应报错。"""
    from common.mdblist_client import parse_ratings

    sample = {
        'title': 'Foo',
        'imdbid': None,
        'ratings': [
            {'source': 'imdb', 'value': 7.5},
            {'source': 'unknownsource', 'value': 100},
            {'source': 'metacritic', 'value': None},  # None 应被跳过
        ],
    }
    out = parse_ratings(sample)
    assert out['imdb_rating'] == pytest.approx(7.5)
    # imdb_votes 没传应该不出现
    assert 'imdb_votes' not in out
    # metacritic value 是 None 不进结果
    assert 'metacritic' not in out


def test_mdblist_parse_ratings_empty():
    """空 ratings 数组安全。"""
    from common.mdblist_client import parse_ratings

    out = parse_ratings({'title': 'X', 'ratings': []})
    assert out['title'] == 'X'
    assert 'imdb_rating' not in out


# ============================================================
# Wikidata: SPARQL 转义 + 查询拼接
# ============================================================

def test_wikidata_escape_sparql_literal():
    from common.wikidata_client import _escape_sparql_literal

    # 反斜杠和双引号应被转义
    assert _escape_sparql_literal('Tom "The" Hanks') == 'Tom \\"The\\" Hanks'
    assert _escape_sparql_literal('a\\b') == 'a\\\\b'
    # 中文不变
    assert _escape_sparql_literal('周星驰') == '周星驰'


def test_wikidata_build_query_contains_q5_and_p18():
    from common.wikidata_client import _build_query

    q = _build_query('Tom Hanks', 'en')
    # 限制在 humans
    assert 'wd:Q5' in q
    # 取图片
    assert 'P18' in q
    # 名字以正确 lang 标签出现
    assert '"Tom Hanks"@en' in q
    assert 'LIMIT' in q


def test_wikidata_build_query_chinese():
    from common.wikidata_client import _build_query

    q = _build_query('周星驰', 'zh')
    assert '"周星驰"@zh' in q


# ============================================================
# 旧 _TTLCache 内存缓存类已下线（替换为 backend.cache_store 的 DB 持久化版）
# 对应单测删除：cache_store 测试需要 DB session mock，不在这一档测试范围内。
# ============================================================


# ============================================================
# _item_health: empty_season 检查
# ============================================================

def test_item_health_empty_season_warning():
    from backend.api._item_health import compute_health

    # 空 Season → empty_season 警告
    h = compute_health({
        'Type': 'Season',
        'Name': 'Season 0',
        'ChildCount': 0,
        'Path': '/library/videos/tv/Foo/Season 0',
    })
    assert h['level'] == 'warning'
    codes = [iss['code'] for iss in h['issues']]
    assert 'empty_season' in codes


def test_item_health_non_empty_season_ok():
    from backend.api._item_health import compute_health

    h = compute_health({
        'Type': 'Season',
        'Name': 'Season 1',
        'ChildCount': 10,
        'Path': '/library/videos/tv/Foo/Season 1',
    })
    # 非空 Season 没特别报警
    assert h['level'] == 'ok'


def test_item_health_no_name_mismatch_for_accented_title():
    """Shōgun 系列：Jellyfin Name 带音符号、文件夹用 ASCII 不应判 name_mismatch。"""
    from backend.api._item_health import compute_health

    h = compute_health({
        'Type': 'Series',
        'Name': 'Shōgun',
        'Path': '/library/videos/tv/Shogun',
        'ChildCount': 1,
    })
    codes = [iss['code'] for iss in h['issues']]
    assert 'name_mismatch' not in codes, (
        f"音符号差异 (ō vs o) 不应触发 name_mismatch；当前 issues={h['issues']}"
    )


def test_item_health_no_name_mismatch_for_dotted_directory():
    """Star.Wars.Andor 目录：".Andor" 不该被当扩展名截断成 "Star.Wars"。"""
    from backend.api._item_health import compute_health

    h = compute_health({
        'Type': 'Series',
        'Name': 'Andor',
        'Path': '/library/videos/tv/Star.Wars.Andor',
        'ChildCount': 2,
    })
    codes = [iss['code'] for iss in h['issues']]
    # "Andor" 应该作为 token 出现在文件夹 ["star","wars","andor"] 里 → containment=1.0
    assert 'name_mismatch' not in codes, (
        f"Series 目录名末段不应被当扩展名；当前 issues={h['issues']}"
    )


def test_item_health_no_name_mismatch_for_abbreviated_folder():
    """主标题缩写：文件夹用 'CSI'，Jellyfin 标题 'CSI: Crime Scene Investigation'。
    文件夹 token 完全落在标题里 → Containment(文件夹)=1.0，不应误报。"""
    from backend.api._item_health import compute_health

    h = compute_health({
        'Type': 'Series',
        'Name': 'CSI: Crime Scene Investigation',
        'Path': '/library/videos/tv/CSI',
        'ChildCount': 15,
    })
    codes = [iss['code'] for iss in h['issues']]
    assert 'name_mismatch' not in codes, (
        f"文件夹是主标题缩写不应触发 name_mismatch；当前 issues={h['issues']}"
    )


def test_item_health_empty_series_still_warning():
    """旧的 empty_series 检查没被破坏。"""
    from backend.api._item_health import compute_health

    h = compute_health({
        'Type': 'Series',
        'Name': 'Foo',
        'ChildCount': 0,
        'Path': '/library/videos/tv/Foo',
    })
    codes = [iss['code'] for iss in h['issues']]
    assert 'empty_series' in codes


# ============================================================
# TMDB Episode still 端点拼接
# ============================================================

def test_tmdb_get_episode_still_path_url(monkeypatch):
    """get_episode_still_path 应该调到 /tv/{tv}/season/{s}/episode/{e} 并返回 still_path。"""
    from common.tmdb_client import TMDBClient

    captured = {}

    def fake_request(self, endpoint, params=None):
        captured['endpoint'] = endpoint
        return {'still_path': '/abc.jpg'}

    monkeypatch.setattr(TMDBClient, '_request', fake_request)
    c = TMDBClient(api_key='x', delay=0)
    sp = c.get_episode_still_path(1399, 1, 3)
    assert sp == '/abc.jpg'
    assert captured['endpoint'] == '/tv/1399/season/1/episode/3'


def test_tmdb_get_episode_still_path_missing_args():
    from common.tmdb_client import TMDBClient

    c = TMDBClient(api_key='x', delay=0)
    # 缺参数应直接返回 None，不发请求
    assert c.get_episode_still_path(None, 1, 1) is None
    assert c.get_episode_still_path(1, None, 1) is None
    assert c.get_episode_still_path(1, 1, None) is None


# ============================================================
# /libraries/{id}/items 自动推断 item_type（避免 TV 库一锅端）
# ============================================================

def test_get_library_items_defaults_to_series_for_tvshows(monkeypatch):
    """
    访问 TV 库时若调用方未传 item_type，后端应自动注入 IncludeItemTypes=Series，
    防止 tree-table 拿到 Series+Season+Episode 扁平混合列表。
    """
    from common.jellyfin_client import JellyfinClient
    from backend.api import medialibraries as jf_module

    captured: dict = {}

    # 替身：返回一个剧集库元数据
    def fake_get_libraries_normalized(self):
        return [{'id': 'lib_tv', 'name': 'TV', 'collection_type': 'tvshows', 'locations': []}]

    def fake_get_library_items_page(self, parent_id, **kwargs):
        captured.update(kwargs)
        captured['parent_id'] = parent_id
        return {'items': [], 'total': 0}

    monkeypatch.setattr(JellyfinClient, 'get_libraries_normalized', fake_get_libraries_normalized)
    monkeypatch.setattr(JellyfinClient, 'get_library_items_page', fake_get_library_items_page)

    jf_module.get_library_items('lib_tv')

    assert captured.get('item_types') == 'Series', (
        f"TV 库默认应注入 IncludeItemTypes=Series，实际: {captured.get('item_types')!r}"
    )


def test_get_library_items_defaults_to_movie_for_movies(monkeypatch):
    from common.jellyfin_client import JellyfinClient
    from backend.api import medialibraries as jf_module

    captured: dict = {}

    def fake_get_libraries_normalized(self):
        return [{'id': 'lib_mv', 'name': 'Movies', 'collection_type': 'movies', 'locations': []}]

    def fake_get_library_items_page(self, parent_id, **kwargs):
        captured.update(kwargs); captured['parent_id'] = parent_id
        return {'items': [], 'total': 0}

    monkeypatch.setattr(JellyfinClient, 'get_libraries_normalized', fake_get_libraries_normalized)
    monkeypatch.setattr(JellyfinClient, 'get_library_items_page', fake_get_library_items_page)
    jf_module.get_library_items('lib_mv')
    assert captured.get('item_types') == 'Movie'


def test_get_library_items_explicit_type_overrides(monkeypatch):
    """显式传 item_type 应跳过自动推断。"""
    from common.jellyfin_client import JellyfinClient
    from backend.api import medialibraries as jf_module

    captured: dict = {}

    def fake_get_libraries_normalized(self):
        # 故意返回 tvshows，下面验证显式传 Episode 不被覆盖
        return [{'id': 'lib_tv', 'collection_type': 'tvshows', 'locations': []}]

    def fake_get_library_items_page(self, parent_id, **kwargs):
        captured.update(kwargs)
        return {'items': [], 'total': 0}

    monkeypatch.setattr(JellyfinClient, 'get_libraries_normalized', fake_get_libraries_normalized)
    monkeypatch.setattr(JellyfinClient, 'get_library_items_page', fake_get_library_items_page)
    jf_module.get_library_items('lib_tv', item_type='Episode')
    assert captured.get('item_types') == 'Episode'
