<template>
  <div class="page-container">
    <!-- 返回 + 标题 -->
    <div class="page-header">
      <div class="header-left">
        <el-button text @click="goBack">
          <el-icon><ArrowLeft /></el-icon>
          返回任务列表
        </el-button>
        <!-- 搜索其他任务并跳转：输入即查，命中后点选直接跳 -->
        <el-autocomplete
          v-model="searchQuery"
          :fetch-suggestions="fetchTaskSuggestions"
          :debounce="300"
          placeholder="搜索其他任务（描述/路径）..."
          clearable
          size="default"
          style="width: 320px; margin-left: 12px"
          popper-class="task-search-popper"
          @select="onTaskSelected"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
          <template #default="{ item }">
            <div class="task-suggest-row">
              <span class="suggest-id">#{{ item.id }}</span>
              <el-tag :type="statusMeta(item.status).tagType" size="small" effect="light">
                {{ statusMeta(item.status).label }}
              </el-tag>
              <span class="suggest-type">{{ typeMeta(item.task_type).label }}</span>
              <span class="suggest-desc" :title="item.value">{{ item.value }}</span>
            </div>
          </template>
        </el-autocomplete>
      </div>
      <div class="header-right">
        <el-button @click="loadTask" :loading="loading">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
        <el-button
          v-if="task && (task.status === 'running' || task.status === 'pending')"
          type="warning"
          @click="cancelTask"
        >
          取消任务
        </el-button>
        <el-button
          v-if="task && task.status !== 'running'"
          type="danger"
          plain
          @click="deleteTask"
        >
          删除任务
        </el-button>
      </div>
    </div>

    <div v-if="loading && !task" class="loading-placeholder" v-loading="true"></div>
    <el-empty v-else-if="!task" description="任务不存在" />
    <template v-else>
      <!-- 任务头信息卡 -->
      <el-card shadow="never" class="task-header-card">
        <div class="task-title-row">
          <div class="task-icon-wrap" :style="{ background: meta.color }">
            <el-icon :size="22" color="#fff"><component :is="meta.icon" /></el-icon>
          </div>
          <div class="task-title-text">
            <div class="task-id">任务 #{{ task.id }}</div>
            <div class="task-name">{{ meta.label }}</div>
          </div>
          <div class="task-status">
            <el-tag :type="statusInfo.tagType" size="large">{{ statusInfo.label }}</el-tag>
          </div>
        </div>

        <!-- 进度 -->
        <el-progress
          v-if="task.status === 'running' || task.status === 'pending'"
          :percentage="Math.floor(task.progress)"
          :stroke-width="8"
          style="margin-top: 14px"
        />

        <!-- 元数据 -->
        <div class="task-meta-grid">
          <div class="meta-item">
            <div class="meta-label">作用范围</div>
            <div class="meta-value">{{ scopeLabel || '—' }}</div>
          </div>
          <div class="meta-item">
            <div class="meta-label">创建时间</div>
            <div class="meta-value">{{ formatTime(task.created_at) }}</div>
          </div>
          <div class="meta-item">
            <div class="meta-label">开始时间</div>
            <div class="meta-value">{{ task.started_at ? formatTime(task.started_at) : '—' }}</div>
          </div>
          <div class="meta-item">
            <div class="meta-label">完成时间</div>
            <div class="meta-value">{{ task.completed_at ? formatTime(task.completed_at) : '—' }}</div>
          </div>
          <div class="meta-item">
            <div class="meta-label">耗时</div>
            <div class="meta-value">{{ formatDuration(taskDuration(task)) }}</div>
          </div>
          <div v-if="autoPolling" class="meta-item">
            <div class="meta-label">实时状态</div>
            <div class="meta-value polling">
              <el-icon class="is-loading"><Loading /></el-icon>
              自动刷新中
            </div>
          </div>
        </div>

        <!-- 当前进度文本（仅运行中显示；完成态由状态 tag + 统计卡片表达结果）-->
        <div v-if="currentProgressMsg && autoPolling" class="current-progress is-running">
          <el-icon class="is-loading"><Loading /></el-icon>
          <span class="progress-label">当前进度</span>
          <span class="progress-text">{{ currentProgressMsg }}</span>
        </div>
      </el-card>

      <!-- task 整体失败时单独 alert 提示（不再用通用 failure-card 收集子项警告，
           子项失败/未找到信息全部在下方 task-type 专用 view 里展示）-->
      <el-alert
        v-if="task.status === 'failed' && task.result?.error"
        type="error"
        :closable="false"
        show-icon
        :title="task.result.error"
        style="margin-bottom: 16px"
      />

      <!-- 详情区 -->
      <el-card shadow="never">
        <template #header>
          <div class="content-header">
            <span>详细数据</span>
            <span v-if="task.status === 'running'" class="muted">任务运行中，数据将随进度更新</span>
          </div>
        </template>

        <component :is="contentComponent" :task="task" :key="task.id" />
      </el-card>
    </template>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Refresh, Loading, Search } from '@element-plus/icons-vue'
