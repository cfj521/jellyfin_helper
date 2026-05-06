<template>
  <div class="generic-json">
    <el-alert
      v-if="task.status === 'failed'"
      type="error"
      :closable="false"
      show-icon
      :title="task.result?.error || '任务执行失败'"
      style="margin-bottom: 16px"
    />
    <el-alert
      v-else
      type="info"
      :closable="false"
      show-icon
      title="该任务类型暂无定制详情视图，下方为原始结果数据"
      style="margin-bottom: 16px"
    />
    <pre class="json-block">{{ jsonText }}</pre>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  task: { type: Object, required: true },
})

const jsonText = computed(() => {
  if (!props.task.result) return '(无结果数据)'
  try {
    return JSON.stringify(props.task.result, null, 2)
  } catch {
    return String(props.task.result)
  }
})
</script>

<style lang="scss" scoped>
.json-block {
  background: #f8fafc;
  padding: 16px;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  overflow: auto;
  max-height: 600px;
  font-size: 12px;
  font-family: 'Consolas', 'Monaco', monospace;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
