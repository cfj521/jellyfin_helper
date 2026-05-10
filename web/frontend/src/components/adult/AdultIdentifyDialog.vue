<template>
  <el-dialog
    v-model="visible"
    title="重新识别"
    width="800"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    @closed="onClosed"
  >
    <div v-if="item" class="identify-dialog">
      <!-- 当前条目摘要 -->
      <div class="current-info">
        <div class="info-label">当前条目：</div>
        <div class="info-row">
          <span v-if="item.code" class="info-code">{{ item.code }}</span>
          <span v-else class="info-code missing">未识别</span>
          <span v-if="item.title" class="info-title">{{ item.title }}</span>
          <span v-if="item.source" class="info-source">来源 {{ item.source }}</span>
        </div>
        <div class="info-actors">
          <span class="info-actors-label">女优</span>
          <span v-if="item.actors?.length">
            {{ item.actors.slice(0, 8).join(', ') }}{{ item.actors.length > 8 ? ` ... +${item.actors.length - 8}` : '' }}
          </span>
          <span v-else class="info-actors-empty">— 未读取到</span>
        </div>
        <div v-if="item.file_path" class="info-path">{{ item.file_path }}</div>
      </div>

      <!-- 搜索表单 -->
      <el-form :model="form" label-width="80px" size="small">
        <el-form-item label="番号">
          <el-input
            v-model="form.code"
            placeholder="如 SSIS-001 / FC2-PPV-1234567"
            clearable
            style="width: 320px"
            @keyup.enter="search"
          />
          <el-button
            type="primary"
            :loading="searching"
            :disabled="!form.code.trim()"
            @click="search"
            style="margin-left: 8px"
          >
            <el-icon><Search /></el-icon>
            在所有源里搜
          </el-button>
          <span class="hint">会同时在每个启用源里查；最坏耗时 ~10s</span>
        </el-form-item>
      </el-form>

      <!-- 候选列表 -->
      <div v-if="candidates.length" class="candidates">
        <div class="candidates-header">
          找到 {{ candidates.length }} 个候选 — 选择要应用的：
        </div>
        <el-radio-group v-model="selectedIdx" class="candidate-list">
          <label
            v-for="(c, idx) in candidates"
            :key="idx"
            class="candidate-card"
            :class="{ active: selectedIdx === idx }"
          >
            <el-radio :label="idx" class="candidate-radio" />
            <el-image
              v-if="c.cover_url"
              :src="c.cover_url"
              fit="cover"
              lazy
              class="cover-img"
              referrerpolicy="no-referrer"
            >
              <template #error>
                <div class="cover-ph">无图</div>
              </template>
            </el-image>
            <div v-else class="cover-ph">无图</div>

            <div class="candidate-meta">
              <div class="candidate-head">
                <span class="src-tag" :class="{ merged: c.is_merged }">
                  {{ c.is_merged ? '🔀 推荐（多源合并）' : c.source }}
                </span>
                <span class="cand-code">{{ c.code }}</span>
                <span v-if="c.release_date" class="cand-date">· {{ c.release_date }}</span>
                <span v-if="c.studio" class="cand-studio">· {{ c.studio }}</span>
              </div>
              <div class="candidate-title">{{ c.title }}</div>
              <div class="candidate-actors">
                <span class="meta-label">演员</span>
                <span v-if="c.actors?.length">
                  {{ c.actors.slice(0, 5).join(', ') }}{{ c.actors.length > 5 ? ' ...' : '' }}
                </span>
                <span v-else class="actors-empty">— 该源未提供</span>
              </div>
              <div v-if="c.tags?.length" class="candidate-tags">
                <el-tag
                  v-for="t in c.tags.slice(0, 6)"
                  :key="t"
                  size="small"
                  type="info"
                  effect="plain"
                  class="tag-chip"
                >{{ t }}</el-tag>
                <span v-if="c.tags.length > 6" class="more">+{{ c.tags.length - 6 }}</span>
              </div>
            </div>
          </label>
        </el-radio-group>
      </div>

      <el-empty
        v-else-if="searched && !searching"
        :image-size="80"
        description="所有启用源都没找到 — 检查番号拼写或在配置页里启用更多源"
      />
    </div>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button
        type="primary"
        :loading="applying"
        :disabled="selectedIdx === null"
        @click="apply"
      >
        应用所选候选
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import { adultApi } from '@/api'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  item: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue', 'applied'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const form = ref({ code: '' })
const candidates = ref([])
const selectedIdx = ref(null)
const searching = ref(false)
const applying = ref(false)
const searched = ref(false)

watch(() => props.modelValue, (v) => {
  if (v && props.item) {
    // 预填当前 code；空的话留空让用户输入
    form.value.code = props.item.code || ''
    candidates.value = []
    selectedIdx.value = null
    searched.value = false
  }
})