import { formatLocalTime } from '@/utils/time'
import { ElMessage, ElMessageBox } from 'element-plus'
import { taskApi } from '@/api'
import { typeMeta, statusMeta, taskDuration, formatDuration } from '@/utils/taskMeta'
import { useTaskStream } from '@/composables/useTaskStream'

// 各 task_type 对应的详情子组件（懒加载）
import GenericJsonView      from '@/components/task-detail/GenericJsonView.vue'
import PosterFixView        from '@/components/task-detail/PosterFixView.vue'
import ActorFixView         from '@/components/task-detail/ActorFixView.vue'
import SubtitleScanView     from '@/components/task-detail/SubtitleScanView.vue'
import SubtitleAutoFixView  from '@/components/task-detail/SubtitleAutoFixView.vue'
import SubtitleRenameView   from '@/components/task-detail/SubtitleRenameView.vue'
import AudioDefaultTrackView from '@/components/task-detail/AudioDefaultTrackView.vue'
import CleanupSamplesView   from '@/components/task-detail/CleanupSamplesView.vue'
import NormalizePathsView   from '@/components/task-detail/NormalizePathsView.vue'
import AutoIdentifyView     from '@/components/task-detail/AutoIdentifyView.vue'
import RunAllView           from '@/components/task-detail/RunAllView.vue'
import AdultScanView          from '@/components/task-detail/AdultScanView.vue'
import AdultScrapeBatchView   from '@/components/task-detail/AdultScrapeBatchView.vue'
import AdultRepairCoversView  from '@/components/task-detail/AdultRepairCoversView.vue'

const route = useRoute()
const router = useRouter()

// SSE 替代轮询：useTaskStream 内部用 EventSource 订阅 /api/tasks/{id}/stream，
// 后端每次 update_task_progress / complete_task 都会推一份 snapshot 过来。
// route.params.id 变化时 composable 自动重连。
const { task, connected: streamConnected } = useTaskStream(() => Number(route.params.id))

// loading：仅在首次连上、还没收到任何 snapshot 之前为 true（用于初始骨架屏）
const loading = computed(() => task.value === null)

const meta = computed(() => task.value ? typeMeta(task.value.task_type) : { label: '', color: '#94a3b8', icon: 'Box' })
const statusInfo = computed(() => task.value ? statusMeta(task.value.status) : { label: '', tagType: 'info' })

// "实时连接"指示：任务还在 running/pending 且 SSE 连接打开
const autoPolling = computed(() =>
  task.value && streamConnected.value && (task.value.status === 'running' || task.value.status === 'pending')
)

/**
 * 作用范围文案（不会被进度更新覆盖）。优先级：
 *   result.initial_message  - create_task 写入，最稳定
 *   result.scope_label      - 部分任务（如 run_all）显式提供的 label
 *   task.message            - 兜底（运行中可能是进度文本，但任务刚创建时还是初始 message）
 */
const scopeLabel = computed(() => {
  if (!task.value) return ''
  const r = task.value.result
  return (r?.initial_message) || (r?.scope_label) || task.value.message || ''
})

/**
 * 当前进度消息（运行中实时变化；与作用范围分开展示）。
 * task.message 在 update_task_progress 里被反复改写，正是用户期望看到的实时进度。
 */
const currentProgressMsg = computed(() => {
  if (!task.value) return ''
  // 如果 message 与初始 message 相同（任务还没真正开始），不重复展示
  const initial = task.value.result?.initial_message
  if (initial && task.value.message === initial) return ''
  return task.value.message || ''
})

// task_type → 子组件映射；未注册的回退到 GenericJsonView
const CONTENT_MAP = {
  poster_fix:           PosterFixView,
  actor_fix:            ActorFixView,
  subtitle_scan:        SubtitleScanView,
  subtitle_auto_fix:    SubtitleAutoFixView,
  subtitle_rename:      SubtitleRenameView,
  audio_default_track:  AudioDefaultTrackView,
  cleanup_samples:      CleanupSamplesView,
  normalize_paths:      NormalizePathsView,
  auto_identify:        AutoIdentifyView,
  run_all:              RunAllView,
  adult_scan:           AdultScanView,
  adult_scrape:         AdultScrapeBatchView,
  adult_scrape_batch:   AdultScrapeBatchView,
  adult_repair_covers:  AdultRepairCoversView,
}

/**
 * 始终走专用 view（如果该 task_type 注册过）。
 * 运行中后端各 run 函数会通过 update_task_progress(result_patch=...) 增量写入
 * details，专用 view 能优雅渲染 partial 数据（用户能看到已完成的子项）。
 *
 * 没专用 view 的类型 → 走 GenericJsonView 兜底。
 */
const contentComponent = computed(() => {
  if (!task.value) return GenericJsonView
  const t = task.value.task_type
  return CONTENT_MAP[t] || GenericJsonView
})

