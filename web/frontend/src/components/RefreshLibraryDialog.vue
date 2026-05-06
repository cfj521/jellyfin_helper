<template>
  <el-dialog
    v-model="visible"
    :title="title"
    width="540px"
    :close-on-click-modal="false"
  >
    <div class="dialog-body">
      <p class="lead">
        将通知 Jellyfin 重新扫描
        <span v-if="libraryName" class="lib-name-inline">{{ libraryName }}</span>
        ，请选择刷新模式：
      </p>

      <el-radio-group v-model="mode" class="mode-list">
        <el-radio
          v-for="opt in MODES"
          :key="opt.value"
          :label="opt.value"
          class="mode-item"
        >
          <div class="mode-content">
            <div class="mode-title">{{ opt.label }}</div>
            <div class="mode-hint">{{ opt.hint }}</div>
          </div>
        </el-radio>
      </el-radio-group>

      <el-alert
        v-if="mode === 'replace_all'"
        type="warning"
        :closable="false"
        show-icon
        class="warn"
      >
        会覆盖你在 Jellyfin 中所有手动修改的元数据（标题、简介、海报等），且耗时较长。
      </el-alert>

      <el-alert
        v-else-if="mode === 'missing_metadata'"
        type="info"
        :closable="false"
        show-icon
        class="warn"
      >
        会比"扫描变更"耗时更长，因为它要为每个缺失元数据的条目重新查询 TMDB / IMDb。
      </el-alert>
    </div>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="loading" @click="onConfirm">
        开始扫描
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, computed } from 'vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  title: { type: String, default: '通知 Jellyfin 重新扫描' },
  libraryName: { type: String, default: '' },
  loading: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue', 'confirm'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const mode = ref('scan_changes')

// 文案与 Jellyfin Web UI 完全对齐
const MODES = [
  {
    value: 'scan_changes',
    label: '扫描新的和有修改的文件',
    hint: '仅查找新增 / 修改的文件，最快。日常推荐。',
  },
  {
    value: 'missing_metadata',
    label: '搜索缺少的元数据',
    hint: '为缺失元数据 / 海报的条目重新查询，不覆盖已有元数据。',
  },
  {
    value: 'replace_all',
    label: '覆盖所有元数据',
    hint: '完全重新刮削，会覆盖手动修改。耗时最长。',
  },
]

const onConfirm = () => {
  emit('confirm', mode.value)
}
</script>

<style lang="scss" scoped>
.dialog-body {
  .lead {
    margin: 0 0 16px;
    color: #475569;

    .lib-name-inline {
      font-weight: 600;
      color: #6366f1;
      margin: 0 4px;
    }
  }

  .mode-list {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin-bottom: 12px;
  }

  .mode-item {
    width: 100%;
    margin-right: 0 !important;
    height: auto !important;
    padding: 12px 14px;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    transition: all 0.2s;
    align-items: flex-start !important;

    &:hover {
      border-color: #c7d2fe;
      background: #f8fafc;
    }

    &.is-checked {
      border-color: #6366f1;
      background: rgba(99, 102, 241, 0.06);
    }

    :deep(.el-radio__label) {
      width: 100%;
    }
  }

  .mode-content {
    display: inline-block;
    width: 100%;

    .mode-title {
      font-size: 14px;
      font-weight: 600;
      color: #0f172a;
      line-height: 1.4;
    }

    .mode-hint {
      margin-top: 4px;
      font-size: 12px;
      color: #64748b;
      line-height: 1.5;
      white-space: normal;
    }
  }

  .warn {
    margin-top: 8px;
  }
}
</style>
