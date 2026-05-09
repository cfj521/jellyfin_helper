<template>
  <div class="source-pool">
    <!-- 顶部虚线框：所有可用源以 chip 列出，已加入下方表的显示禁用样式 -->
    <div class="pool-row">
      <span class="pool-label">可选源：</span>
      <div class="pool-chips">
        <el-tag
          v-for="key in allKeys"
          :key="key"
          :type="isInUse(key) ? 'info' : ''"
          :effect="isInUse(key) ? 'plain' : 'dark'"
          :class="['source-chip', { used: isInUse(key) }]"
          @click="addSource(key)"
        >
          <el-icon v-if="!isInUse(key)" class="chip-icon"><Plus /></el-icon>
          <el-icon v-else class="chip-icon"><Check /></el-icon>
          {{ labels[key] || key }}
        </el-tag>
      </div>
    </div>

    <div v-if="!modelValue.length" class="empty-tip">
      尚未启用任何源 — 点击上方任一 chip 添加
    </div>

    <!-- 已配置（按优先级）。可拖动 / 上下移动 / 删除 -->
    <div v-else class="priority-list">
      <div class="list-header">
        <span class="header-text">已启用（拖动行调整优先级，从上到下尝试）</span>
      </div>

      <div
        v-for="(src, idx) in modelValue"
        :key="`${src.name}-${idx}`"
        class="source-row"
        draggable="true"
        :class="{
          'drag-over': dragOverIdx === idx,
          'dragging': draggingIdx === idx,
        }"
        @dragstart="onDragStart($event, idx)"
        @dragend="onDragEnd"
        @dragover.prevent="onDragOver($event, idx)"
        @dragleave="onDragLeave(idx)"
        @drop.prevent="onDrop($event, idx)"
      >
        <div class="drag-handle" title="拖动调整顺序">
          <el-icon><Rank /></el-icon>
        </div>

        <div class="row-rank">{{ idx + 1 }}</div>

        <span class="row-name">{{ labels[src.name] || src.name }}</span>

        <el-input
          v-if="allowBaseUrl"
          v-model="src.base_url"
          size="small"
          :placeholder="`默认 ${defaultBaseUrls[src.name] || ''}`"
          clearable
          class="base-url-input"
        />

        <el-button-group>
          <el-button
            size="small"
            :disabled="idx === 0"
            @click="move(idx, -1)"
            title="上移（提升优先级）"
          >
            <el-icon><ArrowUp /></el-icon>
          </el-button>
          <el-button
            size="small"
            :disabled="idx === modelValue.length - 1"
            @click="move(idx, 1)"
            title="下移"
          >
            <el-icon><ArrowDown /></el-icon>
          </el-button>
          <el-button size="small" type="danger" @click="remove(idx)" title="移出">
            <el-icon><Delete /></el-icon>
          </el-button>
        </el-button-group>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { Plus, Check, Delete, ArrowUp, ArrowDown, Rank } from '@element-plus/icons-vue'

