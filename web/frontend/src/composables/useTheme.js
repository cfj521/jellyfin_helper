import { ref, watch } from 'vue'

const STORAGE_KEY = 'jt-theme'

const THEMES = [
  { key: 'default', name: '浅色', color: '#6366f1' },
  { key: 'dark',    name: '深色', color: '#334155' },
  { key: 'emerald', name: '翡翠', color: '#10b981' },
]

const current = ref(localStorage.getItem(STORAGE_KEY) || 'default')

function apply(theme) {
  document.documentElement.setAttribute('data-theme', theme)
}

apply(current.value)

watch(current, (val) => {
  apply(val)
  localStorage.setItem(STORAGE_KEY, val)
})

export function useTheme() {
  return {
    themes: THEMES,
    current,
    setTheme(key) {
      current.value = key
    },
  }
}
