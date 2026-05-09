/**
 * 任务类型元信息：标签、所属族、配色、图标、列表摘要生成器。
 *
 * 设计原则：所有 task_type 统一在这里登记。
 * 列表页用 typeMeta(t).label/family/color/icon；详情页 dispatch 子组件用 t 直接 import。
 */

import {
  Picture, User, Document, Download, EditPen, Headset,
  Delete, Compass, Aim, Box, DataLine, Search, MagicStick,
} from '@element-plus/icons-vue'

// 族（family）→ 主题色
export const FAMILY_COLORS = {
  metadata: '#3b82f6',   // 蓝：海报、演员
  subtitle: '#10b981',   // 绿：字幕系
  audio:    '#f59e0b',   // 橙：音轨
  maintenance: '#ef4444',// 红：维护类（删除、移动）
  scan:     '#6366f1',   // 紫：纯扫描入库
  combo:    '#9333ea',   // 紫红：组合任务（一键修复）
  other:    '#94a3b8',   // 灰：未分类
}

// 主登记表
const TASK_META = {
  // ── 元数据修复 ──
  poster_fix:       { label: '海报修复', family: 'metadata', icon: Picture },
  poster_fix_single:{ label: '海报修复（单条）', family: 'metadata', icon: Picture },
  poster_scan:      { label: '海报扫描', family: 'scan', icon: Search },
  actor_fix:        { label: '演员图片修复', family: 'metadata', icon: User },
  actor_fix_single: { label: '演员修复（单条）', family: 'metadata', icon: User },
  actor_scan:       { label: '演员扫描', family: 'scan', icon: Search },
  actor_delete_cjk: { label: '清理 CJK 译名演员', family: 'maintenance', icon: Delete },

  // ── 字幕 ──
  subtitle_scan:    { label: '字幕扫描', family: 'subtitle', icon: Document },
  subtitle_auto_fix:{ label: '自动下载字幕', family: 'subtitle', icon: Download },
  subtitle_rename:  { label: '字幕文件名对齐', family: 'subtitle', icon: EditPen },
  subtitle_download:{ label: '字幕下载（按报告）', family: 'subtitle', icon: Download },

  // ── 音轨 ──
  audio_default_track: { label: '默认音轨', family: 'audio', icon: Headset },

  // ── 维护类 ──
  cleanup_samples:  { label: '清理 Sample', family: 'maintenance', icon: Delete },
  normalize_paths:  { label: '规范路径', family: 'maintenance', icon: Compass },
  auto_identify:    { label: '修复识别错误', family: 'maintenance', icon: Aim },

  // ── 组合 ──
  run_all:          { label: '一键修复', family: 'combo', icon: MagicStick },

  // ── 番号库 ──
  adult_scan:       { label: '番号库扫描', family: 'scan', icon: Search },
  adult_scrape:     { label: '番号刮削（单条）', family: 'metadata', icon: MagicStick },
  adult_scrape_batch: { label: '番号批量刮削', family: 'metadata', icon: MagicStick },

  // ── 其它 ──
  media_scan:       { label: '媒体扫描', family: 'scan', icon: DataLine },
}

const FALLBACK_META = { label: '未知任务', family: 'other', icon: Box }

/**
 * 获取任务的元信息（label / family / color / icon）。
 */
export function typeMeta(taskType) {
  const m = TASK_META[taskType] || { ...FALLBACK_META, label: taskType || '未知任务' }
  return { ...m, color: FAMILY_COLORS[m.family] || FAMILY_COLORS.other }
}

/**
 * 列表筛选下拉用的分组结构。
 */
