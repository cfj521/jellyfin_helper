<template>
  <div class="page-container">
    <div class="page-header">
      <h2>番号库</h2>

      <!-- 中间：媒体库选择 + 应用按钮，靠右 -->
      <div class="header-libs">
        <span class="lib-label">媒体库</span>
        <el-select
          v-model="cfg.library_ids"
          multiple
          filterable
          collapse-tags
          collapse-tags-tooltip
          placeholder="选择 Jellyfin 媒体库"
          :loading="loadingLibs"
          class="lib-select"
          @change="cfgDirty = true"
        >
          <el-option
            v-for="lib in jellyfinLibs"
            :key="lib.id"
            :label="lib.name"
            :value="lib.id"
          >
            <div style="display: flex; justify-content: space-between; align-items: center; gap: 8px">
              <span>{{ lib.name }}</span>
              <el-tag v-if="lib.is_adult_candidate" size="small" type="warning" effect="plain">推测</el-tag>
            </div>
          </el-option>
        </el-select>
        <el-button
          type="primary"
          :disabled="!cfgDirty"
          :loading="saving"
          @click="saveCfg"
        >
          <el-icon><Check /></el-icon>
          应用
        </el-button>
      </div>

      <div class="header-actions">
        <el-button @click="scanNow" :loading="scanning">
          <el-icon><Search /></el-icon>
          立即扫描
        </el-button>
        <el-button type="primary" @click="batchScrape" :loading="scraping">
          <el-icon><MagicStick /></el-icon>
          批量刮削
        </el-button>
      </div>
    </div>

    <el-card shadow="never" class="filter-card">
      <el-form inline>
        <el-form-item label="搜索">
          <el-input v-model="search" placeholder="番号或标题" clearable style="width: 200px" @change="reload" />
        </el-form-item>
        <el-form-item label="刮削状态">
          <el-select v-model="filter.hasMetadata" clearable style="width: 140px" @change="reload">
            <el-option label="全部" :value="null" />
            <el-option label="已刮削" :value="true" />
            <el-option label="未刮削" :value="false" />
          </el-select>
        </el-form-item>
        <el-form-item label="Jellyfin">
          <el-select v-model="filter.inJellyfin" clearable style="width: 140px" @change="reload">
            <el-option label="全部" :value="null" />
            <el-option label="已入库" :value="true" />
            <el-option label="未入库" :value="false" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-radio-group v-model="viewMode">
            <el-radio-button label="grid">网格</el-radio-button>
            <el-radio-button label="table">表格</el-radio-button>
          </el-radio-group>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 进度 -->
    <el-card v-if="currentTask" shadow="never" class="progress-card">
      <div class="progress-message">{{ currentTask.message }}</div>
      <el-progress
        :percentage="Math.floor(currentTask.progress || 0)"
        :status="currentTask.status === 'failed' ? 'exception' : (currentTask.status === 'completed' ? 'success' : '')"
      />
      <div v-if="currentTask.result && currentTask.status === 'completed'" class="task-result">
        <el-tag v-if="currentTask.result.success != null" type="success">成功 {{ currentTask.result.success }}</el-tag>
        <el-tag v-if="currentTask.result.not_found != null" type="warning">未找到 {{ currentTask.result.not_found }}</el-tag>
        <el-tag v-if="currentTask.result.failed != null" type="danger">失败 {{ currentTask.result.failed }}</el-tag>
      </div>
    </el-card>

    <!-- 网格视图 -->
    <el-row v-if="viewMode === 'grid'" :gutter="16" v-loading="loading">
      <el-col v-for="item in items" :key="item.id" :xs="12" :sm="8" :md="6" :lg="4" :xl="3">
        <el-card shadow="hover" class="item-card" body-style="padding: 0" @click="openDetail(item)">
          <el-image
            v-if="item.cover_url"
            :src="item.cover_url"
            fit="cover"
            lazy
            referrerpolicy="no-referrer"
            style="width: 100%; aspect-ratio: 16/11"
          />
          <div v-else class="no-cover">{{ item.code }}</div>
          <!-- Jellyfin 收录角标 -->
          <div v-if="item.jellyfin_id" class="jf-badge" title="已被 Jellyfin 收录">JF</div>
          <div class="info">
            <div class="code">{{ item.code }}</div>
            <div class="title" :title="item.title || '未刮削'">{{ item.title || '未刮削' }}</div>
            <div class="meta">
              <el-tag v-if="item.has_metadata" type="success" size="small">已刮削</el-tag>
              <el-tag v-else type="info" size="small">未刮削</el-tag>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 表格视图 -->
    <el-card v-else shadow="never">
      <el-table :data="items" v-loading="loading" @row-click="openDetail">
        <el-table-column label="封面" width="100">
          <template #default="{ row }">
            <el-image v-if="row.cover_url" :src="row.cover_url" fit="cover" lazy referrerpolicy="no-referrer" style="width: 60px; aspect-ratio: 16/11" />
            <div v-else class="no-cover-sm">无</div>
          </template>
        </el-table-column>
        <el-table-column prop="code" label="番号" width="160" />
        <el-table-column prop="title" label="标题" min-width="240" show-overflow-tooltip />
        <el-table-column prop="release_date" label="发行日期" width="120" />
        <el-table-column prop="studio" label="厂商" width="160" show-overflow-tooltip />
        <el-table-column label="演员" width="200">
          <template #default="{ row }">
            <span class="actors">{{ (row.actors || []).slice(0, 2).join(', ') }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.has_metadata ? 'success' : 'info'" size="small">
              {{ row.has_metadata ? '已刮削' : '未刮削' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="Jellyfin" width="100">
          <template #default="{ row }">
            <a v-if="row.jellyfin_id"
               :href="row.jellyfin_url"
               target="_blank"
               @click.stop
               class="jf-link"
               :title="row.jellyfin_name">
              <el-tag type="success" size="small" effect="plain">已入库 ↗</el-tag>
            </a>
            <el-tag v-else type="info" size="small" effect="plain">未入库</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <div class="pagination">
      <el-pagination
        v-model:current-page="pagination.page"
        v-model:page-size="pagination.size"
        :total="pagination.total"
        :page-sizes="[24, 48, 96]"
        layout="total, sizes, prev, pager, next"
        @change="reload"
      />
    </div>

    <!-- 扫描日志面板 -->
    <el-card v-if="scanLog.length || activeScanTasks.length" shadow="never" class="scan-log-card">
      <template #header>
        <div class="card-header">
          <div class="log-header-left">
            <span>扫描日志</span>
            <el-tag v-if="activeScanTasks.length" type="warning" size="small" effect="dark">
              {{ activeScanTasks.length }} 个扫描中
            </el-tag>
            <el-tag v-else type="info" size="small" effect="plain">空闲</el-tag>
          </div>
          <el-button text size="small" @click="scanLog = []">清空</el-button>
        </div>
      </template>
      <div class="log-list">
        <div
          v-for="(entry, idx) in scanLog"
          :key="idx"
          class="log-entry"
          :class="{ 'log-new': idx < 3 && activeScanTasks.length }"
        >
          <span class="log-icon">{{ logStatusInfo(entry.status).icon }}</span>
          <span class="log-time">{{ formatLogTime(entry.t) }}</span>
          <el-tag
            :style="{ color: logStatusInfo(entry.status).color, borderColor: logStatusInfo(entry.status).color + '50' }"
            size="small"
            effect="plain"
          >
            {{ logStatusInfo(entry.status).label }}
          </el-tag>
          <span class="log-code" v-if="entry.code">{{ entry.code }}</span>
          <span class="log-name" :title="entry.name">{{ entry.name }}</span>
          <span v-if="entry.title" class="log-title">→ {{ entry.title }}</span>
          <span v-if="entry.error" class="log-error">{{ entry.error }}</span>
        </div>
      </div>
    </el-card>

    <!-- 详情对话框 -->
    <el-dialog v-model="showDetail" :title="detail?.code" width="700px" top="5vh">
      <div v-if="detail" class="detail">
        <div class="detail-cover">
          <el-image v-if="detail.cover_url" :src="detail.cover_url" fit="cover" referrerpolicy="no-referrer" />
          <div v-else class="no-cover-lg">无封面</div>
        </div>
        <div class="detail-info">
          <h3>{{ detail.title || detail.code }}</h3>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="番号">{{ detail.code }}</el-descriptions-item>
            <el-descriptions-item label="发行日期">{{ detail.release_date || '-' }}</el-descriptions-item>
            <el-descriptions-item label="厂商">{{ detail.studio || '-' }}</el-descriptions-item>
            <el-descriptions-item label="导演">{{ detail.director || '-' }}</el-descriptions-item>
            <el-descriptions-item label="评分">{{ detail.rating || '-' }}</el-descriptions-item>
            <el-descriptions-item label="演员">
              <el-tag v-for="a in detail.actors || []" :key="a" size="small" style="margin-right: 4px">{{ a }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="标签">
              <el-tag v-for="t in detail.tags || []" :key="t" size="small" type="info" style="margin-right: 4px">{{ t }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="文件路径">{{ detail.file_path || '-' }}</el-descriptions-item>
            <el-descriptions-item label="NFO">{{ detail.nfo_path || '未生成' }}</el-descriptions-item>
            <el-descriptions-item label="数据源">{{ detail.source || '-' }}</el-descriptions-item>
            <el-descriptions-item label="Jellyfin">
              <template v-if="detail.jellyfin_id">
                <el-tag type="success" size="small">已入库</el-tag>
                <a :href="detail.jellyfin_url" target="_blank" class="jf-link" style="margin-left: 8px">
                  在 Jellyfin 中打开 ↗
                </a>
              </template>
              <el-tag v-else type="info" size="small">未入库</el-tag>
            </el-descriptions-item>
          </el-descriptions>
        </div>
      </div>
      <template #footer>
        <el-button @click="scrapeAgain" type="primary" plain>重新刮削</el-button>
        <el-button v-if="detail?.jellyfin_id" @click="syncFromJellyfin" :loading="syncing">
          <el-icon><Refresh /></el-icon>
          从 Jellyfin 同步
        </el-button>
        <el-button @click="regenerateNfo" v-if="detail?.file_path">重新生成 NFO</el-button>
        <el-button type="danger" plain @click="openDeleteDialog">删除</el-button>
      </template>
    </el-dialog>

    <!-- 删除确认对话框 -->
    <el-dialog v-model="showDeleteDialog" title="删除确认" width="480px" :close-on-click-modal="false">
      <div v-if="detail" class="delete-dialog">
        <p>将删除番号库中的 <strong>{{ detail.code }}</strong> 记录。</p>
        <el-divider />
        <div class="delete-options">
          <p class="opt-title">同时执行（可选）：</p>
          <el-checkbox v-model="deleteOpts.deleteFiles" :disabled="!detail.file_path">
            删除硬盘上的视频、NFO、封面文件
            <div class="opt-hint" v-if="detail.file_path">
              {{ detail.file_path }}
            </div>
            <div class="opt-hint warning" v-else>
              （没有关联文件路径，无法删除）
            </div>
          </el-checkbox>
          <el-checkbox v-model="deleteOpts.deleteInJellyfin" :disabled="!detail.jellyfin_id">
            从 Jellyfin 媒体库中删除
            <div class="opt-hint" v-if="detail.jellyfin_id">
              Jellyfin Item ID: {{ detail.jellyfin_id }}
            </div>
            <div class="opt-hint warning" v-else>
              （Jellyfin 中没有对应条目）
            </div>
          </el-checkbox>
        </div>
        <el-alert
          v-if="deleteOpts.deleteFiles"
          type="error"
          :closable="false"
          show-icon
          title="删除文件不可恢复！请确认你已经备份重要数据。"
          style="margin-top: 12px"
        />
      </div>
      <template #footer>
        <el-button @click="showDeleteDialog = false">取消</el-button>
        <el-button type="danger" @click="confirmDelete" :loading="deleting">
          确认删除
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { MagicStick, Refresh, Search, Check } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { adultApi, taskApi, configApi, jellyfinApi, api } from '@/api'

const items = ref([])
const loading = ref(false)
const scraping = ref(false)
const search = ref('')
const filter = ref({ hasMetadata: null, inJellyfin: null })
const viewMode = ref('grid')
const pagination = ref({ page: 1, size: 24, total: 0 })
const currentTask = ref(null)
const showDetail = ref(false)
const detail = ref(null)
const syncing = ref(false)

// 删除对话框相关
const showDeleteDialog = ref(false)
const deleting = ref(false)
const deleteOpts = ref({ deleteFiles: false, deleteInJellyfin: false })

// ==== 库选择 ====
const cfg = reactive({
  library_ids: [],
})
const cfgDirty = ref(false)
const saving = ref(false)
const loadingLibs = ref(false)
const jellyfinLibs = ref([])
const watcherStatus = ref({})
const scanning = ref(false)

// 流式扫描日志
const scanLog = ref([])  // 最新条目（按时间戳合并多个 task）
const activeScanTasks = ref([])  // 当前 running 的 adult_scan task ID 列表

let pollTimer = null
let watcherStatusTimer = null
let scanLogTimer = null

// 计算 WebSocket 是否已连接
const wsConnected = computed(() => {
  return !!(watcherStatus.value?.websocket?.connected)
})

const reload = async () => {
  loading.value = true
  try {
    const res = await adultApi.list({
      search: search.value || undefined,
      has_metadata: filter.value.hasMetadata,
      in_jellyfin: filter.value.inJellyfin,
      limit: pagination.value.size,
      offset: (pagination.value.page - 1) * pagination.value.size,
    })
    items.value = res.data.items
    pagination.value.total = res.data.total
  } catch (e) {
    console.error('加载失败', e)
  } finally {
    loading.value = false
  }
}

const openDetail = async (row) => {
  try {
    const res = await adultApi.get(row.id)
    detail.value = res.data
    showDetail.value = true
  } catch (e) {
    console.error('加载详情失败', e)
  }
}

const scrapeAgain = async () => {
  if (!detail.value) return
  try {
    const res = await adultApi.scrapeOne(detail.value.code)
    currentTask.value = { id: res.data.task_id, status: 'running', progress: 0, message: '刮削中...' }
    showDetail.value = false
    startPolling(res.data.task_id)
  } catch (e) {
    console.error('刮削失败', e)
  }
}

const regenerateNfo = async () => {
  try {
    await adultApi.regenerateNfo(detail.value.id)
    ElMessage.success('NFO 已重新生成')
  } catch (e) {
    console.error('NFO 生成失败', e)
  }
}

const openDeleteDialog = () => {
  // 重置选项后打开
  deleteOpts.value = { deleteFiles: false, deleteInJellyfin: false }
  showDeleteDialog.value = true
}

const confirmDelete = async () => {
  if (!detail.value) return
  deleting.value = true
  try {
    const res = await adultApi.remove(detail.value.id, {
      deleteFiles: deleteOpts.value.deleteFiles,
      deleteInJellyfin: deleteOpts.value.deleteInJellyfin,
    })
    const msgs = ['已从番号库删除']
    if (res.data.jellyfin_deleted) msgs.push('Jellyfin 中已删除')
    if (res.data.deleted_files?.length) msgs.push(`删除了 ${res.data.deleted_files.length} 个文件`)
    if (res.data.failed_deletes?.length) {
      ElMessage.warning(`部分操作失败：${res.data.failed_deletes.join('; ')}`)
    }
    ElMessage.success(msgs.join('；'))
    showDeleteDialog.value = false
    showDetail.value = false
    reload()
  } catch (e) {
    console.error('删除失败', e)
  } finally {
    deleting.value = false
  }
}

const syncFromJellyfin = async () => {
  if (!detail.value) return
  syncing.value = true
  try {
    const res = await adultApi.syncFromJellyfin(detail.value.id)
    const fields = res.data.changed_fields || []
    if (fields.length) {
      ElMessage.success(`已同步 ${fields.length} 个字段：${fields.join('、')}`)
    } else {
      ElMessage.info('数据已是最新，无需同步')
    }
    detail.value = res.data.item
    reload()
  } catch (e) {
    console.error('同步失败', e)
  } finally {
    syncing.value = false
  }
}

const batchScrape = async () => {
  try {
    await ElMessageBox.confirm('开始批量刮削未处理的条目？', '确认', { type: 'warning' })
  } catch { return }
  scraping.value = true
  try {
    const res = await adultApi.scrapeBatch({ only_unscraped: true, write_nfo: true, download_cover: true })
    currentTask.value = { id: res.data.task_id, status: 'running', progress: 0, message: '启动中...' }
    startPolling(res.data.task_id)
  } catch (e) {
    console.error('批量刮削失败', e)
  } finally {
    scraping.value = false
  }
}

const startPolling = (taskId) => {
  stopPolling()
  pollTimer = setInterval(async () => {
    try {
      const res = await taskApi.get(taskId)
      currentTask.value = res.data
      if (['completed', 'failed', 'cancelled'].includes(res.data.status)) {
        stopPolling()
        reload()
      }
    } catch {
      stopPolling()
    }
  }, 2000)
}

const stopPolling = () => {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

// ==== 配置 / 监视 ====

const loadCfg = async () => {
  try {
    const r = await configApi.getFull()
    const adult = r.data?.config?.adult || {}
    cfg.library_ids = adult.library_ids || []
    cfgDirty.value = false
  } catch (e) {
    console.error('加载配置失败', e)
  }
}

const loadJellyfinLibs = async () => {
  loadingLibs.value = true
  try {
    const r = await api.get('/api/adult/detect-libraries')
    jellyfinLibs.value = r.data.libraries || []
  } catch {
    try {
      const r = await jellyfinApi.libraries(false)
      jellyfinLibs.value = (r.data.libraries || []).map(l => ({ ...l, is_adult_candidate: false }))
    } catch {
      jellyfinLibs.value = []
    }
  } finally {
    loadingLibs.value = false
  }
}

const saveCfg = async () => {
  saving.value = true
  try {
    // 只更新 library_ids，其他成人配置（auto_scrape / sources 等）保持不动
    await configApi.saveFull({
      adult: {
        library_ids: cfg.library_ids,
        auto_detect: false,  // 用户已经手动选过，不再首次提示
      },
    })
    ElMessage.success('已保存')
    cfgDirty.value = false
    loadWatcherStatus()
  } catch (e) {
    console.error('保存失败', e)
  } finally {
    saving.value = false
  }
}

const loadWatcherStatus = async () => {
  try {
    const r = await api.get('/api/adult/watcher/status')
    watcherStatus.value = r.data || {}
  } catch {}
}

const scanNow = async () => {
  scanning.value = true
  try {
    if (cfg.library_ids.length === 0) {
      ElMessage.warning('请先选择并保存至少一个 Jellyfin 媒体库')
      scanning.value = false
      return
    }
    if (cfgDirty.value) {
      ElMessage.warning('请先保存配置再扫描')
      scanning.value = false
      return
    }
    const r = await api.post('/api/adult/watcher/run-now')
    ElMessage.success(r.data?.message || '已触发扫描')
    // 立刻去拉一次扫描日志状态
    loadWatcherStatus()
    loadScanLog()
  } catch (e) {
    console.error('扫描失败', e)
  } finally {
    scanning.value = false
  }
}

// ==== 流式扫描日志 ====

const loadScanLog = async () => {
  try {
    // 拉最近的 adult_scan 任务（含 running 和最近完成的）
    const r = await taskApi.list({ task_type: 'adult_scan', limit: 5 })
    const tasks = r.data?.tasks || []
    activeScanTasks.value = tasks.filter(t => t.status === 'running').map(t => t.id)

    // 合并所有 task 的 scanned_files 到一个列表，按时间倒序
    const merged = []
    for (const t of tasks) {
      const files = t.result?.scanned_files || []
      for (const f of files) {
        merged.push({ ...f, task_id: t.id, task_status: t.status })
      }
    }
    // 按 t（时间戳）倒序，最多 200 条
    merged.sort((a, b) => (b.t || 0) - (a.t || 0))
    scanLog.value = merged.slice(0, 200)
  } catch {}
}

const logStatusInfo = (status) => ({
  new:              { label: '新增',     color: '#10b981', icon: '✨' },
  scraped:          { label: '刮削成功', color: '#6366f1', icon: '📦' },
  skipped:          { label: '已扫描',   color: '#94a3b8', icon: '⏭️' },
  unrecognized:     { label: '未识别',   color: '#f59e0b', icon: '❓' },
  updated:          { label: '已更新',   color: '#3b82f6', icon: '🔄' },
  moved:            { label: '位置变化', color: '#3b82f6', icon: '↔️' },
  scrape_not_found: { label: '刮削无果', color: '#f59e0b', icon: '🔍' },
  scrape_failed:    { label: '刮削失败', color: '#ef4444', icon: '⚠️' },
}[status] || { label: status, color: '#64748b', icon: '·' })

const formatLogTime = (ts) => {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

const formatRelative = (ts) => {
  if (!ts) return ''
  const diff = Date.now() / 1000 - ts
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
}

onMounted(() => {
  reload()
  loadCfg()
  loadJellyfinLibs()
  loadWatcherStatus()
  loadScanLog()
  // 每 10 秒刷新 watcher 状态
  watcherStatusTimer = setInterval(loadWatcherStatus, 10000)
  // 每 2 秒刷新扫描日志（仅当有活跃扫描时）
  scanLogTimer = setInterval(() => {
    if (activeScanTasks.value.length > 0 || cfg.auto_scrape) {
      loadScanLog()
    }
  }, 2000)
})

onUnmounted(() => {
  stopPolling()
  if (watcherStatusTimer) clearInterval(watcherStatusTimer)
  if (scanLogTimer) clearInterval(scanLogTimer)
})
</script>

<style lang="scss" scoped>
.scan-log-card {
  margin-top: 16px;

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;

    .log-header-left {
      display: flex;
      align-items: center;
      gap: 10px;
      font-weight: 600;
    }
  }

  .log-list {
    max-height: 360px;
    overflow-y: auto;
    font-family: 'Consolas', 'Monaco', monospace;
    font-size: 12px;

    .log-entry {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 4px 8px;
      border-bottom: 1px solid #f1f5f9;
      transition: background-color 0.3s ease;

      &:hover {
        background: #f8fafc;
      }

      &.log-new {
        background: rgba(99, 102, 241, 0.05);
        animation: fade-in 0.4s ease;
      }

      .log-icon {
        font-size: 14px;
        flex-shrink: 0;
      }

      .log-time {
        color: #94a3b8;
        flex-shrink: 0;
        min-width: 60px;
      }

      .log-code {
        color: #6366f1;
        font-weight: 600;
        flex-shrink: 0;
      }

      .log-name {
        color: #475569;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        max-width: 380px;
      }

      .log-title {
        color: #0f172a;
        font-weight: 500;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        flex: 1;
      }

      .log-error {
        color: #ef4444;
        font-size: 11px;
      }
    }
  }
}

@keyframes fade-in {
  from { opacity: 0; transform: translateY(-4px); }
  to { opacity: 1; transform: translateY(0); }
}

// page-header 三段布局：标题 / 媒体库选择(中间靠右) / 操作按钮
.page-header {
  .header-libs {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
    margin: 0 16px;

    .lib-label {
      font-weight: 600;
      color: #475569;
      font-size: 13px;
    }

    .lib-select {
      flex: 1;
      max-width: 480px;
    }
  }
}

.filter-card { margin-bottom: 16px; }
.progress-card {
  margin-bottom: 16px;
  .progress-message { margin-bottom: 8px; color: #606266; font-size: 14px; }
  .task-result { margin-top: 12px; display: flex; gap: 8px; }
}
.item-card {
  margin-bottom: 16px;
  cursor: pointer;
  overflow: hidden;
  position: relative;

  .jf-badge {
    position: absolute;
    top: 6px;
    right: 6px;
    padding: 2px 6px;
    background: rgba(16, 185, 129, 0.92);
    color: #fff;
    font-size: 10px;
    font-weight: 700;
    border-radius: 3px;
    letter-spacing: 0.5px;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
  }

  .no-cover, .no-cover-sm, .no-cover-lg {
    background: #f5f7fa;
    color: #909399;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .no-cover { width: 100%; aspect-ratio: 16/11; }
  .info {
    padding: 8px 10px;
    .code { font-weight: 600; font-size: 13px; }
    .title { font-size: 12px; color: #606266; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 4px; }
    .meta { margin-top: 6px; }
  }
}
.no-cover-sm { width: 60px; aspect-ratio: 16/11; font-size: 12px; }
.no-cover-lg { width: 100%; aspect-ratio: 16/11; }
.actors { color: #606266; font-size: 13px; }
.pagination { margin-top: 16px; display: flex; justify-content: flex-end; }
.detail {
  display: grid;
  grid-template-columns: 240px 1fr;
  gap: 16px;
  .detail-cover {
    .el-image { width: 100%; aspect-ratio: 16/11; }
  }
  .detail-info h3 { margin: 0 0 12px; }
}

.jf-link {
  color: #6366f1;
  text-decoration: none;
  &:hover { text-decoration: underline; }
}

.delete-dialog {
  p { margin: 0; color: #475569; }
  .delete-options {
    .opt-title { margin-bottom: 10px; font-weight: 500; color: #0f172a; }
    .el-checkbox {
      display: block;
      margin-bottom: 12px;
      align-items: flex-start !important;
      :deep(.el-checkbox__label) { width: 100%; }
    }
    .opt-hint {
      margin-top: 4px;
      margin-left: 0;
      font-size: 12px;
      color: #94a3b8;
      word-break: break-all;
      &.warning { color: #f59e0b; }
    }
  }
}
</style>
