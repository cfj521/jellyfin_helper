<template>
  <component
    :is="item.jellyfin_url ? 'a' : 'div'"
    :href="item.jellyfin_url || undefined"
    :target="item.jellyfin_url ? '_blank' : undefined"
    rel="noopener"
    class="title-cell"
    :class="{ clickable: !!item.jellyfin_url }"
    :title="item.title || item.file_name || item.code || ''"
    @click.stop
  >
    <div class="code-line">
      <span v-if="item.code" class="code">{{ item.code }}</span>
      <span v-else class="code-missing">未识别</span>
    </div>
    <div class="title-line">
      <span v-if="item.title">{{ item.title }}</span>
      <span v-else class="muted">{{ item.file_name || '—' }}</span>
    </div>
    <div
      v-if="item.source_name"
      class="source-line"
      :title="item.source_name"
    >{{ item.source_name }}</div>
  </component>
</template>

<script setup>
defineProps({
  item: { type: Object, required: true },
})
</script>

<style lang="scss" scoped>
.title-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  text-decoration: none;
  color: inherit;

  &.clickable {
    cursor: pointer;
    .title-line {
      transition: color 0.15s;
    }
    &:hover .title-line {
      color: var(--jt-brand);
    }
    &:hover .code {
      color: var(--jt-brand-dark);
    }
  }

  .code-line {
    font-size: 14px;
    line-height: 1.3;
    margin-bottom: 2px;

    .code {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-weight: 600;
      color: #0369a1;
    }
    .code-missing {
      color: #dc2626;
      font-weight: 500;
    }
  }

  .title-line {
    font-size: 13px;
    line-height: 1.4;
    color: var(--jt-text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;

    .muted { color: var(--jt-text-muted); }
  }

  .source-line {
    font-size: 11px;
    line-height: 1.3;
    color: var(--jt-text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
}
</style>