export const TYPE_FILTER_GROUPS = [
  {
    label: '元数据',
    options: [
      { value: 'poster_fix', label: '海报修复' },
      { value: 'poster_scan', label: '海报扫描' },
      { value: 'actor_fix', label: '演员图片修复' },
      { value: 'actor_scan', label: '演员扫描' },
      { value: 'actor_delete_cjk', label: '清理 CJK 译名演员' },
    ],
  },
  {
    label: '字幕',
    options: [
      { value: 'subtitle_scan', label: '字幕扫描' },
      { value: 'subtitle_auto_fix', label: '自动下载字幕' },
      { value: 'subtitle_rename', label: '字幕文件名对齐' },
      { value: 'subtitle_download', label: '字幕下载（按报告）' },
    ],
  },
  {
    label: '音轨',
    options: [
      { value: 'audio_default_track', label: '默认音轨' },
    ],
  },
  {
    label: '维护',
    options: [
      { value: 'cleanup_samples', label: '清理 Sample' },
      { value: 'normalize_paths', label: '规范路径' },
      { value: 'auto_identify', label: '修复识别错误' },
    ],
  },
  {
    label: '组合',
    options: [
      { value: 'run_all', label: '一键修复' },
    ],
  },
]

/**
 * 状态 → 中文 / 颜色
 */
export const STATUS_META = {
  pending:   { label: '等待中', color: '#909399', tagType: 'info' },
  running:   { label: '运行中', color: '#409eff', tagType: 'primary' },
  completed: { label: '已完成', color: '#67c23a', tagType: 'success' },
  failed:    { label: '失败',   color: '#f56c6c', tagType: 'danger' },
  cancelled: { label: '已取消', color: '#909399', tagType: 'info' },
}

export function statusMeta(s) {
  return STATUS_META[s] || { label: s || '未知', color: '#909399', tagType: 'info' }
}

/**
 * 列表页摘要生成器（A 风格：图标式紧凑 chip 列表）。
 * 返回 [{ icon, value, color, tooltip }] 数组，列表里渲染。
 *
 * 设计：每族任务有自己的关键指标。运行中或没结果时返回空数组。
 */
