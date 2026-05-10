# 下载流水线部分重复处理 报告

**日期**：2026-05-11
**问题域**：当多个种子在不同程度上"重复"（同一部影片不同版本、季 pack 与单集混搭、补档/Repack）时，流水线的现有行为是否合理。

---

## 1. 重复的几种类型

| 编号 | 场景 | 例子 | 频率 |
|---|---|---|---|
| D1 | **完全相同种子**（同 hash 重加） | 用户清了 qB 又重新添加同一种子 | 偶尔 |
| D2 | **同 hash 但 qb_added_on 变化** | 删除重加 | 偶尔 |
| D3 | **不同 hash, 相同 TMDB ID, 相同质量** | 不同站点发布的同片同源 | 较常 |
| D4 | **不同 hash, 相同 TMDB ID, 不同质量** | 1080p 已下载，又下了 4K Remux | **常见** |
| D5 | **同剧集，单集 vs 季 pack** | 已有 S01E01-12 单集，又来了 S01 完整季 | **常见** |
| D6 | **同剧集，部分集重叠** | 已有 S01E01-08，又来了 S01E05-12 | 偶尔 |
| D7 | **Repack / Proper / RERIP** | 同片同质量的修订版 | 较常 |
| D8 | **同片不同剪辑** | Theatrical Cut vs Director's Cut | 偶尔 |

---

## 2. 当前流水线的处理行为

### 2.1 完全相同种子（D1）— ✅ 已处理

`download_dispatch_map.torrent_hash` 是主键，重复 add 直接被 DB UNIQUE 约束挡掉。
`adopt.py` 在创建行前会按 `(torrent_hash, qb_added_on)` 判定是否复活已存在记录。

**结论**：D1/D2 设计上稳。

### 2.2 文件级 resume（部分覆盖单文件）— ✅ 已处理

`tools/dispatch/copier.py:46-70` 处理目标文件已存在的三种情况：

```python
if dst_size == total: 跳过整个复制    # 完全一致
elif 0 < dst_size < total: 续传       # 部分写入，append
else: 删除重写                        # 异常（更大 / mtime 异常）
```

这是**文件级**的重复保护，对"同一个种子之前复制到一半被中断"是必要的。但它**只看大小**，不看内容 hash —— 如果两个不同 release 的同名文件碰巧大小一致，会被误判为已完成而跳过。**这种情况在实际项目里极少**（不同 release 大小总有几 MB 差异），暂可接受。

### 2.3 跨种子目标路径冲突（D3 / D4 / D7）— 🔴 **未处理**

`organizer.py:_compose_movie_path` 用 `file_template` 渲染目标路径：

```
movie:  '{title} ({year})'           → /library/Movie ({year}).mkv
tv:     '({series_name})S{season:02d}E{episode:02d}'
adult:  '{code}({title})'
```

所有同 TMDB ID 的种子（D3/D4/D7）会渲染出**完全一样的 dst 路径**。然后进 `copy_file_with_progress`：

- 如果新种子的视频文件**比原来大**（D4 的 4K Remux > 1080p）→ 走 "异常分支" → **删除原文件再写新的**
- 如果新种子的视频文件**比原来小**（D7 的 Proper 比原 Release 小一点）→ 走 "续传分支" → **目标文件被破坏**（在原文件尾部 append 新种子的剩余内容）
- 如果大小完全一致（罕见）→ 跳过

**这是一个严重的隐患**：D7 Proper/Repack 场景下目标文件可能被损坏。D4 升级质量时虽然能成功覆盖，但没有保留旧文件备份的机会。

### 2.4 季 pack 与单集混合（D5 / D6）— 🟠 部分未处理

剧集场景 `organizer.py:165-186` 用 `_extract_episode(name)` 从文件名提 SxxExx，渲染到 `(<series>)S01E01.mkv` 这样的目标。

当已有 `S01E01.mkv`（来自前一个单集种子），新种子是季 pack 时：
- 季 pack 内的 `S01E01.mkv` 会**覆盖**已有文件（按 D3/D4 同样的"异常分支"逻辑）
- 如果两个文件元数据不同（不同 release group），实际视频内容被替换

更糟的是：**两个种子都在做种**。覆盖了之后，原种子仍在 `dispatch_map` 表里 phase=all_jobs_done，但它的 `dispatched_files[i]` 指向的内容已经不是它的源文件了。配额清理时如果以这个文件做归属判断，会出乱。

### 2.5 重复目标在数据库层无校验

进入 `copying` phase 之前没有任何"check 这个 target_path 已经被另一行 dispatched_files 占用过没有"。`dispatched_files` 是 JSONB 数组，技术上可以反查（PG 支持 `dispatched_files @> '["/path/to/file.mkv"]'`），但代码里**没有这步预检**。

---

## 3. 推荐的修复方向

### 3.1 P0：copier 增加"目标已被其他种子占用"探测

最小改动：进 `copy_file_with_progress` 之前在 `organizer.organize` 里查 dispatch_map：

