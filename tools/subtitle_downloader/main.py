"""
字幕自动下载工具

按 config.subtitle.sources 顺序尝试多个字幕源，第一个返回结果即采用。
内置 provider：opensubtitles / assrt。
"""
import sys
import json
import argparse
import logging
from pathlib import Path
from typing import Dict, List, Optional, Callable, Any
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from common.config import load_config, setup_logging
from .opensubtitles import OpenSubtitlesClient
from . import assrt as _assrt
from . import shooter as _shooter
from .scoring import SubtitleCandidate, VideoInfo, pick_best

logger = logging.getLogger(__name__)


# ============================================================================
# Providers：每个字幕源实现 try_download() 接口
# ============================================================================

def _lookup_video_metadata(video_path: Path) -> Dict[str, Any]:
    """
    从 jellyfin path index 反查视频对应的元数据（tmdb_id / imdb_id / name / year）。
    用于：
      - OpenSubtitles 优先用 ID 搜
      - assrt 第一次用 release 名搜不到时 fallback 到 "name year"

    web 后端启动后 jellyfin path index 才可用；CLI 模式可能拿不到，返回空 dict。
    """
    try:
        from web.backend.api.jellyfin import lookup_jellyfin_item
    except Exception:
        return {}
    info = lookup_jellyfin_item(str(video_path))
    if not info:
        return {}
    return {
        'tmdb_id': info.get('tmdb_id'),
        'imdb_id': info.get('imdb_id'),
        'name': info.get('name'),
        'year': info.get('year'),
    }


class _Provider:
    """字幕源 provider 抽象基类。"""

    name: str = "unknown"

    def try_download(
        self,
        video_path: Path,
        languages: List[str],
        dry_run: bool,
    ) -> Dict[str, Any]:
        """
        尝试下载字幕。返回字典：
          - status: success / exists / not_found / failed
          - subtitle: 落地后的文件名（success/exists 时）
          - language: 字幕语言代码
          - source: provider 名（成功时）
          - error: 错误信息（failed 时）
        """
        raise NotImplementedError


class _OpenSubtitlesProvider(_Provider):
    """OpenSubtitles 字幕源。"""

    name = "opensubtitles"

    LANG_REVERSE = {
        'zh-cn': 'chs', 'zh-tw': 'cht',
        'chinese': 'chs', 'english': 'eng', 'en': 'eng',
        'japanese': 'jpn', 'ja': 'jpn',
        'korean': 'kor', 'ko': 'kor',
    }

    def __init__(self, sub_cfg: Dict):
        api_key = sub_cfg.get('opensubtitles_api_key', '')
        if not api_key:
            raise ValueError("OpenSubtitles 未配置 API Key")
        self.client = OpenSubtitlesClient(
            api_key=api_key,
            username=sub_cfg.get('opensubtitles_username'),
            password=sub_cfg.get('opensubtitles_password'),
            request_delay=sub_cfg.get('opensubtitles_request_delay', 2.0),
        )

    def _map_lang_code(self, lang: str) -> str:
        return self.LANG_REVERSE.get(lang.lower(), lang)

    def try_download(self, video_path, languages, dry_run):
        # 优先用 IMDB / TMDB ID 搜（精度远高于 query）；ID 拿不到再退回文件名
        meta = _lookup_video_metadata(video_path)
        try:
            results = self.client.search(
                video_path=video_path,
                languages=languages,
                imdb_id=meta.get('imdb_id'),
                tmdb_id=meta.get('tmdb_id'),
            )
            # ID 搜没结果 fallback 到文件名搜
            if not results and (meta.get('imdb_id') or meta.get('tmdb_id')):
                logger.info(
                    f"[opensubtitles] {video_path.name} ID 搜无结果，fallback 到文件名"
                )
                results = self.client.search(video_path=video_path, languages=languages)
        except Exception as e:
            logger.error(f"[opensubtitles] 搜索失败 {video_path.name}: {e}")
            return {"status": "failed", "error": f"搜索失败: {e}"}

        if not results:
            return {"status": "not_found"}

        # 选下载量最高的
        results.sort(
            key=lambda x: x.get('attributes', {}).get('download_count', 0),
            reverse=True,
        )
        best = results[0]
        attrs = best.get('attributes', {})
        files = attrs.get('files', []) or []
        if not files:
            return {"status": "not_found"}

        file_info = files[0]
        file_id = file_info.get('file_id')
        file_name = file_info.get('file_name', 'subtitle.srt')

        lang_code = self._map_lang_code(attrs.get('language', 'unknown'))
        ext = Path(file_name).suffix or '.srt'
        output_name = f"{video_path.stem}.{lang_code}{ext}"
        output_path = video_path.parent / output_name

        if output_path.exists():
            return {"status": "exists", "subtitle": output_name, "language": lang_code, "source": self.name}

        if dry_run:
            return {
                "status": "success", "subtitle": output_name,
                "language": lang_code, "source": self.name, "dry_run": True,
            }

        try:
            ok = self.client.download(file_id, output_path)
        except Exception as e:
            logger.error(f"[opensubtitles] 下载失败 {video_path.name}: {e}")
            return {"status": "failed", "error": f"下载失败: {e}"}

        if ok:
            return {"status": "success", "subtitle": output_name, "language": lang_code, "source": self.name}
        return {"status": "failed", "error": "下载请求未成功"}


