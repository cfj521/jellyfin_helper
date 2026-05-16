# 下载流水线部分重复处理 — 报告与落地

**最初记录**：2026-05-11
**最近更新**：2026-05-16（C 方案完整实施）
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

## 2. 当前流水线的处理行为（截至 2026-05-16）

### 2.1 完全相同种子（D1）— ✅ 已处理

`download_dispatch_map.torrent_hash` 是主键，重复 add 直接被 DB UNIQUE 约束挡掉。
`adopt.py` 在创建行前会按 `(torrent_hash, qb_added_on)` 判定是否复活已存在记录。

**结论**：D1/D2 设计上稳。

### 2.2 文件级 resume（部分覆盖单文件）— ✅ 已处理

[copier.py](../tools/dispatch/copier.py) 处理目标文件已存在的几种情况：

```python
if dst_size == total:            跳过整个复制            # 完全一致
elif 0 < dst_size < total:
    if mtime_age > 1h:           raise CrossTorrentCollision  # ★ fail-safe 防 append 损坏
    else:                        续传 append
elif dst_size > total / 异常:    on_displace 钩子 → trash    # ★ 不再硬删
```

**当前文件级 resume 同时具备两层保护**：mtime guard（防 D7 把不同源的尾巴 append 到旧文件上）+ on_displace 钩子（替换旧文件先入 trash，永远不 hard delete）。

只看大小、不看 hash 的精确度问题仍在 —— 但实际 release 大小总有 MB 级差异，配合 mtime guard 几乎不可能误判。

### 2.3 跨种子目标路径冲突（D3 / D4 / D7）— ✅ 已处理（main-path + fail-safe 双防线）

`organizer.py:_compose_movie_path` / `_compose_episode_path` 用 `file_template` 渲染目标路径。所有同 TMDB ID 的种子（D3/D4/D7）会渲染出**完全一样的 dst**。

新方案：渲染 dst 后**立即查 `dispatch_map`**（[duplicate.py](../tools/dispatch/duplicate.py)），通过 JSONB `@> [dst]` 反向查找占用方，按 `DispatchRule.duplicate_policy` 决策：

| policy | 行为 |
|---|---|
| `higher_quality_wins`（默认） | [quality.py](../tools/dispatch/quality.py) 比较 release tier；新胜 → 旧入 trash 后覆盖；旧胜 → 跳过；持平 → needs_review |
| `always_skip` | 任何冲突都跳过 |
| `always_replace` | 任何冲突都覆盖（旧入 trash） |
| `needs_review` | 任何冲突都标 `phase=copying/needs_review`，前端弹决策 modal |

**默认值**：movie/tv/anime 用 `higher_quality_wins`；adult 用 `always_skip`（番号同 code 不轻易覆盖）。

### 2.4 季 pack 与单集混合（D5 / D6）— ✅ 同 §2.3 覆盖

剧集场景 `organizer.organize` 在 `videos > 1 且 tv/anime` 时按 SxxExx 拆出每集 dst，每个 dst 单独走 duplicate_resolver。也就是：

- 已有 `S01E01.mkv`（单集种子），新来季 pack 内的 `S01E01.mkv` → 按 policy 决策（默认按质量胜出）
- 季 pack 内 `S01E13.mkv` 在库里没有 → 正常落地
- 单种子整包都被跳过（files_count == 0）→ pipeline_worker 在 `_step_copy` 末尾把 phase 标 `succeeded(skipped)` 且不通知 Jellyfin

### 2.5 数据库层反查

dispatched_files 用 JSONB 存路径数组（[database.py:346](../web/backend/database.py)）。`duplicate._find_existing_owner` 用 `dispatched_files @> [dst]` + `torrent_hash != current` + `phase NOT IN ('cleaned','dismissed')` 反查占用方。代码在 [duplicate.py](../tools/dispatch/duplicate.py)。

---

## 3. 关键改动文件

| 文件 | 角色 |
|---|---|
| [config_models.py](../web/backend/config_models.py) | `DispatchRule.duplicate_policy` 字段 + 各 media_type 默认值 |
| [config.yaml.example](../config.yaml.example) | 4 个 media_type 各自的 duplicate_policy 默认值 |
| [Settings.vue](../web/frontend/src/views/Settings.vue) | UI 暴露策略下拉 |
| [tools/dispatch/quality.py](../tools/dispatch/quality.py) | tier 提取 + Repack/Proper 识别 + compare() |
| [tools/dispatch/duplicate.py](../tools/dispatch/duplicate.py) | resolve() 主入口 + `DuplicateConflictError` + 旧文件入 trash |
| [tools/dispatch/copier.py](../tools/dispatch/copier.py) | `on_displace` 钩子（不再 hard delete）+ 原有 `CrossTorrentCollisionError` |
| [tools/dispatch/organizer.py](../tools/dispatch/organizer.py) | `organize()` 增加 `duplicate_resolver` 参数 + `skipped_files` 返回值 + `_displace_to_trash` 钩子 |
| [tools/dispatch/pipeline_worker.py](../tools/dispatch/pipeline_worker.py) | 注入 resolver；捕 `DuplicateConflictError` 落 needs_review；整包 skipped 时 phase=succeeded |
| [web/backend/api/dispatch.py](../web/backend/api/dispatch.py) | `/copy-conflict/{hash}` GET + `/replace` + `/skip` |
| [DownloadPipeline.vue](../web/frontend/src/views/downloadpipeline/DownloadPipeline.vue) | `openReview` 按 phase 路由；新增 copy-conflict 决策 dialog |
| [tests/test_dispatch_duplicates.py](../tests/test_dispatch_duplicates.py) | quality + duplicate.resolve + copier on_displace 共 19 用例 |

