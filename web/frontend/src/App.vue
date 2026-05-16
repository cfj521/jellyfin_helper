<template>
  <el-config-provider :locale="zhCn">
    <el-container class="app-container">
      <!-- 侧边栏 -->
      <el-aside width="220px" class="app-aside">
        <div class="logo">
          <el-icon :size="28"><MagicStick /></el-icon>
          <span>Jellyfin Helper</span>
        </div>

        <el-menu
          :default-active="$route.path"
          router
          class="app-menu"
        >
          <el-menu-item index="/tasks">
            <el-icon><List /></el-icon>
            <span>任务管理</span>
          </el-menu-item>

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

          <!-- 配置入口：放在菜单末尾，视觉上与导航项分开 -->
          <el-menu-item index="/settings" class="settings-menu-item">
            <el-icon><Setting /></el-icon>
            <span>设置</span>
          </el-menu-item>
        </el-menu>

        <!-- 主题切换 -->
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
      </el-aside>

      <!-- 主内容区（无 header，主内容直接占满）-->
      <!-- keep-alive include：列出来的组件 name 在切换其他页面时不卸载，状态保留。
           Downloads 不能进，因为它的轮询 timer 用 onMounted/onUnmounted 管理。
           Search 是首选 —— 用户来回切不应该丢搜索结果和分页位置。 -->
      <el-main class="app-main">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <keep-alive :include="['TorrentSearch', 'Trending']">
              <component :is="Component" />
            </keep-alive>
          </transition>
        </router-view>

        <!-- 回到顶部：滚动 .app-main（全站统一滚动容器）超过 200px 显示，hover 出 tooltip -->
        <el-tooltip content="回到顶部" placement="left" :show-after="300">
          <el-backtop target=".app-main" :visibility-height="200" :right="40" :bottom="40" />
        </el-tooltip>
      </el-main>
    </el-container>
  </el-config-provider>
</template>

<script setup>
import zhCn from 'element-plus/dist/locale/zh-cn.mjs'
import { useTheme } from '@/composables/useTheme'

const { themes, current: currentTheme, setTheme } = useTheme()
</script>

<style lang="scss">
@use '@/styles/theme.scss';

// ============ 容器 ============
.app-container {
  height: 100vh;
}

// ============ 侧边栏 ============
.app-aside {
  background: var(--jt-sidebar-bg);
  display: flex;
  flex-direction: column;
  box-shadow: 2px 0 8px rgba(0, 0, 0, 0.08);
}

.logo {
  height: 64px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: #fff;
  font-size: 17px;
  font-weight: 600;
  letter-spacing: 0.3px;
  background: linear-gradient(135deg, var(--jt-brand) 0%, var(--jt-accent) 100%);
  position: relative;

  span {
    white-space: nowrap;
  }
}

// ============ 菜单 ============
.app-menu {
  flex: 1;
  border-right: none;
  background: transparent;
  overflow-y: auto;
  padding: 8px 0;

  .el-menu-item,
  .el-sub-menu__title {
    color: var(--jt-sidebar-text);
    background-color: transparent !important;
    height: 44px;
    line-height: 44px;
    margin: 2px 8px;
    border-radius: 6px;
    padding-left: 16px !important;
    transition: all 0.2s;

    .el-icon {
      color: var(--jt-sidebar-text-muted);
      transition: color 0.2s;
    }

    &:hover {
      background-color: var(--jt-sidebar-bg-hover) !important;
      color: #fff;

      .el-icon {
        color: var(--jt-brand-soft);
      }
    }
  }

  // 选中态
  .el-menu-item.is-active {
    color: var(--jt-sidebar-text-active) !important;
    background: linear-gradient(90deg, rgba(var(--jt-brand-rgb), 0.28) 0%, rgba(var(--jt-brand-rgb), 0.08) 100%) !important;
    position: relative;

    &::before {
      content: '';
      position: absolute;
      left: 0;
      top: 6px;
      bottom: 6px;
      width: 3px;
      background: var(--jt-brand);
      border-radius: 0 2px 2px 0;
    }

    .el-icon {
      color: var(--jt-brand-soft);
    }
  }

  .el-sub-menu.is-active > .el-sub-menu__title {
    color: var(--jt-sidebar-text-active);

    .el-icon {
      color: var(--jt-brand-soft);
    }
  }

  .el-menu--inline {
    background: var(--jt-sidebar-bg-deep) !important;
    padding: 4px 0;

    .el-menu-item {
      background-color: transparent !important;
      padding-left: 48px !important;
      font-size: 13px;
      height: 38px;
      line-height: 38px;
      color: var(--jt-sidebar-text-muted);

      &:hover {
        background-color: var(--jt-sidebar-bg-hover) !important;
        color: #fff;
      }

      &.is-active {
        color: var(--jt-sidebar-text-active) !important;
        background: linear-gradient(90deg, rgba(var(--jt-brand-rgb), 0.22) 0%, rgba(var(--jt-brand-rgb), 0.06) 100%) !important;
      }
    }
  }

  .settings-menu-item {
    margin-top: 16px !important;
    position: relative;

    &::after {
      content: '';
      position: absolute;
      top: -10px;
      left: 16px;
      right: 16px;
      height: 1px;
      background: var(--jt-sidebar-bg-hover);
    }
  }
}

// ============ 主题切换器 ============
.theme-picker {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 14px 16px;
  border-top: 1px solid var(--jt-sidebar-divider);
}

.theme-dot {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--dot-color);
  cursor: pointer;
  border: 2px solid rgba(255, 255, 255, 0.2);
  transition: all 0.2s ease;
  position: relative;

  &:hover {
    transform: scale(1.2);
    border-color: rgba(255, 255, 255, 0.5);
  }

  &.active {
    border-color: rgba(255, 255, 255, 0.7);
    box-shadow: 0 0 0 3px var(--dot-color), 0 0 8px var(--dot-color);
    transform: scale(1.15);
  }
}

// ============ 主内容 ============
.app-main {
  background: var(--jt-content-bg);
  padding: 20px;
}

// ============ 路由切换动画 ============
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
