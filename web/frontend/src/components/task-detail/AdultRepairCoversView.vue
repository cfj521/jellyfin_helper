<template>
  <div>
    <StatCards :cards="cards" />

    <el-card v-if="r.error" shadow="never" style="margin-bottom: 16px">
      <el-alert type="error" :closable="false" show-icon :title="r.error" />
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="header">
          <div class="header-left">
            <span class="title">修复明细</span>
            <el-radio-group v-model="filterStatus" size="small">
              <el-radio-button value="all">全部 ({{ details.length }})</el-radio-button>
              <el-radio-button
                v-for="s in availableStatuses"
                :key="s.key"
                :value="s.key"
              >
                {{ s.label }} ({{ s.count }})
              </el-radio-button>
            </el-radio-group>
          </div>
          <el-input
            v-model="search"
            placeholder="搜索番号"
            clearable
            size="small"
            style="width: 200px"
          />
        </div>
      </template>

      <el-empty
        v-if="!filteredDetails.length"
        :description="details.length ? '当前过滤无匹配' : (task.status === 'running' ? '修复中…' : '暂无明细')"
      />
      <el-table v-else :data="pagedDetails" stripe size="small">
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag
              :type="statusInfo(row.status).type"
              size="small"
              effect="plain"
            >
              {{ statusInfo(row.status).icon }} {{ statusInfo(row.status).label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="番号" width="160">
          <template #default="{ row }">
            <span class="code">{{ row.code || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="说明" min-width="320">
          <template #default="{ row }">
            <span v-if="row.status === 'success'" class="muted">封面已下载到本地</span>
            <span v-else-if="row.status === 'skipped'" class="muted">{{ row.reason || '已跳过' }}</span>
            <span v-else-if="row.status === 'failed'" class="error-msg">{{ row.error || '未知错误' }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="filteredDetails.length > pageSize" class="pagination">
        <el-pagination
          v-model:current-page="page"
          :page-size="pageSize"
          :total="filteredDetails.length"
          layout="total, prev, pager, next"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import StatCards from './StatCards.vue'

const props = defineProps({ task: { type: Object, required: true } })

const r = computed(() => props.task.result || {})
const details = computed(() => r.value.details || [])

const filterStatus = ref('all')
const search = ref('')
const page = ref(1)
const pageSize = 50

const STATUS_META = {
  success: { label: '成功',   type: 'success', icon: '✓' },
  skipped: { label: '已跳过', type: 'info',    icon: '⏭' },
  failed:  { label: '失败',   type: 'danger',  icon: '✗' },
}

const statusInfo = (s) => STATUS_META[s] || { label: s || '?', type: 'info', icon: '·' }

const statusCounts = computed(() => {
  const m = {}
  for (const d of details.value) m[d.status] = (m[d.status] || 0) + 1
  return m
})

const availableStatuses = computed(() =>
  Object.keys(statusCounts.value).map(k => ({
    key: k,
    label: statusInfo(k).label,
    count: statusCounts.value[k],
  }))
)

const filteredDetails = computed(() => {
  let arr = details.value
  if (filterStatus.value !== 'all') {
    arr = arr.filter(d => d.status === filterStatus.value)
  }
  const q = search.value.trim().toLowerCase()
  if (q) {
    arr = arr.filter(d => (d.code || '').toLowerCase().includes(q))
  }
  return arr
})

const pagedDetails = computed(() => {
  const start = (page.value - 1) * pageSize
  return filteredDetails.value.slice(start, start + pageSize)
})

const cards = computed(() => {
  const total = r.value.total ?? 0
  const success = r.value.success ?? 0
  const failed = r.value.failed ?? 0
  const processed = success + failed
  return [
    { label: '总数',     value: total,     color: '#6366f1' },
    { label: '已处理',   value: processed, color: '#94a3b8',
      tip: total ? `${Math.floor(processed / total * 100)}%` : '' },
    { label: '成功',     value: success,   color: '#10b981' },
    { label: '失败/跳过', value: failed,    color: '#ef4444' },
  ]
})
</script>

<style lang="scss" scoped>
.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;

  .header-left {
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
  }
  .title { font-weight: 500; }
}

.muted { color: #94a3b8; }
.code { color: #6366f1; font-weight: 600; font-family: monospace; }
.error-msg { color: #ef4444; font-size: 12px; line-height: 1.5; }

.pagination {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
</style>
