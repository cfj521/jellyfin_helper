"""
NFO 文件生成
按 Kodi/Jellyfin movie.nfo 规范输出。
"""
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET
from xml.dom import minidom


def _add(parent, tag: str, text):
    if text is None or text == "":
        return None
    elem = ET.SubElement(parent, tag)
    elem.text = str(text)
    return elem


def build_movie_nfo(data: dict) -> str:
    """
    根据刮削结果 dict 构造 movie.nfo XML 字符串。

    data 字段（来自 ScrapeResult.to_dict()）：
      code, title, original_title, release_date, studio, director,
      duration_minutes, actors, tags, cover_url, rating, source
    """
    movie = ET.Element('movie')

    _add(movie, 'title', data.get('title') or data.get('code'))
    _add(movie, 'originaltitle', data.get('original_title'))
    _add(movie, 'sorttitle', data.get('code'))
    _add(movie, 'plot', data.get('title'))  # 如无简介，用标题填充
    _add(movie, 'studio', data.get('studio'))
    _add(movie, 'director', data.get('director'))

    if data.get('release_date'):
        _add(movie, 'premiered', data['release_date'])
        _add(movie, 'releasedate', data['release_date'])
        # 提取年份
        year = data['release_date'][:4]
        if year.isdigit():
            _add(movie, 'year', year)

    if data.get('duration_minutes'):
        _add(movie, 'runtime', data['duration_minutes'])

    if data.get('rating') is not None:
        _add(movie, 'rating', data['rating'])

    for actor_name in data.get('actors') or []:
        actor_elem = ET.SubElement(movie, 'actor')
        _add(actor_elem, 'name', actor_name)
        _add(actor_elem, 'role', '')

    for tag in data.get('tags') or []:
        _add(movie, 'genre', tag)
        _add(movie, 'tag', tag)

    if data.get('cover_url'):
        _add(movie, 'poster', data['cover_url'])
        _add(movie, 'fanart', data['cover_url'])

    # uniqueid
    uid = ET.SubElement(movie, 'uniqueid', {'type': 'num', 'default': 'true'})
    uid.text = data.get('code', '')

    if data.get('source'):
        _add(movie, 'source', data['source'])

    # 美化输出
    rough = ET.tostring(movie, encoding='utf-8')
    return minidom.parseString(rough).toprettyxml(indent='  ', encoding='utf-8').decode('utf-8')


def write_nfo(target_path: Path, data: dict) -> Path:
    """
    把 NFO 写到 target_path 旁边的同名 .nfo 文件。
    target_path 是视频文件路径，输出会是 <video_stem>.nfo。
    """
    nfo_path = target_path.with_suffix('.nfo')
    content = build_movie_nfo(data)
    nfo_path.write_text(content, encoding='utf-8')
    return nfo_path