```python
# 在 _compose_movie_path / _compose_episode_path 渲染出 dst 后
existing = db.query(DownloadDispatchMap).filter(
    DownloadDispatchMap.dispatched_files.contains([str(dst)]),
    DownloadDispatchMap.torrent_hash != current_hash,
    DownloadDispatchMap.phase != 'cleaned',
).first()

if existing:
    # 决策点：用户偏好是覆盖、跳过、还是改名共存？
    return _handle_duplicate(existing, current_hash, dst)
```

### 3.2 P0：暴露"重复处理策略"为 DispatchRule 配置项

在 `DispatchRule` 增加字段（每 media_type 各自配置）：

```yaml
duplicate_policy:
  movie:
    same_or_lower_quality: skip       # 已有同等或更高质量 → 跳过新种子
    higher_quality:        replace    # 新种子质量更高 → 替换（备份旧文件到 trash）
    repack_proper:         replace    # 文件名含 PROPER/REPACK → 替换
  tv:
    season_pack_vs_singles: prefer_singles   # 已有单集 → 跳过 pack
    singles_vs_season_pack: replace_with_pack # 已有 pack → 用 pack 替换（罕见）
  adult: replace  # 番号去重，质量优先
```

UI 在 dispatch 规则编辑窗口暴露三个简单选项：
- **永远跳过新的**（保守）
- **质量更高的胜出**（默认推荐）
- **总是替换**（粗暴）

### 3.3 P1：质量比较函数

要做"质量更高胜出"，需要从文件名/路径提取分辨率和编码档位。已有现成思路：

```python
QUALITY_TIERS = {
    '2160p': 4, '4K': 4, 'UHD': 4,
    '1080p': 3, 'FHD': 3,
    '720p': 2, 'HD': 2,
    '480p': 1, 'SD': 1,
}
def extract_quality(filename: str) -> int:
    s = filename.upper()
    for tag, tier in QUALITY_TIERS.items():
        if tag.upper() in s:
            return tier
    return 0  # 未知
```

更精细需要看 codec/bitrate（Remux > BluRay > WEB-DL > HDTV），但起步够用。

### 3.4 P1：被替换文件转 trash 而不是 unlink

`copier._handle_duplicate` 当决定 replace 时，原文件先 move 到 `trash_dir`（已有这个机制，复用），保留 N 天后再清。**永远不要 hard delete，给用户留反悔机会**。

### 3.5 P2：UI 提示 dispatch 阶段冲突

目前 organizer 阶段失败只在 `phase_status=failed` + `error_log`。
建议加 `phase_status=needs_review` + 弹出"质量比较 + 用户选择"的交互（前端流水线页加一栏冲突列表）。

---

## 4. 短期 vs 长期

### 短期（1-2 天）

最少要做两件事，避免现有 D7 数据损坏隐患：

1. **`copier.py` 续传逻辑加严**：`0 < dst_size < total` 分支前，加 mtime 检查。如果 dst.mtime 比当前 src.mtime 早**且**两者文件名/路径完全一致来自不同 torrent_hash，**视为冲突，refuse 而不是 append**。这至少让 D7 不再损坏文件。

2. **UI 文档说明**：到-do.txt / README 里写一句"目前流水线对同名不同源种子的去重不完善，建议同 movie 不重复添加"。

### 长期（1-2 周）

实现 §3.1-3.5 的完整方案。

---

## 5. 测试用例

应该补充 `tests/test_dispatch_*.py`：

| 用例 | 场景 | 期望 |
|---|---|---|
| test_duplicate_same_size_skipped | 相同大小目标存在 → 跳过 | 不重复传输 |
| test_duplicate_higher_quality_replaces | 1080p 已存在，4K 来 → trash 旧 + 写新 | 旧文件在 trash，新文件落地 |
| test_duplicate_lower_quality_skipped | 4K 已存在，1080p 来 | 跳过新种子，dispatch_map 标 skipped |
| test_repack_replaces | 同质量 PROPER → 替换 + trash | 旧→trash |
| test_season_pack_skipped_when_singles_exist | S01E01-12 单集已在，pack 来 | pack 跳过 |
| test_partial_resume_unaffected | 单种子续传场景仍工作 | 续传成功 |

---

## 6. 总结

| 类型 | 现状 | 风险等级 |
|---|---|---|
| D1/D2（完全相同种子） | ✅ 完善 | 低 |
| 单种子续传 | ✅ 完善 | 低 |
| D3（同片同质量） | 🟠 静默覆盖，无审计 | 中 |
| **D4（升级质量）** | 🟠 自动覆盖，无备份 | **中** |
| **D7（Repack/Proper）** | 🔴 **可能损坏文件** | **高** |
| D5（pack vs singles） | 🟠 静默覆盖 | 中 |
| D8（不同剪辑版） | ❌ 无支持，二者必有一损 | 中 |

**首要修复目标**：D7 文件损坏隐患（短期 mtime 检查），D4 备份缺失（长期 quality_policy）。
