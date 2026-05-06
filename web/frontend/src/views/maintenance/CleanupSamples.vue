<template>
  <div class="page-container">
    <div class="page-header">
      <h2>清理 Sample 内容</h2>
      <div class="hint-tag">
        扫描作用范围内所有视频文件，识别 sample 后只删除文件（不删目录），完成后触发 Jellyfin 重扫
      </div>
    </div>

    <el-alert
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 12px"
    >
      <strong>这是不可撤销的删除操作</strong>。建议先用预览模式跑一遍看清楚要删什么再执行。
      Sample 通常嵌在 release 子目录里，会被 Jellyfin 当作 Folder/Movie 入库 ——
      删除后 Jellyfin 会重扫并消除这些垃圾条目。
    </el-alert>

    <el-alert
      v-if="scopeLabel"
      :title="`作用范围：${scopeLabel}`"
      type="info"
      :closable="false"
      show-icon
      style="margin-bottom: 12px"
    />

    <el-card shadow="never" class="form-card">
      <el-form :model="form" label-width="120px">
        <el-form-item label="模式">
          <el-radio-group v-model="form.dry_run">
            <el-radio :label="true">预览（仅扫描，不删除）</el-radio>
            <el-radio :label="false">执行（实际删除文件）</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item>
          <el-button
            type="primary"
            :loading="starting"
            @click="start"
          >
            {{ form.dry_run ? '开始预览' : '开始清理' }}
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card v-if="currentTask" shadow="never" class="progress-card">
      <template #header>
        <div class="card-header">
          <span>任务进度 (#{{ currentTask.id }})</span>
          <el-tag :type="statusTagType(currentTask.status)">
            {{ statusLabel(currentTask.status) }}
          </el-tag>
        </div>
      </template>
      <div class="progress-message">{{ currentTask.message || '...' }}</div>
      <el-progress
        :percentage="Math.floor(currentTask.progress || 0)"
        :status="currentTask.status === 'failed' ? 'exception' : (currentTask.status === 'completed' ? 'success' : '')"
      />

      <div v-if="currentTask.status === 'completed' && currentTask.result" class="result-block">
        <el-descriptions :column="5" border>
          <el-descriptions-item label="扫描视频">{{ currentTask.result.scanned || 0 }}</el-descriptions-item>
          <el-descriptions-item label="疑似 Sample">
            <el-tag type="warning">{{ currentTask.result.sample_count || 0 }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="删除文件">
            <el-tag type="success">{{ currentTask.result.deleted_file_count || 0 }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="删除目录">
            <el-tag type="success">{{ currentTask.result.deleted_dir_count || 0 }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="失败">
            <el-tag :type="(currentTask.result.errors?.length || 0) > 0 ? 'danger' : 'info'">
              {{ currentTask.result.errors?.length || 0 }}
            </el-tag>
          </el-descriptions-item>
        </el-descriptions>

        <div v-if="currentTask.result.libraries_refreshed?.length" class="muted hint-line">
          已通知 Jellyfin 重扫：{{ currentTask.result.libraries_refreshed.length }} 个库
        </div>

        <el-divider>{{ currentTask.result.dry_run ? '建议删除明细（预览）' : '删除明细' }}</el-divider>
        <el-table
          :data="currentTask.result.details || []"
          stripe
          size="small"
          max-height="500"
          empty-text="无 sample 文件被识别"
        >
          <el-table-column type="expand">
            <template #default="{ row }">
              <ul class="reasons">
                <li v-for="(r, idx) in row.reasons" :key="idx">• {{ r }}</li>
              </ul>
            </template>
          </el-table-column>
          <el-table-column prop="path" label="文件路径" min-width="320" show-overflow-tooltip />
          <el-table-column label="大小" width="100" align="right">
            <template #default="{ row }">{{ row.size_display || '—' }}</template>
          </el-table-column>
          <el-table-column label="判定" width="100">
            <template #default="{ row }">
              <el-tag :type="row.verdict === 'sample' ? 'danger' : 'warning'" size="small">
                {{ row.verdict }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="结果" width="100">
            <template #default="{ row }">
              <el-tag v-if="row.deleted" type="success" size="small">已删除</el-tag>
              <el-tag v-else-if="currentTask.result.dry_run" type="info" size="small">预览</el-tag>
              <el-tag v-else type="danger" size="small">失败</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="error" label="错误" min-width="160" show-overflow-tooltip />
        </el-table>

        <el-divider v-if="(currentTask.result.dir_details || []).length">
          清理空目录明细（无视频文件的目录被一并清掉）
        </el-divider>
        <el-table
          v-if="(currentTask.result.dir_details || []).length"
          :data="currentTask.result.dir_details"
          stripe size="small" max-height="300"
        >
          <el-table-column prop="path" label="目录路径" min-width="320" show-overflow-tooltip />
          <el-table-column label="结果" width="100">
            <template #default="{ row }">
              <el-tag v-if="row.deleted" type="success" size="small">已删除</el-tag>
              <el-tag v-else-if="currentTask.result.dry_run" type="info" size="small">预览</el-tag>
              <el-tag v-else type="danger" size="small">失败</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="error" label="说明" min-width="160" show-overflow-tooltip />
        </el-table>
      </div>

      <div v-if="currentTask.status === 'failed' && currentTask.result?.error" class="error-box">
        <el-alert type="error" :title="currentTask.result.error" :closable="false" show-icon />
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, computed, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { maintenanceApi, taskApi } from '@/api'

const route = useRoute()

const scope = computed(() => {
  const itemPaths = route.query.item_paths
  if (itemPaths) return { mode: 'items', item_paths: String(itemPaths).split(',').filter(Boolean) }
  if (route.query.library_ids) return { mode: 'multi-library', library_ids: String(route.query.library_ids).split(',').filter(Boolean) }
  if (route.query.library_id) return { mode: 'single-library', library_id: String(route.query.library_id) }
  if (route.query.scope === 'all') return { mode: 'all' }
  return { mode: 'manual' }
})

const scopeLabel = computed(() => {
  const s = scope.value
  if (s.mode === 'items') return `选中的 ${s.item_paths.length} 个条目`
  if (s.mode === 'multi-library') return `${s.library_ids.length} 个库`
  if (s.mode === 'single-library') return `单个库 (id=${s.library_id})`
  if (s.mode === 'all') return '全部媒体库'
  return ''
})

const form = ref({ dry_run: true })
const starting = ref(false)
const currentTask = ref(null)
let pollTimer = null

const start = async () => {
  if (!form.value.dry_run) {
    try {
      await ElMessageBox.confirm(
        '执行模式会真实删除磁盘上的视频文件，操作不可撤销。建议先用预览看一遍。\n\n确定继续吗？',
        '危险操作',
        { type: 'warning', confirmButtonText: '我已确认，执行删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' },
      )
    } catch { return }
  }

  starting.value = true
  try {
    const payload = { dry_run: form.value.dry_run }
    const s = scope.value
    if (s.mode === 'items') payload.item_paths = s.item_paths
    else if (s.mode === 'multi-library') payload.library_ids = s.library_ids
    else if (s.mode === 'single-library') payload.library_id = s.library_id
    // 'all' 不传，后端默认全库

    const res = await maintenanceApi.cleanupSamples(payload)
    currentTask.value = { id: res.data.task_id, status: 'running', progress: 0, message: '任务已启动...' }
    startPolling(res.data.task_id)
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || e.message || '启动失败')
  } finally {
    starting.value = false
  }
}

const startPolling = (taskId) => {
  stopPolling()
  pollTimer = setInterval(async () => {
    try {
      const res = await taskApi.get(taskId)
      currentTask.value = res.data
      if (['completed', 'failed', 'cancelled'].includes(res.data.status)) stopPolling()
    } catch { stopPolling() }
  }, 1500)
}
const stopPolling = () => { if (pollTimer) { clearInterval(pollTimer); pollTimer = null } }

const statusLabel = (s) => ({
  pending: '等待中', running: '运行中', completed: '已完成', failed: '失败', cancelled: '已取消',
}[s] || s)
const statusTagType = (s) => ({
  pending: 'info', running: '', completed: 'success', failed: 'danger', cancelled: 'warning',
}[s] || '')

onUnmounted(stopPolling)
</script>

<style lang="scss" scoped>
.page-header {
  display: flex; align-items: center; gap: 16px;
  .hint-tag { color: #6b7280; font-size: 13px; }
}
.form-card, .progress-card { margin-bottom: 16px; }
.progress-card {
  .card-header { display: flex; justify-content: space-between; align-items: center; }
  .progress-message { margin-bottom: 10px; color: #606266; font-size: 14px; word-break: break-all; }
  .result-block { margin-top: 16px; }
  .error-box { margin-top: 16px; }
}
.muted { color: #94a3b8; }
.hint-line { font-size: 12px; margin-top: 8px; }
.reasons {
  margin: 8px 0; padding-left: 60px;
  li { font-size: 12px; color: #475569; line-height: 1.6; word-break: break-all; }
}
</style>
