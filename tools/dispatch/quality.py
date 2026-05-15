"""
质量 tier 提取 + Repack/Proper 识别。

用于"目标位置已被其他种子占用"时按质量裁决：
  - extract_tier(name) → 综合 tier int，越大越好
  - is_repack(name)    → 文件名是否含 PROPER/REPACK/RERIP 标记
  - compare(old, new)  → 'new_wins' / 'old_wins' / 'tie'

简单字符串匹配，不解析 mediainfo（那个要拉文件读 header，太重）。
99% 场景文件名/release name 已经含足够标记（1080p / 2160p / WEB-DL / BluRay / Remux）。
"""
from __future__ import annotations

import re

# 分辨率：主权重
_RESOLUTION = [
    (re.compile(r'\b(2160p|4k|uhd)\b', re.I), 40),
    (re.compile(r'\b1080p\b',          re.I), 30),
    (re.compile(r'\b720p\b',           re.I), 20),
    (re.compile(r'\b(480p|sd)\b',      re.I), 10),
]

# 源/格式：次权重（同分辨率内 Remux > BluRay > WEB-DL > WEBRip > HDTV）
_SOURCE = [
    (re.compile(r'\bremux\b',                re.I), 8),
    (re.compile(r'\b(bluray|blu-ray|bdrip)\b', re.I), 6),
    (re.compile(r'\bweb-?dl\b',              re.I), 4),
    (re.compile(r'\bwebrip\b',               re.I), 3),
    (re.compile(r'\bhdtv\b',                 re.I), 2),
    (re.compile(r'\b(dvdrip|dvd)\b',         re.I), 1),
]

# HDR / 色深：小加分
_HDR = [
    (re.compile(r'\bdolby[._\- ]?vision\b|dovi|\bdv\b', re.I), 2),
    (re.compile(r'\bhdr10\+?\b',             re.I), 1),
    (re.compile(r'\bhdr\b',                  re.I), 1),
]

# Repack 标记：替换决策依赖这个识别
_REPACK = re.compile(r'\b(proper|repack|rerip)\b', re.I)


def extract_tier(name: str) -> int:
    """从文件名/release 字符串里抽出综合质量 tier（越大越好）。
    未知 → 0。同 tier 不分胜负（让上层选 'tie' 走人工或保旧）。"""
    if not name:
        return 0
    score = 0
    for pat, w in _RESOLUTION:
        if pat.search(name):
            score += w
            break
    for pat, w in _SOURCE:
        if pat.search(name):
            score += w
            break
    # HDR/DV 分级：DolbyVision 比 HDR 强，最多取一种（避免 HDR10 同时撞 HDR）
    for pat, w in _HDR:
        if pat.search(name):
            score += w
            break
    return score


def is_repack(name: str) -> bool:
    """PROPER / REPACK / RERIP 这类修订版标记。"""
    return bool(name) and bool(_REPACK.search(name))


def compare(old_name: str, new_name: str) -> str:
    """比较两个候选 release 名字。
    返回 'new_wins' / 'old_wins' / 'tie'。
    repack/proper 在质量持平时让新种子胜出（同质量修订版替换旧的）。"""
    old_tier = extract_tier(old_name)
    new_tier = extract_tier(new_name)
    if new_tier > old_tier:
        return 'new_wins'
    if new_tier < old_tier:
        return 'old_wins'
    # tie：看 repack/proper
    if is_repack(new_name) and not is_repack(old_name):
        return 'new_wins'
    if is_repack(old_name) and not is_repack(new_name):
        return 'old_wins'
    return 'tie'
