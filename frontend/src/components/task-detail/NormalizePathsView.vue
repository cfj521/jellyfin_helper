<template>
  <div>
    <StatCards :cards="cards" />

    <el-tabs v-model="activeTab">
      <el-tab-pane :label="`移动操作 (${moved.length})`" name="moved">
        <el-empty v-if="!moved.length" description="无需要移动的文件" />
        <template v-else>
          <el-table :data="pagedMoved" stripe size="small">
            <el-table-column label="原位置" min-width="320">
              <template #default="{ row }">
                <div class="from-cell">
                  <span class="path">{{ row.from || row.original_path }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="移动到" min-width="320">
              <template #default="{ row }">
                <div class="to-cell">
                  <el-icon class="arrow"><Right /></el-icon>
                  <span class="path">{{ row.to || row.new_path }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="结果" width="100">
              <template #default="{ row }">
                <el-tag v-if="row.error === '(预览模式)'" size="small" type="info">预览</el-tag>
                <el-tag v-else-if="row.moved || row.success" size="small" type="success">✓ 已移动</el-tag>
                <el-tag v-else size="small" type="danger">✗ 失败</el-tag>
              </template>
            </el-table-column>
          </el-table>
          <el-pagination
            v-if="moved.length > pageSize"
            v-model:current-page="movedPage"
            :page-size="pageSize"
            :total="moved.length"
            layout="total, prev, pager, next"
            class="pager"
          />
        </template>
      </el-tab-pane>

      <el-tab-pane v-if="failed.length" :label="`失败 (${failed.length})`" name="failed">
        <template v-if="failed.length">
          <el-table :data="pagedFailed" stripe size="small">
            <el-table-column label="文件" min-width="320">
              <template #default="{ row }">
                <span class="path">{{ row.from || row.original_path }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="error" label="错误信息" />
          </el-table>
          <el-pagination
            v-if="failed.length > pageSize"
            v-model:current-page="failedPage"
            :page-size="pageSize"
            :total="failed.length"
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
import { Right } from '@element-plus/icons-vue'
import StatCards from './StatCards.vue'

const props = defineProps({ task: { type: Object, required: true } })
const r = computed(() => props.task.result || {})
const details = computed(() => r.value.details || [])

const moved = computed(() =>
  details.value.filter(d => !d.error || d.error === '(预览模式)' || d.moved || d.success)
)
const failed = computed(() =>
  details.value.filter(d => d.error && d.error !== '(预览模式)' && !d.moved && !d.success)
)

const pageSize = 100
const movedPage = ref(1)
const failedPage = ref(1)

const pagedMoved = computed(() => {
  const s = (movedPage.value - 1) * pageSize
  return moved.value.slice(s, s + pageSize)
})
const pagedFailed = computed(() => {
  const s = (failedPage.value - 1) * pageSize
  return failed.value.slice(s, s + pageSize)
})

const cards = computed(() => [
  { label: '扫描数',     value: r.value.scanned       ?? 0, color: '#6366f1' },
  { label: '嵌套文件',   value: r.value.nested_count  ?? 0, color: '#f59e0b' },
  { label: '已移动',     value: r.value.moved_count   ?? 0, color: '#10b981',
    tip: r.value.dry_run ? '预览模式' : '' },
  { label: '跳过',       value: r.value.skipped_count ?? 0, color: '#94a3b8' },
])

const activeTab = ref('moved')
</script>

<style lang="scss" scoped>
.path { word-break: break-all; font-family: 'Consolas', monospace; font-size: 12px; }
.from-cell, .to-cell { display: flex; align-items: center; gap: 6px; }
.arrow { color: var(--jt-success); flex-shrink: 0; }
.pager {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
}
</style>
