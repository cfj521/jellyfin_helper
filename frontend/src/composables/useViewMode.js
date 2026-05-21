/**
 * 视图模式持久化 composable。
 * 使用：
 *   const viewMode = useViewMode('library-detail', 'list')
 *   // 自动从 localStorage 读旧值；改 v-model 立即写回
 */
import { ref, watch } from 'vue'

const STORAGE_KEY_PREFIX = 'viewMode:'
const VALID_MODES = new Set(['grid', 'list'])

export function useViewMode(scopeKey, defaultMode = 'list') {
  const storageKey = STORAGE_KEY_PREFIX + scopeKey

  let initial = defaultMode
  try {
    const saved = localStorage.getItem(storageKey)
    if (saved && VALID_MODES.has(saved)) initial = saved
  } catch {
    // localStorage 在某些环境（隐私模式 / iframe）不可用，忽略
  }

  const mode = ref(initial)

  watch(mode, (v) => {
    if (!VALID_MODES.has(v)) return
    try {
      localStorage.setItem(storageKey, v)
    } catch {}
  })

  return mode
}
