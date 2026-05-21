// 全局 debug 状态（事后删）：跨组件共享
// Trending.vue 写入；App.vue 侧边栏读取展示。
import { reactive } from 'vue'

export const debugInfo = reactive({
  enabled: false,         // 当前页面是否在用 debug（侧边栏只在 enabled=true 时显示）
  source: '',
  cols: 0,
  scrollRow: 0,
  totalRows: 0,
  items: 0,
  wanted: 0,
})
