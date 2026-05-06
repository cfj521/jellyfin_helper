<template>
  <el-dialog
    v-model="visible"
    :title="dialogTitle"
    width="720"
    :close-on-click-modal="false"
    @opened="onOpened"
    @closed="onClosed"
  >
    <div v-if="loading" class="loading-block">
      <el-icon class="spin"><Loading /></el-icon>
      正在收集证据...
    </div>

    <div v-else-if="evidence" class="evidence-block">
      <!-- 文件基本信息 -->
      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="标题">{{ evidence.name }}</el-descriptions-item>
        <el-descriptions-item label="类型">{{ evidence.type }}</el-descriptions-item>
        <el-descriptions-item label="文件大小" :span="1">
          <span v-if="evidence.path_accessible">{{ evidence.file_size_display }}</span>
          <span v-else class="muted">— 后端无法访问</span>
        </el-descriptions-item>
        <el-descriptions-item label="时长">
          <span v-if="evidence.runtime_min">{{ evidence.runtime_min }} 分钟</span>
          <span v-else class="muted">—</span>
        </el-descriptions-item>
        <el-descriptions-item label="路径" :span="2">
          <span class="path-text">{{ evidence.path }}</span>
        </el-descriptions-item>
      </el-descriptions>

      <!-- Verdict 横幅 -->
      <div :class="['verdict-banner', `verdict-${evidence.verdict}`]">
        <el-icon class="verdict-icon">
          <CircleCheck v-if="evidence.verdict === 'main-content'" />
          <Warning v-else-if="evidence.verdict === 'unclear'" />
          <WarningFilled v-else-if="evidence.verdict === 'sample-likely'" />
          <CircleClose v-else-if="evidence.verdict === 'sample'" />
          <Folder v-else-if="evidence.verdict === 'non-media'" />
          <QuestionFilled v-else />
        </el-icon>
        <div class="verdict-text">
          <div class="verdict-label">{{ verdictLabel }}</div>
          <div class="verdict-hint">{{ verdictHint }}</div>
        </div>
      </div>

      <!-- 证据明细 -->
      <div v-if="evidence.high_confidence.length || evidence.medium_confidence.length" class="reasons">
        <div v-if="evidence.high_confidence.length" class="reason-group">
          <div class="reason-title">强证据：</div>
          <ul>
            <li
              v-for="(r, idx) in evidence.high_confidence"
              :key="`h-${idx}`"
              class="reason-line strong"
            >• {{ r }}</li>
          </ul>
        </div>
        <div v-if="evidence.medium_confidence.length" class="reason-group">
          <div class="reason-title">弱证据：</div>
          <ul>
            <li
              v-for="(r, idx) in evidence.medium_confidence"
              :key="`m-${idx}`"
              class="reason-line"
            >• {{ r }}</li>
          </ul>
        </div>
      </div>

      <!-- 兄弟文件 -->
      <div v-if="evidence.siblings && evidence.siblings.length" class="siblings">
        <div class="siblings-title">同目录兄弟视频（按大小降序）：</div>
        <el-table :data="evidence.siblings" size="small" border>
          <el-table-column prop="name" label="文件名" min-width="320" show-overflow-tooltip />
          <el-table-column label="大小" width="120" align="right">
            <template #default="{ row }">
              <span :class="{ larger: row.is_larger }">{{ row.size_display }}</span>
            </template>
          </el-table-column>
          <el-table-column label="对比" width="100" align="center">
            <template #default="{ row }">
              <el-tag v-if="row.is_larger" type="warning" size="small">更大</el-tag>
              <el-tag v-else type="info" size="small">更小</el-tag>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <el-alert
        v-if="!evidence.path_accessible"
        type="warning"
        :closable="false"
        show-icon
        title="后端无法访问该文件路径"
        style="margin-top: 12px"
      >
        证据收集只用了 Jellyfin 元数据（路径关键字 / 时长），没法对比兄弟文件大小。
        建议确认后端运行环境与 Jellyfin 媒体目录是否在同一台机或挂载到一致路径。
      </el-alert>
    </div>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button
        type="danger"
        :loading="deleting"
        :disabled="!canDelete"
        @click="confirmDelete"
      >
        {{ deleteButtonLabel }}
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Loading, CircleCheck, Warning, WarningFilled, CircleClose, QuestionFilled, Folder,
} from '@element-plus/icons-vue'
import { jellyfinApi } from '@/api'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  itemId: { type: String, default: '' },
  // 'sample' = 清除 Sample；'unrecognized' = 删除未识别条目
  mode: { type: String, default: 'sample' },
})
const emit = defineEmits(['update:modelValue', 'deleted'])

const dialogTitle = computed(() =>
  props.mode === 'unrecognized' ? '删除未识别条目' : '清除 Sample 文件',
)

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const evidence = ref(null)
const loading = ref(false)
const deleting = ref(false)

const onOpened = async () => {
  if (!props.itemId) return
  evidence.value = null
  loading.value = true
  try {
    const res = await jellyfinApi.sampleEvidence(props.itemId)
    evidence.value = res.data
  } catch (e) {
    console.error(e)
    ElMessage.error(e.response?.data?.detail || e.message || '取证失败')
    visible.value = false
  } finally {
    loading.value = false
  }
}

const onClosed = () => {
  evidence.value = null
}

