"""copy-phase 冲突上下文解析 / 人工审核决策回归测试。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from backend.api.dispatch import (
    _assert_copy_conflict,
    _load_copy_conflict,
    _stage_displaced_file,
)
from tools.dispatch.copier import CrossTorrentCollisionError


def _row(error_log):
    return SimpleNamespace(error_log=error_log)


class CopyConflictReviewTests(unittest.TestCase):
    def test_load_prefixed_json(self):
        payload = {'kind': 'cross_torrent_collision', 'dst': '/library/a.mp4'}
        row = _row('[copying] ' + json.dumps(payload, ensure_ascii=False) + '\n')
        self.assertEqual(_load_copy_conflict(row), payload)

    def test_load_last_json_line(self):
        first = json.dumps({'kind': 'old', 'dst': '/library/old.mp4'})
        last = json.dumps({'kind': 'new', 'dst': '/library/new.mp4'})
        row = _row(f'[copying] {first}\n[copying] {last}\n')
        self.assertEqual(_load_copy_conflict(row)['dst'], '/library/new.mp4')

    def test_cross_torrent_error_payload_has_dst(self):
        err = CrossTorrentCollisionError(
            '目标已存在',
            dst='/library/HRSM-016.mp4',
            dst_size=15089802,
            src_size=9947308571,
            age_seconds=154440,
        )
        info = err.to_dict()
        self.assertEqual(info['kind'], 'cross_torrent_collision')
        self.assertEqual(info['dst'], '/library/HRSM-016.mp4')
        self.assertEqual(info['dst_size'], 15089802)
        self.assertEqual(info['src_size'], 9947308571)


class CopyConflictStateTests(unittest.TestCase):
    def test_running_state_is_reported_as_already_replaced(self):
        row = SimpleNamespace(
            phase='copying', phase_status='running',
            error_log=json.dumps({'kind': 'cross_torrent_collision', 'dst': '/library/a.mp4'}),
        )
        with self.assertRaises(HTTPException) as ctx:
            _assert_copy_conflict(row)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn('覆盖', ctx.exception.detail)

    def test_skipped_state_is_reported_as_already_skipped(self):
        row = SimpleNamespace(
            phase='copying', phase_status='skipped',
            error_log=json.dumps({'kind': 'cross_torrent_collision', 'dst': '/library/a.mp4'}),
        )
        with self.assertRaises(HTTPException) as ctx:
            _assert_copy_conflict(row)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn('保留', ctx.exception.detail)

    def test_stage_displaced_file_renames_within_same_directory(self):
        with tempfile.TemporaryDirectory() as d:
            original = Path(d) / 'movie.mkv'
            original.write_bytes(b'old data')
            staged = _stage_displaced_file(original, 'abcdef123456')
            self.assertFalse(original.exists())
            self.assertEqual(staged.parent, original.parent)
            self.assertTrue(staged.name.startswith('.jf-replaced-'))
            self.assertEqual(staged.read_bytes(), b'old data')


if __name__ == '__main__':
    unittest.main()
