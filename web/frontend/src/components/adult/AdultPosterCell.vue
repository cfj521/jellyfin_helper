<template>
  <component
    :is="item.jellyfin_url ? 'a' : 'div'"
    :href="item.jellyfin_url || undefined"
    :target="item.jellyfin_url ? '_blank' : undefined"
    rel="noopener"
    class="adult-poster"
    :class="{ clickable: !!item.jellyfin_url }"
    :title="item.jellyfin_url ? '在 Jellyfin 中打开' : undefined"
    @click.stop
  >
    <el-image
      v-if="hasImage"
      :src="src"
      fit="cover"
      lazy
      loading="lazy"
      decoding="async"
      class="poster-img"
    >
      <template #error>
        <div class="placeholder">
          <span class="ph-code">{{ item.code || '?' }}</span>
        </div>
      </template>
    </el-image>
    <div v-else class="placeholder" :class="{ 'is-unrecognized': !item.code }">
      <span class="ph-code">{{ item.code || '未识别' }}</span>
    </div>
  </component>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  item: { type: Object, required: true },
})

// 优先用 backend 直接给的 image_url（Jellyfin 模式拼好的直链）；
// 否则走 /api/adult/items/{id}/poster 代理（adult 模式）
const hasImage = computed(() => !!(props.item.image_url || props.item.cover_url || props.item.poster_path))

// cache buster：重新识别 / 修复封面后 updated_at 会被 SQLAlchemy onupdate 自动 bump，
// URL ?v=<updated_at> 跟着变 → 浏览器/CDN 不会复用旧图
const cacheKey = computed(() => {
  const t = props.item.updated_at
  if (!t) return ''
  // ISO 字符串里冒号/连字符/点都不是 URL 安全字符，编码下
  return encodeURIComponent(t)
})
const src = computed(() => {
  if (props.item.image_url) return props.item.image_url
  if (props.item.id) {
    const base = `/api/adult/items/${props.item.id}/poster`
    return cacheKey.value ? `${base}?v=${cacheKey.value}` : base
  }
  return ''
})
</script>

<style lang="scss" scoped>
.adult-poster {
  display: block;
  width: 160px;
  aspect-ratio: 16 / 9;
  border-radius: 4px;
  overflow: hidden;
  background: var(--jt-fill-light);
  text-decoration: none;
  color: inherit;

  &.clickable {
    cursor: pointer;
    transition: transform 0.15s, box-shadow 0.15s;
    &:hover {
      transform: translateY(-1px);
      box-shadow: 0 4px 10px rgba(var(--jt-brand-rgb), 0.25);
    }
  }

  .poster-img,
  .placeholder {
    width: 100%;
    height: 100%;
  }

  .placeholder {
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--jt-fill-light);
    color: var(--jt-text-secondary);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 13px;
    font-weight: 600;
    user-select: none;

    &.is-unrecognized {
      background: var(--jt-danger-tint);
      color: var(--jt-danger-text);
    }
  }

  .ph-code {
    padding: 0 8px;
    text-align: center;
    line-height: 1.2;
  }
}
</style>