class _AssrtProvider(_Provider):
    """射手字幕源（assrt.net）。"""

    name = "assrt"

    def __init__(self, sub_cfg: Dict):
        token = sub_cfg.get('assrt_api_token', '')
        if not token:
            raise ValueError("assrt 未配置 API Token")
        self.client = _assrt.AssrtClient(
            token=token,
            request_delay=sub_cfg.get('assrt_request_delay', 3.0),
        )
        # 字幕格式偏好：默认 ass>srt>sup
        self.preferred_formats = sub_cfg.get('preferred_formats', ['ass', 'srt', 'sup'])

    def try_download(self, video_path, languages, dry_run):
        # ---- 1. 第一次用 release 名（视频文件 stem）按文件名匹配 ----
        try:
            subs = self.client.search(video_path.stem, is_file=True, cnt=15)
        except _assrt.AssrtRateLimitError as e:
            return {"status": "failed", "error": f"配额超限: {e}"}
        except _assrt.AssrtError as e:
            logger.error(f"[assrt] 搜索失败 {video_path.name}: {e}")
            return {"status": "failed", "error": f"搜索失败: {e}"}
        except ValueError as e:
            return {"status": "failed", "error": str(e)}

        # ---- 2. release 名搜不到 → fallback 到 jellyfin "name year" 通用搜 ----
        if not subs:
            meta = _lookup_video_metadata(video_path)
            name = (meta.get('name') or '').strip()
            year = meta.get('year')
            if name:
                fallback_q = f"{name} {year}" if year else name
                if len(fallback_q) >= 3:
                    logger.info(
                        f"[assrt] {video_path.name} release 名无结果，fallback 到 '{fallback_q}'"
                    )
                    try:
                        subs = self.client.search(fallback_q, is_file=False, cnt=15)
                    except (_assrt.AssrtRateLimitError, _assrt.AssrtError, ValueError) as e:
                        logger.warning(f"[assrt] fallback 搜索失败: {e}")

        if not subs:
            return {"status": "not_found"}

        # ---- 2. 选最匹配的：先按语言命中过滤，再按 vote_score 排序 ----
        def lang_codes_of(s: Dict) -> List[str]:
            lang_block = s.get('lang') or {}
            return _assrt.parse_langlist(
                lang_block.get('langlist'), lang_block.get('desc') or '',
            )

        # 给候选打两层分：第一层语言匹配位置（越靠前越优），第二层 -vote
        def sort_key(s: Dict):
            codes = lang_codes_of(s)
            for i, want in enumerate(languages):
                if want in codes:
                    return (i, -float(s.get('vote_score') or 0))
            return (999, -float(s.get('vote_score') or 0))

        subs.sort(key=sort_key)
        best = subs[0]
        sub_id = best.get('id')

        # 候选语言里挑一个跟 languages 最契合的，作为 dry_run 兜底标签
        best_codes = lang_codes_of(best)
        chosen_lang = next(
            (c for c in languages if c in best_codes),
            languages[0] if languages else 'chs',
        )

        if dry_run:
            return {
                "status": "success",
                "subtitle": f"(预览) {best.get('native_name') or best.get('videoname') or sub_id}",
                "language": chosen_lang,
                "source": self.name,
                "dry_run": True,
            }

        # ---- 3. 取详情拿临时 url ----
        try:
            detail = self.client.detail(sub_id)
        except _assrt.AssrtRateLimitError as e:
            return {"status": "failed", "error": f"配额超限: {e}"}
        except _assrt.AssrtError as e:
            return {"status": "failed", "error": f"详情失败: {e}"}
        if not detail or not detail.get('url'):
            return {"status": "failed", "error": "详情未返回下载 URL"}

        # ---- 4. 拉字节 ----
        try:
            content, ext = self.client.fetch_archive(detail['url'])
        except _assrt.AssrtError as e:
            return {"status": "failed", "error": str(e)}

        # ---- 5a. 直接是字幕文件 ----
        if ext.lower() in _assrt.SUB_EXTENSIONS:
            archive_name = detail.get('filename') or f"assrt_{sub_id}{ext}"
            guessed = _assrt.guess_lang_from_filename(archive_name) or chosen_lang
            target = video_path.parent / f"{video_path.stem}.{guessed}{ext.lower()}"
            if target.exists():
                return {"status": "exists", "subtitle": target.name, "language": guessed, "source": self.name}
            target.write_bytes(content)
            return {"status": "success", "subtitle": target.name, "language": guessed, "source": self.name}

        # ---- 5b. 压缩包：解压挑最佳（按 preferred_langs + preferred_formats）----
        files = _assrt.extract_subtitles_from_archive(content, ext)
        if not files:
            return {"status": "failed", "error": f"未识别压缩格式（{ext or '?'}），请改用手动下载"}

        # 包元数据 fallback：assrt 包里很多字幕文件是裸 release 名（无 lang token），
        # 但 detail.lang.langlist 已声明整包语言 —— 不传 fallback 会让全包 score 并列(1,9999)
        lang_block = detail.get('lang') or {}
        pkg_lang_codes = _assrt.parse_langlist(
            lang_block.get('langlist'), lang_block.get('desc') or '',
        )
        pkg_fallback = '.'.join(pkg_lang_codes) if pkg_lang_codes else None

        # 自动下载链只下"一份"—— 严格按用户偏好挑最优 (lang × format)，不再多格式重复
        # 同时按视频文件名集号过滤（包内是季全集时挑 E?? 命中的那个）
        picked = _assrt.pick_best_subtitle(
            files, languages, self.preferred_formats,
            fallback_lang=pkg_fallback,
            video_filename=video_path.name,
        )
        if not picked:
            return {"status": "not_found"}

        best_name, best_data, best_lang = picked
        sub_ext = Path(best_name).suffix.lower() or '.srt'
        target = video_path.parent / f"{video_path.stem}.{best_lang}{sub_ext}"
        if target.exists():
            return {"status": "exists", "subtitle": target.name, "language": best_lang, "source": self.name}
        target.write_bytes(best_data)
        return {"status": "success", "subtitle": target.name, "language": best_lang, "source": self.name}


