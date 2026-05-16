<template>
  <div>
    <StatCards :cards="cards" />

    <el-card shadow="never">
      <template #header>
        <div class="header">
          <span>步骤进度</span>
          <span v-if="r.audio_lang" class="muted">音轨语言：{{ langDisplay(r.audio_lang) }}</span>
        </div>
      </template>

      <!-- 任务刚启动还没写入第一个 step → 占位 -->
      <div v-if="!steps.length" class="placeholder">
        <el-icon class="is-loading"><Loading /></el-icon>
        <span>任务正在准备启动，第一个步骤即将开始...</span>
      </div>

      <div v-else class="step-list">
        <div
          v-for="(s, idx) in steps"
          :key="s.task_id || idx"
          class="step-row"
          :class="{
            'is-completed': s.status === 'completed',
            'is-failed': s.status === 'failed',
            'is-running': s.status === 'running',
            'is-pending': s.status === 'pending',
          }"
        >
          <div class="step-line">
            <div class="step-icon">
              <el-icon v-if="s.status === 'completed'"><Check /></el-icon>
              <el-icon v-else-if="s.status === 'failed'"><Close /></el-icon>
              <el-icon v-else-if="s.status === 'running'" class="is-loading"><Loading /></el-icon>
              <span v-else>{{ idx + 1 }}</span>
            </div>
            <div class="step-body">
              <div class="step-title">
                <strong>{{ s.label }}</strong>
                <el-tag size="small" :type="statusTagType(s.status)">{{ statusLabel(s.status) }}</el-tag>
                <span class="step-duration muted">{{ formatStepDuration(s) }}</span>
              </div>
              <div v-if="s.message" class="step-message muted">{{ s.message }}</div>
              <div v-if="s.summary && Object.keys(s.summary).length" class="step-summary">
                <span v-for="(v, k) in s.summary" :key="k" class="summary-chip">
                  <span class="chip-label">{{ summaryFieldLabel(k) }}</span>
                  <span class="chip-value">{{ v ?? '—' }}</span>
                </span>
              </div>
            </div>
            <div class="step-action">
              <el-button
                v-if="s.task_id"
                text
                type="primary"
                size="small"
                @click="goToChild(s.task_id)"
              >
                查看子任务 #{{ s.task_id }}
                <el-icon><Right /></el-icon>
              </el-button>
            </div>
          </div>
        </div>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import dayjs from 'dayjs'
import { Check, Close, Loading, Right } from '@element-plus/icons-vue'
// Loading 图标已在 import 列表里
import StatCards from './StatCards.vue'

const props = defineProps({ task: { type: Object, required: true } })
const router = useRouter()

const r = computed(() => props.task.result || {})
const steps = computed(() => r.value.steps || [])

const cards = computed(() => [
  { label: '总步骤数', value: r.value.total_steps   ?? steps.value.length, color: '#6366f1' },
  { label: '成功',    value: r.value.success_steps ?? 0, color: '#10b981' },
  { label: '失败',    value: r.value.failed_steps  ?? 0, color: '#ef4444' },
  { label: '作用范围', value: r.value.scope_label   ?? '—', color: '#94a3b8' },
])

const STATUS_LABELS = { pending: '等待', running: '运行中', completed: '完成', failed: '失败', cancelled: '已取消' }
const STATUS_TAGS = { pending: 'info', running: 'primary', completed: 'success', failed: 'danger', cancelled: 'info' }
const statusLabel = (s) => STATUS_LABELS[s] || s
const statusTagType = (s) => STATUS_TAGS[s] || 'info'

const langDisplay = (code) => ({
  eng: '英语', chs: '普通话', yue: '粤语', jpn: '日语',
}[code] || code)

const SUMMARY_FIELD_LABELS = {
  total: '总数',
  success: '成功',
  failed: '失败',
  not_found: '未找到',
  sample_count: 'Sample',
  deleted_file_count: '已删文件',
  deleted_dir_count: '已删空目录',
  nested_count: '嵌套主文件',
  moved_count: '已移动',
  unrecognized_count: '未识别',
  fixed_count: '已修复',
  no_match_count: '未匹配',
  total_videos: '视频总数',
  with_subtitles: '有任意字幕',     // 至少有 1 条字幕（不区分语言；可能仍缺所需语言）
  without_subtitles: '完全无字幕',   // 一条字幕都没有（既无外挂也无内嵌）
  without_required: '缺所需语言',    // required_langs 任一缺失（更严格指标）
  download_success: '下载成功',
  download_total: '下载总数',
  rename_success: '改名成功',
  candidates: '需修改',
  apply_success: '已写入',
  apply_failed: '写入失败',
  error: '错误',
}
const summaryFieldLabel = (key) => SUMMARY_FIELD_LABELS[key] || key

const formatStepDuration = (s) => {
  if (!s.started_at || !s.completed_at) return ''
  const ms = dayjs(s.completed_at) - dayjs(s.started_at)
  if (ms < 1000) return '<1s'
  const sec = Math.floor(ms / 1000)
  if (sec < 60) return `${sec}s`
  const m = Math.floor(sec / 60)
  return `${m}m${String(sec % 60).padStart(2, '0')}s`
}

const goToChild = (taskId) => {
  router.push({ path: `/tasks/${taskId}` })
}
</script>

<style lang="scss" scoped>
.header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}
.muted { color: #94a3b8; font-size: 12px; }

.placeholder {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 24px;
  color: #64748b;
  font-size: 13px;
  justify-content: center;
}

.step-list {
  display: flex;
  flex-direction: column;
}

.step-row {
  position: relative;

  &:not(:last-child)::before {
    content: '';
    position: absolute;
    left: 18px;
    top: 36px;
    bottom: -8px;
    width: 2px;
    background: #e2e8f0;
  }

  &.is-completed::before { background: #10b981; }
  &.is-failed::before { background: #ef4444; }
}

.step-line {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  padding: 12px 0;
}

.step-icon {
  width: 36px; height: 36px;
  border-radius: 50%;
  background: #e2e8f0;
  color: #64748b;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 600;
  flex-shrink: 0;
  z-index: 1;
}

.is-completed .step-icon {
  background: #10b981;
  color: #fff;
}
.is-failed .step-icon {
  background: #ef4444;
  color: #fff;
}
.is-running .step-icon {
  background: #409eff;
  color: #fff;
}

.step-body {
  flex: 1;
  min-width: 0;

  .step-title {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 14px;
    margin-bottom: 4px;

    .step-duration {
      font-family: monospace;
    }
  }

  .step-message {
    word-break: break-all;
    margin-bottom: 6px;
  }

  .step-summary {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .summary-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    font-size: 12px;

    .chip-label { color: #64748b; }
    .chip-value { font-family: monospace; color: #1e293b; }
  }
}

.step-action {
  flex-shrink: 0;
}
</style>
