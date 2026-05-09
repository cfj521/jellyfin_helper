<template>
  <div class="health-cell">
    <!-- 第一行：圆点 + 状态文字 -->
    <div class="health-status" :class="state">
      <span class="dot" />
      <span class="state-text">{{ stateLabel }}</span>
    </div>

    <!-- 第二行：动作按钮 -->
    <div class="health-actions">
      <el-button
        size="small"
        text
        type="primary"
        :disabled="!item.id"
        title="重新识别番号"
        @click.stop="$emit('rescrape', item)"
      >
        <el-icon><Refresh /></el-icon>
        重新识别
      </el-button>

      <el-button
        v-if="item.code"
        size="small"
        text
        type="primary"
        title="手动编辑元数据"
        @click.stop="$emit('edit', item)"
      >
        <el-icon><Edit /></el-icon>
        编辑
      </el-button>

      <el-button
        v-if="(state === 'red' || state === 'yellow') && !item.excluded"
        size="small"
        text
        type="danger"
        title="从库中删除本条目（不删文件）"
        @click.stop="$emit('delete', item)"
      >
        <el-icon><Delete /></el-icon>
        删除
      </el-button>

      <el-button
        v-if="item.excluded"
        size="small"
        text
        type="info"
        title="取消排除，恢复自动流程"
        @click.stop="$emit('unexclude', item)"
      >
        <el-icon><RefreshLeft /></el-icon>
        取消排除
      </el-button>

      <el-button
        v-else-if="state === 'cooldown'"
        size="small"
        text
        type="success"
        title="清冷却立即重试"
        @click.stop="$emit('unexclude', item)"
      >
        <el-icon><RefreshLeft /></el-icon>
        立即重试
      </el-button>

      <el-button
        v-if="!item.excluded && (state === 'red' || state === 'gray' || state === 'cooldown')"
        size="small"
        text
        type="warning"
        title="排除：自动流程不再处理"
        @click.stop="$emit('exclude', item)"
      >
        <el-icon><CircleClose /></el-icon>
        排除
      </el-button>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { Refresh, Delete, CircleClose, RefreshLeft, Edit } from '@element-plus/icons-vue'

const props = defineProps({
  item: { type: Object, required: true },
})
defineEmits(['rescrape', 'delete', 'exclude', 'unexclude', 'edit'])

// cooldown_until 是 UTC ISO 字符串
const cooldownRemainingDays = computed(() => {
  const cu = props.item.cooldown_until
  if (!cu) return 0
  const expiry = new Date(cu).getTime()
  const now = Date.now()
  if (Number.isNaN(expiry) || expiry <= now) return 0
  return Math.ceil((expiry - now) / 86400000)
})

// 状态优先级：excluded > cooldown > red > gray > yellow > green
const state = computed(() => {
  const it = props.item
  if (it.excluded) return 'excluded'
  if (cooldownRemainingDays.value > 0) return 'cooldown'
  if (!it.code) return 'red'
  if (it.source === 'not_found') return 'red'
  if (!it.title || !it.source || it.source === 'pending') return 'gray'

  // 缺封面 / NFO：优先用后端权威字段（含文件实存判定），缺失时回退到字段非空
  const coverLocalOk = it.cover_local_ok
  const nfoLocalOk = it.nfo_local_ok
  const missingCover = (coverLocalOk === undefined)
    ? (!it.poster_path && !it.cover_url)
    : !coverLocalOk
  const missingNfo = (nfoLocalOk === undefined)
    ? !it.nfo_path
    : !nfoLocalOk
  if (missingCover || missingNfo) return 'yellow'

  return 'green'
})

const stateLabel = computed(() => {
  if (state.value === 'excluded') return '已排除'
  if (state.value === 'cooldown') {
    const n = props.item.scrape_attempts || 0
    return `冷却中（剩 ${cooldownRemainingDays.value} 天 / 失败 ${n} 次）`
  }
  return ({
    green: '完整',
    yellow: '部分缺失',
    gray: '未刮削',
    red: !props.item.code ? '未识别' : '刮削失败',
  })[state.value]
})
</script>

<style lang="scss" scoped>
.health-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-start;       // 圆点 + 按钮左缘对齐到同一垂直线
  gap: 8px;                      // 状态行与按钮区上下间隔
  padding: 6px 0;                // 整个 cell 上下留白，行密度更舒展
}

.health-status {
  display: flex;
  align-items: center;
  gap: 6px;
  padding-left: 6px;             // 与按钮的 text-button padding(6px) 同步，圆点正好对齐按钮文字左缘
  font-size: 12px;

  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  &.green    .dot { background: #16a34a; }
  &.yellow   .dot { background: #f59e0b; }
  &.gray     .dot { background: #94a3b8; }
  &.red      .dot { background: #ef4444; }
  &.cooldown .dot { background: #8b5cf6; }
  &.excluded .dot { background: #1e293b; }

  &.green    .state-text { color: #16a34a; }
  &.yellow   .state-text { color: #d97706; }
  &.gray     .state-text { color: #64748b; }
  &.red      .state-text { color: #dc2626; font-weight: 500; }
  &.cooldown .state-text { color: #7c3aed; font-weight: 500; }
  &.excluded .state-text { color: #475569; font-weight: 500; }
}

.health-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 2px;                  // 行间距 4px、列间距 2px（多按钮换行时不挤在一起）

  :deep(.el-button.is-text) {
    padding: 2px 6px;
    margin: 0;                   // 清掉 element-plus 默认 button-margin，避免左缘错位
    height: 24px;
    font-size: 12px;
  }
}
</style>
