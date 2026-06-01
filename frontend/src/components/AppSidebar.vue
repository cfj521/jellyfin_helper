<template>
  <div class="app-sidebar">
    <div class="logo">
      <el-icon :size="28"><MagicStick /></el-icon>
      <div class="logo-text">
        <span class="logo-title">Jellyfin Helper</span>
        <span class="logo-version">v{{ appVersion }}</span>
      </div>
    </div>

    <el-menu
      :default-active="$route.path"
      router
      class="app-menu"
    >
      <el-menu-item index="/medialibraries">
        <el-icon><Collection /></el-icon>
        <span>媒体库</span>
      </el-menu-item>

      <el-menu-item index="/discover">
        <el-icon><TrendCharts /></el-icon>
        <span>热门推荐</span>
      </el-menu-item>

      <el-menu-item index="/resourcesearch">
        <el-icon><Search /></el-icon>
        <span>资源搜索</span>
      </el-menu-item>

      <el-menu-item index="/downloadpipeline">
        <el-icon><Download /></el-icon>
        <span>下载流水线</span>
      </el-menu-item>

      <el-menu-item index="/tasks" class="tasks-menu-item">
        <el-icon><List /></el-icon>
        <span>任务管理</span>
      </el-menu-item>

      <el-menu-item index="/settings" class="settings-menu-item">
        <el-icon><Setting /></el-icon>
        <span>设置</span>
      </el-menu-item>
    </el-menu>

    <!-- 主题切换 + 用户 -->
    <div class="sidebar-footer">
      <div class="theme-picker">
        <el-tooltip
          v-for="t in themes"
          :key="t.key"
          :content="t.name"
          placement="top"
          :show-after="300"
        >
          <div
            class="theme-dot"
            :class="{ active: currentTheme === t.key }"
            :style="{ '--dot-color': t.color }"
            @click="setTheme(t.key)"
          />
        </el-tooltip>
      </div>
      <div class="user-bar" v-if="currentUser">
        <el-tooltip content="退出登录" placement="top" :show-after="300">
          <!-- 自定义 logout 图标（门 + 右箭头），lucide-vue 风格 inline SVG -->
          <el-icon class="logout-btn" @click="handleLogout">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
              <polyline points="16 17 21 12 16 7" />
              <line x1="21" y1="12" x2="9" y2="12" />
            </svg>
          </el-icon>
        </el-tooltip>
        <span class="user-name">{{ currentUser.username }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useTheme } from '@/composables/useTheme'

const router = useRouter()
const { themes, current: currentTheme, setTheme } = useTheme()

const appVersion = __APP_VERSION__

const currentUser = computed(() => {
  try { return JSON.parse(localStorage.getItem('user') || 'null') } catch { return null }
})

function handleLogout() {
  localStorage.removeItem('token')
  localStorage.removeItem('user')
  router.push('/login')
}
</script>

<style lang="scss" scoped>
// sidebar 内部布局；容器宽度/背景由父（el-aside / el-drawer 包装）决定
.app-sidebar {
  height: 100%;
  display: flex;
  flex-direction: column;
}
</style>
