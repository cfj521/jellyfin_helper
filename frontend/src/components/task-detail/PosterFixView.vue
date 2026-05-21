<template>
  <div>
    <StatCards :cards="cards" />

    <el-tabs v-model="activeTab">
      <el-tab-pane :label="`成功 (${counts.success})`" name="success">
        <el-empty v-if="!groups.success.length" description="无" />
        <template v-else>
          <el-table :data="pagedSuccess" stripe size="small">
            <el-table-column prop="title" label="标题" />
            <el-table-column prop="media_type" label="类型" width="80">
              <template #default="{ row }">
                <el-tag size="small" type="info">{{ row.media_type === 'movie' ? '电影' : '剧集' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="海报" width="80">
              <template #default="{ row }">
                <el-image v-if="row.poster_url" :src="row.poster_url" :preview-src-list="[row.poster_url]" style="width: 50px; height: 75px" fit="cover" />
              </template>
            </el-table-column>
          </el-table>
          <el-pagination
            v-if="groups.success.length > pageSize"
            v-model:current-page="successPage"
            :page-size="pageSize"
            :total="groups.success.length"
            layout="total, prev, pager, next"
            class="pager"
          />
        </template>
      </el-tab-pane>

      <el-tab-pane :label="`未找到 (${counts.not_found})`" name="not_found">
        <el-empty v-if="!groups.not_found.length" description="无" />
        <template v-else>
          <el-table :data="pagedNotFound" stripe size="small">
            <el-table-column prop="title" label="标题" />
            <el-table-column prop="media_type" label="类型" width="80">
              <template #default="{ row }">
                <el-tag size="small" type="info">{{ row.media_type === 'movie' ? '电影' : '剧集' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="error" label="原因" width="220" />
          </el-table>
          <el-pagination
            v-if="groups.not_found.length > pageSize"
            v-model:current-page="notFoundPage"
            :page-size="pageSize"
            :total="groups.not_found.length"
            layout="total, prev, pager, next"
            class="pager"
          />
        </template>
      </el-tab-pane>

      <el-tab-pane :label="`失败 (${counts.failed})`" name="failed">
        <el-empty v-if="!groups.failed.length" description="无" />
        <template v-else>
          <el-table :data="pagedFailed" stripe size="small">
            <el-table-column prop="title" label="标题" />
            <el-table-column prop="media_type" label="类型" width="80">
              <template #default="{ row }">
                <el-tag size="small" type="info">{{ row.media_type === 'movie' ? '电影' : '剧集' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="error" label="错误信息" />
          </el-table>
          <el-pagination
            v-if="groups.failed.length > pageSize"
            v-model:current-page="failedPage"
            :page-size="pageSize"
            :total="groups.failed.length"
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

const cards = computed(() => [
  { label: '总数',   value: r.value.total   ?? 0, color: '#6366f1' },
  { label: '成功',   value: r.value.success ?? 0, color: '#10b981' },
  { label: '未找到', value: r.value.not_found ?? 0, color: '#f59e0b' },
  { label: '失败',   value: r.value.failed  ?? 0, color: '#ef4444',
    tip: r.value.scan_only ? '仅扫描模式' : (r.value.jellyfin_refreshed ? '已通知 Jellyfin 刷新' : '') },
])

const details = computed(() => r.value.details || [])
const counts = computed(() => ({
  success:   details.value.filter(d => d.status === 'success' || d.status === 'found').length,
  not_found: details.value.filter(d => d.status === 'not_found').length,
  failed:    details.value.filter(d => d.status === 'failed').length,
}))
const groups = computed(() => ({
  success:   details.value.filter(d => d.status === 'success' || d.status === 'found'),
  not_found: details.value.filter(d => d.status === 'not_found'),
  failed:    details.value.filter(d => d.status === 'failed'),
}))

const pageSize = 100
const successPage = ref(1)
const notFoundPage = ref(1)
const failedPage = ref(1)

const pagedSuccess = computed(() => {
  const s = (successPage.value - 1) * pageSize
  return groups.value.success.slice(s, s + pageSize)
})
const pagedNotFound = computed(() => {
  const s = (notFoundPage.value - 1) * pageSize
  return groups.value.not_found.slice(s, s + pageSize)
})
const pagedFailed = computed(() => {
  const s = (failedPage.value - 1) * pageSize
  return groups.value.failed.slice(s, s + pageSize)
})

const activeTab = ref(counts.value.failed ? 'failed' : (counts.value.not_found ? 'not_found' : 'success'))
</script>

<style scoped>
.pager {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
}
</style>
