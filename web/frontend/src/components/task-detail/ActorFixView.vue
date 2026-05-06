<template>
  <div>
    <StatCards :cards="cards" />

    <el-alert
      v-if="!details.length"
      type="info"
      :closable="false"
      show-icon
      title="该任务未保留明细数据"
    />

    <el-tabs v-else v-model="activeTab">
      <el-tab-pane :label="`成功 (${counts.success})`" name="success">
        <el-empty v-if="!groups.success.length" description="无" />
        <el-table v-else :data="groups.success" stripe size="small">
          <el-table-column prop="name" label="演员" />
          <el-table-column prop="tmdb_id" label="TMDB ID" width="120">
            <template #default="{ row }">
              <el-link v-if="row.tmdb_id" :href="`https://www.themoviedb.org/person/${row.tmdb_id}`" target="_blank">
                {{ row.tmdb_id }}
              </el-link>
              <span v-else class="muted">-</span>
            </template>
          </el-table-column>
          <el-table-column label="预览" width="80">
            <template #default="{ row }">
              <el-image v-if="row.image_url" :src="row.image_url" :preview-src-list="[row.image_url]" style="width: 40px; height: 60px" fit="cover" />
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane :label="`未找到 (${counts.not_found})`" name="not_found">
        <el-empty v-if="!groups.not_found.length" description="无" />
        <el-table v-else :data="groups.not_found" stripe size="small">
          <el-table-column prop="name" label="演员" />
          <el-table-column prop="error" label="原因" />
        </el-table>
      </el-tab-pane>

      <el-tab-pane :label="`无照片 (${counts.no_image})`" name="no_image">
        <el-empty v-if="!groups.no_image.length" description="无" />
        <el-table v-else :data="groups.no_image" stripe size="small">
          <el-table-column prop="name" label="演员" />
          <el-table-column prop="tmdb_id" label="TMDB ID" width="120">
            <template #default="{ row }">
              <el-link v-if="row.tmdb_id" :href="`https://www.themoviedb.org/person/${row.tmdb_id}`" target="_blank">
                {{ row.tmdb_id }}
              </el-link>
            </template>
          </el-table-column>
          <el-table-column prop="error" label="说明" />
        </el-table>
      </el-tab-pane>

      <el-tab-pane :label="`失败 (${counts.failed})`" name="failed">
        <el-empty v-if="!groups.failed.length" description="无" />
        <el-table v-else :data="groups.failed" stripe size="small">
          <el-table-column prop="name" label="演员" />
          <el-table-column prop="error" label="错误" />
        </el-table>
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

const cards = computed(() => [
  { label: '总数',  value: r.value.total   ?? 0, color: '#6366f1' },
  { label: '成功',  value: r.value.success ?? 0, color: '#10b981' },
  { label: '失败',  value: r.value.failed  ?? 0, color: '#ef4444',
    tip: r.value.scan_only ? '仅扫描模式（未上传）' : (r.value.jellyfin_refreshed ? '已通知 Jellyfin 刷新' : '') },
  { label: '跳过',  value: r.value.skipped ?? 0, color: '#94a3b8' },
])

const counts = computed(() => ({
  success:   details.value.filter(d => d.status === 'success').length,
  not_found: details.value.filter(d => d.status === 'not_found').length,
  no_image:  details.value.filter(d => d.status === 'no_image').length,
  failed:    details.value.filter(d => d.status === 'failed').length,
}))
const groups = computed(() => ({
  success:   details.value.filter(d => d.status === 'success'),
  not_found: details.value.filter(d => d.status === 'not_found'),
  no_image:  details.value.filter(d => d.status === 'no_image'),
  failed:    details.value.filter(d => d.status === 'failed'),
}))

const activeTab = ref(
  counts.value.failed ? 'failed'
  : counts.value.not_found ? 'not_found'
  : counts.value.no_image ? 'no_image'
  : 'success'
)
</script>

<style scoped>
.muted { color: #94a3b8; }
</style>