const verdictLabel = computed(() => ({
  'sample': '判定：是 Sample（可放心删除）',
  'sample-likely': '判定：很可能是 Sample（建议删除，请人工复核）',
  'unclear': '判定：不确定（需要你判断）',
  'main-content': '判定：像是正片（不建议删除）',
  'non-media': '判定：可清理',
  'unknown': '判定：信息不足',
}[evidence.value?.verdict] || '未知'))

const verdictHint = computed(() => ({
  'sample': '强证据已命中（路径关键字 / 极短时长 / 同目录有大得多的兄弟文件）',
  'sample-likely': '多条弱证据叠加，但不绝对',
  'unclear': '只有 1 条弱证据；删除前请仔细看下面明细',
  'main-content': '没有任何 sample 信号 —— 删除有风险',
  'non-media': '目录里没有视频文件（cover / poster / extras 之类的辅助子目录）',
  'unknown': '条目缺少必要字段',
}[evidence.value?.verdict] || ''))

const canDelete = computed(() => {
  const v = evidence.value?.verdict
  // 未识别模式：用户明确想删未识别条目，不被 sample verdict 阻塞
  if (props.mode === 'unrecognized') {
    return v !== 'unknown'
  }
  // sample 模式：sample / sample-likely / unclear / non-media 都允许删
  return v === 'sample' || v === 'sample-likely' || v === 'unclear' || v === 'non-media'
})

const deleteButtonLabel = computed(() => {
  const v = evidence.value?.verdict
  if (props.mode === 'unrecognized') {
    return v === 'unknown' ? '信息不足' : '确认删除条目 + 文件'
  }
  if (v === 'main-content') return '不建议删除'
  if (v === 'unknown') return '信息不足无法删除'
  if (v === 'non-media') return '清除非媒体目录'
  return '确认删除文件 + 移除条目'
})

const confirmDelete = async () => {
  // 二次确认（不可恢复）
  try {
    await ElMessageBox.confirm(
      `这会从 Jellyfin 库移除条目并删除磁盘文件：\n${evidence.value.path}\n\n操作不可撤销，确定吗？`,
      '危险操作确认',
      {
        type: 'warning',
        confirmButtonText: '我已确认，删除',
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
        dangerouslyUseHTMLString: false,
      },
    )
  } catch {
    return
  }

  deleting.value = true
  try {
    const res = await jellyfinApi.deleteItem(props.itemId)
    const data = res.data || {}
    if (data.method === 'physical_delete') {
      // Jellyfin DELETE 失败 → 走了物理删除 fallback
      ElMessage.warning({
        message: '已通过物理删除清理（Jellyfin DELETE 不可用，已自动重扫库以清理孤儿条目）',
        duration: 5000,
      })
    } else {
      ElMessage.success('已删除')
    }
    emit('deleted', { itemId: props.itemId, path: evidence.value.path })
    visible.value = false
  } catch (e) {
    console.error(e)
    const msg = e.response?.data?.detail || e.message || '删除失败'
    ElMessage.error('删除失败: ' + msg)
  } finally {
    deleting.value = false
  }
}
</script>

<style lang="scss" scoped>
.loading-block {
  text-align: center;
  padding: 40px;
  color: #94a3b8;

  .spin {
    animation: spin 1.2s linear infinite;
    margin-right: 6px;
  }
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.muted {
  color: #94a3b8;
}

.path-text {
  font-family: ui-monospace, monospace;
  font-size: 12px;
  word-break: break-all;
  color: #475569;
}


.verdict-banner {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 16px 0;
  padding: 12px 16px;
  border-radius: 8px;
  border: 1px solid;

  .verdict-icon {
    font-size: 28px;
    flex-shrink: 0;
  }

  .verdict-text {
    flex: 1;
  }

  .verdict-label {
    font-size: 14px;
    font-weight: 600;
  }

  .verdict-hint {
    font-size: 12px;
    margin-top: 2px;
    opacity: 0.85;
  }

  &.verdict-sample {
    background: #fef2f2;
    border-color: #fecaca;
    color: #b91c1c;
  }
  &.verdict-sample-likely {
    background: #fff7ed;
    border-color: #fed7aa;
    color: #c2410c;
  }
  &.verdict-unclear {
    background: #fefce8;
    border-color: #fde68a;
    color: #a16207;
  }
  &.verdict-main-content {
    background: #f0fdf4;
    border-color: #bbf7d0;
    color: #15803d;
  }
  &.verdict-non-media {
    background: #faf5ff;
    border-color: #e9d5ff;
    color: #7e22ce;
  }
  &.verdict-unknown {
    background: #f8fafc;
    border-color: #e2e8f0;
    color: #475569;
  }
}

.reasons {
  margin: 12px 0;

  .reason-group {
    margin-bottom: 8px;
  }

  .reason-title {
    font-size: 12px;
    color: #64748b;
    margin-bottom: 4px;
  }

  ul {
    margin: 0;
    padding: 0;
    list-style: none;

    .reason-line {
      font-size: 13px;
      color: #475569;
      line-height: 1.6;

      &.strong {
        color: #b91c1c;
        font-weight: 500;
      }
    }
  }
}

.siblings {
  margin-top: 12px;

  .siblings-title {
    font-size: 12px;
    color: #64748b;
    margin-bottom: 6px;
  }

  .larger {
    color: #c2410c;
    font-weight: 600;
  }
}
</style>
