<template>
  <div class="page-container">
    <div class="page-header">
      <h2>下载管理 (qBittorrent)</h2>
      <div class="header-actions">
        <el-select v-model="filter" style="width: 140px" @change="load">
          <el-option label="全部" value="all" />
          <el-option label="下载中" value="downloading" />
          <el-option label="已完成" value="completed" />
          <el-option label="做种中" value="seeding" />
          <el-option label="已暂停" value="paused" />
        </el-select>
        <el-button @click="load" :loading="loading">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
        <el-switch v-model="autoRefresh" active-text="自动刷新" />
      </div>
    </div>

    <el-card shadow="never">
      <el-table :data="qbitTorrents" v-loading="loading" max-height="600">
        <el-table-column prop="name" label="名称" min-width="320" show-overflow-tooltip />
        <el-table-column label="进度" width="200">
          <template #default="{ row }">
            <el-progress :percentage="row.progress" :status="row.progress >= 100 ? 'success' : ''" />
          </template>
        </el-table-column>
        <el-table-column label="大小" width="100">
          <template #default="{ row }">{{ formatSize(row.size) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="stateType(row.state)" size="small">{{ stateLabel(row.state) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="↓ 速度" width="110">
          <template #default="{ row }">{{ formatSize(row.dlspeed) }}/s</template>
        </el-table-column>
        <el-table-column label="↑ 速度" width="110">
          <template #default="{ row }">{{ formatSize(row.upspeed) }}/s</template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button v-if="isPaused(row.state)" size="small" @click="resume(row)">恢复</el-button>
            <el-button v-else size="small" @click="pause(row)">暂停</el-button>
            <el-button size="small" type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-empty v-if="!loading && !qbitTorrents.length" description="qBittorrent 暂无任务（也可能是连接失败，请检查配置）" />
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { discoverApi } from '@/api'

const filter = ref('all')
const loading = ref(false)
const autoRefresh = ref(true)
const qbitTorrents = ref([])

let timer = null

const load = async () => {
  loading.value = true
  try {
    const res = await discoverApi.listDownloads({ filter_status: filter.value })
    qbitTorrents.value = res.data.qbittorrent || []
  } catch (e) {
    console.error('加载失败', e)
  } finally {
    loading.value = false
  }
}

let syncCounter = 0
const startTimer = () => {
  stopTimer()
  if (autoRefresh.value) {
    timer = setInterval(async () => {
      await load()
      // 每 10 个周期（30s）检查一次完成下载并触发 Jellyfin 刷新
      if (++syncCounter >= 10) {
        syncCounter = 0
        try {
          const r = await discoverApi.syncCompleted()
          if (r.data?.refreshed) {
            ElMessage.success(`检测到 ${r.data.updated} 个新完成下载，已通知 Jellyfin 刷新`)
          }
        } catch {}
      }
    }, 3000)
  }
}

const stopTimer = () => {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

watch(autoRefresh, startTimer)

const pause = async (row) => {
  await discoverApi.pause(row.hash)
  await load()
}

const resume = async (row) => {
  await discoverApi.resume(row.hash)
  await load()
}

const remove = async (row) => {
  try {
    const action = await ElMessageBox.confirm(
      `删除 ${row.name}？`,
      '确认删除',
      {
        confirmButtonText: '同时删除文件',
        cancelButtonText: '仅移除任务',
        distinguishCancelAndClose: true,
        type: 'warning',
      }
    )
    await discoverApi.remove(row.hash, action === 'confirm')
    ElMessage.success('已删除')
    await load()
  } catch (action) {
    if (action === 'cancel') {
      await discoverApi.remove(row.hash, false)
      ElMessage.success('已移除任务（保留文件）')
      await load()
    }
  }
}

const isPaused = (state) => /paused/i.test(state || '')

const stateLabel = (s) => ({
  downloading: '下载中', uploading: '做种中', pausedUP: '已暂停(UP)', pausedDL: '已暂停(DL)',
  queuedUP: '排队中', queuedDL: '排队中', stalledUP: '停滞中', stalledDL: '停滞中',
  checkingUP: '校验中', checkingDL: '校验中', error: '错误', missingFiles: '缺文件',
}[s] || s)

const stateType = (s) => {
  if (!s) return ''
  if (/error|missing/i.test(s)) return 'danger'
  if (/paused|stalled/i.test(s)) return 'info'
  if (/uploading/i.test(s)) return 'success'
  return ''
}

const formatSize = (bytes) => {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (bytes >= 1024 && i < units.length - 1) {
    bytes /= 1024
    i++
  }
  return `${bytes.toFixed(1)} ${units[i]}`
}

onMounted(() => {
  load()
  startTimer()
})

onUnmounted(stopTimer)
</script>

<style lang="scss" scoped>
.header-actions {
  display: flex;
  gap: 12px;
  align-items: center;
}
</style>