---

## 4. 决策矩阵：用户在前端能做什么

任何 copy-phase 冲突（`phase=copying, phase_status=needs_review`），主表"人工审核"按钮会打开 **CopyConflictReviewDialog**，展示：

```
本种子     : Movie.2024.2160p.UHD.Remux.HDR.mkv
冲突目标   : /library/Movies/Movie (2024)/Movie (2024).mkv
已被       : Movie.2024.1080p.BluRay.x264.mkv
             hash 7f3a... · phase all_jobs_done
原因       : policy=needs_review  /  quality_tie  /  其他
```

两个按钮：

- **覆盖（用新的）** → POST `/copy-conflict/{hash}/replace`
  - 把对家行的 `dispatched_files` 数组里移除这条路径（仅元数据 cleanup，对家本身保留以便 quota 清 NVMe）
  - 旧物理文件移到 `<trash_dir>/_replaced/<YYYYMMDD-HHMMSS>_<old_hash[:8]>/`
  - 本行 phase 重置为 `copying/running`，pipeline_worker 下一轮重跑
- **跳过（保留旧）** → POST `/copy-conflict/{hash}/skip`
  - 本行 `phase_status=skipped, dispatched_files=[]`
  - 不动 qB（用户仍可手动从 qB 移除种子）

**"改名重试"暂未实现**：需要给 dispatch_map 加 `target_path_override` 列才能稳妥支持，留作 P2。当前可让用户改完 file_template 后从"重试"菜单触发。

---

## 5. 测试覆盖（[tests/test_dispatch_duplicates.py](../tests/test_dispatch_duplicates.py)）

| 用例 | 验证 |
|---|---|
| `test_quality_extract_tier_baseline` × 5 | 各分辨率/源组合的 tier 基线 |
| `test_quality_higher_resolution_beats_lower` | 2160p > 1080p Remux |
| `test_quality_repack_detection` | PROPER/REPACK/RERIP 识别 |
| `test_quality_compare_*` × 4 | 4K 升级 / 降级 / repack tie 处理 / 普通 tie |
| `test_resolve_no_conflict_proceeds` | 无占用直接 proceed |
| `test_resolve_higher_quality_wins` | D4 升级：旧入 trash |
| `test_resolve_lower_quality_skipped` | 反向降级：跳过新种子 |
| `test_resolve_always_skip` | adult 默认策略 |
| `test_resolve_always_replace_moves_to_trash` | 强制覆盖也走 trash |
| `test_resolve_needs_review_raises` | needs_review policy 抛 DuplicateConflictError + JSON 上下文 |
| `test_resolve_tie_raises_needs_review` | 质量持平也走人工 |
| `test_copier_on_displace_called_on_oversized_dst` | copier 异常分支调钩子而非 unlink |

测试不依赖 PostgreSQL（用 `MagicMock` 模拟 SQLAlchemy query 链）。

---

## 6. 风险总结（更新表）

| 类型 | 现状 | 风险等级 |
|---|---|---|
| D1/D2（完全相同种子） | ✅ DB UNIQUE + adopt 复活 | 低 |
| 单种子续传 | ✅ size + mtime guard | 低 |
| D3（同片同质量） | ✅ tie → needs_review | 低 |
| **D4（升级质量）** | ✅ 默认自动覆盖 + 旧入 trash | **低** |
| **D7（Repack/Proper）** | ✅ PROPER/REPACK 识别 + tie 时新胜出 | **低** |
| D5（pack vs singles） | ✅ 按集逐个 dst 决策 | 低 |
| D8（不同剪辑版） | 🟠 文件名通常含 "Director.Cut" / "Theatrical" 字样，但 tier 提取不识别 → 大概率 tie → needs_review 兜底 | 中 |

D8 留作潜在改进：在 quality.compare 里加 `cut_marker` 探测，但优先级不高（同片不同剪辑通常用户会主动选择落地哪个版本）。

---

## 7. 后续可能的增强（P2）

1. **改名重试**：dispatch_map 加 `target_path_override` 列，前端冲突 modal 加输入框
2. **冲突列表概览页**：所有 `phase=copying/needs_review` 行在 "待处理" 面板集中展示
3. **质量比较升级**：从 mediainfo 读真实 codec/bitrate（需要先复制小 header 块）
4. **D8 cut_marker**：识别 "Director's Cut" / "Theatrical" / "Extended" 并作为独立维度
