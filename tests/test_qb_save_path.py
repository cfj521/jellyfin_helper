"""qB 加种路径解析 + 下载根守卫测试。

背景 bug：设置页改了 dispatch.download_path，实际加种仍落在 qB 自己的默认目录
（`/download`）—— 那个字段只被配额监视用了，从没传给 qB。这里锁死行为。

不依赖 PostgreSQL：只测纯函数 + monkeypatch settings。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import qb_paths
from backend.config import settings
from tools.dispatch import quota


@pytest.fixture
def cfg(monkeypatch):
    """设置下载目录 + 路径映射（默认：映射关闭，等价同机部署）。"""
    def _set(download_path='', mappings_enabled=False, rules=None):
        monkeypatch.setattr(settings, 'qbittorrent_download_path', download_path, raising=False)
        monkeypatch.setattr(settings, 'path_mappings_enabled', mappings_enabled, raising=False)
        monkeypatch.setattr(settings, 'path_mappings_rules', rules or [], raising=False)
    return _set


# ---------- resolve_qb_save_path ----------

def test_explicit_save_path_wins(cfg):
    cfg(download_path='/configured/downloads')
    assert qb_paths.resolve_qb_save_path('/explicit/path') == '/explicit/path'


def test_falls_back_to_configured_download_path(cfg):
    """没显式指定 → 必须用设置页的下载目录，不能返回空让 qB 用自己默认值。"""
    cfg(download_path='/configured/downloads')
    assert qb_paths.resolve_qb_save_path() == '/configured/downloads'
    assert qb_paths.resolve_qb_save_path('') == '/configured/downloads'
    assert qb_paths.resolve_qb_save_path(None) == '/configured/downloads'


def test_empty_config_means_qb_default(cfg):
    """两者都空 → 返回空串，调用方不传 savepath（qB 用自己的默认目录）。"""
    cfg(download_path='')
    assert qb_paths.resolve_qb_save_path() == ''


def test_whitespace_config_is_empty(cfg):
    cfg(download_path='   ')
    assert qb_paths.resolve_qb_save_path() == ''


def test_backend_view_reverse_translated_for_qb(cfg):
    """配置写的是后端视角（后端能 stat），传给 qB 前要翻译成 qB 视角。"""
    cfg(
        download_path='Z:/downloads',
        mappings_enabled=True,
        rules=[{'from': '/downloads', 'to': 'Z:/downloads'}],
    )
    assert qb_paths.resolve_qb_save_path() == '/downloads'


def test_explicit_path_also_translated(cfg):
    cfg(
        download_path='/downloads',
        mappings_enabled=True,
        rules=[{'from': '/downloads', 'to': 'Z:/downloads'}],
    )
    assert qb_paths.resolve_qb_save_path('Z:/downloads/foo') == '/downloads/foo'


def test_unmapped_path_passes_through(cfg):
    cfg(
        download_path='X:/other',
        mappings_enabled=True,
        rules=[{'from': '/downloads', 'to': 'Z:/downloads'}],
    )
    assert qb_paths.resolve_qb_save_path() == 'X:/other'


# ---------- 下载根守卫（delete_files） ----------

@pytest.mark.parametrize('save_path', [
    '/download',
    '/download/movies/foo',
    '/download/',           # 尾斜杠
])
def test_guard_accepts_backend_view(cfg, save_path):
    cfg(download_path='/download')
    assert quota._save_path_under_downloads(save_path) is True


def test_guard_accepts_qb_view_after_translation(cfg):
    """跨机部署：配置是 Z:/downloads，qB 回报 /downloads —— 也必须认。"""
    cfg(
        download_path='Z:/downloads',
        mappings_enabled=True,
        rules=[{'from': '/downloads', 'to': 'Z:/downloads'}],
    )
    assert quota._save_path_under_downloads('/downloads') is True
    assert quota._save_path_under_downloads('/downloads/movies/foo') is True


def test_guard_rejects_other_paths(cfg):
    cfg(download_path='/download')
    assert quota._save_path_under_downloads('/download_other') is False
    assert quota._save_path_under_downloads('/library/videos/movie') is False
    assert quota._save_path_under_downloads('') is False
    assert quota._save_path_under_downloads(None) is False


def test_guard_disabled_when_not_configured(cfg):
    """下载根没配 → 一律不许带文件删（保守）。"""
    cfg(download_path='')
    assert quota._save_path_under_downloads('/download') is False
