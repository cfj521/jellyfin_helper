"""
字幕自动下载工具
根据扫描报告自动下载缺失的字幕
"""
import sys
import json
import argparse
import logging
from pathlib import Path
from typing import Dict, List, Optional, Callable
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from common.config import load_config, setup_logging
from .opensubtitles import OpenSubtitlesClient

logger = logging.getLogger(__name__)


class SubtitleDownloader:
    """字幕下载器"""

    def __init__(self, config: dict):
        self.config = config

        api_key = config.get('subtitle', {}).get('opensubtitles_api_key', '')
        if not api_key:
            raise ValueError("请在 config.yaml 中配置 subtitle.opensubtitles_api_key")

        self.client = OpenSubtitlesClient(
            api_key=api_key,
            username=config.get('subtitle', {}).get('opensubtitles_username'),
            password=config.get('subtitle', {}).get('opensubtitles_password')
        )

        self.preferred_langs = config.get('subtitle', {}).get('preferred_langs', ['chs', 'eng'])

        self.stats = {
            'total': 0,
            'success': 0,
            'failed': 0,
            'skipped': 0
        }

    def download_for_video(self, video_path: Path, languages: list = None, dry_run: bool = True) -> Dict:
        """
        为视频下载字幕。

        返回 dict:
          - status: "success" | "skipped" | "exists" | "not_found" | "failed"
          - subtitle: 下载后的字幕文件名（success/exists 时）
          - language: 字幕语言代码
          - error: 错误信息（failed 时）
        """
        if languages is None:
            languages = self.preferred_langs

        logger.info(f"搜索字幕: {video_path.name}")

        try:
            results = self.client.search(video_path, languages)
        except Exception as e:
            logger.error(f"搜索失败: {video_path.name} - {e}")
            return {"status": "failed", "error": f"搜索失败: {e}"}

        if not results:
            logger.warning(f"未找到字幕: {video_path.name}")
            return {"status": "not_found"}

        # 按下载量排序，选最热门那个
        best = sorted(results, key=lambda x: x.get('attributes', {}).get('download_count', 0), reverse=True)

        for sub in best[:1]:
            attrs = sub.get('attributes', {})
            files = attrs.get('files', [])

            if not files:
                continue

            file_info = files[0]
            file_id = file_info.get('file_id')
            file_name = file_info.get('file_name', 'subtitle.srt')

            lang = attrs.get('language', 'unknown')
            lang_code = self._map_lang_code(lang)
            ext = Path(file_name).suffix or '.srt'

            output_name = f"{video_path.stem}.{lang_code}{ext}"
            output_path = video_path.parent / output_name

            if output_path.exists():
                logger.info(f"字幕已存在: {output_name}")
                return {"status": "exists", "subtitle": output_name, "language": lang_code}

            if dry_run:
                logger.info(f"[预览] 将下载: {output_name}")
                return {"status": "success", "subtitle": output_name, "language": lang_code, "dry_run": True}

            try:
                if self.client.download(file_id, output_path):
                    return {"status": "success", "subtitle": output_name, "language": lang_code}
                return {"status": "failed", "error": "下载请求未成功"}
            except Exception as e:
                logger.error(f"下载失败: {video_path.name} - {e}")
                return {"status": "failed", "error": f"下载失败: {e}"}

        return {"status": "failed", "error": "搜索结果中无可下载文件"}

    def _map_lang_code(self, lang: str) -> str:
        """映射语言代码"""
        mapping = {
            'zh-cn': 'chs',
            'zh-tw': 'cht',
            'chinese': 'chs',
            'english': 'eng',
            'en': 'eng',
            'japanese': 'jpn',
            'ja': 'jpn',
            'korean': 'kor',
            'ko': 'kor',
        }
        return mapping.get(lang.lower(), lang)

    def process_videos(
        self,
        video_paths: List[Path],
        languages: Optional[List[str]] = None,
        dry_run: bool = True,
        progress_cb: Optional[Callable[[int, int, Dict], None]] = None,
    ) -> List[Dict]:
        """
        处理一批视频，下载字幕。返回每个视频的明细 list。

        progress_cb(index, total, item_result) 在每个视频处理完后被调用，供 Web 端汇报进度。
        """
        details: List[Dict] = []
        total = len(video_paths)
        self.stats = {'total': total, 'success': 0, 'failed': 0, 'skipped': 0}

        for idx, video_path in enumerate(video_paths, start=1):
            item = {
                "video": video_path.name,
                "video_path": str(video_path),
            }
            try:
                if not video_path.exists():
                    item.update({"status": "failed", "error": "视频文件不存在"})
                else:
                    result = self.download_for_video(video_path, languages=languages, dry_run=dry_run)
                    item.update(result)
            except Exception as e:
                logger.error(f"处理失败: {video_path} - {e}")
                item.update({"status": "failed", "error": str(e)})

            status = item.get("status")
            if status in ("success", "exists"):
                self.stats['success'] += 1
            elif status in ("not_found", "skipped"):
                self.stats['skipped'] += 1
            else:
                self.stats['failed'] += 1

            details.append(item)
            if progress_cb:
                try:
                    progress_cb(idx, total, item)
                except Exception:
                    logger.exception("progress_cb 抛错，已忽略")

        return details

    @staticmethod
    def collect_videos_from_report(report: Dict) -> List[Path]:
        """
        从扫描报告 dict 中提取缺字幕的视频路径列表。
        新版（含 embedded 检测后）：missing_langs 非空即为缺；
        老报告兼容：报告里没有 missing_langs 字段时退化到"无外挂字幕"判断。
        """
        videos: List[Path] = []
        for dir_info in report.get('directories', []):
            for video in dir_info.get('videos', []):
                if 'missing_langs' in video:
                    if video.get('missing_langs'):
                        videos.append(Path(video['path']))
                else:
                    if not video.get('subtitles'):
                        videos.append(Path(video['path']))
        return videos

    @staticmethod
    def collect_targets_from_report(report: Dict) -> List[Dict]:
        """
        给 auto-fix 用：返回 [{path, missing_langs}] 列表，
        每个视频按自己缺的语言单独下载，避免对已有 chs 的视频又下一遍 chs。
        """
        targets: List[Dict] = []
        for dir_info in report.get('directories', []):
            for video in dir_info.get('videos', []):
                missing = video.get('missing_langs') or []
                if missing:
                    targets.append({
                        'path': Path(video['path']),
                        'missing_langs': list(missing),
                    })
        return targets

    def auto_fix_from_report(
        self,
        report: Dict,
        dry_run: bool = True,
        progress_cb: Optional[Callable[[int, int, Dict], None]] = None,
    ) -> List[Dict]:
        """
        按扫描报告里每个视频自身的 missing_langs 下载。
        返回每个视频处理结果列表（结构同 process_videos）。
        """
        targets = self.collect_targets_from_report(report)
        details: List[Dict] = []
        total = len(targets)
        self.stats = {'total': total, 'success': 0, 'failed': 0, 'skipped': 0}

        for idx, t in enumerate(targets, start=1):
            video_path: Path = t['path']
            langs: List[str] = t['missing_langs']
            item = {
                'video': video_path.name,
                'video_path': str(video_path),
                'requested_langs': langs,
            }
            try:
                if not video_path.exists():
                    item.update({'status': 'failed', 'error': '视频文件不存在'})
                else:
                    result = self.download_for_video(video_path, languages=langs, dry_run=dry_run)
                    item.update(result)
            except Exception as e:
                logger.error(f"处理失败: {video_path} - {e}")
                item.update({'status': 'failed', 'error': str(e)})

            status = item.get('status')
            if status in ('success', 'exists'):
                self.stats['success'] += 1
            elif status in ('not_found', 'skipped'):
                self.stats['skipped'] += 1
            else:
                self.stats['failed'] += 1

            details.append(item)
            if progress_cb:
                try:
                    progress_cb(idx, total, item)
                except Exception:
                    logger.exception("progress_cb 抛错，已忽略")

        return details

    def process_from_report(self, report_path: Path, dry_run: bool = True):
        """根据扫描报告处理（CLI 入口）"""
        with open(report_path, 'r', encoding='utf-8') as f:
            report = json.load(f)

        videos_to_process = self.collect_videos_from_report(report)

        if not videos_to_process:
            print("没有需要下载字幕的视频")
            return

        print(f"共 {len(videos_to_process)} 个视频需要下载字幕")

        if not dry_run:
            confirm = input("是否继续？[y/N] ").strip().lower()
            if confirm != 'y':
                print("已取消")
                return

        bar = tqdm(total=len(videos_to_process), desc="下载进度")

        def _cb(idx, total, item):
            bar.update(1)

        try:
            self.process_videos(videos_to_process, dry_run=dry_run, progress_cb=_cb)
        finally:
            bar.close()

        self._print_summary(dry_run)

    def _print_summary(self, dry_run: bool):
        print("\n" + "=" * 50)
        print(f"{'预览' if dry_run else '下载'}完成！统计结果：")
        print(f"  总数: {self.stats['total']}")
        print(f"  成功: {self.stats['success']}")
        print(f"  失败: {self.stats['failed']}")
        print("=" * 50)


