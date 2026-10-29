"""字幕语言优先级回归测试。

覆盖两个容易复发的点：
1. 候选排序按“覆盖 preferred 语言数量”分层，双语包不会被单语包靠 release 分反超。
2. auto-fix 的缺失语言（例如只有 chs）不能覆盖下载偏好（chs.eng > chs）。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.subtitle_downloader.main import SubtitleDownloader, _AssrtProvider
from tools.subtitle_downloader.scoring import SubtitleCandidate, VideoInfo, pick_best


class SubtitleLanguagePriorityTest(unittest.TestCase):
    def test_bilingual_beats_single_language(self):
        video = VideoInfo(filename="Show.S01E01.1080p.WEB-DL-GROUP.mkv")
        candidates = [
            SubtitleCandidate(source="assrt", raw={}, language="chs", vote=10),
            SubtitleCandidate(source="assrt", raw={}, language="chs.eng", vote=0),
            SubtitleCandidate(source="assrt", raw={}, language="eng", vote=10),
            SubtitleCandidate(source="assrt", raw={}, language="jpn", vote=10),
        ]
        for preferred in (["chs", "eng"], ["chs.eng", "chs"]):
            best = pick_best(candidates, video, preferred)
            self.assertEqual(best.language, "chs.eng", preferred)

    def test_assrt_sort_prefers_bilingual_over_higher_vote_chinese(self):
        class FakeClient:
            def search(self, query, is_file=True, cnt=15):
                return [
                    {
                        "id": 1,
                        "lang": {"langlist": {"langchs": True}, "desc": "简体"},
                        "vote_score": 99,
                        "native_name": "Chinese only",
                    },
                    {
                        "id": 2,
                        "lang": {
                            "langlist": {"langchs": True, "langeng": True},
                            "desc": "简体&英文",
                        },
                        "vote_score": 0,
                        "native_name": "Bilingual",
                    },
                ]

        provider = object.__new__(_AssrtProvider)
        provider.name = "assrt"
        provider.client = FakeClient()
        provider.preferred_formats = ["ass", "srt", "sup"]

        result = provider.try_download(
            Path("Show.S01E01.mkv"),
            ["chs"],
            dry_run=True,
            rank_langs=["chs", "eng"],
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["language"], "chs.eng")

    def test_auto_fix_keeps_download_preference_separate_from_missing_langs(self):
        class FakeProvider:
            name = "fake"

            def __init__(self):
                self.calls = []

            def try_download(self, video_path, languages, dry_run, rank_langs=None):
                self.calls.append((list(languages), list(rank_langs or [])))
                return {"status": "success", "subtitle": video_path.name}

        downloader = object.__new__(SubtitleDownloader)
        downloader.providers = [FakeProvider()]
        downloader.downloading_langs = ["chs.eng", "chs"]
        downloader.stats = {}
        downloader.config = {}

        with tempfile.NamedTemporaryFile(suffix=".mkv", delete=False) as f:
            video_path = Path(f.name)
        try:
            report = {
                "directories": [
                    {"videos": [{"path": str(video_path), "missing_langs": ["chs"]}]}
                ]
            }
            details = downloader.auto_fix_from_report(report, dry_run=True)
        finally:
            video_path.unlink(missing_ok=True)

        self.assertEqual(details[0]["status"], "success")
        self.assertEqual(downloader.providers[0].calls[0][0], ["chs"])
        self.assertEqual(
            downloader.providers[0].calls[0][1],
            ["chs.eng", "chs"],
        )


if __name__ == "__main__":
    unittest.main()
