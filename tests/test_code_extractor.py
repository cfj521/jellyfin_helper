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
    # 方括号包裹的合法 FC2 番号（之前 \[.*?\] 噪音规则会整段吃掉）
    ("[FC2-PPV-2701833] 線上觀看.ts", "FC2-PPV-2701833"),
    ("[FC2-PPV-683577] xxx.ts", "FC2-PPV-683577"),
    # TOKYO HOT N 系列大写（之前正则用小写 n 永远匹配不到）
    ("N0821 Kyouko Maki TOKYO HOT.ts", "N0821"),
    ("N1048 GxxBust.ts", "N1048"),
    # 番号紧跟中文（之前 \b 在中英交界不算 boundary）
    ("PPPE-135处理.mp4", "PPPE-135"),
    ("MIDA-039 处理.mp4", "MIDA-039"),
    # 番号后紧跟字母后缀（中字 ch / 版本 V 等）：之前 \d{}\b 在 9c/7V 间不算边界
    ("MIDA-039ch.mp4", "MIDA-039"),
    ("MIMK-187ch", "MIMK-187"),
    ("STARS-977V", "STARS-977"),
    # "水印站名@番号" 格式：之前 hhd800 被通用模式误识别为 HHD-800，
    # 且 @[A-Za-z0-9]+ 部分回溯吞掉 @PFE 留下 S-103
    ("hhd800.com@PFES-103.mp4", "PFES-103"),
    ("hhd800.com@SSIS-001.mp4", "SSIS-001"),
    ("@PFES-103.mp4", "PFES-103"),
    ("hhd800.mp4", None),
    ("nyap2p.com_IPZZ-208.mp4", "IPZZ-208"),
    # 通用域名水印（任何 xxx.com / .fun / .tv 等都该被当成噪音清掉）
    ("pornhub.com_SSIS-001.mp4", "SSIS-001"),
    ("xvideos.com@SSIS-002.mp4", "SSIS-002"),
    ("xnxx.com.SSIS-003.mp4", "SSIS-003"),
    ("someweirdsite.tv_SSIS-004.mp4", "SSIS-004"),
    ("4k2.com@IPZZ-519.mp4", "IPZZ-519"),
    ("manko.fun.mp4", None),
    ("site.com.mp4", None),
    # 主体必须含字母：番号尾巴 "12345" 后跟 ".com" 不该被吃
    ("XXX-12345.com.mp4", "XXX-12345"),
    # @水印 + 多段番号（@FC2-PPV-N），FC2 前缀不该被 @ 规则吞
    ("hhd800.com@FC2-PPV-2386297.mp4", "FC2-PPV-2386297"),
    ("@FC2-PPV-1234567.mp4", "FC2-PPV-1234567"),
    # 日期 / 日期+时间戳：之前 "video_2024-12-18..." 被识别为 VIDEO-2024
    ("video_2024-12-18_21-23-11.mp4", None),
    ("IMG_2024.12.18T08-30-00.mp4", None),
    # 不应识别
    ("Random.Movie.2024.mkv", None),
    ("just_a_name.mp4", None),
])
def test_extract_code(filename, expected):
    assert extract_code(filename) == expected


def test_clean_filename_strips_quality_tags():
    assert "1080p" not in clean_filename("Movie.1080p.H265.mkv").lower()
    assert "bluray" not in clean_filename("Movie.BluRay.mkv").lower()
    # 字幕组里的中文会被非 ASCII 替换规则清掉
    assert "字幕组" not in clean_filename("[字幕组]Movie.mkv")
