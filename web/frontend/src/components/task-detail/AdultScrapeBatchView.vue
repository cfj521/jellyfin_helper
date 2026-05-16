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
            <span class="title">刮削明细</span>
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
            placeholder="搜索番号 / 标题"
            clearable
            size="small"
            style="width: 240px"
          />
        </div>
      </template>

      <el-empty
        v-if="!filteredDetails.length"
        :description="details.length ? '当前过滤无匹配' : (task.status === 'running' ? '刮削中…' : '暂无明细')"
      />
      <el-table v-else :data="pagedDetails" stripe size="small">
        <el-table-column label="状态" width="120">
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
            <span class="code">{{ row.code }}</span>
          </template>
        </el-table-column>
        <el-table-column label="刮削结果" min-width="420">
          <template #default="{ row }">
            <!-- 失败 → 显示原因 -->
            <div v-if="row.status === 'failed'" class="result-cell">
              <div class="error-msg">
                <strong v-if="row.error_type">[{{ row.error_type }}]</strong>
                {{ row.error || '未知错误' }}
              </div>
            </div>

            <!-- 未找到 → 短提示 -->
            <div v-else-if="row.status === 'not_found'" class="result-cell">
              <span class="muted">所有源都没匹配到</span>
            </div>

            <!-- 成功 → 标题 + 抓到的字段汇总 -->
            <div v-else-if="row.status === 'success'" class="result-cell success-cell">
              <div class="title-line" :title="row.title">{{ row.title || '(无标题)' }}</div>
              <div class="fields-line" v-if="row.scraped_fields">
                <span
                  v-for="f in scrapedFieldChips(row.scraped_fields)"
                  :key="f.label"
                  :class="['chip', f.cls]"
                  :title="f.tip"
                >{{ f.label }}{{ f.value !== undefined ? ': ' + f.value : '' }}</span>
              </div>
            </div>

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
  success:   { label: '成功',     type: 'success', icon: '✓' },
  not_found: { label: '未找到',   type: 'warning', icon: '?' },
  failed:    { label: '失败',     type: 'danger',  icon: '✗' },
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
    arr = arr.filter(d =>
      (d.code || '').toLowerCase().includes(q) ||
      (d.title || '').toLowerCase().includes(q)
    )
  }
  return arr
})

const pagedDetails = computed(() => {
  const start = (page.value - 1) * pageSize
  return filteredDetails.value.slice(start, start + pageSize)
})

/**
 * 把 scraped_fields 拍成一组 chip 数据，给前端 v-for 渲染。
 * 命名约定：
 *   .chip-ok  = 该字段有数据（绿）
 *   .chip-no  = 该字段没数据（灰）
 *   .chip-warn = 文件没落地但抓到（黄）
 */
const scrapedFieldChips = (sf) => {
  const out = []
  out.push({
    label: sf.title ? '标题' : '无标题',
    cls: sf.title ? 'chip-ok' : 'chip-no',
  })
  out.push({
    label: '演员',
    value: sf.actors_count || 0,
    cls: sf.actors_count ? 'chip-ok' : 'chip-no',
  })
  out.push({
    label: '标签',
    value: sf.tags_count || 0,
    cls: sf.tags_count ? 'chip-ok' : 'chip-no',
  })
  out.push({
    label: '封面',
    cls: sf.saved_poster ? 'chip-ok' : (sf.cover ? 'chip-warn' : 'chip-no'),
    tip: sf.saved_poster ? '已下载到本地' : (sf.cover ? '源给了 url 但没落地' : '源没提供'),
  })
  out.push({
    label: 'NFO',
    cls: sf.saved_nfo ? 'chip-ok' : 'chip-no',
    tip: sf.saved_nfo ? '已写到磁盘' : '未写',
  })
  if (sf.release_date) out.push({ label: '发行', value: sf.release_date, cls: 'chip-ok' })
  if (sf.studio) out.push({ label: '厂商', value: sf.studio, cls: 'chip-ok' })
  if (sf.director) out.push({ label: '导演', value: sf.director, cls: 'chip-ok' })
  if (sf.rating) out.push({ label: '评分', value: sf.rating, cls: 'chip-ok' })
  if (sf.source) out.push({ label: '源', value: sf.source.replace(/^merged:/, ''), cls: 'chip-source', tip: sf.source })
  return out
}

const cards = computed(() => {
  const total = r.value.total ?? 0
  const success = r.value.success ?? 0
  const failed = r.value.failed ?? 0
  const not_found = r.value.not_found ?? 0
  const processed = success + failed + not_found
  const out = [
    { label: '总数',      value: total,     color: '#6366f1' },
    { label: '已处理',    value: processed, color: '#94a3b8',
      tip: total ? `${Math.floor(processed / total * 100)}%` : '' },
    { label: '成功',      value: success,   color: '#10b981' },
    { label: '未找到',    value: not_found, color: '#f59e0b' },
    { label: '失败',      value: failed,    color: '#ef4444' },
  ]
  if (r.value.jellyfin_refreshed) {
    out.push({ label: 'Jellyfin', value: '已通知刷新', color: '#3b82f6' })
  }
  return out
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

.muted { color: var(--jt-text-muted); }
.code { color: var(--jt-brand); font-weight: 600; font-family: monospace; }
.error-msg { color: var(--jt-danger); font-size: 12px; line-height: 1.5; }

.result-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.success-cell {
  .title-line {
    font-size: 13px;
    color: var(--jt-text-primary);
    line-height: 1.4;
    overflow: hidden;
    text-overflow: ellipsis;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
  }
  .fields-line {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
}

.chip {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 3px;
  white-space: nowrap;
  font-weight: 500;
  user-select: none;

  &.chip-ok {
    background: var(--jt-success-tint);
    color: var(--jt-success-text);
  }
  &.chip-no {
    background: var(--jt-fill-light);
    color: var(--jt-text-muted);
  }
  &.chip-warn {
    background: var(--jt-warning-tint);
    color: var(--jt-warning-text);
  }
  &.chip-source {
    background: rgba(var(--jt-brand-rgb), 0.1);
    color: var(--jt-brand);
    font-family: ui-monospace, monospace;
  }
}

.pagination {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
</style>
