"""成人视频封面文件名过长时的降级逻辑测试。"""
import errno
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.services import adult_assets


class AdultAssetsTest(unittest.TestCase):
    def test_fallback_poster_name_is_short(self):
        video = Path('/library/adult') / ('中' * 100 + '.mp4')
        out = adult_assets.fallback_poster_path(video)
        self.assertLess(len(out.name.encode('utf-8')), 100)
        self.assertTrue(out.name.endswith('-poster.jpg'))

    def test_save_poster_falls_back_on_enametoolong(self):
        with tempfile.TemporaryDirectory() as d:
            video = Path(d) / ('中' * 100 + '.mp4')
            target = video.with_name(video.stem + '-poster.jpg')
            real_write = Path.write_bytes
            uploaded = []

            def fake_write(self, data):
                if self == target:
                    raise OSError(errno.ENAMETOOLONG, 'File name too long')
                return real_write(self, data)

            with patch.object(Path, 'write_bytes', fake_write), \
                    patch.object(adult_assets, 'upload_poster_to_jellyfin',
                                 lambda *args: uploaded.append(args) or True):
                out = adult_assets.save_poster(video, b'image-bytes')

            self.assertNotEqual(out, target)
            self.assertTrue(out.exists())
            self.assertEqual(out.read_bytes(), b'image-bytes')
            self.assertEqual(uploaded[0][1], b'image-bytes')

    def test_find_local_poster_reads_fallback_name(self):
        with tempfile.TemporaryDirectory() as d:
            video = Path(d) / ('中' * 100 + '.mp4')
            fallback = adult_assets.fallback_poster_path(video, '.jpg')
            fallback.write_bytes(b'image-bytes')
            self.assertEqual(adult_assets.find_local_poster(video), fallback)


if __name__ == '__main__':
    unittest.main()
