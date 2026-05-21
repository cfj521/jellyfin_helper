<template>
  <div class="actress-chips" v-if="actors.length">
    <el-tag
      v-for="name in visibleActors"
      :key="name"
      size="small"
      :type="resolvedNames.has(name) ? '' : 'info'"
      :effect="resolvedNames.has(name) ? 'light' : 'plain'"
      class="chip"
      :class="{ resolvable: resolvedNames.has(name) }"
      @click.stop="onClick(name)"
    >
      {{ displayName(name) }}
    </el-tag>

    <!-- 折叠状态下显示 "..." chip：点击展开行高 -->
    <button
      v-if="!expanded && hiddenCount > 0"
      class="chip-more"
      :title="`展开剩余 ${hiddenCount} 个`"
      @click.stop="expanded = true"
    >
      …<span class="hidden-count">+{{ hiddenCount }}</span>
    </button>

    <!-- 展开状态下显示"收起" -->
    <button
      v-if="expanded && actors.length > collapseLimit"
      class="chip-more chip-collapse"
      title="收起"
      @click.stop="expanded = false"
    >
      收起
    </button>
  </div>
  <span v-else class="muted">—</span>
</template>

<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  actors: { type: Array, default: () => [] },
  // 已知 actress map：name → { id, jp_name, zh_name, en_name } —— 由父组件批量解析后传入
  resolvedMap: { type: Object, default: () => ({}) },
  // 折叠时最多显示几个 chip；超出的折成"..."
  collapseLimit: { type: Number, default: 3 },
})
const emit = defineEmits(['open-actress'])

const expanded = ref(false)

const resolvedNames = computed(() => new Set(Object.keys(props.resolvedMap || {})))

const visibleActors = computed(() =>
  expanded.value ? props.actors : props.actors.slice(0, props.collapseLimit),
)
const hiddenCount = computed(() =>
  Math.max(0, props.actors.length - props.collapseLimit),
)

// 优先显示已知的 zh_name；没有就显示原 name（jp_name）
const displayName = (name) => {
  const r = props.resolvedMap[name]
  if (!r) return name
  return r.zh_name || r.jp_name || name
}

const onClick = (name) => {
  // 命中 actress 库 → emit 让父组件弹 Drawer；未命中也 emit，由 Drawer 提供"未解析"状态
  emit('open-actress', { name, resolved: props.resolvedMap[name] || null })
}
</script>

<style lang="scss" scoped>
.actress-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
}
.chip {
  cursor: pointer;
  transition: all 0.12s ease;
  user-select: none;

  &.resolvable:hover {
    transform: translateY(-1px);
    box-shadow: 0 2px 4px rgba(var(--jt-brand-rgb), 0.25);
  }
}

// "..." 折叠按钮：胶囊样式，跟 el-tag size=small 视觉密度一致
.chip-more {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 0 8px;
  height: 22px;
  font-size: 12px;
  color: var(--jt-brand);
  background: var(--jt-brand-light-9);
  border: 1px dashed #c7d2fe;
  border-radius: 11px;
  cursor: pointer;
  transition: all 0.12s ease;

  &:hover {
    background: #e0e7ff;
    border-style: solid;
  }

  .hidden-count {
    font-size: 11px;
    font-weight: 500;
  }
}
.chip-collapse {
  background: var(--jt-fill-light);
  color: var(--jt-text-secondary);
  border-color: var(--jt-card-border);

  &:hover {
    background: var(--jt-card-border);
  }
}

.muted { color: var(--jt-text-muted); }
</style>
