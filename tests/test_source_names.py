import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.services.source_names import normalize_path


class SourceNameHelpersTest(unittest.TestCase):
    def test_normalize_path_handles_windows_and_trailing_slash(self):
        self.assertEqual(
            normalize_path('Z:\\videos\\movie\\A\\a.mkv'),
            'Z:/videos/movie/A/a.mkv',
        )
        self.assertEqual(
            normalize_path('/library/videos/movie/A/'),
            '/library/videos/movie/A',
        )
        self.assertEqual(normalize_path(None), '')
        self.assertEqual(normalize_path(''), '')

    def test_media_item_has_source_name_column(self):
        from backend.database import AdultItem, DownloadDispatchMap, MediaItem

        self.assertIn('source_name', MediaItem.__table__.columns.keys())
        self.assertIn('source_name', AdultItem.__table__.columns.keys())
        self.assertIn('source_name', DownloadDispatchMap.__table__.columns.keys())


if __name__ == '__main__':
    unittest.main()
