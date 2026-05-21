<template>
  <div class="tag-chips" v-if="tags.length">
    <el-tag
      v-for="t in visibleTags"
      :key="t"
      size="small"
      effect="plain"
      type="info"
      class="chip"
    >{{ t }}</el-tag>
    <el-popover
      v-if="hiddenCount > 0"
      placement="top"
      :width="320"
      trigger="click"
    >
      <template #reference>
        <el-tag size="small" type="info" effect="plain" class="chip more-chip" @click.stop>
          +{{ hiddenCount }}
        </el-tag>
      </template>
      <div class="popover-tags">
        <el-tag
          v-for="t in tags"
          :key="t"
          size="small"
          effect="plain"
          type="info"
          class="chip"
        >{{ t }}</el-tag>
      </div>
    </el-popover>
  </div>
  <span v-else class="muted">—</span>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  tags: { type: Array, default: () => [] },
  max: { type: Number, default: 3 },
})

const visibleTags = computed(() => props.tags.slice(0, props.max))
const hiddenCount = computed(() => Math.max(0, props.tags.length - props.max))
</script>

<style lang="scss" scoped>
.tag-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.chip {
  cursor: default;
  user-select: none;
}
.more-chip {
  cursor: pointer;
  background: var(--jt-brand-light-9);
  color: var(--jt-brand);
  &:hover { background: var(--jt-brand-light-9); }
}
.popover-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.muted { color: var(--jt-text-muted); }
</style>
