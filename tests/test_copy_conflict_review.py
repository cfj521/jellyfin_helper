"""copy-phase 冲突上下文解析 / 人工审核决策回归测试。"""
import json
import unittest
from types import SimpleNamespace

from backend.api.dispatch import _load_copy_conflict
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


if __name__ == '__main__':
    unittest.main()
