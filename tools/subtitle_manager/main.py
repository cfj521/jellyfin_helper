"""
字幕管理工具
功能：扫描报告、自动重命名、缺失分析
"""
import sys
import argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from .scanner import SubtitleScanner
from .report import generate_html_report
from .renamer import SubtitleRenamer


def cmd_scan(args):
    """扫描命令"""
    path = Path(args.path)
    if not path.exists():
        print(f"错误: 路径不存在: {path}")
        return 1

    print("=" * 50)
    print("字幕扫描工具")
    print("=" * 50)
    print(f"扫描路径: {path}")
    print()

    scanner = SubtitleScanner(preferred_langs=args.langs.split(',') if args.langs else None)
    result = scanner.scan(path, recursive=not args.no_recursive)

    # 打印摘要
    print(f"扫描完成!")
    print(f"  视频总数: {result.total_videos}")
    print(f"  有字幕: {result.total_with_sub}")
    print(f"  缺少字幕: {result.total_without_sub}")
    print(f"  字幕文件: {result.total_subtitles}")

    # 生成报告
    if args.output:
        output_path = Path(args.output)
    else:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_path = Path(f"subtitle_report_{timestamp}.html")

    if args.json:
        json_path = output_path.with_suffix('.json')
        scanner.save_json(result, json_path)
        print(f"\nJSON报告: {json_path}")

    generate_html_report(result, output_path)
    print(f"HTML报告: {output_path}")

    return 0


def cmd_rename(args):
    """重命名命令"""
    path = Path(args.path)
    if not path.exists():
        print(f"错误: 路径不存在: {path}")
        return 1

    dry_run = not args.execute

    print("=" * 50)
    print("字幕重命名工具")
    print("=" * 50)
    print(f"目录: {path}")
    print(f"模式: {'执行' if args.execute else '预览'}")
    if args.lang:
        print(f"语言代码: {args.lang}")
    print()

    renamer = SubtitleRenamer()
    results = renamer.process_directory(
        path,
        lang=args.lang,
        dry_run=dry_run,
        recursive=not args.no_recursive,
        verbose=args.verbose
    )
    total = len(results)

    print()
    print("=" * 50)
    if dry_run:
        print(f"预览完成: 共 {total} 个字幕将被重命名")
        print("使用 --execute 参数执行实际重命名")
    else:
        print(f"完成: 共重命名 {total} 个字幕")
    print("=" * 50)

    return 0


def main():
    parser = argparse.ArgumentParser(
        description='Jellyfin 字幕管理工具',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest='command', help='可用命令')

    # scan 命令
    scan_parser = subparsers.add_parser('scan', help='扫描字幕并生成报告')
    scan_parser.add_argument('path', help='媒体目录路径')
    scan_parser.add_argument('-o', '--output', help='报告输出路径')
    scan_parser.add_argument('--json', action='store_true', help='同时生成JSON报告')
    scan_parser.add_argument('--langs', help='期望的语言代码，逗号分隔 (如: chs,eng)')
    scan_parser.add_argument('--no-recursive', action='store_true', help='不递归扫描')

    # rename 命令
    rename_parser = subparsers.add_parser('rename', help='重命名字幕文件')
    rename_parser.add_argument('path', help='媒体目录路径')
    rename_parser.add_argument('--execute', action='store_true', help='执行重命名（默认预览）')
    rename_parser.add_argument('--lang', help='强制使用的语言代码')
    rename_parser.add_argument('--no-recursive', action='store_true', help='不递归处理')
    rename_parser.add_argument('-v', '--verbose', action='store_true', help='显示详细信息')

    args = parser.parse_args()

    if args.command == 'scan':
        return cmd_scan(args)
    elif args.command == 'rename':
        return cmd_rename(args)
    else:
        parser.print_help()
        return 0


if __name__ == '__main__':
    sys.exit(main())
