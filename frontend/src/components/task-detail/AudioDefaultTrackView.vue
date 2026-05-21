<template>
  <div>
    <StatCards :cards="cards" />

    <el-tabs v-model="activeTab">
      <el-tab-pane v-if="apply.executed" :label="`应用结果 (${appliedDetails.length})`" name="applied">
        <el-empty v-if="!appliedDetails.length" description="无" />
        <template v-else>
        <el-table :data="pagedApplied" stripe size="small">
          <el-table-column label="文件" min-width="240">
            <template #default="{ row }">
              <span class="path">{{ row.name || row.path }}</span>
            </template>
          </el-table-column>
          <el-table-column label="目标轨" width="80">
            <template #default="{ row }">#{{ row.target_track }}</template>
          </el-table-column>
          <el-table-column label="目标语言" width="100">
            <template #default="{ row }">
              <el-tag size="small">{{ langLabel(row.target_lang) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="结果" width="100">
            <template #default="{ row }">
              <el-tag :type="row.success ? 'success' : 'danger'" size="small">
                {{ row.success ? '✓ 已写入' : '✗ 失败' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="error" label="错误信息" />
        </el-table>
        <el-pagination
          v-if="appliedDetails.length > pageSize"
          v-model:current-page="appliedPage"
          :page-size="pageSize"
          :total="appliedDetails.length"
          layout="total, prev, pager, next"
          class="pager"
        />
        </template>
      </el-tab-pane>

      <el-tab-pane :label="`候选列表 (${candidates.length})`" name="candidates">
        <div v-if="!apply.executed && candidates.length" class="candidate-tip">
          <el-icon><InfoFilled /></el-icon>
          预览模式：以下是按当前规则建议修改默认音轨的文件，但未实际写入
        </div>
        <el-empty v-if="!candidates.length" description="所有文件默认音轨都已正确" />
        <template v-else>
        <el-table :data="pagedCandidates" stripe size="small">
          <el-table-column label="文件" min-width="220">
            <template #default="{ row }">
              <span class="path">{{ row.name }}</span>
            </template>
          </el-table-column>
          <el-table-column label="当前默认" width="120">
            <template #default="{ row }">
              <span>#{{ row.current_default_track }}</span>
              <el-tag v-if="row.current_default_lang" size="small" type="info" style="margin-left: 4px">
                {{ langLabel(row.current_default_lang) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="目标默认" width="120">
            <template #default="{ row }">
              <span>#{{ row.target_track }}</span>
              <el-tag size="small" type="warning" style="margin-left: 4px">
                {{ langLabel(row.target_lang) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="所有音轨" min-width="220">
            <template #default="{ row }">
              <el-tag
                v-for="t in row.tracks"
                :key="t.mkv_track_id"
                size="small"
                :type="t.mkv_track_id === row.target_track ? 'success' : (t.is_default ? 'warning' : 'info')"
                style="margin-right: 4px"
              >
                #{{ t.mkv_track_id }} {{ langLabel(t.lang_code) || t.language || '?' }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
        <el-pagination
          v-if="candidates.length > pageSize"
          v-model:current-page="candidatesPage"
          :page-size="pageSize"
          :total="candidates.length"
          layout="total, prev, pager, next"
          class="pager"
        />
        </template>
      </el-tab-pane>

      <el-tab-pane :label="`跳过文件 (${skipped.length})`" name="skipped">
        <el-empty v-if="!skipped.length" description="无" />
        <template v-else>
        <el-table :data="pagedSkipped" stripe size="small">
          <el-table-column label="文件" min-width="280">
            <template #default="{ row }">{{ row.name || row.path }}</template>
          </el-table-column>
          <el-table-column prop="reason" label="跳过原因" />
        </el-table>
        <el-pagination
          v-if="skipped.length > pageSize"
          v-model:current-page="skippedPage"
          :page-size="pageSize"
          :total="skipped.length"
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
import { InfoFilled } from '@element-plus/icons-vue'
import StatCards from './StatCards.vue'

const props = defineProps({ task: { type: Object, required: true } })
const r = computed(() => props.task.result || {})
const scan = computed(() => r.value.scan || {})
const apply = computed(() => r.value.apply || {})
const candidates = computed(() => r.value.candidates || [])
const appliedDetails = computed(() => r.value.applied_details || [])
const skipped = computed(() => r.value.skipped_sample || [])

const cards = computed(() => [
  { label: '扫描总数', value: scan.value.total      ?? 0, color: '#6366f1' },
  { label: '已正确',   value: scan.value.already_ok ?? 0, color: '#10b981' },
  { label: '需修改',   value: scan.value.candidates ?? 0, color: '#f59e0b' },
  apply.value.executed
    ? { label: '已写入', value: apply.value.success ?? 0, color: '#10b981',
        tip: apply.value.failed ? `${apply.value.failed} 个失败` : '' }
    : { label: '跳过',   value: scan.value.skipped  ?? 0, color: '#94a3b8',
        tip: '预览模式（未写入）' },
])

const pageSize = 100
const appliedPage = ref(1)
const candidatesPage = ref(1)
const skippedPage = ref(1)

const pagedApplied = computed(() => {
  const s = (appliedPage.value - 1) * pageSize
  return appliedDetails.value.slice(s, s + pageSize)
})
const pagedCandidates = computed(() => {
  const s = (candidatesPage.value - 1) * pageSize
  return candidates.value.slice(s, s + pageSize)
})
const pagedSkipped = computed(() => {
  const s = (skippedPage.value - 1) * pageSize
  return skipped.value.slice(s, s + pageSize)
})

const LANG_LABEL = {
  chs: '简中', cht: '繁中', yue: '粤语', eng: '英语', jpn: '日语', kor: '韩语',
}
const langLabel = (code) => LANG_LABEL[code] || code || ''

const activeTab = ref(apply.value.executed ? 'applied' : 'candidates')
</script>

<style lang="scss" scoped>
.candidate-tip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  background: var(--jt-warning-tint);
  color: var(--jt-warning-text);
  font-size: 12px;
  border-radius: 4px;
  margin-bottom: 12px;
}
.path { word-break: break-all; }
.pager {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
}
</style>
