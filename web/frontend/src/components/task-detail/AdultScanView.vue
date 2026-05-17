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
            <span class="title">扫描明细</span>
            <el-radio-group v-model="filterStatus" size="small">
              <el-radio-button value="all">全部 ({{ files.length }})</el-radio-button>
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
            placeholder="搜索文件名/番号"
            clearable
            size="small"
            style="width: 240px"
          />
        </div>
      </template>

      <el-empty
        v-if="!filteredFiles.length"
        :description="files.length ? '当前过滤无匹配' : '暂无扫描记录'"
      />
      <el-table v-else :data="pagedFiles" stripe size="small">
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag
              :style="{
                color: statusInfo(row.status).color,
                borderColor: statusInfo(row.status).color + '60',
                background: statusInfo(row.status).color + '10',
              }"
              size="small"
              effect="plain"
            >
              {{ statusInfo(row.status).icon }} {{ statusInfo(row.status).label }}
            </el-tag>
          </template>
        </el-table-column>

        <!-- 第二列：按 tab 过滤动态调整 -->
        <!-- unrecognized：番号一直是 NULL，无意义 → 隐藏整列 -->
        <!-- scraped：刮削后才有 title，专门展示刮削标题 -->
        <!-- 其它：番号 -->
        <el-table-column
          v-if="filterStatus !== 'unrecognized'"
          :label="secondColLabel"
          :min-width="filterStatus === 'scraped' ? 240 : 140"
          :width="filterStatus === 'scraped' ? undefined : 140"
          show-overflow-tooltip
        >
          <template #default="{ row }">
            <span v-if="filterStatus === 'scraped'">
              <span v-if="row.title">{{ row.title }}</span>
              <span v-else-if="row.error" class="error-msg">{{ row.error }}</span>
              <span v-else class="muted">—</span>
            </span>
            <template v-else>
              <span v-if="row.code" class="code">{{ row.code }}</span>
              <span v-else class="muted">—</span>
            </template>
          </template>
        </el-table-column>

        <el-table-column label="文件名" min-width="320">
          <template #default="{ row }">
            <span class="filename" :title="row.name">{{ row.name }}</span>
          </template>
        </el-table-column>

        <el-table-column label="时间" width="100">
          <template #default="{ row }">
            <span class="muted">{{ formatTime(row.t) }}</span>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="filteredFiles.length > pageSize" class="pagination">
        <el-pagination
          v-model:current-page="page"
          :page-size="pageSize"
          :total="filteredFiles.length"
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
const files = computed(() => r.value.scanned_files || [])

const filterStatus = ref('all')
const search = ref('')
const page = ref(1)
const pageSize = 100

const STATUS_META = {
  new:              { label: '新增',     color: '#10b981', icon: '✨' },
  scraped:          { label: '刮削成功', color: '#6366f1', icon: '📦' },
  skipped:          { label: '已扫过',   color: '#94a3b8', icon: '⏭' },
  unrecognized:     { label: '未识别',   color: '#f59e0b', icon: '❓' },
  updated:          { label: '已更新',   color: '#3b82f6', icon: '🔄' },
  moved:            { label: '位置变化', color: '#3b82f6', icon: '↔' },
  excluded:         { label: '排除',     color: '#94a3b8', icon: '🚫' },
  scrape_not_found: { label: '刮削无果', color: '#f59e0b', icon: '🔍' },
  scrape_failed:    { label: '刮削失败', color: '#ef4444', icon: '⚠' },
}

// 第二列 label：跟 filterStatus 联动
const secondColLabel = computed(() => {
  if (filterStatus.value === 'scraped') return '刮削标题'
  return '番号'
})

const statusInfo = (s) => STATUS_META[s] || { label: s || '?', color: '#64748b', icon: '·' }

const statusCounts = computed(() => {
  const m = {}
  for (const f of files.value) m[f.status] = (m[f.status] || 0) + 1
  return m
})

const availableStatuses = computed(() =>
  Object.keys(statusCounts.value).map(k => ({
    key: k,
    label: statusInfo(k).label,
    count: statusCounts.value[k],
  }))
)

const filteredFiles = computed(() => {
  let arr = files.value
  if (filterStatus.value !== 'all') {
    arr = arr.filter(f => f.status === filterStatus.value)
  }
  const q = search.value.trim().toLowerCase()
  if (q) {
    arr = arr.filter(f =>
      (f.name || '').toLowerCase().includes(q) ||
      (f.code || '').toLowerCase().includes(q)
    )
  }
  return arr
})

const pagedFiles = computed(() => {
  const start = (page.value - 1) * pageSize
  return filteredFiles.value.slice(start, start + pageSize)
})

const cards = computed(() => [
  { label: '扫描文件',  value: r.value.scanned ?? 0,      color: '#6366f1' },
  { label: '新增',      value: r.value.new ?? 0,          color: '#10b981' },
  { label: '已存在',    value: r.value.skipped ?? 0,      color: '#94a3b8',
    tip: '路径与 mtime 都未变，跳过' },
  { label: '未识别',    value: r.value.unrecognized ?? 0, color: '#f59e0b',
    tip: '从文件名提取番号失败，未入库' },
  { label: '已刮削',    value: r.value.scraped ?? 0,      color: '#3b82f6' },
  { label: '失败',      value: r.value.failed ?? 0,       color: '#ef4444' },
])

const formatTime = (ts) => {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}
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

.muted { color: var(--jt-text-muted); }
.code { color: var(--jt-brand); font-weight: 600; font-family: monospace; }
.filename { word-break: break-all; }
.error-msg { color: var(--jt-danger); font-size: 12px; }

.pagination {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
</style>
