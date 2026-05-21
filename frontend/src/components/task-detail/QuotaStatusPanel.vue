<template>
  <el-card v-if="hasActiveIssues" shadow="never" class="quota-panel">
    <template #header>
      <div class="panel-header">
        <span><el-icon><Warning /></el-icon> 外部服务配额状态</span>
        <el-button text size="small" @click="loadStatus">
          <el-icon><Refresh /></el-icon>
        </el-button>
      </div>
    </template>

    <div class="source-list">
      <div
        v-for="item in activeItems"
        :key="item.source"
        class="source-row"
      >
        <div class="source-info">
          <span class="source-name">{{ item.source }}</span>
          <!-- 区分两种暂停：external = 真被对方限了（红，醒目）；preventive = 本地预暂停（橙，软提示）-->
          <el-tooltip
            :content="pauseTooltip(item.pause_reason)"
            placement="top"
          >
            <el-tag
              :type="item.pause_reason === 'external' ? 'danger' : 'warning'"
              size="small"
              effect="plain"
            >
              {{ pauseLabel(item.pause_reason) }}
            </el-tag>
          </el-tooltip>
          <span class="source-desc">{{ item.description }}</span>
        </div>
        <div class="source-meta">
          <span class="resume-at" :title="`unix ts ${item.paused_until_ts}`">
            恢复于 {{ formatResumeAt(item.paused_until_ts) }}
          </span>
          <span class="hit-count">累计触发 {{ item.total_hits }} 次</span>
          <el-button
            text
            type="primary"
            size="small"
            @click="resetSource(item.source)"
          >
            重置
          </el-button>
        </div>
      </div>
    </div>
  </el-card>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { Warning, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { taskApi } from '@/api'

const allStatus = ref({})
let timer = null

const activeItems = computed(() => {
  return Object.values(allStatus.value).filter((s) => s.is_paused)
})

const hasActiveIssues = computed(() => activeItems.value.length > 0)

// 显示恢复时间点（绝对时刻），不显示剩余倒计时 —— 这个面板不自动刷新，
// 倒计时会越过越不准；时间点是静态的，用户一眼能判断"是不是过去时间了"
// 暂停状态文案 —— 区分"对方真在限流"和"本地保护性降速"两档
// external = 对方服务器拒绝了请求（429/30900/403...）
// internal = 本地配额触顶提前暂停（对方未拒绝）
// '' = 旧状态从 kv_cache 恢复的（未知原因），按通用「暂停中」兜底
const pauseLabel = (reason) => {
  if (reason === 'external') return '被限流中'
  if (reason === 'internal') return '本地保护中'
  return '暂停中'
}
const pauseTooltip = (reason) => {
  if (reason === 'external') return '对方服务器真的拒绝了请求（429/30900/403...），需要等一段时间'
  if (reason === 'internal') return '本地配额预暂停 —— 没有被对方限，只是我们自己保守降速避免被限。对方端没有任何拒绝信号'
  return '暂停中（旧状态恢复，未知原因）'
}

const formatResumeAt = (unixTs) => {
  if (!unixTs) return '-'
  const d = new Date(unixTs * 1000)
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  if (sameDay) return `今天 ${hh}:${mm}`
  // 不同日：带月-日
  const M = String(d.getMonth() + 1).padStart(2, '0')
  const D = String(d.getDate()).padStart(2, '0')
  return `${M}-${D} ${hh}:${mm}`
}

const loadStatus = async () => {
  try {
    const res = await taskApi.quotaStatus()
    allStatus.value = res.data || {}
  } catch (e) {
    // 静默失败
  }
}

const resetSource = async (source) => {
  try {
    await taskApi.resetQuota(source)
    ElMessage.success(`${source} 已重置`)
    await loadStatus()
  } catch (e) {
    ElMessage.error('重置失败')
  }
}

onMounted(() => {
  loadStatus()
  // 每 15s 刷新一次（仅在有活跃限流时才有意义，但开销很低）
  timer = setInterval(loadStatus, 15000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<style lang="scss" scoped>
.quota-panel {
  margin-bottom: 16px;
  border-color: var(--el-color-warning-light-5);
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 14px;
  font-weight: 500;

  .el-icon {
    margin-right: 4px;
    color: var(--el-color-warning);
  }
}

.source-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.source-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  background: var(--el-color-warning-light-9);
  border: 1px solid var(--el-color-warning-light-5);
  border-radius: 6px;
}

.source-info {
  display: flex;
  align-items: center;
  gap: 8px;

  .source-name {
    font-weight: 600;
    font-size: 13px;
  }
  .source-desc {
    font-size: 12px;
    color: var(--jt-text-muted);
  }
}

.source-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 12px;

  .resume-at {
    font-family: monospace;
    color: var(--el-color-warning-dark-2);
    font-weight: 500;
  }
  .hit-count {
    color: var(--jt-text-secondary);
  }
}
</style>
