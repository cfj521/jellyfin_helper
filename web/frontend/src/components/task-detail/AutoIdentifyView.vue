<template>
  <div>
    <StatCards :cards="cards" />

    <el-tabs v-model="activeTab">
      <el-tab-pane :label="`已修复 (${groups.applied.length})`" name="applied">
        <el-empty v-if="!groups.applied.length" description="无" />
        <el-table v-else :data="groups.applied" stripe size="small">
          <el-table-column label="原路径 / 名称" min-width="240">
            <template #default="{ row }">
              <div class="item-cell">
                <div class="item-name">{{ row.item_name }}</div>
                <div class="item-path muted">{{ row.path }}</div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="提取标题/年份" width="200">
            <template #default="{ row }">
              <span>{{ row.extracted_title }}</span>
              <span v-if="row.extracted_year" class="muted"> ({{ row.extracted_year }})</span>
            </template>
          </el-table-column>
          <el-table-column label="TMDB 匹配" min-width="280">
            <template #default="{ row }">
              <div v-if="row.first_candidate" class="cand-cell">
                <el-image
                  v-if="row.first_candidate.image_url"
                  :src="row.first_candidate.image_url"
                  :preview-src-list="[row.first_candidate.image_url]"
                  style="width: 36px; height: 54px"
                  fit="cover"
                />
                <div class="cand-text">
                  <div>
                    <strong>{{ row.first_candidate.name }}</strong>
                    <span v-if="row.first_candidate.year" class="muted">
                      ({{ row.first_candidate.year }})
                    </span>
                  </div>
                  <el-link
                    v-if="row.first_candidate.tmdb_id"
                    :href="tmdbUrl(row.item_type, row.first_candidate.tmdb_id)"
                    target="_blank"
                    type="primary"
                  >
                    TMDB #{{ row.first_candidate.tmdb_id }}
                  </el-link>
                </div>
              </div>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane :label="`未匹配 (${groups.no_match.length})`" name="no_match">
        <el-empty v-if="!groups.no_match.length" description="无" />
        <el-table v-else :data="groups.no_match" stripe size="small">
          <el-table-column label="原路径 / 名称" min-width="320">
            <template #default="{ row }">
              <div class="item-cell">
                <div class="item-name">{{ row.item_name }}</div>
                <div class="item-path muted">{{ row.path }}</div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="提取标题/年份" width="180">
            <template #default="{ row }">
              <span v-if="row.extracted_title">{{ row.extracted_title }}</span>
              <span v-else class="muted">—</span>
              <span v-if="row.extracted_year" class="muted"> ({{ row.extracted_year }})</span>
            </template>
          </el-table-column>
          <el-table-column prop="error" label="原因" />
        </el-table>
      </el-tab-pane>

      <el-tab-pane v-if="groups.failed.length" :label="`失败 (${groups.failed.length})`" name="failed">
        <el-table :data="groups.failed" stripe size="small">
          <el-table-column label="项目" min-width="280">
            <template #default="{ row }">{{ row.item_name }}</template>
          </el-table-column>
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
  { label: '扫描数',     value: r.value.scanned            ?? 0, color: '#6366f1' },
  { label: '未识别数',   value: r.value.unrecognized_count ?? 0, color: '#f59e0b' },
  { label: '已修复',     value: r.value.fixed_count        ?? 0, color: '#10b981',
    tip: r.value.dry_run ? '预览模式' : '' },
  { label: '未匹配',     value: r.value.no_match_count     ?? 0, color: '#ef4444' },
])

const groups = computed(() => ({
  applied:  details.value.filter(d => d.applied || d.error === '(预览模式)' && d.first_candidate),
  no_match: details.value.filter(d => !d.applied && d.error && d.error !== '(预览模式)' && d.error.includes('未找到')),
  failed:   details.value.filter(d => !d.applied && d.error && d.error !== '(预览模式)' && !d.error.includes('未找到')),
}))

const tmdbUrl = (type, tmdbId) => {
  const base = type === 'Series' ? 'tv' : 'movie'
  return `https://www.themoviedb.org/${base}/${tmdbId}`
}

const activeTab = ref(groups.value.applied.length ? 'applied' : 'no_match')
</script>

<style lang="scss" scoped>
.item-cell {
  .item-name { font-weight: 500; word-break: break-all; }
  .item-path { font-size: 12px; margin-top: 2px; word-break: break-all; }
}
.cand-cell {
  display: flex;
  align-items: flex-start;
  gap: 8px;

  .cand-text > div { line-height: 1.4; }
}
.muted { color: #94a3b8; font-size: 12px; }
</style>
