<template>
  <div>
    <StatCards :cards="cards" />

    <el-tabs v-model="activeTab">
      <el-tab-pane :label="`移动操作 (${moved.length})`" name="moved">
        <el-empty v-if="!moved.length" description="无需要移动的文件" />
        <el-table v-else :data="moved" stripe size="small">
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
      </el-tab-pane>

      <el-tab-pane v-if="failed.length" :label="`失败 (${failed.length})`" name="failed">
        <el-table :data="failed" stripe size="small">
          <el-table-column label="文件" min-width="320">
            <template #default="{ row }">
              <span class="path">{{ row.from || row.original_path }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="error" label="错误信息" />
        </el-table>
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
.arrow { color: #10b981; flex-shrink: 0; }
</style>
