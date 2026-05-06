"""测试 NFO 生成"""
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.adult_manager.nfo_writer import build_movie_nfo


def test_nfo_contains_basic_fields():
    data = {
        "code": "SSIS-001",
        "title": "测试标题",
        "release_date": "2024-01-15",
        "studio": "测试厂商",
        "director": "导演甲",
        "duration_minutes": 120,
        "actors": ["演员A", "演员B"],
        "tags": ["类型1", "类型2"],
        "cover_url": "https://example.com/cover.jpg",
        "rating": 4.5,
        "source": "javbus",
    }
    xml_str = build_movie_nfo(data)
    root = ET.fromstring(xml_str)

    assert root.tag == 'movie'
    assert root.find('title').text == '测试标题'
    assert root.find('sorttitle').text == 'SSIS-001'
    assert root.find('premiered').text == '2024-01-15'
    assert root.find('year').text == '2024'
    assert root.find('runtime').text == '120'
    assert root.find('studio').text == '测试厂商'
    assert root.find('director').text == '导演甲'

    actor_names = [a.find('name').text for a in root.findall('actor')]
    assert actor_names == ['演员A', '演员B']

    genres = [g.text for g in root.findall('genre')]
    assert genres == ['类型1', '类型2']

    uid = root.find('uniqueid')
    assert uid.text == 'SSIS-001'
    assert uid.get('default') == 'true'


def test_nfo_minimal_data():
    """缺字段不应崩溃"""
    xml_str = build_movie_nfo({"code": "ABC-123"})
    root = ET.fromstring(xml_str)
    assert root.tag == 'movie'
    assert root.find('title').text == 'ABC-123'  # 无 title 时回落到 code
