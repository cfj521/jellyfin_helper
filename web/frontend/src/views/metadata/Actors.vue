<template>
  <div class="page-container">
    <div class="page-header">
      <h2>演员管理</h2>
      <div class="header-actions">
        <el-button type="primary" @click="scanActors" :loading="scanning">
          <el-icon><Search /></el-icon>
          扫描演员
        </el-button>
        <el-button type="success" @click="showFixDialog = true">
          <el-icon><MagicStick /></el-icon>
          批量修复
        </el-button>
      </div>
    </div>

    <!-- 统计卡片 -->
    <el-row :gutter="20" class="stat-row">
      <el-col :span="8">
        <el-card shadow="hover">
          <el-statistic title="演员总数" :value="stats.total" />
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover">
          <el-statistic title="有图片" :value="stats.with_image" suffix="人" />
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover">
          <el-statistic title="缺失图片" :value="stats.without_image" suffix="人">
            <template #suffix>
              <el-tag v-if="stats.without_image > 0" type="danger" size="small">
                需要修复
              </el-tag>
            </template>
          </el-statistic>
        </el-card>
      </el-col>
    </el-row>

    <!-- 进度 -->
    <el-card v-if="currentTask" shadow="never" class="progress-card">
      <template #header>任务进度</template>
      <div class="progress-header">
        <span>{{ currentTask.message }}</span>
        <span :class="'status-' + currentTask.status">{{ statusLabel(currentTask.status) }}</span>
      </div>
      <el-progress
        :percentage="currentTask.progress"
        :status="currentTask.status === 'failed' ? 'exception' : (currentTask.status === 'completed' ? 'success' : '')"
      />
      <div v-if="currentTask.result" class="task-result">
        <el-tag type="success">成功: {{ currentTask.result.success }}</el-tag>
        <el-tag type="danger">失败: {{ currentTask.result.failed }}</el-tag>
      </div>
    </el-card>

    <!-- 演员列表 -->
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>演员列表</span>
          <div class="filters">
            <el-input v-model="search" placeholder="搜索演员" clearable style="width: 200px" @change="loadActors">
              <template #prefix><el-icon><Search /></el-icon></template>
            </el-input>
            <el-select v-model="filter.hasImage" placeholder="筛选" clearable @change="loadActors">
              <el-option label="全部" :value="null" />
              <el-option label="有图片" :value="true" />
              <el-option label="无图片" :value="false" />
            </el-select>
          </div>
        </div>
      </template>

      <el-table :data="actors" v-loading="loading">
        <el-table-column prop="name" label="演员名称" />
        <el-table-column prop="tmdb_id" label="TMDB ID" width="120">
          <template #default="{ row }">
            <el-link v-if="row.tmdb_id" :href="`https://www.themoviedb.org/person/${row.tmdb_id}`" target="_blank">
              {{ row.tmdb_id }}
            </el-link>
            <span v-else class="text-muted">-</span>
          </template>
        </el-table-column>
        <el-table-column prop="has_image" label="图片" width="90">
          <template #default="{ row }">
            <el-tag :type="row.has_image ? 'success' : 'danger'" size="small">
              {{ row.has_image ? '有' : '无' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="image_source" label="来源" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.image_source" :type="sourceTagType(row.image_source)" size="small">
              {{ sourceLabel(row.image_source) }}
            </el-tag>
            <span v-else class="text-muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220">
          <template #default="{ row }">
            <el-button
              v-if="!row.has_image"
              text
              type="primary"
              @click="fixSingleActor(row)"
              :loading="row.fixing"
            >
              修复
            </el-button>
            <el-button
              text
              type="warning"
              @click="triggerUpload(row)"
              :loading="row.uploading"
            >
              {{ row.has_image ? '换图' : '上传本地图' }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 隐藏的文件输入：点击操作列"上传"按钮时触发 -->
      <input
        ref="fileInputRef"
        type="file"
        accept="image/*"
        style="display: none"
        @change="onFileSelected"
      />

      <div class="pagination">
        <el-pagination
          v-model:current-page="pagination.page"
          v-model:page-size="pagination.size"
          :total="pagination.total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          @change="loadActors"
        />
      </div>
    </el-card>

    <!-- 批量修复对话框 -->
    <el-dialog v-model="showFixDialog" title="批量修复演员图片" width="500px">
      <el-form :model="fixForm" label-width="100px">
        <el-form-item label="模式">
          <el-radio-group v-model="fixForm.scanOnly">
            <el-radio :label="true">仅扫描（不上传）</el-radio>
            <el-radio :label="false">扫描并上传</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="数量限制">
          <el-input-number v-model="fixForm.limit" :min="0" :max="1000" />
          <span class="form-tip">0 表示不限制</span>
        </el-form-item>
        <el-form-item label="跳过已有">
          <el-switch v-model="fixForm.skipExisting" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showFixDialog = false">取消</el-button>
        <el-button type="primary" @click="startBatchFix">开始修复</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { metadataApi, taskApi, statsApi } from '@/api'
import { ElMessage } from 'element-plus'

const actors = ref([])
const stats = ref({ total: 0, with_image: 0, without_image: 0 })
const loading = ref(false)
const scanning = ref(false)
const currentTask = ref(null)
const showFixDialog = ref(false)
const search = ref('')
const filter = ref({ hasImage: null })
const pagination = ref({ page: 1, size: 50, total: 0 })

// 手动上传：用一个隐藏 file input 顺序触发，避免每行都渲染 input
const fileInputRef = ref(null)
const uploadTargetActor = ref(null)

const fixForm = ref({
  scanOnly: false,
  limit: 0,
  skipExisting: true
})

let pollTimer = null

const loadStats = async () => {
  try {
    const res = await statsApi.getActorStats()
    stats.value = {
      total: res.data.total,
      with_image: res.data.with_image,
      without_image: res.data.without_image
    }
  } catch (e) {
    console.error('加载统计失败', e)
  }
}

const loadActors = async () => {
  loading.value = true
  try {
    const res = await metadataApi.getActors({
      search: search.value || undefined,
      has_image: filter.value.hasImage,
      limit: pagination.value.size,
      offset: (pagination.value.page - 1) * pagination.value.size
    })
    actors.value = res.data.actors
    pagination.value.total = res.data.total
  } catch (e) {
    console.error('加载演员失败', e)
  } finally {
    loading.value = false
  }
}

const scanActors = async () => {
  scanning.value = true
  try {
    const res = await metadataApi.scanActors()
    currentTask.value = { id: res.data.task_id, status: 'running', progress: 0, message: '扫描中...' }
    startPolling(res.data.task_id)
  } catch (e) {
    console.error('扫描失败', e)
  } finally {
    scanning.value = false
  }
}

const startBatchFix = async () => {
  showFixDialog.value = false
  try {
    const res = await metadataApi.fixActors({
      scan_only: fixForm.value.scanOnly,
      limit: fixForm.value.limit || null,
      skip_existing: fixForm.value.skipExisting
    })
    currentTask.value = { id: res.data.task_id, status: 'running', progress: 0, message: '修复中...' }
    startPolling(res.data.task_id)
  } catch (e) {
    console.error('修复失败', e)
  }
}

const fixSingleActor = async (actor) => {
  actor.fixing = true
  try {
    const res = await metadataApi.fixSingleActor(actor.id)
    currentTask.value = { id: res.data.task_id, status: 'running', progress: 0, message: `修复: ${actor.name}` }
    startPolling(res.data.task_id)
  } catch (e) {
    console.error('修复失败', e)
  } finally {
    actor.fixing = false
  }
}

const startPolling = (taskId) => {
  pollTimer = setInterval(async () => {
    try {
      const res = await taskApi.get(taskId)
      currentTask.value = res.data
      if (res.data.status === 'completed' || res.data.status === 'failed') {
        stopPolling()
        loadStats()
        loadActors()
      }
    } catch (e) {
      stopPolling()
    }
  }, 1000)
}

const stopPolling = () => {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

const statusLabel = (status) => {
  const labels = { pending: '等待中', running: '运行中', completed: '已完成', failed: '失败' }
  return labels[status] || status
}

const sourceLabel = (s) => {
  const labels = { tmdb: 'TMDB', wikidata: 'Wikidata', manual: '本地上传', none: '都没找到' }
  return labels[s] || s
}

const sourceTagType = (s) => {
  // none = 警告色（提醒用户可手动补图）；manual = info（中性）；其他 = success
  if (s === 'none') return 'warning'
  if (s === 'manual') return 'info'
  return 'success'
}

const triggerUpload = (actor) => {
  uploadTargetActor.value = actor
  // 重置 input.value 让同一文件再选一次也能触发 change
  if (fileInputRef.value) fileInputRef.value.value = ''
  fileInputRef.value?.click()
}

const onFileSelected = async (e) => {
  const file = e.target.files?.[0]
  const actor = uploadTargetActor.value
  if (!file || !actor) return

  // 简单本地校验：类型 + 大小（与后端保持一致）
  if (!file.type.startsWith('image/')) {
    ElMessage.error('请选择图片文件')
    return
  }
  const MAX_BYTES = 10 * 1024 * 1024
  if (file.size > MAX_BYTES) {
    ElMessage.error(`图片过大（${(file.size / 1024 / 1024).toFixed(1)} MB），上限 10 MB`)
    return
  }

  actor.uploading = true
  try {
    await metadataApi.uploadActorImage(actor.id, file)
    ElMessage.success(`${actor.name} 已上传`)
    loadStats()
    loadActors()
  } catch (err) {
    // 拦截器已经 ElMessage.error，这里只兜底
    console.error('上传失败', err)
  } finally {
    actor.uploading = false
    uploadTargetActor.value = null
  }
}

onMounted(() => {
  loadStats()
  loadActors()
})

onUnmounted(() => stopPolling())
</script>

<style lang="scss" scoped>
.header-actions {
  display: flex;
  gap: 10px;
}

.stat-row {
  margin-bottom: 20px;
}

.progress-card {
  margin-bottom: 20px;

  .progress-header {
    display: flex;
    justify-content: space-between;
    margin-bottom: 12px;
  }

  .task-result {
    margin-top: 12px;
    display: flex;
    gap: 10px;
  }
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;

  .filters {
    display: flex;
    gap: 10px;
  }
}

.pagination {
  margin-top: 20px;
  display: flex;
  justify-content: flex-end;
}

.text-muted {
  color: var(--jt-text-muted);
}

.form-tip {
  margin-left: 10px;
  color: var(--jt-text-muted);
  font-size: 12px;
}
</style>
