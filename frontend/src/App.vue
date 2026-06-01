<template>
  <el-config-provider :locale="zhCn">
    <!-- 登录页：无侧边栏布局 -->
    <router-view v-if="$route.meta.public" />

    <!-- 正常布局 -->
    <el-container v-else class="app-container">
      <!-- Mobile 顶栏：≤768px 显示 -->
      <header class="mobile-topbar">
        <el-icon class="hamburger" :size="26" @click="drawerOpen = true">
          <Expand />
        </el-icon>
        <span class="mobile-title">Jellyfin Helper</span>
      </header>

      <el-container class="app-body">
        <!-- Desktop 侧栏：>768px 显示 -->
        <el-aside width="180px" class="app-aside desktop-only">
          <AppSidebar />
        </el-aside>

        <!-- Mobile drawer：≤768px 用，hamburger 打开 -->
        <el-drawer
          v-model="drawerOpen"
          direction="ltr"
          :with-header="false"
          size="240px"
          class="mobile-drawer-wrap"
        >
          <AppSidebar />
        </el-drawer>

        <!-- 主内容区（keep-alive include 的组件切换时不卸载，状态保留） -->
        <el-main class="app-main">
          <router-view v-slot="{ Component }">
            <transition name="fade" mode="out-in">
              <keep-alive :include="['TorrentSearch', 'Trending']">
                <component :is="Component" />
              </keep-alive>
            </transition>
          </router-view>

          <!-- 回到顶部：滚动 .app-main（全站统一滚动容器）超过 200px 显示 -->
          <el-tooltip content="回到顶部" placement="left" :show-after="300">
            <el-backtop target=".app-main" :visibility-height="200" :right="40" :bottom="40" />
          </el-tooltip>
        </el-main>
      </el-container>
    </el-container>
  </el-config-provider>
</template>

<script setup>
import { ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import zhCn from 'element-plus/dist/locale/zh-cn.mjs'
import AppSidebar from '@/components/AppSidebar.vue'

const route = useRoute()
const drawerOpen = ref(false)

// 切换路由时自动关 mobile drawer（避免点完菜单 drawer 还遮挡）
watch(() => route.path, () => {
  drawerOpen.value = false
})
</script>

<style lang="scss">
@use '@/styles/theme.scss';

// ============ 容器 ============
.app-container {
  height: 100vh;
  flex-direction: column;
}

.app-body {
  flex: 1;
  min-height: 0;   // 让内部 flex 子元素能正确滚动
}

// ============ Mobile topbar（默认隐藏） ============
.mobile-topbar {
  display: none;
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
  background: var(--jt-logo-bg, linear-gradient(135deg, var(--jt-brand) 0%, var(--jt-accent) 100%));
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

  .tasks-menu-item {
    margin-top: 16px !important;
    position: relative;

    &::before {
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

// ============ 侧边栏底部 ============
.sidebar-footer {
  border-top: 1px solid var(--jt-sidebar-divider);
}

.theme-picker {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 12px 16px 6px;
}

.user-bar {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 8px 16px 14px;
  color: var(--jt-sidebar-text-muted);
  font-size: 13px;

  .user-name {
    opacity: 0.8;
  }

  .logout-btn {
    cursor: pointer;
    font-size: 20px;
    opacity: 0.75;
    transition: all 0.2s;

    &:hover {
      opacity: 1;
      color: #f87171;
    }
  }
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

// ============ Mobile drawer 容器样式 ============
// 让 drawer 内部塞下 AppSidebar 时跟桌面 aside 视觉一致
.el-drawer.mobile-drawer-wrap {
  background: var(--jt-sidebar-bg);

  .el-drawer__body {
    padding: 0;
    overflow: hidden;
  }
}

// ============ 主内容区 ============
.app-main {
  background: var(--jt-content-bg, #f5f7fa);
  padding: 20px;
  overflow-y: auto;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

// ============ Mobile 响应式切换 ============
@media (max-width: 768px) {
  // Desktop sidebar 隐藏
  .desktop-only {
    display: none !important;
  }

  // Mobile topbar 显示
  .mobile-topbar {
    display: flex;
    align-items: center;
    gap: 12px;
    height: 52px;
    padding: 0 16px;
    background: var(--jt-logo-bg, linear-gradient(135deg, var(--jt-brand) 0%, var(--jt-accent) 100%));
    color: #fff;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.12);
    position: sticky;
    top: 0;
    z-index: 100;
    flex-shrink: 0;

    .hamburger {
      cursor: pointer;
      padding: 6px;
      border-radius: 4px;
      transition: background 0.15s;

      &:active {
        background: rgba(255, 255, 255, 0.15);
      }
    }

    .mobile-title {
      font-size: 16px;
      font-weight: 600;
      letter-spacing: 0.3px;
    }
  }

  // 主内容区 padding 减小，给小屏幕让路
  .app-main {
    padding: 12px 10px;
  }
}
</style>
