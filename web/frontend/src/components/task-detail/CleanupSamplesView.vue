<template>
  <div>
    <StatCards :cards="cards" />

    <el-tabs v-model="activeTab">
      <el-tab-pane :label="`Sample 文件 (${details.length})`" name="files">
        <el-empty v-if="!details.length" description="未发现 Sample 文件" />
        <template v-else>
          <el-table :data="pagedFiles" stripe size="small">
            <el-table-column label="文件路径" min-width="300">
              <template #default="{ row }">
                <span class="path">{{ row.path || row.name }}</span>
              </template>
            </el-table-column>
            <el-table-column label="大小" width="100">
              <template #default="{ row }">
                <span class="muted">{{ row.size_display || formatSize(row.size) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="判定" width="120">
              <template #default="{ row }">
                <el-tag size="small" :type="verdictTagType(row.verdict)">{{ verdictLabel(row.verdict) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="结果" width="100">
              <template #default="{ row }">
                <el-tag v-if="row.error === '(预览模式)'" size="small" type="info">预览</el-tag>
                <el-tag v-else-if="row.deleted" size="small" type="danger">✓ 已删除</el-tag>
                <el-tag v-else-if="row.error" size="small" type="warning">✗ 失败</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="error" label="错误" />
          </el-table>
          <el-pagination
            v-if="details.length > pageSize"
            v-model:current-page="filesPage"
            :page-size="pageSize"
            :total="details.length"
            layout="total, prev, pager, next"
            class="pager"
          />
        </template>
      </el-tab-pane>

      <el-tab-pane :label="`空目录 (${dirDetails.length})`" name="dirs">
        <el-empty v-if="!dirDetails.length" description="未删除任何目录" />
        <template v-else>
          <el-table :data="pagedDirs" stripe size="small">
            <el-table-column label="目录" min-width="320">
              <template #default="{ row }">
                <span class="path">{{ row.path }}</span>
              </template>
            </el-table-column>
            <el-table-column label="结果" width="120">
              <template #default="{ row }">
                <el-tag v-if="row.deleted" size="small" type="danger">✓ 已删除</el-tag>
                <el-tag v-else size="small" type="info">未删除</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="error" label="说明" />
          </el-table>
          <el-pagination
            v-if="dirDetails.length > pageSize"
            v-model:current-page="dirsPage"
            :page-size="pageSize"
            :total="dirDetails.length"
            layout="total, prev, pager, next"
            class="pager"
          />
        </template>
      </el-tab-pane>

      <el-tab-pane v-if="errors.length" :label="`错误 (${errors.length})`" name="errors">
        <template v-if="errors.length">
          <el-table :data="pagedErrors" stripe size="small">
            <el-table-column label="路径" min-width="280">
              <template #default="{ row }">{{ row.path }}</template>
            </el-table-column>
            <el-table-column prop="error" label="错误" />
          </el-table>
          <el-pagination
            v-if="errors.length > pageSize"
            v-model:current-page="errorsPage"
            :page-size="pageSize"
            :total="errors.length"
            layout="total, prev, pager, next"
            class="pager"
          />
        </template>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import StatCards from './StatCards.vue'

const props = defineProps({ task: { type: Object, required: true } })
const r = computed(() => props.task.result || {})
const details = computed(() => r.value.details || [])
const dirDetails = computed(() => r.value.dir_details || [])
const errors = computed(() => r.value.errors || [])

const cards = computed(() => [
  { label: '扫描数',      value: r.value.scanned             ?? 0, color: '#6366f1' },
  { label: 'Sample 数',  value: r.value.sample_count         ?? 0, color: '#f59e0b' },
  { label: '已删文件',    value: r.value.deleted_file_count   ?? 0, color: '#ef4444',
    tip: r.value.dry_run ? '预览模式（未删除）' : '' },
  { label: '已删空目录',  value: r.value.deleted_dir_count    ?? 0, color: '#ef4444' },
])

const VERDICT_LABEL = {
  sample: 'Sample',
  'sample-likely': '疑似 Sample',
  'non-media': '非媒体目录',
  'maybe-sample': '可能 Sample',
  extras: '附加内容',
}
const VERDICT_TYPE = {
  sample: 'danger',
  'sample-likely': 'warning',
  'non-media': 'warning',
  'maybe-sample': 'info',
  extras: 'success',  // 不会被清理
}
const verdictLabel = (v) => VERDICT_LABEL[v] || v || '-'
const verdictTagType = (v) => VERDICT_TYPE[v] || 'info'

const pageSize = 100
const filesPage = ref(1)
const dirsPage = ref(1)
const errorsPage = ref(1)

const pagedFiles = computed(() => {
  const s = (filesPage.value - 1) * pageSize
  return details.value.slice(s, s + pageSize)
})
const pagedDirs = computed(() => {
  const s = (dirsPage.value - 1) * pageSize
  return dirDetails.value.slice(s, s + pageSize)
})
const pagedErrors = computed(() => {
  const s = (errorsPage.value - 1) * pageSize
  return errors.value.slice(s, s + pageSize)
})

const formatSize = (bytes) => {
  if (!bytes) return '-'
  const KB = 1024, MB = KB * 1024, GB = MB * 1024
  if (bytes >= GB) return (bytes / GB).toFixed(2) + ' GB'
  if (bytes >= MB) return (bytes / MB).toFixed(2) + ' MB'
  if (bytes >= KB) return (bytes / KB).toFixed(2) + ' KB'
  return bytes + ' B'
}

const activeTab = ref(details.value.length ? 'files' : (dirDetails.value.length ? 'dirs' : 'files'))
</script>

<style scoped>
.path { word-break: break-all; font-family: 'Consolas', monospace; font-size: 12px; }
.muted { color: var(--jt-text-muted); font-family: monospace; }
.pager {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
}
</style>
