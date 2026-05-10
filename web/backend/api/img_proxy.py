"""
图片反代理：解决豆瓣等站防盗链 (Referer 检查) 不让浏览器直接加载图片的问题。

工作流程：
  前端 <img src="/api/img-proxy?url=<encoded>"> →
    后端按 url 拉远端图（带正确 Referer）→ 流式回写。

安全：
  - 仅允许 host 白名单（避免开放代理被滥用）
  - 同 host 自带"假装是站内访问"的 Referer，绕过它自家的防盗链
  - 缓存 Cache-Control public, max-age=86400 让浏览器缓存一天

性能：每张图一次后端中转，对小量列表（每页 25-30）足够。
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

import requests
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)
router = APIRouter()


# host → 用于伪装的 Referer。绕过自家防盗链的关键。
_HOST_REFERERS = {
    'doubanio.com':       'https://movie.douban.com/',
    'douban.com':         'https://movie.douban.com/',
    'img1.doubanio.com':  'https://movie.douban.com/',
    'img2.doubanio.com':  'https://movie.douban.com/',
    'img3.doubanio.com':  'https://movie.douban.com/',
    'img9.doubanio.com':  'https://movie.douban.com/',
    'image.tmdb.org':     'https://www.themoviedb.org/',
    's4.anilist.co':      'https://anilist.co/',
    'walter.trakt.tv':    'https://trakt.tv/',
}


def _host_allowed(host: str) -> Optional[str]:
    """精确匹配或后缀匹配，命中返回伪装 Referer，未命中 None。"""
    host = (host or '').lower()
    if host in _HOST_REFERERS:
        return _HOST_REFERERS[host]
    # 后缀匹配（image.tmdb.org → tmdb.org 也允许，doubanio 子域名同理）
    for k, v in _HOST_REFERERS.items():
        if host.endswith('.' + k) or host == k:
            return v
    return None


@router.get('/img-proxy')
def img_proxy(url: str = Query(..., min_length=8, max_length=2048)):
    """图片反代。仅放行白名单 host。"""
    try:
        parsed = urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail='非法 URL')

    if parsed.scheme not in ('http', 'https'):
        raise HTTPException(status_code=400, detail='只支持 http/https')

    referer = _host_allowed(parsed.netloc)
    if not referer:
        raise HTTPException(
            status_code=403,
            detail=f'host 不在白名单: {parsed.netloc}（如需代理请加到 _HOST_REFERERS）',
        )

    headers = {
        'Referer': referer,
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept': 'image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
    }

    try:
        upstream = requests.get(url, headers=headers, stream=True, timeout=15)
    except requests.RequestException as e:
        logger.warning(f"img-proxy 拉取失败 {url}: {e}")
        raise HTTPException(status_code=502, detail=f'上游拉取失败: {e}')

    if upstream.status_code != 200:
        logger.info(f"img-proxy upstream {upstream.status_code} {url}")
        raise HTTPException(status_code=upstream.status_code, detail=f'上游返回 {upstream.status_code}')

    media_type = upstream.headers.get('Content-Type', 'image/jpeg')

    def _iter():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    # 让浏览器自缓存 1 天，少打后端
    return StreamingResponse(
        _iter(),
        media_type=media_type,
        headers={
            'Cache-Control': 'public, max-age=86400',
        },
    )