class _ShooterProvider(_Provider):
    """Shooter（射手网）字幕源 —— 中文电影 hash 命中率高。"""

    name = "shooter"

    def __init__(self, sub_cfg: Dict):
        self.client = _shooter.ShooterClient(
            request_delay=sub_cfg.get('shooter_request_delay', 2.0),
        )

    def try_download(self, video_path, languages, dry_run):
        # Shooter 仅支持 Chn / Eng；按目标语言挑一个发起请求
        want_chn = any(l in ('chs', 'cht', 'zh', 'zh-cn', 'zh-tw') for l in languages)
        want_eng = any(l in ('eng', 'en') for l in languages)
        if not (want_chn or want_eng):
            # 目标语言不在 shooter 支持范围 — 放弃
            return {"status": "not_found"}

        # 优先 Chn，其次 Eng。两轮各发一次
        all_subs: List[Dict] = []
        for shooter_lang, label in [('Chn', want_chn), ('Eng', want_eng)]:
            if not label:
                continue
            try:
                results = self.client.search(video_path, lang=shooter_lang)
            except Exception as e:
                logger.warning(f"[shooter] 搜索失败 {video_path.name}: {e}")
                continue
            for sub in results:
                sub['_shooter_lang_param'] = shooter_lang
            all_subs.extend(results)

        if not all_subs:
            return {"status": "not_found"}

        # 包装成 candidate（hash 命中是 shooter 协议的核心特性 → hash_match=True）
        video_info = VideoInfo.from_filename(video_path.name)
        candidates: List[SubtitleCandidate] = []
        for sub in all_subs:
            files = sub.get('Files') or []
            if not files:
                continue
            desc = sub.get('Desc') or ''
            lang = _shooter.guess_lang_from_desc(desc, files)
            # shooter 不支持 cht 单独标记；当用户只要 cht 时跳过 chs 候选
            if lang == 'unknown':
                # 按请求时的 lang 参数兜底
                lang = 'chs' if sub.get('_shooter_lang_param') == 'Chn' else 'eng'
            candidates.append(SubtitleCandidate(
                source=self.name,
                raw=sub,
                language=lang,
                release_name=desc,
                hash_match=True,  # shooter 命中即 hash 匹配
                vote=5.0,  # shooter 不返回投票数；给个中等值兜底
            ))

        best = pick_best(candidates, video_info, languages)
        if not best:
            return {"status": "not_found"}

        # 选 best 的第一个 file
        files = best.raw.get('Files') or []
        if not files:
            return {"status": "not_found"}
        chosen_file = files[0]
        link = chosen_file.get('Link')
        ext = ('.' + (chosen_file.get('Ext') or 'srt').lstrip('.')).lower()
        if not link:
            return {"status": "not_found"}

        output_name = f"{video_path.stem}.{best.language}{ext}"
        target = video_path.parent / output_name
        if target.exists():
            return {"status": "exists", "subtitle": output_name, "language": best.language, "source": self.name}
        if dry_run:
            return {
                "status": "success", "subtitle": output_name,
                "language": best.language, "source": self.name,
                "dry_run": True, "score": best.score, "score_breakdown": best.score_breakdown,
            }
        ok = self.client.download(link, target)
        if not ok:
            return {"status": "failed", "error": "shooter 下载失败"}
        return {
            "status": "success", "subtitle": output_name,
            "language": best.language, "source": self.name,
            "score": best.score, "score_breakdown": best.score_breakdown,
        }