def main():
    parser = argparse.ArgumentParser(description='字幕自动下载工具')
    parser.add_argument('report', help='扫描报告JSON文件路径')
    parser.add_argument('-c', '--config', default=None, help='配置文件路径')
    parser.add_argument('--execute', action='store_true', help='执行下载（默认预览）')
    parser.add_argument('-v', '--verbose', action='store_true', help='详细日志')

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    setup_logging(level=log_level)

    print("=" * 50)
    print("字幕自动下载工具")
    print("=" * 50)

    try:
        config = load_config(args.config)
        downloader = SubtitleDownloader(config)

        report_path = Path(args.report)
        if not report_path.exists():
            print(f"错误: 报告文件不存在: {report_path}")
            return 1

        downloader.process_from_report(report_path, dry_run=not args.execute)

    except ValueError as e:
        print(f"\n配置错误: {e}")
        print("\n获取 OpenSubtitles API Key:")
        print("1. 访问 https://www.opensubtitles.com/consumers")
        print("2. 注册账号并创建 API consumer")
        print("3. 将 API Key 填入 config.yaml")
        return 1
    except FileNotFoundError as e:
        print(f"\n错误: {e}")
        return 1
    except KeyboardInterrupt:
        print("\n\n用户中断")
        return 0

    return 0


if __name__ == '__main__':
    sys.exit(main())