const goBack = () => router.push({ path: '/tasks' })

// ====== 头部搜索：输入即查 → 点选跳详情 ======
const searchQuery = ref('')

/**
 * el-autocomplete 的 fetch-suggestions 回调。
 * 每次输入（debounce 300ms 后）会被调用，需要把结果通过 cb([...]) 返回。
 *
 * 后端 /api/tasks 已支持 search 参数（ILIKE message + result）。
 * 取 limit=10 让下拉简洁；空查询返回空数组。
 */
const fetchTaskSuggestions = async (queryString, cb) => {
  const q = (queryString || '').trim()
  if (!q) { cb([]); return }
  try {
    const res = await taskApi.list({ search: q, limit: 10 })
    const list = (res.data?.tasks || []).map(t => ({
      // el-autocomplete 要求 item.value（默认作为输入框回填值）
      value: t.result?.initial_message || t.message || `任务 #${t.id}`,
      id: t.id,
      task_type: t.task_type,
      status: t.status,
    }))
    cb(list)
  } catch (e) {
    cb([])
  }
}

const onTaskSelected = (item) => {
  if (!item?.id || item.id === task.value?.id) {
    searchQuery.value = ''
    return
  }
  searchQuery.value = ''
  // 路由参数变化时 watch 会触发 loadTask 重新加载（已有逻辑）
  router.push({ path: `/tasks/${item.id}` })
}

const cancelTask = async () => {
  try {
    await taskApi.cancel(task.value.id)
    ElMessage.success('任务已取消')
    // 后端 cancel 后会主动 publish 一份终态 snapshot，SSE 会自动推过来，无需手动 reload
  } catch (e) { /* 拦截器已提示 */ }
}

const deleteTask = async () => {
  try {
    await ElMessageBox.confirm(`确定删除任务 #${task.value.id}？`, '确认', { type: 'warning' })
    await taskApi.delete(task.value.id)
    ElMessage.success('任务已删除')
    router.push({ path: '/tasks' })
  } catch (e) {
    if (e !== 'cancel') console.error('删除失败', e)
  }
}

const formatTime = (t) => formatLocalTime(t)

// 路由 id 切换由 useTaskStream 内部 watch 处理，无需在这里再 reload
// 组件 mount/unmount 由 EventSource 自动管理（composable 在 onUnmounted 里 disconnect）
</script>

<style lang="scss" scoped>
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;

  .header-left {
    display: flex;
    align-items: center;
  }

  .header-right {
    display: flex;
    gap: 8px;
  }
}

.loading-placeholder {
  height: 200px;
}

// el-autocomplete 默认 popper width 跟随输入框；这里避免太窄看不全描述
:deep(.el-autocomplete__popper.task-search-popper) {
  min-width: 480px !important;
}

.task-header-card {
  margin-bottom: 16px;

  .task-title-row {
    display: flex;
    align-items: center;
    gap: 14px;

    .task-icon-wrap {
      width: 48px;
      height: 48px;
      border-radius: 10px;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }
    .task-title-text {
      flex: 1;

      .task-id {
        color: #94a3b8;
        font-family: monospace;
        font-size: 13px;
      }
      .task-name {
        font-size: 18px;
        font-weight: 600;
        color: #1e293b;
        margin-top: 2px;
      }
    }
  }

  .task-meta-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 14px;
    margin-top: 18px;
    padding-top: 16px;
    border-top: 1px solid #e2e8f0;
  }
  .meta-item {
    .meta-label {
      font-size: 12px;
      color: #94a3b8;
      margin-bottom: 4px;
    }
    .meta-value {
      font-size: 13px;
      color: #475569;
      word-break: break-all;
    }
    .meta-value.polling {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      color: #409eff;
    }
  }
}

.content-header {
  display: flex;
  align-items: baseline;
  gap: 10px;

  .muted { color: #94a3b8; font-size: 12px; }
}

// 当前进度（运行中实时变化的 message 文本）
.current-progress {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  padding: 10px 14px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  font-size: 13px;

  &.is-running {
    background: #eff6ff;
    border-color: #bfdbfe;
  }

  .progress-label {
    color: #64748b;
    font-weight: 500;
    flex-shrink: 0;
  }
  .progress-text {
    color: #1e293b;
    word-break: break-all;
  }
}
</style>

<!-- 非 scoped：el-autocomplete 的下拉是 teleport 到 body 的，scoped 选择器到不了 -->
<style lang="scss">
.el-autocomplete__popper.task-search-popper {
  .task-suggest-row {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    line-height: 1.4;

    .suggest-id {
      font-family: monospace;
      color: #94a3b8;
      flex-shrink: 0;
    }
    .suggest-type {
      font-size: 12px;
      color: #64748b;
      flex-shrink: 0;
    }
    .suggest-desc {
      flex: 1;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: #475569;
      font-size: 13px;
    }
  }
}
</style>
