<template>
  <!--
    网格 / 列表 视图切换。胶囊容器 + 两个 icon 按钮，活动项蓝底白图标。
    用法：<ViewModeToggle v-model="viewMode" />，mode 取值 'grid' | 'list'
  -->
  <div class="view-mode-toggle">
    <button
      type="button"
      :class="['vmt-btn', { active: modelValue === 'grid' }]"
      title="网格视图"
      @click="$emit('update:modelValue', 'grid')"
    >
      <el-icon><Grid /></el-icon>
    </button>
    <button
      type="button"
      :class="['vmt-btn', { active: modelValue === 'list' }]"
      title="列表视图"
      @click="$emit('update:modelValue', 'list')"
    >
      <!-- 自定义 SVG：经典"无序列表"图标（左侧圆点 + 右侧横线），与截图风格一致；
           element-plus 自带的 Menu 是三横线（汉堡），不符合用户期望 -->
      <svg
        class="vmt-list-icon"
        viewBox="0 0 24 24"
        fill="currentColor"
        aria-hidden="true"
      >
        <circle cx="5" cy="6" r="1.6" />
        <rect x="9" y="5" width="11" height="2" rx="1" />
        <circle cx="5" cy="12" r="1.6" />
        <rect x="9" y="11" width="11" height="2" rx="1" />
        <circle cx="5" cy="18" r="1.6" />
        <rect x="9" y="17" width="11" height="2" rx="1" />
      </svg>
    </button>
  </div>
</template>

<script setup>
import { Grid } from '@element-plus/icons-vue'

defineProps({
  modelValue: { type: String, default: 'list' },
})
defineEmits(['update:modelValue'])
</script>

<style lang="scss" scoped>
// 与用户截图对齐：白底胶囊容器 + 浅灰描边/阴影 + 按钮间间距更大 + 蓝色 active 圆角矩形
.view-mode-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px;
  background: #fafafa;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
  user-select: none;

  .vmt-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    padding: 0;
    border: none;
    background: transparent;
    color: #94a3b8;
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.15s ease;

    .el-icon {
      font-size: 17px;
    }

    // 自定义 SVG list 图标尺寸
    .vmt-list-icon {
      width: 18px;
      height: 18px;
      display: block;
    }

    &:hover:not(.active) {
      color: #475569;
    }

    &.active {
      background: #4f8cff;        // 截图里的鲜亮蓝
      color: #fff;
      box-shadow: 0 2px 6px rgba(79, 140, 255, 0.35);
    }
  }
}
</style>