const props = defineProps({
  /** v-model: List<{name, base_url?}>。新加项自动置为启用，无需 enabled 字段 */
  modelValue: { type: Array, required: true },
  /** 全部支持的 key 列表 */
  allKeys: { type: Array, required: true },
  /** key → 显示名 */
  labels: { type: Object, default: () => ({}) },
  /** 是否显示 base_url 自定义输入（成人内容用，字幕不用） */
  allowBaseUrl: { type: Boolean, default: false },
  /** key → 默认 base_url 提示 */
  defaultBaseUrls: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['update:modelValue'])

// 拖拽状态
const draggingIdx = ref(-1)
const dragOverIdx = ref(-1)

const isInUse = (key) => props.modelValue.some(s => s.name === key)

const addSource = (key) => {
  if (isInUse(key)) return
  // 默认每个新加进来的源都启用；保留 enabled:true 以兼容旧后端解析（视为冗余安全字段）
  const entry = { name: key, enabled: true }
  if (props.allowBaseUrl) entry.base_url = ''
  emit('update:modelValue', [...props.modelValue, entry])
}

const remove = (idx) => {
  const next = [...props.modelValue]
  next.splice(idx, 1)
  emit('update:modelValue', next)
}

const move = (idx, delta) => {
  const newIdx = idx + delta
  if (newIdx < 0 || newIdx >= props.modelValue.length) return
  reorder(idx, newIdx)
}

// idx → 把 from 插到 to 之前；如果 from < to，目标位置往左偏 1
const reorder = (from, to) => {
  if (from === to) return
  const next = [...props.modelValue]
  const [moved] = next.splice(from, 1)
  // 注意：从前往后移时，splice 后的索引要 -1
  const insertAt = from < to ? to - 1 : to
  next.splice(insertAt, 0, moved)
  emit('update:modelValue', next)
}

// ---- HTML5 拖拽事件 ----

const onDragStart = (e, idx) => {
  draggingIdx.value = idx
  // dataTransfer 必须设点东西，否则 Firefox 不触发 drop
  e.dataTransfer.effectAllowed = 'move'
  try { e.dataTransfer.setData('text/plain', String(idx)) } catch {}
}

const onDragOver = (e, idx) => {
  e.dataTransfer.dropEffect = 'move'
  dragOverIdx.value = idx
}

const onDragLeave = (idx) => {
  if (dragOverIdx.value === idx) dragOverIdx.value = -1
}

const onDrop = (e, idx) => {
  const from = draggingIdx.value
  dragOverIdx.value = -1
  draggingIdx.value = -1
  if (from < 0 || from === idx) return
  // 把 from 插到 idx 处（替换其位置，原 idx 行向下挤）
  // 习惯：拖到目标行的上方即换到 idx 处
  const next = [...props.modelValue]
  const [moved] = next.splice(from, 1)
  const insertAt = from < idx ? idx : idx
  next.splice(insertAt, 0, moved)
  emit('update:modelValue', next)
}

const onDragEnd = () => {
  draggingIdx.value = -1
  dragOverIdx.value = -1
}
</script>

<style lang="scss" scoped>
.source-pool {
  .pool-row {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    padding: 12px;
    background: #f8fafc;
    border: 1px dashed #cbd5e1;
    border-radius: 6px;
    margin-bottom: 14px;

    .pool-label {
      font-size: 13px;
      color: #475569;
      font-weight: 500;
      flex-shrink: 0;
      line-height: 28px;
    }

    .pool-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
  }

  .source-chip {
    cursor: pointer;
    user-select: none;
    transition: transform 0.15s ease;
    padding: 0 12px;

    .chip-icon {
      vertical-align: middle;
      margin-right: 4px;
      font-size: 12px;
    }

    &:not(.used):hover {
      transform: translateY(-1px);
      box-shadow: 0 2px 6px rgba(99, 102, 241, 0.25);
    }

    &.used {
      cursor: not-allowed;
      opacity: 0.55;
    }
  }

  .empty-tip {
    padding: 16px;
    text-align: center;
    color: #94a3b8;
    font-size: 13px;
    background: #fafafa;
    border-radius: 6px;
  }

  .priority-list {
    .list-header {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 10px;
      padding: 0 4px;

      .header-text {
        font-size: 13px;
        color: #475569;
        font-weight: 500;
      }
    }
  }

  .source-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    margin-bottom: 8px;
    background: #fff;
    transition: all 0.15s ease;

    &:hover {
      border-color: #c7d2fe;
      box-shadow: 0 1px 3px rgba(99, 102, 241, 0.1);
    }

    // 拖动中：原位置半透明
    &.dragging {
      opacity: 0.4;
    }

    // 拖动悬停的目标行：高亮上边沿暗示插入点
    &.drag-over {
      border-color: #6366f1;
      box-shadow: 0 -2px 0 0 #6366f1;
    }

    .drag-handle {
      flex-shrink: 0;
      width: 20px;
      height: 24px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #94a3b8;
      cursor: grab;
      transition: color 0.15s ease;

      &:hover {
        color: #6366f1;
      }

      &:active {
        cursor: grabbing;
      }
    }

    .row-rank {
      width: 24px;
      height: 24px;
      flex-shrink: 0;
      border-radius: 50%;
      background: #6366f1;
      color: #fff;
      font-size: 12px;
      font-weight: 600;
      display: flex;
      align-items: center;
      justify-content: center;
    }

    .row-name {
      min-width: 110px;
      font-size: 13px;
      color: #334155;
      font-weight: 500;
    }

    .base-url-input {
      flex: 1;
    }
  }
}
</style>
