<template>
  <el-config-provider :locale="zhCn">
    <el-container class="app-container">
      <!-- 侧边栏 -->
      <el-aside width="220px" class="app-aside">
        <div class="logo">
          <el-icon :size="28"><VideoCamera /></el-icon>
          <span>Jellyfin Tools</span>
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

        <!-- DEBUG 信息：仅在子页面写入 debugInfo 时显示（事后可删整段） -->
        <div v-if="debugInfo.enabled" class="debug-panel">
          <div class="debug-title">DEBUG · {{ debugInfo.source || '?' }}</div>
          <div class="debug-row">
            <span class="debug-key">列</span>
            <span class="debug-val">{{ debugInfo.cols }}</span>
          </div>
          <div class="debug-row">
            <span class="debug-key">滚动行</span>
            <span class="debug-val">{{ debugInfo.scrollRow }} / {{ debugInfo.totalRows }}</span>
          </div>
          <div class="debug-row">
            <span class="debug-key">items</span>
            <span class="debug-val">{{ debugInfo.items }}</span>
          </div>
          <div class="debug-row">
            <span class="debug-key">wanted</span>
            <span class="debug-val">{{ debugInfo.wanted }}</span>
          </div>
        </div>
      </el-aside>

      <!-- 主内容区（无 header，主内容直接占满）-->
      <!-- keep-alive include：列出来的组件 name 在切换其他页面时不卸载，状态保留。
           Downloads 不能进，因为它的轮询 timer 用 onMounted/onUnmounted 管理。
           Search 是首选 —— 用户来回切不应该丢搜索结果和分页位置。 -->
      <el-main class="app-main">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <keep-alive :include="['TorrentSearch']">
              <component :is="Component" />
            </keep-alive>
          </transition>
        </router-view>
      </el-main>
    </el-container>
  </el-config-provider>
</template>

<script setup>
import zhCn from 'element-plus/dist/locale/zh-cn.mjs'
import { debugInfo } from '@/composables/useDebugInfo'
</script>

<style lang="scss">
@use '@/styles/theme.scss' as *;

// ============ 容器 ============
.app-container {
  height: 100vh;
}

// ============ 侧边栏 ============
.app-aside {
  background: $sidebar-bg;
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
  background: linear-gradient(135deg, $brand 0%, $accent-purple 100%);
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

  // 主项 + 子菜单标题
  .el-menu-item,
  .el-sub-menu__title {
    color: $sidebar-text;
    background-color: transparent !important;
    height: 44px;
    line-height: 44px;
    margin: 2px 8px;
    border-radius: 6px;
    padding-left: 16px !important;
    transition: all 0.2s;

    .el-icon {
      color: $sidebar-text-muted;
      transition: color 0.2s;
    }

    &:hover {
      background-color: $sidebar-bg-hover !important;
      color: #fff;

      .el-icon {
        color: $brand-soft;
      }
    }
  }

  // 选中态：渐变背景 + 左侧蓝紫高亮条
  .el-menu-item.is-active {
    color: $sidebar-text-active !important;
    background: linear-gradient(90deg, rgba($brand, 0.28) 0%, rgba($brand, 0.08) 100%) !important;
    position: relative;

    &::before {
      content: '';
      position: absolute;
      left: 0;
      top: 6px;
      bottom: 6px;
      width: 3px;
      background: $brand;
      border-radius: 0 2px 2px 0;
    }

    .el-icon {
      color: $brand-soft;
    }
  }

  // 展开的子菜单标题（活动状态）
  .el-sub-menu.is-active > .el-sub-menu__title {
    color: $sidebar-text-active;

    .el-icon {
      color: $brand-soft;
    }
  }

  // ============ 关键修复：子菜单展开时的背景 ============
  // 默认 Element Plus 给 .el-menu--inline 一个白色背景，跟深色侧边栏冲突。
  // 改成比 sidebar 更深的色，体现层级嵌套。
  .el-menu--inline {
    background: $sidebar-bg-deep !important;
    padding: 4px 0;

    // 内部子菜单项：缩进 + 略小字号 + 沿用深色背景
    .el-menu-item {
      background-color: transparent !important;
      padding-left: 48px !important;
      font-size: 13px;
      height: 38px;
      line-height: 38px;
      color: $sidebar-text-muted;

      &:hover {
        background-color: $sidebar-bg-hover !important;
        color: #fff;
      }

      &.is-active {
        color: $sidebar-text-active !important;
        // 子菜单内的选中态用更柔和的色调
        background: linear-gradient(90deg, rgba($brand, 0.22) 0%, rgba($brand, 0.06) 100%) !important;
      }
    }
  }

  // 设置菜单项：与导航项视觉分隔
  // 用 ::after 画一条横向分隔线（::before 已被 is-active 高亮条占用）
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
      background: $sidebar-bg-hover;
    }
  }
}

// ============ DEBUG 面板（侧边栏底部，事后删整段）============
.debug-panel {
  margin: 12px;
  padding: 10px 12px;
  background: rgba(239, 68, 68, 0.12);
  border: 1px solid rgba(239, 68, 68, 0.5);
  border-radius: 6px;
  color: #fee;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  letter-spacing: 0.3px;
  line-height: 1.5;

  .debug-title {
    color: #fca5a5;
    font-weight: 700;
    font-size: 11px;
    margin-bottom: 6px;
    letter-spacing: 0.5px;
  }
  .debug-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    .debug-key {
      color: #fca5a5;
    }
    .debug-val {
      color: #fff;
      font-weight: 600;
    }
  }
}

// ============ 主内容 ============
.app-main {
  background: $content-bg;
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