# 注册表：name → 构造函数
_PROVIDER_FACTORIES = {
    'opensubtitles': _OpenSubtitlesProvider,
    'assrt': _AssrtProvider,
    'shooter': _ShooterProvider,
}


# ============================================================================
# SubtitleDownloader：编排多 provider 的回退链
# ============================================================================

class SubtitleDownloader:
    """字幕下载器。按 subtitle.sources 配置的顺序尝试多个 provider。"""

    def __init__(self, config: dict):
        self.config = config
        sub_cfg = config.get('subtitle', {}) or {}

        self.preferred_langs = sub_cfg.get('preferred_langs', ['chs', 'eng'])

        # 解析 sources 优先级；如果配置缺失，给个回退（assrt 优先）
        configured = sub_cfg.get('sources') or [
            {'name': 'assrt', 'enabled': True},
            {'name': 'opensubtitles', 'enabled': True},
        ]

        self.providers: List[_Provider] = []
        init_errors: List[str] = []
        for entry in configured:
            # 出现在 sources 列表里就视为启用；旧 yaml 里写了 enabled:false 才跳过
            if not entry.get('enabled', True):
                continue
            name = entry.get('name')
            factory = _PROVIDER_FACTORIES.get(name)
            if factory is None:
                logger.warning(f"未知字幕源: {name}（跳过）")
                continue
            try:
                self.providers.append(factory(sub_cfg))
            except ValueError as e:
                # 配置不全（无 api key 等）→ 跳过该源；其他源还能用就行
                init_errors.append(f"{name}: {e}")
                logger.warning(f"字幕源 {name} 初始化失败: {e}")

        if not self.providers:
            # 所有启用源都没配齐 / 都没启用 → 抛错（auto-fix 流程外层会捕获）
            if init_errors:
                raise ValueError(
                    f"没有可用的字幕源；配置错误：{'; '.join(init_errors)}"
                )
            raise ValueError("subtitle.sources 中没有启用任何字幕源")

        logger.info(f"字幕源链: {[p.name for p in self.providers]}")

        # 第一个 provider 当主源（CLI 兼容路径用，提供 .client 属性）
        self.client = self.providers[0].client if hasattr(self.providers[0], 'client') else None

        self.stats = {'total': 0, 'success': 0, 'failed': 0, 'skipped': 0}

    # ---- 单视频下载（按 provider 链回退）----

    def download_for_video(
        self,
        video_path: Path,
        languages: list = None,
        dry_run: bool = True,
    ) -> Dict:
        if languages is None:
            languages = self.preferred_langs

        last_failure: Optional[Dict] = None

        for provider in self.providers:
            logger.info(f"[{provider.name}] 搜索字幕: {video_path.name}")
            try:
                result = provider.try_download(video_path, languages, dry_run)
            except Exception as e:
                logger.exception(f"[{provider.name}] 异常: {video_path.name}")
                last_failure = {"status": "failed", "error": f"{provider.name}: {e}", "source": provider.name}
                continue

            status = result.get('status')
            if status in ('success', 'exists'):
                # 成功：直接返回
                result.setdefault('source', provider.name)
                return result

            if status == 'not_found':
                logger.info(f"[{provider.name}] 未找到 {video_path.name}，尝试下一个源")
                last_failure = {**result, 'source': provider.name}
                continue

            # failed：记录、继续兜底
            logger.warning(f"[{provider.name}] 失败 {video_path.name}: {result.get('error')}")
            last_failure = {**result, 'source': provider.name}

        # 所有源都没成功
        if last_failure:
            return last_failure
        return {"status": "not_found"}

    # ---- 批量入口（保持原签名兼容）----

    def process_videos(
        self,
        video_paths: List[Path],
        languages: Optional[List[str]] = None,
        dry_run: bool = True,
        progress_cb: Optional[Callable[[int, int, Dict], None]] = None,
    ) -> List[Dict]:
        details: List[Dict] = []
        total = len(video_paths)
        self.stats = {'total': total, 'success': 0, 'failed': 0, 'skipped': 0}

        for idx, video_path in enumerate(video_paths, start=1):
            item = {"video": video_path.name, "video_path": str(video_path)}
            try:
                if not video_path.exists():
                    item.update({"status": "failed", "error": "视频文件不存在"})
                else:
                    item.update(self.download_for_video(video_path, languages=languages, dry_run=dry_run))
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
                    item.update(self.download_for_video(video_path, languages=langs, dry_run=dry_run))
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

    # ---- CLI 入口 ----

    def process_from_report(self, report_path: Path, dry_run: bool = True):
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
        print("\n获取 API Key:")
        print("  OpenSubtitles: https://www.opensubtitles.com/consumers")
        print("  assrt: https://secure.assrt.net/usercp.php")
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
