"""模板渲染 + 路径清理测试。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.dispatch.template_render import (
    render_template, sanitize_path_segment, sanitize_path,
)


# ============================================================================
# render_template
# ============================================================================

class TestRenderTemplate:
    def test_basic_substitution(self):
        assert render_template('{title}', {'title': 'X'}) == 'X'
        assert render_template('{title} ({year})', {'title': 'X', 'year': 2024}) == 'X (2024)'

    def test_format_spec(self):
        assert render_template('S{s:02d}E{e:02d}', {'s': 1, 'e': 5}) == 'S01E05'
        assert render_template('{n:04d}', {'n': 12}) == '0012'

    def test_empty_value_drops_brackets(self):
        # year=None → 去掉 ( ) + 中间空白
        assert render_template('{title} ({year})', {'title': 'X', 'year': None}) == 'X'
        assert render_template('{title} ({year})', {'title': 'X', 'year': ''}) == 'X'
        # 缺 key 视同 None
        assert render_template('{title} ({year})', {'title': 'X'}) == 'X'

    def test_empty_value_drops_square_brackets(self):
        assert render_template('{title} [{tag}]', {'title': 'X', 'tag': None}) == 'X'

    def test_zero_not_empty(self):
        # 0 / False 不算空，照常输出
        assert render_template('Ep {n}', {'n': 0}) == 'Ep 0'
        assert render_template('{flag}', {'flag': False}) == 'False'

    def test_chinese_template(self):
        # ({series_name})S01E02 当 series_name 是中文
        out = render_template(
            '({series_name})S{season:02d}E{episode:02d}',
            {'series_name': '地球脉动 第二季', 'season': 1, 'episode': 2},
        )
        assert out == '(地球脉动 第二季)S01E02'

    def test_chinese_empty_inside_parens(self):
        # series_name=None → 整段去掉
        out = render_template(
            '({series_name})S{season:02d}E{episode:02d}',
            {'season': 1, 'episode': 2},
        )
        assert out == 'S01E02'

    def test_no_brackets_around_empty_var(self):
        # year=None 但无 () 包裹时不动周围
        assert render_template('{title} {year}', {'title': 'X', 'year': None}) == 'X'
        # 多空格收掉

    def test_consecutive_empties(self):
        # 两个连续空 var 都被去掉
        out = render_template('{title} ({year}) [{tag}]', {'title': 'X'})
        assert out == 'X'

    def test_format_spec_with_string_value(self):
        # 'd' spec 跟字符串值不匹配 → 退化为 str
        out = render_template('{n:02d}', {'n': 'abc'})
        # 退化输出原值，不抛
        assert out == 'abc'

    def test_empty_template(self):
        assert render_template('', {}) == ''
        assert render_template('', {'a': 1}) == ''

    def test_pure_literal(self):
        assert render_template('Plain Text', {}) == 'Plain Text'

    def test_real_movie_template_with_year(self):
        assert render_template(
            '{library_root}/{title} ({year})',
            {'library_root': '/lib', 'title': 'The Martian', 'year': 2015},
        ) == '/lib/The Martian (2015)'

    def test_real_movie_template_no_year(self):
        assert render_template(
            '{library_root}/{title} ({year})',
            {'library_root': '/lib', 'title': 'The Martian'},
        ) == '/lib/The Martian'


# ============================================================================
# sanitize_path_segment
# ============================================================================

class TestSanitizePathSegment:
    def test_invalid_chars_dropped(self):
        # Windows 非法字符 <>:"/\|?* 直接删除（之前替换为 '_' 太难看）
        assert sanitize_path_segment('Pirates: At World\'s End') == "Pirates At World's End"
        assert sanitize_path_segment('Movie<2024>') == 'Movie2024'
        assert sanitize_path_segment('A?B*C|D') == 'ABCD'

    def test_strip_trailing_dots(self):
        assert sanitize_path_segment('Movie.') == 'Movie'
        assert sanitize_path_segment('Movie...') == 'Movie'
        assert sanitize_path_segment('  Movie.  ') == 'Movie'

    def test_collapse_whitespace(self):
        assert sanitize_path_segment('A   B') == 'A B'
        assert sanitize_path_segment('A\t\tB') == 'A B'

    def test_control_chars(self):
        # 非空白控制字符直接删
        assert sanitize_path_segment('A\x00B\x1fC') == 'ABC'

    def test_chinese_preserved(self):
        assert sanitize_path_segment('地球脉动 第二季') == '地球脉动 第二季'

    def test_windows_reserved_name(self):
        # CON → CON_
        assert sanitize_path_segment('CON') == 'CON_'
        assert sanitize_path_segment('con') == 'con_'    # core uppercased before check
        assert sanitize_path_segment('NUL.txt') == 'NUL.txt_'
        # 不在保留名集合的不动
        assert sanitize_path_segment('CONFIG.txt') == 'CONFIG.txt'

    def test_length_truncation_preserves_extension(self):
        long_name = 'A' * 220 + '.mkv'
        out = sanitize_path_segment(long_name, max_len=200)
        assert out.endswith('.mkv')
        assert len(out) <= 200

    def test_empty(self):
        assert sanitize_path_segment('') == ''
        assert sanitize_path_segment('   ') == ''


# ============================================================================
# sanitize_path
# ============================================================================

class TestSanitizePath:
    def test_unix_absolute(self):
        assert sanitize_path('/library/movies/X') == '/library/movies/X'

    def test_invalid_in_segment(self):
        assert sanitize_path('/lib/Pirates: At World\'s End/movie.mkv') == \
            "/lib/Pirates At World's End/movie.mkv"

    def test_windows_drive(self):
        out = sanitize_path('Z:/videos/movie/X (2024)')
        assert out == 'Z:/videos/movie/X (2024)'

    def test_windows_drive_backslash(self):
        # backslash 输入 → 输出仍是 backslash 形式
        out = sanitize_path('Z:\\videos\\movie\\X')
        assert out == 'Z:\\videos\\movie\\X'

    def test_chinese_path(self):
        out = sanitize_path('/lib/tv/地球脉动 第二季/Season 01')
        assert out == '/lib/tv/地球脉动 第二季/Season 01'