export function summaryChips(task) {
  if (!task || !task.result || task.status === 'failed') return []
  const r = task.result
  const t = task.task_type
  const out = []

  const push = (icon, value, color, tooltip) => {
    if (value === null || value === undefined) return
    out.push({ icon, value, color, tooltip })
  }

  switch (t) {
    case 'poster_fix':
      push('✓', r.success, '#67c23a', `成功 ${r.success}`)
      push('✗', r.failed, '#f56c6c', `失败 ${r.failed}`)
      push('◌', r.not_found, '#e6a23c', `未找到 ${r.not_found}`)
      break
    case 'actor_fix':
      push('✓', r.success, '#67c23a', `成功 ${r.success}`)
      push('✗', r.failed, '#f56c6c', `失败 ${r.failed}`)
      break
    case 'actor_scan':
      push('+', r.new, '#409eff', `新增 ${r.new}`)
      push('↻', r.updated, '#67c23a', `更新 ${r.updated}`)
      push('◌', r.without_image, '#e6a23c', `缺图 ${r.without_image}`)
      break
    case 'poster_scan':
      push('Σ', r.total, '#409eff', `总数 ${r.total}`)
      push('+', r.new, '#67c23a', `新增 ${r.new}`)
      push('◌', r.without_poster, '#e6a23c', `缺海报 ${r.without_poster}`)
      break
    case 'actor_delete_cjk':
      push('Σ', r.total, '#409eff', `待删 ${r.total}`)
      push('✓', r.deleted, '#f56c6c', `已删 ${r.deleted}`)
      break

    case 'subtitle_scan':
      push('Σ', r.total_videos, '#409eff', `视频 ${r.total_videos}`)
      push('✓', r.with_subtitles, '#67c23a', `有字幕 ${r.with_subtitles}`)
      push('◌', r.without_subtitles, '#e6a23c', `缺字幕 ${r.without_subtitles}`)
      break
    case 'subtitle_auto_fix': {
      const dl = r.download || {}
      const rn = r.rename || {}
      push('Σ', r.scan?.total_videos, '#409eff', `视频 ${r.scan?.total_videos}`)
      push('↓', `${dl.success || 0}/${dl.total || 0}`, '#67c23a',
        `下载成功 ${dl.success || 0} / 共 ${dl.total || 0}`)
      push('✎', rn.success, '#9333ea', `重命名 ${rn.success || 0}`)
      break
    }
    case 'subtitle_rename':
      push('✓', r.success, '#67c23a', `成功 ${r.success}`)
      push('✗', r.failed, '#f56c6c', `失败 ${r.failed}`)
      break
    case 'subtitle_download':
      push('✓', r.success, '#67c23a', `成功 ${r.success}`)
      push('✗', r.failed, '#f56c6c', `失败 ${r.failed}`)
      push('⊘', r.skipped, '#909399', `跳过 ${r.skipped}`)
      break

    case 'audio_default_track': {
      const sc = r.scan || {}
      const ap = r.apply || {}
      push('Σ', sc.total, '#409eff', `总数 ${sc.total}`)
      push('Δ', sc.candidates, '#e6a23c', `需修改 ${sc.candidates}`)
      if (ap.executed) {
        push('✓', ap.success, '#67c23a', `已写入 ${ap.success}`)
        if (ap.failed) push('✗', ap.failed, '#f56c6c', `失败 ${ap.failed}`)
      }
      break
    }

    case 'cleanup_samples':
      push('Σ', r.scanned, '#409eff', `扫描 ${r.scanned}`)
      push('🗑', r.deleted_file_count, '#f56c6c', `删文件 ${r.deleted_file_count}`)
      push('▢', r.deleted_dir_count, '#e6a23c', `删空目录 ${r.deleted_dir_count}`)
      break
    case 'normalize_paths':
      push('Σ', r.scanned, '#409eff', `扫描 ${r.scanned}`)
      push('↪', r.moved_count, '#67c23a', `移动 ${r.moved_count}`)
      break
    case 'auto_identify':
      push('Σ', r.unrecognized_count, '#409eff', `未识别 ${r.unrecognized_count}`)
      push('✓', r.fixed_count, '#67c23a', `已修复 ${r.fixed_count}`)
      push('◌', r.no_match_count, '#e6a23c', `未匹配 ${r.no_match_count}`)
      break

    case 'run_all':
      push('Σ', r.total_steps, '#9333ea', `共 ${r.total_steps} 步`)
      push('✓', r.success_steps, '#67c23a', `成功 ${r.success_steps}`)
      if (r.failed_steps) push('✗', r.failed_steps, '#f56c6c', `失败 ${r.failed_steps}`)
      break

    case 'adult_scan':
      push('Σ', r.scanned, '#409eff', `扫到 ${r.scanned}`)
      push('+', r.new, '#67c23a', `新增 ${r.new}`)
      push('?', r.unrecognized, '#e6a23c', `未识别 ${r.unrecognized}`)
      if (r.scraped) push('✎', r.scraped, '#9333ea', `刮削 ${r.scraped}`)
      break

    case 'adult_scrape':
    case 'adult_scrape_batch':
      push('Σ', r.total, '#409eff', `共 ${r.total}`)
      push('✓', r.success, '#67c23a', `成功 ${r.success}`)
      push('◌', r.not_found, '#e6a23c', `未找到 ${r.not_found}`)
      if (r.failed) push('✗', r.failed, '#f56c6c', `失败 ${r.failed}`)
      break

    default:
      // 未识别的任务类型：不输出 chip
      break
  }
  return out
}

/**
 * 计算任务耗时（毫秒）；尚未完成 → null。
 */
export function taskDuration(task) {
  if (!task) return null
  const start = task.started_at || task.created_at
  const end = task.completed_at
  if (!start || !end) return null
  return new Date(end) - new Date(start)
}

/**
 * 把毫秒格式化为 1m32s / 1h05m / 8s 等紧凑形式。
 */
export function formatDuration(ms) {
  if (ms === null || ms === undefined || isNaN(ms)) return '—'
  if (ms < 1000) return '<1s'
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const rs = s % 60
  if (m < 60) return `${m}m${String(rs).padStart(2, '0')}s`
  const h = Math.floor(m / 60)
  const rm = m % 60
  return `${h}h${String(rm).padStart(2, '0')}m`
}
