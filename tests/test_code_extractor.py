"""测试番号识别"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.adult_manager.code_extractor import extract_code, clean_filename


@pytest.mark.parametrize("filename,expected", [
    # 标准 ABC-123
    ("SSIS-001.mp4", "SSIS-001"),
    ("[字幕组]MIDE-789-1080p.mkv", "MIDE-789"),
    ("ssis001.mp4", "SSIS-001"),  # 小写无连字符也能识别
    # 带噪音的
    ("ABCD-456 1080p H265 BluRay.mkv", "ABCD-456"),
    ("[Uncensored]@user_PRED-300_HEVC.mp4", "PRED-300"),
    # FC2 系列
    ("FC2-PPV-1234567.mp4", "FC2-PPV-1234567"),
    ("FC2PPV1234567.mp4", "FC2-PPV-1234567"),
    # HEYZO
    ("HEYZO-2345.mp4", "HEYZO-2345"),
    ("heyzo_2345_1080p.mp4", "HEYZO-2345"),
    # 日期型 1pondo / Caribbean
    ("010120-001.mp4", "010120-001"),
    ("010120_001-1080p.mp4", "010120-001"),
    # 不应识别
    ("Random.Movie.2024.mkv", None),
    ("just_a_name.mp4", None),
])
def test_extract_code(filename, expected):
    assert extract_code(filename) == expected


def test_clean_filename_strips_quality_tags():
    assert "1080p" not in clean_filename("Movie.1080p.H265.mkv").lower()
    assert "bluray" not in clean_filename("Movie.BluRay.mkv").lower()
    # 中括号内容应被清掉
    assert "字幕组" not in clean_filename("[字幕组]Movie.mkv")
