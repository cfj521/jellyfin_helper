"""library_matcher 纯逻辑单测。

dispatch 落库前用它把识别结果匹配到 jellyfin 库里已存在的同一作品目录：
  优先级 tmdb_id → imdb_id → 归一化 name 精确相等。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ============================================================
# normalize_title
# ============================================================

def test_normalize_dots_and_spaces_equivalent():
    from tools.dispatch.library_matcher import normalize_title
    assert normalize_title('House.of.the.Dragon') == normalize_title('House of the Dragon')
    assert normalize_title('House.of.the.Dragon') == 'house of the dragon'


def test_normalize_case_insensitive():
    from tools.dispatch.library_matcher import normalize_title
    assert normalize_title('BREAKING.bad') == 'breaking bad'


def test_normalize_strips_trailing_year():
    from tools.dispatch.library_matcher import normalize_title
    assert normalize_title('The.Martian.2015') == 'the martian'
    assert normalize_title('The Martian (2015)') == 'the martian'


def test_normalize_keeps_internal_digits():
    """年份只去尾部；剧名内部数字/年份段不动（如 Planet Earth II 不是年份）。"""
    from tools.dispatch.library_matcher import normalize_title
    assert normalize_title('Planet.Earth.III') == 'planet earth iii'
    # 12.12 这种片名内部数字不应被当年份删
    assert normalize_title('12.12.The.Day.2023') == '12 12 the day'


def test_normalize_collapses_separators():
    from tools.dispatch.library_matcher import normalize_title
    assert normalize_title('Game___of---Thrones') == 'game of thrones'


# ============================================================
# match_library_dir：ID 优先级
# ============================================================

def _cand(name, path, tmdb=None, imdb=None):
    return {'name': name, 'path': path, 'tmdb': tmdb, 'imdb': imdb}


def test_match_by_tmdb_id():
    from tools.dispatch.library_matcher import match_library_dir
    cands = [
        _cand('The Boys', '/tv/The Boys', tmdb='76479'),
        _cand('House of the Dragon', '/tv/House.of.the.Dragon', tmdb='94997', imdb='tt11198330'),
    ]
    hit = match_library_dir(cands, tmdb_id='94997', imdb_id=None, name='House of the Dragon')
    assert hit is not None and hit['path'] == '/tv/House.of.the.Dragon'


def test_tmdb_beats_name():
    """tmdb 命中优先于 name；即便 name 对不上也以 ID 为准。"""
    from tools.dispatch.library_matcher import match_library_dir
    cands = [_cand('House of the Dragon', '/tv/House.of.the.Dragon', tmdb='94997')]
    hit = match_library_dir(cands, tmdb_id='94997', imdb_id=None, name='Totally Different Name')
    assert hit is not None and hit['path'] == '/tv/House.of.the.Dragon'


def test_imdb_fallback_when_no_tmdb_match():
    from tools.dispatch.library_matcher import match_library_dir
    cands = [_cand('House of the Dragon', '/tv/House.of.the.Dragon', tmdb='94997', imdb='tt11198330')]
    # tmdb 不中（库项是 94997，查询给了别的），imdb 命中
    hit = match_library_dir(cands, tmdb_id='99999', imdb_id='tt11198330', name='x')
    assert hit is not None and hit['path'] == '/tv/House.of.the.Dragon'


def test_name_normalize_match_dot_vs_space():
    """无 ID 时，name 归一化精确相等命中（点 vs 空格）。"""
    from tools.dispatch.library_matcher import match_library_dir
    cands = [_cand('House of the Dragon', '/tv/House.of.the.Dragon', tmdb='94997')]
    hit = match_library_dir(cands, tmdb_id=None, imdb_id=None, name='House.of.the.Dragon')
    assert hit is not None and hit['path'] == '/tv/House.of.the.Dragon'


def test_name_match_against_path_basename():
    """jellyfin name 缺失时，回退用 path 末段做归一化比较。"""
    from tools.dispatch.library_matcher import match_library_dir
    cands = [_cand('', '/tv/House.of.the.Dragon', tmdb=None)]
    hit = match_library_dir(cands, tmdb_id=None, imdb_id=None, name='House of the Dragon')
    assert hit is not None and hit['path'] == '/tv/House.of.the.Dragon'


# ============================================================
# 假阳性防护 + 不命中
# ============================================================

def test_no_substring_false_positive():
    """The.Terminal.List 不应命中 The.Terminal.List.Dark.Wolf（精确相等，非包含）。"""
    from tools.dispatch.library_matcher import match_library_dir
    cands = [_cand('The Terminal List Dark Wolf', '/tv/The.Terminal.List.Dark.Wolf.2025')]
    hit = match_library_dir(cands, tmdb_id=None, imdb_id=None, name='The Terminal List')
    assert hit is None


def test_empty_id_does_not_match_candidate_with_empty_id():
    """查询无 tmdb 且库项也无 tmdb，不能因为两个都空就命中。"""
    from tools.dispatch.library_matcher import match_library_dir
    cands = [_cand('Other Show', '/tv/Other.Show', tmdb=None, imdb=None)]
    hit = match_library_dir(cands, tmdb_id=None, imdb_id=None, name='House of the Dragon')
    assert hit is None


def test_no_match_returns_none():
    from tools.dispatch.library_matcher import match_library_dir
    cands = [_cand('The Boys', '/tv/The Boys', tmdb='76479')]
    hit = match_library_dir(cands, tmdb_id='94997', imdb_id='tt11198330', name='House of the Dragon')
    assert hit is None


def test_empty_candidates_returns_none():
    from tools.dispatch.library_matcher import match_library_dir
    assert match_library_dir([], tmdb_id='94997', imdb_id=None, name='x') is None


# ============================================================
# choose_season_dirname：季子目录风格跟随
# ============================================================

def test_season_follows_short_style():
    """已有 S01/S02 → 新季 S03。"""
    from tools.dispatch.library_matcher import choose_season_dirname
    assert choose_season_dirname(['S01', 'S02'], 3) == 'S03'


def test_season_follows_long_style():
    """已有 Season 01/Season 02 → Season 03。"""
    from tools.dispatch.library_matcher import choose_season_dirname
    assert choose_season_dirname(['Season 01', 'Season 02'], 3) == 'Season 03'


def test_season_reuses_existing_same_season():
    """目标季已存在该子目录 → 原样复用。"""
    from tools.dispatch.library_matcher import choose_season_dirname
    assert choose_season_dirname(['S01', 'S02', 'S03'], 3) == 'S03'
    assert choose_season_dirname(['Season 01', 'Season 03'], 3) == 'Season 03'


def test_season_no_padding_style():
    """已有不补零（Season 3）→ 跟随不补零（Season 5）。"""
    from tools.dispatch.library_matcher import choose_season_dirname
    assert choose_season_dirname(['Season 3'], 5) == 'Season 5'


def test_season_empty_falls_back_to_template():
    """无任何季目录 → 退回模板 Season XX（补零两位）。"""
    from tools.dispatch.library_matcher import choose_season_dirname
    assert choose_season_dirname([], 3) == 'Season 03'


def test_season_ignores_non_season_dirs():
    """Specials / 杂目录不算季目录，无可参考 → 退回模板。"""
    from tools.dispatch.library_matcher import choose_season_dirname
    assert choose_season_dirname(['Specials', 'extras', 'metadata'], 3) == 'Season 03'


def test_season_mixed_style_uses_highest():
    """风格不一致时跟随最高季的风格（最可能是当前约定）。"""
    from tools.dispatch.library_matcher import choose_season_dirname
    assert choose_season_dirname(['Season 01', 'S02'], 3) == 'S03'