const search = async () => {
  const code = form.value.code.trim()
  if (!code) return
  searching.value = true
  searched.value = true
  selectedIdx.value = null
  candidates.value = []
  try {
    const r = await adultApi.identifySearch(props.item.id, code)
    candidates.value = r.data?.candidates || []
    if (!candidates.value.length) {
      ElMessage.info('所有源都没找到')
    } else if (candidates.value.length === 1) {
      // 只有一个候选时直接选中
      selectedIdx.value = 0
    }
  } catch (e) {
    ElMessage.error('搜索失败：' + (e.response?.data?.detail || e.message))
  } finally {
    searching.value = false
  }
}

const apply = async () => {
  if (selectedIdx.value === null) return
  const c = candidates.value[selectedIdx.value]
  applying.value = true
  try {
    const r = await adultApi.identifyApply(props.item.id, {
      code: c.code,
      title: c.title,
      release_date: c.release_date,
      studio: c.studio,
      director: c.director,
      actors: c.actors || [],
      tags: c.tags || [],
      cover_url: c.cover_url,
      rating: c.rating,
      source: c.source,
    })
    ElMessage.success(`已应用「${c.source}」的数据`)
    emit('applied', { itemId: props.item.id, row: r.data })
    visible.value = false
  } catch (e) {
    ElMessage.error('应用失败：' + (e.response?.data?.detail || e.message))
  } finally {
    applying.value = false
  }
}

const onClosed = () => {
  candidates.value = []
  selectedIdx.value = null
  searched.value = false
}
</script>

<style lang="scss" scoped>
.identify-dialog .hint {
  color: #94a3b8;
  font-size: 12px;
  margin-left: 8px;
}

.current-info {
  margin-bottom: 14px;
  padding: 10px 12px;
  background: #f8fafc;
  border-left: 3px solid #6366f1;
  border-radius: 4px;
  font-size: 13px;

  .info-label { color: #64748b; margin-bottom: 4px; font-size: 12px; }
  .info-row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;

    .info-code {
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-weight: 600;
      color: #0369a1;
      &.missing { color: #dc2626; }
    }
    .info-title { color: #0f172a; font-weight: 500; }
    .info-source {
      font-size: 11px;
      color: #94a3b8;
      background: #e2e8f0;
      padding: 1px 6px;
      border-radius: 3px;
    }
  }
  .info-actors {
    margin-top: 4px;
    font-size: 12px;
    color: #475569;

    .info-actors-label {
      color: #94a3b8;
      font-size: 11px;
      margin-right: 6px;
    }
    .info-actors-empty {
      color: #94a3b8;
      font-style: italic;
    }
  }
  .info-path {
    color: #94a3b8;
    font-size: 11px;
    margin-top: 4px;
    font-family: ui-monospace, SFMono-Regular, monospace;
    word-break: break-all;
  }
}

.candidates {
  margin-top: 4px;

  .candidates-header {
    font-size: 13px;
    color: #475569;
    margin-bottom: 8px;
  }

  .candidate-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
    width: 100%;
  }
}

.candidate-card {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 10px;
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s;

  &:hover {
    border-color: #c7d2fe;
    background: #f5f3ff;
  }
  &.active {
    border-color: #6366f1;
    background: #eef2ff;
    box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.15);
  }

  .candidate-radio { margin: 0; flex-shrink: 0; }

  .cover-img,
  .cover-ph {
    width: 110px;
    aspect-ratio: 16 / 11;
    border-radius: 4px;
    flex-shrink: 0;
    background: #f1f5f9;
  }
  .cover-ph {
    display: flex;
    align-items: center;
    justify-content: center;
    color: #94a3b8;
    font-size: 11px;
  }

  .candidate-meta {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;

    .candidate-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      color: #64748b;
    }
    .src-tag {
      background: #6366f1;
      color: #fff;
      padding: 1px 6px;
      border-radius: 3px;
      font-size: 11px;
      font-weight: 600;

      &.merged {
        background: linear-gradient(135deg, #d97706 0%, #f59e0b 100%);
        padding: 2px 8px;
      }
    }
    .cand-code {
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-weight: 600;
      color: #0369a1;
    }
    .candidate-title {
      font-size: 14px;
      font-weight: 500;
      color: #0f172a;
      line-height: 1.4;
    }
    .candidate-actors {
      font-size: 12px;
      color: #475569;
      .meta-label {
        color: #94a3b8;
        margin-right: 4px;
        font-size: 11px;
      }
      .actors-empty {
        color: #94a3b8;
        font-style: italic;
      }
    }
    .candidate-tags {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
      align-items: center;
      .more { font-size: 11px; color: #94a3b8; }
    }
  }
}
</style>
