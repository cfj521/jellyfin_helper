"""
HTML 报告生成器
"""
from pathlib import Path
from .scanner import ScanResult


def generate_html_report(result: ScanResult, output_path: Path):
    """生成 HTML 报告"""

    # 统计缺失字幕的视频
    missing_subtitle_videos = []
    for dir_info in result.directories:
        for video in dir_info.videos:
            if not video.subtitles:
                missing_subtitle_videos.append({
                    'dir': dir_info.name,
                    'video': video.name,
                    'path': str(video.path)
                })

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>字幕扫描报告</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: #f5f5f5;
            color: #333;
            line-height: 1.6;
        }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
        h1 {{ color: #2c3e50; margin-bottom: 10px; }}
        .scan-info {{ color: #666; margin-bottom: 20px; }}

        .summary {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 30px;
        }}
        .summary-card {{
            background: white;
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .summary-card h3 {{ font-size: 14px; color: #666; margin-bottom: 5px; }}
        .summary-card .number {{ font-size: 32px; font-weight: bold; }}
        .summary-card.success .number {{ color: #27ae60; }}
        .summary-card.warning .number {{ color: #f39c12; }}
        .summary-card.danger .number {{ color: #e74c3c; }}

        .section {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .section h2 {{ margin-bottom: 15px; color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px; }}

        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #eee; }}
        th {{ background: #f8f9fa; font-weight: 600; }}
        tr:hover {{ background: #f8f9fa; }}

        .status-ok {{ color: #27ae60; }}
        .status-missing {{ color: #e74c3c; }}
        .lang-tag {{
            display: inline-block;
            padding: 2px 8px;
            background: #e8f4fd;
            color: #2980b9;
            border-radius: 4px;
            font-size: 12px;
            margin: 2px;
        }}
        .lang-tag.missing {{
            background: #fdeaea;
            color: #c0392b;
        }}

        .dir-header {{
            background: #f8f9fa;
            padding: 10px 15px;
            margin: 15px 0 10px 0;
            border-left: 4px solid #3498db;
            font-weight: 600;
        }}
        .video-list {{ margin-left: 20px; }}
        .video-item {{
            padding: 8px 0;
            border-bottom: 1px solid #eee;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .video-name {{ flex: 1; }}
        .video-subs {{ text-align: right; }}

        .filter-bar {{
            margin-bottom: 15px;
            display: flex;
            gap: 10px;
        }}
        .filter-bar button {{
            padding: 8px 16px;
            border: 1px solid #ddd;
            background: white;
            border-radius: 4px;
            cursor: pointer;
        }}
        .filter-bar button.active {{
            background: #3498db;
            color: white;
            border-color: #3498db;
        }}
        .hidden {{ display: none !important; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>字幕扫描报告</h1>
        <p class="scan-info">扫描时间: {result.scan_time} | 扫描路径: {result.root_path}</p>

        <div class="summary">
            <div class="summary-card">
                <h3>视频总数</h3>
                <div class="number">{result.total_videos}</div>
            </div>
            <div class="summary-card success">
                <h3>有字幕</h3>
                <div class="number">{result.total_with_sub}</div>
            </div>
            <div class="summary-card danger">
                <h3>缺少字幕</h3>
                <div class="number">{result.total_without_sub}</div>
            </div>
            <div class="summary-card">
                <h3>字幕文件总数</h3>
                <div class="number">{result.total_subtitles}</div>
            </div>
        </div>

        <div class="section">
            <h2>缺少字幕的视频 ({len(missing_subtitle_videos)})</h2>
            <table>
                <thead>
                    <tr>
                        <th>目录</th>
                        <th>视频文件</th>
                    </tr>
                </thead>
                <tbody>
'''

    for item in missing_subtitle_videos:
        html += f'''                    <tr>
                        <td>{item['dir']}</td>
                        <td>{item['video']}</td>
                    </tr>
'''

    html += '''                </tbody>
            </table>
        </div>

        <div class="section">
            <h2>详细列表</h2>
            <div class="filter-bar">
                <button class="active" onclick="filterVideos('all')">全部</button>
                <button onclick="filterVideos('missing')">缺少字幕</button>
                <button onclick="filterVideos('has')">有字幕</button>
            </div>
'''

    for dir_info in result.directories:
        html += f'''
            <div class="dir-header">{dir_info.name} ({dir_info.videos_with_sub}/{dir_info.total_videos} 有字幕)</div>
            <div class="video-list">
'''
        for video in dir_info.videos:
            status_class = 'has-sub' if video.subtitles else 'no-sub'
            status_icon = '✓' if video.subtitles else '✗'
            status_color = 'status-ok' if video.subtitles else 'status-missing'

            subs_html = ''
            for sub in video.subtitles:
                lang = sub['lang'] or '未知'
                subs_html += f'<span class="lang-tag">{lang}</span>'

            for lang in video.missing_langs:
                subs_html += f'<span class="lang-tag missing">缺{lang}</span>'

            html += f'''                <div class="video-item {status_class}">
                    <span class="video-name"><span class="{status_color}">{status_icon}</span> {video.name}</span>
                    <span class="video-subs">{subs_html}</span>
                </div>
'''
        html += '''            </div>
'''

    html += '''
        </div>
    </div>

    <script>
        function filterVideos(type) {
            document.querySelectorAll('.filter-bar button').forEach(b => b.classList.remove('active'));
            event.target.classList.add('active');

            document.querySelectorAll('.video-item').forEach(item => {
                if (type === 'all') {
                    item.classList.remove('hidden');
                } else if (type === 'missing') {
                    item.classList.toggle('hidden', !item.classList.contains('no-sub'));
                } else if (type === 'has') {
                    item.classList.toggle('hidden', !item.classList.contains('has-sub'));
                }
            });
        }
    </script>
</body>
</html>
'''

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
