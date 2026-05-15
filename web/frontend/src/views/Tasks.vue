<template>
  <div class="page-container">
    <div class="page-header">
      <h2>任务管理</h2>
      <div class="header-actions">
        <span v-if="autoPolling" class="polling-tip">
          <el-icon class="is-loading"><Loading /></el-icon>
          运行中任务自动刷新
        </span>
        <el-button @click="loadTasks">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
      </div>
    </div>

    <!-- 快捷筛选 -->
    <el-card shadow="never" class="quick-filters">
      <div class="quick-row">
        <span class="quick-label">快捷：</span>
        <el-radio-group v-model="quickFilter" size="small" @change="onQuickFilterChange">
          <el-radio-button value="">全部</el-radio-button>
          <el-radio-button value="running">运行中</el-radio-button>
          <el-radio-button value="failed">失败</el-radio-button>
          <el-radio-button value="today">今天</el-radio-button>
          <el-radio-button value="week">近 7 天</el-radio-button>
        </el-radio-group>

        <el-divider direction="vertical" />

        <el-select
          v-model="filter.type"
          placeholder="任务类型"
          clearable
          style="width: 220px"
          @change="loadTasks"
        >
          <el-option-group
            v-for="group in TYPE_FILTER_GROUPS"
            :key="group.label"
            :label="group.label"
          >
            <el-option
              v-for="opt in group.options"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-option-group>
        </el-select>

        <el-select
          v-model="filter.status"
          placeholder="状态"
          clearable
          style="width: 140px"
          @change="loadTasks"
        >
          <el-option label="等待中" value="pending" />
          <el-option label="运行中" value="running" />
          <el-option label="已完成" value="completed" />
          <el-option label="失败" value="failed" />
          <el-option label="已取消" value="cancelled" />
        </el-select>

        <el-button v-if="hasActiveFilter" link size="small" @click="clearFilters">
          清除筛选
        </el-button>
      </div>

      <!-- 搜索行：左侧搜索框 + 右侧分页（服务端 ILIKE，跨所有页） -->
      <div class="quick-row search-row">
        <el-input
          v-model="searchInput"
          placeholder="按描述搜索（例：扫描 / 路径 / 库名）"
          clearable
          size="small"
          style="max-width: 380px"
          @keyup.enter="onSearchSubmit"
          @clear="onSearchSubmit"
        >
          <template #prefix>
            <el-icon><Search /></el-icon>
          </template>
        </el-input>
        <el-button size="small" type="primary" @click="onSearchSubmit">搜索</el-button>
        <span v-if="filter.search" class="muted">
          已搜索：<b>"{{ filter.search }}"</b>
        </span>
        <!-- 分页右对齐：margin-left: auto 把它推到行尾 -->
        <el-pagination
          v-model:current-page="pagination.page"
          v-model:page-size="pagination.size"
          :total="pagination.total"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          size="small"
          class="inline-pagination"
          @change="loadTasks"
        />
      </div>
    </el-card>

    <!-- 任务列表 -->
    <el-card shadow="never">
      <el-table
        :data="tasks"
        v-loading="loading"
        row-key="id"
        stripe
        class="clickable-table"
        @row-click="openDetail"
      >
        <el-table-column prop="id" label="ID" width="70">
          <template #default="{ row }">
            <span class="task-id">#{{ row.id }}</span>
          </template>
        </el-table-column>

        <el-table-column label="类型" width="180">
          <template #default="{ row }">
            <div class="type-cell" :style="{ color: typeMeta(row.task_type).color }">
              <el-icon><component :is="typeMeta(row.task_type).icon" /></el-icon>
              <span>{{ typeMeta(row.task_type).label }}</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="描述">
          <template #default="{ row }">
            <span class="message">{{ rowDescription(row) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="180">
          <template #default="{ row }">
            <div class="status-cell">
              <el-tag
                :type="statusMeta(row.status).tagType"
                size="small"
                effect="light"
              >
                {{ statusMeta(row.status).label }}
              </el-tag>
              <el-progress
                v-if="row.status === 'running' || row.status === 'pending'"
                :percentage="Math.floor(row.progress)"
                :stroke-width="4"
                :show-text="false"
                style="flex: 1"
              />
            </div>
          </template>
        </el-table-column>

        <el-table-column label="摘要" width="240">
          <template #default="{ row }">
            <div v-if="summaryChips(row).length" class="summary-chips">
              <el-tooltip
                v-for="(chip, idx) in summaryChips(row)"
                :key="idx"
                :content="chip.tooltip"
                placement="top"
              >
                <span class="chip" :style="{ borderColor: chip.color, color: chip.color }">
                  <span class="chip-icon">{{ chip.icon }}</span>
                  <span class="chip-value">{{ chip.value }}</span>
                </span>
              </el-tooltip>
            </div>
            <span v-else-if="row.status === 'failed'" class="error-summary">
              {{ row.result?.error || '执行失败' }}
            </span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>

        <el-table-column label="耗时" width="80">
          <template #default="{ row }">
            <span class="duration">{{ formatDuration(taskDuration(row)) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="创建时间" width="160">
          <template #default="{ row }">
            <span class="time">{{ formatTime(row.created_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'running' || row.status === 'pending'"
              text
              type="warning"
              @click.stop="cancelTask(row)"
            >
              取消
            </el-button>
            <el-button
              v-if="row.status !== 'running'"
              text
              type="danger"
              @click.stop="deleteTask(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Refresh, Loading, Search } from '@element-plus/icons-vue'
import { taskApi } from '@/api'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  TYPE_FILTER_GROUPS, typeMeta, statusMeta,
  summaryChips, taskDuration, formatDuration,
} from '@/utils/taskMeta'
import { formatLocalTimeShort, localDayjs } from '@/utils/time'
import { useTasksStream } from '@/composables/useTaskStream'
import dayjs from 'dayjs'

const router = useRouter()

const tasks = ref([])
const loading = ref(false)
const filter = ref({ type: '', status: '', search: '' })
const searchInput = ref('')  // 输入框双向绑定，按回车/点搜索时同步到 filter.search
const quickFilter = ref('')
const pagination = ref({ page: 1, size: 20, total: 0 })

const hasActiveFilter = computed(() =>
  filter.value.type || filter.value.status || filter.value.search || quickFilter.value
)

/**
 * 加载任务列表。
 * silent=true（轮询用）：不显示 loading 蒙层；增量合并已有 row（保持对象引用），
 *   避免 el-table 整表重渲染导致用户的点击 / hover 被打断。
 */
const loadTasks = async (silent = false) => {
  if (!silent) loading.value = true
  try {
    const params = {
      task_type: filter.value.type || undefined,
      status: filter.value.status || undefined,
      search: filter.value.search || undefined,
      limit: pagination.value.size,
      offset: (pagination.value.page - 1) * pagination.value.size,
    }
    const res = await taskApi.list(params)
    let list = res.data.tasks || []

    // 客户端日期 / 状态二次筛选（后端只支持 task_type + status）
    if (quickFilter.value === 'today') {
      const today = dayjs().startOf('day')
      list = list.filter(t => localDayjs(t.created_at)?.isAfter(today))
    } else if (quickFilter.value === 'week') {
      const weekAgo = dayjs().subtract(7, 'day').startOf('day')
      list = list.filter(t => localDayjs(t.created_at)?.isAfter(weekAgo))
    }

    if (silent && tasks.value.length) {
      // 增量更新：按 id 复用现有对象，仅 Object.assign 改变化字段。
      // 配合 el-table row-key="id"，已存在的行 DOM 不会被重建。
      const byId = new Map(tasks.value.map(t => [t.id, t]))
      tasks.value = list.map(incoming => {
        const existing = byId.get(incoming.id)
        if (existing) {
          Object.assign(existing, incoming)
          return existing
        }
        return incoming
      })
    } else {
      tasks.value = list
    }
    pagination.value.total = res.data.total
  } catch (e) {
    if (!silent) console.error('加载任务失败', e)
  } finally {
    if (!silent) loading.value = false
  }
}

const onQuickFilterChange = (val) => {
  // 把快捷筛选映射到 filter 字段
  if (val === 'running') {
    filter.value.status = 'running'
  } else if (val === 'failed') {
    filter.value.status = 'failed'
  } else if (val === 'today' || val === 'week') {
    filter.value.status = ''  // 日期筛选客户端做
  } else {
    filter.value.status = ''
  }
  pagination.value.page = 1
  loadTasks()
}

const clearFilters = () => {
  filter.value = { type: '', status: '', search: '' }
  searchInput.value = ''
  quickFilter.value = ''
  pagination.value.page = 1
  loadTasks()
}

const onSearchSubmit = () => {
  filter.value.search = (searchInput.value || '').trim()
  pagination.value.page = 1
  loadTasks()
}

const cancelTask = async (task) => {
  try {
    await taskApi.cancel(task.id)
    ElMessage.success('任务已取消')
    loadTasks()
  } catch (e) { /* 拦截器已提示 */ }
}

const deleteTask = async (task) => {
  try {
    await ElMessageBox.confirm(`确定删除任务 #${task.id}？`, '确认', { type: 'warning' })
    await taskApi.delete(task.id)
    ElMessage.success('任务已删除')
    loadTasks()
  } catch (e) {
    if (e !== 'cancel') console.error('删除失败', e)
  }
}

const openDetail = (task) => {
  router.push({ path: `/tasks/${task.id}` })
}

const formatTime = (time) => formatLocalTimeShort(time)

/**
 * 列表"描述"列：优先用任务初始 message（含作用范围信息，不会被进度文本覆盖）。
 * task.message 在运行中会被反复改写为进度文本，结束时是最后一次的中间态，对列表展示意义不大。
 */
const rowDescription = (row) => {
  return row?.result?.initial_message || row?.message || '—'
}

// SSE 接管"任务变化时刷新列表"。
// 闭包内引用 loadTasks 没问题（闭包延迟解析，setTimeout 触发时 loadTasks 已声明）。
const { connected: streamConnected } = useTasksStream(() => loadTasks(true))

// "实时"badge 显示条件：SSE 连上 + 有运行中/等待中的任务
const autoPolling = computed(() =>
  streamConnected.value &&
  tasks.value.some(t => t.status === 'running' || t.status === 'pending')
)

// onMounted 只负责首次加载基线数据，后续刷新由 SSE 触发
onMounted(() => {
  loadTasks()
})
</script>

<style lang="scss" scoped>
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;

  .header-actions {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .polling-tip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: #409eff;
    font-size: 13px;
  }
}

.quick-filters {
  margin-bottom: 12px;

  :deep(.el-card__body) { padding: 12px 16px; }

  .quick-row {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .search-row {
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px dashed var(--el-border-color-lighter, #ebeef5);

    .inline-pagination {
      margin-left: auto;  // 行内右对齐
      flex-shrink: 0;
    }
  }
  .quick-label {
    font-size: 13px;
    color: #64748b;
  }
}

// 整行可点击：手势 + hover 高亮
.clickable-table :deep(.el-table__row) {
  cursor: pointer;
  transition: background-color 0.15s ease;
}
.clickable-table :deep(.el-table__row:hover > td) {
  background-color: #f0f9ff !important;
}

.task-id { color: #94a3b8; font-family: monospace; }

.type-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 500;
}

.message {
  font-size: 13px;
  color: #475569;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
  word-break: break-all;
}

.status-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}

.summary-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 1px 7px;
  border: 1px solid;
  border-radius: 10px;
  font-size: 12px;
  line-height: 18px;
  background: #fff;

  .chip-icon { font-weight: bold; }
  .chip-value { font-family: monospace; }
}

.error-summary {
  color: #f56c6c;
  font-size: 12px;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
}

.duration { color: #64748b; font-size: 12px; font-family: monospace; }
.time     { color: #64748b; font-size: 12px; }
.muted    { color: #94a3b8; }

</style>
