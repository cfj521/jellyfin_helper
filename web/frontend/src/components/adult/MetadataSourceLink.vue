<template>
  <a
    v-if="url"
    :href="url"
    target="_blank"
    rel="noopener"
    class="meta-link"
    :title="`在 ${item.source} 查看`"
    @click.stop
  >
    <el-icon><Link /></el-icon>
    {{ item.source }}
    <el-icon class="ext-icon"><Top /></el-icon>
  </a>
</template>

<script setup>
import { computed } from 'vue'
import { Link, Top } from '@element-plus/icons-vue'

const props = defineProps({
  item: { type: Object, required: true },
})

// 按 source 字段拼站点 URL；找不到就不渲染
const SOURCE_URLS = {
  javbus:  (code) => `https://www.javbus.com/${code}`,
  javdb:   (code) => `https://javdb.com/search?q=${encodeURIComponent(code)}&f=video`,
  missav:  (code) => `https://missav.ai/cn/${code}`,
  avbase:  (code) => `https://www.avbase.net/works?q=${encodeURIComponent(code)}`,
  javlibrary: (code) => `https://www.javlibrary.com/cn/vl_searchbyid.php?keyword=${encodeURIComponent(code)}`,
}

const url = computed(() => {
  const fn = SOURCE_URLS[props.item.source]
  return fn && props.item.code ? fn(props.item.code) : null
})
</script>

<style lang="scss" scoped>
.meta-link {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 12px;
  color: #6366f1;
  text-decoration: none;
  padding: 2px 6px;
  border-radius: 3px;
  transition: background 0.15s;

  &:hover {
    background: rgba(99, 102, 241, 0.1);
  }

  .ext-icon {
    font-size: 10px;
    transform: rotate(45deg);
    opacity: 0.7;
  }
}
</style>
