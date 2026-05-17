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
        :class="{ 'is-circuit': item.is_circuit_open }"
      >
        <div class="source-info">
          <span class="source-name">{{ item.source }}</span>
          <el-tag
            :type="item.is_circuit_open ? 'danger' : 'warning'"
            size="small"
            effect="plain"
          >
            {{ item.is_circuit_open ? '熔断中' : '暂停中' }}
          </el-tag>
          <span class="source-desc">{{ item.description }}</span>
        </div>
        <div class="source-meta">
          <span class="remaining">
            剩余 {{ formatRemaining(item.is_circuit_open ? item.circuit_remaining_sec : item.paused_remaining_sec) }}
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
  return Object.values(allStatus.value).filter(
    (s) => s.is_paused || s.is_circuit_open
  )
})

const hasActiveIssues = computed(() => activeItems.value.length > 0)

const formatRemaining = (sec) => {
  if (!sec || sec <= 0) return '0s'
  if (sec < 60) return `${Math.ceil(sec)}s`
  const m = Math.floor(sec / 60)
  const s = Math.ceil(sec % 60)
  if (m < 60) return `${m}m${String(s).padStart(2, '0')}s`
  const h = Math.floor(m / 60)
  return `${h}h${String(m % 60).padStart(2, '0')}m`
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

  &.is-circuit {
    background: var(--el-color-danger-light-9);
    border-color: var(--el-color-danger-light-5);
  }
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

  .remaining {
    font-family: monospace;
    color: var(--el-color-warning-dark-2);
    font-weight: 500;
  }
  .hit-count {
    color: var(--jt-text-secondary);
  }
}
</style>
