<template>
  <div class="page-container">
    <div class="page-header">
      <h2>扫描并修复识别错误</h2>
      <div class="hint-tag">
        筛选健康度=未识别（type=Folder）的条目，用自动提取的标题+年份在 TMDB 搜索，
        把第一个匹配应用上去；搜不到的会报错保留
      </div>
    </div>

    <el-alert
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 12px"
    >
      <strong>批量自动识别：第一个匹配可能不是最优解</strong>。
      建议先用预览模式跑一遍，看建议匹配是否合理；不准的可在该条目上手动点"重新识别"。
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
            <el-radio :label="true">预览（仅搜索，不应用）</el-radio>
            <el-radio :label="false">执行（应用第一个匹配）</el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="starting" @click="start">
            {{ form.dry_run ? '开始预览' : '开始修复' }}
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card v-if="currentTask" shadow="never" class="progress-card">
      <template #header>
        <div class="card-header">
          <span>任务进度 (#{{ currentTask.id }})</span>
          <el-tag :type="statusTagType(currentTask.status)">{{ statusLabel(currentTask.status) }}</el-tag>
        </div>
      </template>
      <div class="progress-message">{{ currentTask.message || '...' }}</div>
      <el-progress
        :percentage="Math.floor(currentTask.progress || 0)"
        :status="currentTask.status === 'failed' ? 'exception' : (currentTask.status === 'completed' ? 'success' : '')"
      />

      <div v-if="currentTask.status === 'completed' && currentTask.result" class="result-block">
        <el-descriptions :column="6" border>
          <el-descriptions-item label="扫描条目">{{ currentTask.result.scanned || 0 }}</el-descriptions-item>
          <el-descriptions-item label="未识别项">
            <el-tag type="warning">{{ currentTask.result.unrecognized_count || 0 }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="已跳过 (无主体文件)">
            <el-tooltip placement="top">
              <template #content>
                按库类型检查目录内是否有主体文件（电影/剧集要视频、音乐要音频、书籍要电子书等），<br>
                没有的目录被判为"空壳"——多是字幕包 / 缩略图缓存 / 孤儿 NFO 目录——直接跳过不送 TMDB
              </template>
              <el-tag type="info">{{ currentTask.result.skipped_count || 0 }}</el-tag>
            </el-tooltip>
          </el-descriptions-item>
          <el-descriptions-item label="已修复">
            <el-tag type="success">{{ currentTask.result.fixed_count || 0 }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="搜不到">
            <el-tag type="info">{{ currentTask.result.no_match_count || 0 }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="错误">
            <el-tag :type="(currentTask.result.errors?.length || 0) > 0 ? 'danger' : 'info'">
              {{ currentTask.result.errors?.length || 0 }}
            </el-tag>
          </el-descriptions-item>
        </el-descriptions>

        <!-- 跳过条目列表：折叠默认收起，展开能看到具体被哪些路径跳过了 -->
        <el-collapse v-if="(currentTask.result.skipped?.length || 0) > 0" style="margin-top: 12px">
          <el-collapse-item :title="`已跳过的空壳目录（${currentTask.result.skipped.length}）`" name="skipped">
            <el-table :data="currentTask.result.skipped" stripe size="small" max-height="300">
              <el-table-column prop="path" label="路径" min-width="320" show-overflow-tooltip />
              <el-table-column prop="item_name" label="名称" min-width="180" show-overflow-tooltip />
              <el-table-column prop="collection_type" label="库类型" width="120" />
              <el-table-column label="原因" width="160">
                <template #default="{ row }">
                  <el-tag size="small" type="info">{{ row.reason === 'no_media_files' ? '无主体文件' : row.reason }}</el-tag>
                </template>
              </el-table-column>
            </el-table>
          </el-collapse-item>
        </el-collapse>

        <div v-if="currentTask.result.libraries_refreshed?.length" class="muted hint-line">
          已通知 Jellyfin 重扫：{{ currentTask.result.libraries_refreshed.length }} 个库
        </div>

        <el-divider>{{ currentTask.result.dry_run ? '建议匹配明细（预览）' : '修复明细' }}</el-divider>
        <el-table
          :data="currentTask.result.details || []"
          stripe size="small" max-height="600"
          empty-text="无未识别条目"
        >
          <el-table-column type="expand">
            <template #default="{ row }">
              <div v-if="row.first_candidate" class="candidate-detail">
                <el-image
                  v-if="row.first_candidate.image_url"
                  :src="row.first_candidate.image_url"
                  fit="cover"
                  lazy
                  style="width: 60px; height: 90px; border-radius: 3px; flex-shrink: 0"
                />
                <div class="candidate-info">
                  <div><b>候选标题：</b>{{ row.first_candidate.name }} ({{ row.first_candidate.year || '?' }})</div>
                  <div v-if="row.first_candidate.tmdb_id">
                    <a
                      :href="`https://www.themoviedb.org/${row.item_type === 'Series' ? 'tv' : 'movie'}/${row.first_candidate.tmdb_id}`"
                      target="_blank"
                      rel="noopener noreferrer"
                    >TMDB#{{ row.first_candidate.tmdb_id }}</a>
                  </div>
                  <div v-if="row.first_candidate.overview" class="overview">
                    {{ row.first_candidate.overview }}
                  </div>
                </div>
              </div>
              <div v-else class="muted">无候选信息</div>
            </template>
          </el-table-column>
          <el-table-column prop="path" label="原路径" min-width="280" show-overflow-tooltip />
          <el-table-column label="提取查询" width="220">
            <template #default="{ row }">
              <span v-if="row.extracted_title">
                <strong>{{ row.extracted_title }}</strong>
                <span v-if="row.extracted_year" class="muted"> ({{ row.extracted_year }})</span>
              </span>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="建议匹配" min-width="200">
            <template #default="{ row }">
              <span v-if="row.first_candidate">
                {{ row.first_candidate.name }}
                <span class="muted">({{ row.first_candidate.year || '?' }})</span>
              </span>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="结果" width="100">
            <template #default="{ row }">
              <el-tag v-if="row.applied" type="success" size="small">已应用</el-tag>
              <el-tag v-else-if="currentTask.result.dry_run && row.first_candidate" type="info" size="small">预览</el-tag>
              <el-tag v-else type="danger" size="small">未应用</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="error" label="说明" min-width="180" show-overflow-tooltip />
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
        '执行模式会把每个未识别条目自动绑到 TMDB 搜索的第一个匹配。错配可在该条目上手动重识别。\n\n确定继续吗？',
        '确认执行',
        { type: 'warning', confirmButtonText: '执行', cancelButtonText: '取消' },
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

    const res = await maintenanceApi.autoIdentify(payload)
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

.candidate-detail {
  display: flex;
  gap: 12px;
  padding: 8px 60px;
  align-items: flex-start;

  .candidate-info {
    flex: 1;
    font-size: 13px;
    line-height: 1.6;

    a { color: #0ea5e9; text-decoration: none; font-family: ui-monospace, monospace; font-size: 12px; }
    .overview { color: #475569; font-size: 12px; margin-top: 4px; }
  }
}
</style>
